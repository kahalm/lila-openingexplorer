#!/usr/bin/env python3
"""Spielerindex aus den Lichess-Monatsdumps (Nebenleser zum lokalen Explorer).

Der Explorer speichert je Stellung nur Zähler und die letzten 8 Partien; die Spielernamen der
übrigen Partien gehen beim Import verloren, und explorer-sync löscht den Dump danach. Dieses
Werkzeug hält je Partie Spieler, Wertung, Tempo, Ergebnis und die ersten 24 Halbzüge fest
(eine Parquet-Datei je Monat) — Grundlage für „Konto anhand von Partien finden".

Ablauf eines Laufs (Cron alle 10 min, siehe README-rookhub.md):
  1. link:  jeder fertige Dump in $EXPLORER/dumps/ wird nach inbox/ hart verlinkt, bevor
            explorer-sync ihn nach dem Import löscht (kein zweiter Download).
  2. fetch: Monate, die der Explorer schon importiert UND gelöscht hat, werden einzeln
            nachgeladen (Lücke vor Einführung dieses Index). Monate, die der Explorer noch
            nicht angefasst hat, werden NICHT geladen — die kommen über Schritt 1.
  3. index: jeder Dump in inbox/ wird gelesen (sha256 läuft mit), nach parquet/<Monat>.parquet
            geschrieben, in state/indexed.txt vermerkt und aus inbox/ gelöscht.
Schritt 1 läuft immer; 2+3 nur unter einem Lock (ein Lauf zur Zeit).
"""
import argparse, datetime, fcntl, hashlib, io, os, re, subprocess, sys, time, urllib.request

EXPLORER = os.environ.get('EXPLORER_DATA', '/mnt/disks/sdf/rookhub-explorer')
ROOT = os.environ.get('PLAYER_INDEX_DATA', f'{EXPLORER}/player-index')
FROM = os.environ.get('PLAYER_INDEX_FROM', '2024-01')
MAX_INBOX = int(os.environ.get('PLAYER_INDEX_MAX_INBOX', '2'))   # höchstens so viele Dumps vorhalten
PLIES = 24
HASH_AT = (8, 12, 16, 20, 24)
BASE = 'https://database.lichess.org/standard'
NAME = re.compile(r'^lichess_db_standard_rated_(\d{4}-\d{2})\.pgn\.zst$')
UA = 'rookhub-player-index/1.0 (+https://rookhub.oberschmid.homes)'

INBOX, PARQ, STATE = f'{ROOT}/inbox', f'{ROOT}/parquet', f'{ROOT}/state'


def log(msg):
    print(f'{datetime.datetime.now().astimezone().isoformat(timespec="seconds")} {msg}', flush=True)


def fetch_text(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def read_set(path):
    try:
        return {l.strip() for l in open(path) if l.strip()}
    except FileNotFoundError:
        return set()


# ------------------------------------------------------------------ 1. link
def link_dumps():
    for f in os.listdir(f'{EXPLORER}/dumps'):
        m = NAME.match(f)
        if not m or m.group(1) < FROM or m.group(1) in read_set(f'{STATE}/indexed.txt'):
            continue
        dst = f'{INBOX}/{f}'
        if not os.path.exists(dst):
            try:
                os.link(f'{EXPLORER}/dumps/{f}', dst)
                log(f'{m.group(1)}: Dump des Explorers verlinkt')
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------ 2. fetch
def fetch_missing(sums):
    """Lädt höchstens einen Monat nach; True, wenn etwas geladen wurde."""
    have = {NAME.match(f).group(1) for f in os.listdir(INBOX) if NAME.match(f)}
    if len(have) >= MAX_INBOX:
        return False
    done = read_set(f'{STATE}/indexed.txt')
    imported = read_set(f'{EXPLORER}/state/lichess-imported.txt')
    in_explorer = {NAME.match(f).group(1) for f in os.listdir(f'{EXPLORER}/dumps') if NAME.match(f)}
    todo = sorted((m for m in imported if m >= FROM and m not in done and m not in have and m not in in_explorer
                   and f'lichess_db_standard_rated_{m}.pgn.zst' in sums), reverse=True)
    if not todo:
        return False
    month = todo[0]
    f = f'lichess_db_standard_rated_{month}.pgn.zst'
    log(f'{month}: Download (Explorer hat den Dump schon gelöscht)')
    part = f'{INBOX}/{f}.part'
    rc = subprocess.call(['curl', '-fsS', '--retry', '5', '-C', '-', '-A', UA, '-o', part, f'{BASE}/{f}'])
    if rc != 0:
        log(f'{month}: Download fehlgeschlagen (curl {rc}), nächster Lauf setzt fort')
        return False
    os.rename(part, f'{INBOX}/{f}')
    return True


# ------------------------------------------------------------------ 3. index
def speed_of(tc):
    if tc == '-' or not tc:
        return 'c'                       # correspondence
    try:
        base, inc = tc.split('+')
        est = int(base) + 40 * int(inc)
    except ValueError:
        return '?'
    return 'u' if est < 30 else 'b' if est < 180 else 'z' if est < 480 else 'r' if est < 1500 else 'k'


COMMENT = re.compile(rb'\{[^}]*\}')
RESULTS = {b'1-0', b'0-1', b'1/2-1/2', b'*'}


def first_moves(line):
    def tok(chunk):
        chunk = COMMENT.sub(b' ', chunk)
        cut = chunk.find(b'{')
        if cut >= 0:
            chunk = chunk[:cut]
        out = []
        for t in chunk.split():
            if t[-1:] == b'.' or t in RESULTS or t[:1] == b'$':
                continue
            out.append(t.rstrip(b'?!').decode())
            if len(out) == PLIES:
                break
        return out
    mv = tok(line[:1600])
    if len(mv) < PLIES and len(line) > 1600:
        mv = tok(line)
    return mv


def h64(s):
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), 'big', signed=True)


class HashingReader(io.RawIOBase):
    def __init__(self, f):
        self.f, self.sha = f, hashlib.sha256()
    def readable(self):
        return True
    def readinto(self, b):
        n = self.f.readinto(b)
        if n:
            self.sha.update(memoryview(b)[:n])
        return n


def index_file(path, month, want_sha):
    import pyarrow as pa, pyarrow.parquet as pq, zstandard
    cols = ['id', 'day', 'white', 'black', 'welo', 'belo', 'speed', 'result', 'tc', 'moves', 'n'] + [f'h{k}' for k in HASH_AT]
    schema = pa.schema([('id', pa.string()), ('day', pa.int32()), ('white', pa.string()), ('black', pa.string()),
                        ('welo', pa.int16()), ('belo', pa.int16()), ('speed', pa.string()), ('result', pa.int8()),
                        ('tc', pa.string()), ('moves', pa.string()), ('n', pa.int8())] + [(f'h{k}', pa.int64()) for k in HASH_AT])
    tmp = f'{PARQ}/{month}.parquet.tmp'
    writer = pq.ParquetWriter(tmp, schema, compression='zstd', compression_level=6, use_dictionary=['speed', 'tc', 'white', 'black'])
    buf = {c: [] for c in cols}
    raw = open(path, 'rb', buffering=0)
    hr = HashingReader(raw)
    stream = zstandard.ZstdDecompressor().stream_reader(io.BufferedReader(hr, 1 << 20), read_size=1 << 20)
    lines = io.BufferedReader(stream, 1 << 22)
    h = {}
    n_games = n_kept = 0
    t0 = time.time()

    def flush():
        writer.write_table(pa.table(buf, schema=schema))
        for c in cols:
            buf[c].clear()

    for line in lines:
        if line[:1] == b'[':
            sp = line.find(b' ')
            h[line[1:sp]] = line[sp + 2:line.rfind(b'"')]
            continue
        if line[:1] != b'1':
            continue
        n_games += 1
        tc = h.get(b'TimeControl', b'').decode()
        sp = speed_of(tc)
        if sp in ('u', 'b'):              # wie der Explorer: ohne Bullet/UltraBullet
            h = {}
            continue
        mv = first_moves(line)
        res = h.get(b'Result', b'*')
        site = h.get(b'Site', b'')
        try:
            welo = int(h.get(b'WhiteElo', b'0')); belo = int(h.get(b'BlackElo', b'0'))
        except ValueError:
            welo = belo = 0
        buf['id'].append(site[site.rfind(b'/') + 1:].decode())
        buf['day'].append(int(h.get(b'UTCDate', b'0').replace(b'.', b'') or 0))
        buf['white'].append(h.get(b'White', b'?').decode('utf-8', 'replace'))
        buf['black'].append(h.get(b'Black', b'?').decode('utf-8', 'replace'))
        buf['welo'].append(min(welo, 32767)); buf['belo'].append(min(belo, 32767))
        buf['speed'].append(sp)
        buf['result'].append(1 if res == b'1-0' else -1 if res == b'0-1' else 0 if res == b'1/2-1/2' else 9)
        buf['tc'].append(tc)
        buf['moves'].append(' '.join(mv))
        buf['n'].append(len(mv))
        for k in HASH_AT:
            buf[f'h{k}'].append(h64(' '.join(mv[:k])) if len(mv) >= k else None)
        n_kept += 1
        h = {}
        if len(buf['id']) >= 500_000:
            flush()
            if n_kept % 5_000_000 < 500_000:
                log(f'{month}: {n_kept:,} Partien im Index ({n_games:,} gelesen, {n_games / (time.time() - t0):,.0f}/s)')
    if buf['id']:
        flush()
    writer.close()
    while raw.read(1 << 20):              # Rest für die Prüfsumme (Puffer hinter dem letzten Spiel)
        pass
    have = hr.sha.hexdigest()
    if want_sha and have != want_sha:
        os.remove(tmp)
        raise RuntimeError(f'Prüfsumme falsch ({have} != {want_sha})')
    os.replace(tmp, f'{PARQ}/{month}.parquet')
    return n_games, n_kept, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--link-only', action='store_true')
    a = ap.parse_args()
    for d in (INBOX, PARQ, STATE):
        os.makedirs(d, exist_ok=True)
    link_dumps()
    if a.link_only:
        return
    lock = open(f'{STATE}/run.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    sums = {}
    for l in fetch_text(f'{BASE}/sha256sums.txt').splitlines():
        p = l.split()
        if len(p) == 2:
            sums[p[1]] = p[0]
    while True:
        link_dumps()
        files = sorted((f for f in os.listdir(INBOX) if NAME.match(f)), reverse=True)
        done = read_set(f'{STATE}/indexed.txt')
        files = [f for f in files if NAME.match(f).group(1) not in done]
        if not files:
            if fetch_missing(sums):
                continue
            break
        f = files[0]
        month = NAME.match(f).group(1)
        log(f'{month}: Index startet')
        try:
            n, k, sec = index_file(f'{INBOX}/{f}', month, sums.get(f))
        except Exception as e:
            log(f'{month}: FEHLER {e} — Dump verworfen, wird neu geladen')
            os.remove(f'{INBOX}/{f}')
            continue
        with open(f'{STATE}/indexed.txt', 'a') as s:
            s.write(month + '\n')
        os.remove(f'{INBOX}/{f}')
        log(f'{month}: fertig — {k:,} von {n:,} Partien im Index, {sec / 60:.0f} min')
        # Während der Index lief, kann ein weiterer Download nötig geworden sein: Vorrat auffüllen.
    log('nichts mehr zu tun')


if __name__ == '__main__':
    main()
