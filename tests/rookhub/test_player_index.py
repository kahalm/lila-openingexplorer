"""Tests fuer rookhub/player-index/player-index.py (S2-003): ein dauerhaft scheiternder Monat
wird nicht in einer Schleife neu geladen. Ohne Netz, ohne pyarrow (index_file gestubbt).

Aufruf: python3 -I tests/rookhub/test_player_index.py
"""
import datetime
import importlib.machinery
import importlib.util
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


if __name__ == '__main__':
    unittest.main(verbosity=1)
