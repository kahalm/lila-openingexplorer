"""Tests fuer rookhub/player-index/player-index.py (S2-003): ein dauerhaft scheiternder Monat
wird nicht in einer Schleife neu geladen. Ohne Netz, ohne pyarrow (index_file gestubbt).
IndexFileTests (G4-004) brauchen zstandard + pyarrow und werden sonst uebersprungen — mit der
Cron-venv laufen sie: ~/claude/player-index/.venv/bin/python -I tests/rookhub/test_player_index.py

Aufruf: python3 -I tests/rookhub/test_player_index.py
"""
import contextlib
import datetime
import hashlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', '..', 'rookhub', 'player-index', 'player-index.py')
MONTH = '2024-05'
DUMP = f'lichess_db_standard_rated_{MONTH}.pgn.zst'


def load(explorer):
    os.environ['EXPLORER_DATA'] = explorer
    os.environ.pop('PLAYER_INDEX_DATA', None)
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader('player_index', SCRIPT)
    spec = importlib.util.spec_from_loader('player_index', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ex = self.tmp.name
        os.makedirs(f'{ex}/dumps')
        os.makedirs(f'{ex}/state')
        with open(f'{ex}/state/lichess-imported.txt', 'w') as f:
            f.write(MONTH + '\n')
        self.pi = load(ex)
        self.fetches = 0

    def tearDown(self):
        self.tmp.cleanup()

    def fake_fetch(self, sums):
        self.fetches += 1
        if self.fetches > 3:
            raise RuntimeError('Endlosschleife: fetch_missing zu oft')
        open(f'{self.pi.INBOX}/{DUMP}', 'wb').close()
        return True

    def run_main(self):
        with mock.patch.object(self.pi, 'fetch_text', return_value=f'abc {DUMP}\n'), \
                mock.patch.object(self.pi, 'fetch_missing', side_effect=self.fake_fetch), \
                mock.patch.object(self.pi, 'index_file', side_effect=ImportError('No module named pyarrow')), \
                mock.patch.object(sys, 'argv', ['player-index']), \
                mock.patch.object(self.pi, 'log'):
            self.pi.main()

    def test_failure_ends_run_and_is_recorded(self):
        self.run_main()
        self.assertEqual(self.fetches, 1)
        lines = open(f'{self.pi.STATE}/failed.txt').read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(MONTH + ' '))
        self.assertIn('pyarrow', lines[0])
        self.assertFalse(os.path.exists(f'{self.pi.INBOX}/{DUMP}'))

    def test_recent_failure_is_not_fetched_again(self):
        self.run_main()
        with mock.patch.object(self.pi.subprocess, 'call') as curl:
            self.assertFalse(self.pi.fetch_missing({DUMP: 'abc'}))
            curl.assert_not_called()

    def test_recent_failure_is_not_relinked(self):
        self.run_main()
        open(f'{self.tmp.name}/dumps/{DUMP}', 'wb').close()
        self.pi.link_dumps()
        self.assertFalse(os.path.exists(f'{self.pi.INBOX}/{DUMP}'))

    def test_old_failure_is_retried(self):
        os.makedirs(self.pi.STATE, exist_ok=True)
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)).isoformat()
        with open(f'{self.pi.STATE}/failed.txt', 'w') as f:
            f.write(f'{MONTH} {old} alter Fehler\n')
        self.assertEqual(self.pi.recently_failed(), set())
        with mock.patch.object(self.pi.subprocess, 'call', return_value=1) as curl, \
                mock.patch.object(self.pi, 'log'):
            os.makedirs(self.pi.INBOX, exist_ok=True)
            self.pi.fetch_missing({DUMP: 'abc'})
            curl.assert_called_once()



try:
    import pyarrow.parquet
    import zstandard
    HAVE_LIBS = True
except ImportError:
    HAVE_LIBS = False

GAME = (b'[Site "https://lichess.org/%s"]\n[White "w%d"]\n[Black "b%d"]\n[Result "1-0"]\n'
        b'[UTCDate "2024.05.01"]\n[WhiteElo "1500"]\n[BlackElo "1500"]\n[TimeControl "600+0"]\n\n'
        b'1. e4 e5 2. Nf3 Nc6 1-0\n\n')


@unittest.skipUnless(HAVE_LIBS, 'zstandard/pyarrow fehlen (Cron-venv nutzen)')
class IndexFileTests(unittest.TestCase):
    """G4-004: Dumps aus mehreren zstd-Frames werden ganz gelesen und die Pruefsumme passt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pi = load(self.tmp.name)
        os.makedirs(self.pi.PARQ, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_dump(self, frames, pad=0):
        cctx = zstandard.ZstdCompressor()
        data = b''.join(cctx.compress(GAME % (b'g%d' % i, i, i)) for i in range(frames))
        if pad:  # angehaengter Skippable-Frame (Magic 0x184D2A50), groesser als der 1-MB-Lesepuffer
            data += (0x184D2A50).to_bytes(4, 'little') + pad.to_bytes(4, 'little') + b'\0' * pad
        path = f'{self.tmp.name}/{DUMP}'
        with open(path, 'wb') as f:
            f.write(data)
        return path, hashlib.sha256(data).hexdigest()

    def run_index(self, frames, pad=0, stop_after_first=None):
        path, sha = self.write_dump(frames, pad)
        patches = [mock.patch.object(self.pi, 'log')]
        if stop_after_first is not None:
            patches.append(mock.patch.object(zstandard, 'ZstdDecompressor', _decompressor(stop_after_first)))
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            n_games, n_kept, _ = self.pi.index_file(path, MONTH, sha)
        rows = pyarrow.parquet.read_table(f'{self.pi.PARQ}/{MONTH}.parquet').num_rows
        return n_games, n_kept, rows

    def test_single_frame(self):
        self.assertEqual(self.run_index(1), (1, 1, 1))

    def test_two_frames_read_fully_and_checksum_ok(self):
        self.assertEqual(self.run_index(2), (2, 2, 2))

    def test_reader_without_read_across_frames_would_stop(self):
        # Aeltere python-zstandard-Staende beenden den Leser ohne read_across_frames nach Frame 1
        # (nachgestellt): index_file muss trotzdem beide Partien lesen und die Pruefsumme treffen.
        self.assertEqual(self.run_index(2, pad=2 << 20, stop_after_first='flag'), (2, 2, 2))

    def test_unread_rest_counts_in_checksum(self):
        # Liest der Dekompressor den Rest (Skippable-Frame hinter dem letzten Spiel) nicht mehr,
        # muss er trotzdem in die Pruefsumme eingehen — sonst RuntimeError trotz korrektem Dump.
        self.assertEqual(self.run_index(1, pad=2 << 20, stop_after_first='always'), (1, 1, 1))


def _decompressor(mode):
    """Ersatz fuer zstandard.ZstdDecompressor, dessen stream_reader nach dem ersten Frame endet —
    'flag': nur ohne read_across_frames=True (alte Semantik), 'always': immer."""
    real = zstandard.ZstdDecompressor

    class _FirstFrameOnly(io.RawIOBase):
        def __init__(self, inner):
            self.inner, self.done = inner, False

        def readable(self):
            return True

        def readinto(self, b):
            if self.done:
                return 0
            self.done = True
            return self.inner.readinto(b)

    class _Dctx:
        def stream_reader(self, src, read_size=1 << 20, read_across_frames=False):
            if mode == 'always' or not read_across_frames:
                return _FirstFrameOnly(real().stream_reader(src, read_size=read_size))
            return real().stream_reader(src, read_size=read_size, read_across_frames=True)

    return _Dctx


if __name__ == '__main__':
    unittest.main(verbosity=1)
