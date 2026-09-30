# RookHub: lokaler Eröffnungs-Explorer

Fork von [lila-openingexplorer](https://github.com/lichess-org/lila-openingexplorer) (AGPL-3.0,
Remote `upstream`). Liefert dieselben Endpoints wie `explorer.lichess.ovh`, aber aus lokalen Daten,
ohne Token und ohne Rate-Limit. Genutzt vom Lochfinder in rookhub (`LichessExplorer:LocalUrl`).

## Betrieb

| Was | Wo |
|---|---|
| Stack | `/opt/stacks/rookhub-explorer/` = `rookhub/compose.yaml` + `rookhub/explorer-gateway.conf` (Container `rookhub-explorer`, `rookhub-explorer-gateway`) |
| Cache | 3 GiB RocksDB-Block-Cache (`--db-cache`, 2026-09-23 von 6 GiB gesenkt: Host hat 47 GB für alle Dienste) |
| Daten | `/mnt/disks/sdf/rookhub-explorer/` — `db/` (RocksDB), `dumps/`, `masters/`, `state/`, `sync.log` |
| Image | `rookhub-explorer:latest`, lokal gebaut: `docker build -f Dockerfile.rookhub -t rookhub-explorer:latest .` |
| rookhub-Netze | `http://rookhub-explorer:9002/` in `rookhub-schach_rookhub` und `rookhub-schach-dev_rookhub-dev` = der **Gateway** (Alias), nur `GET /lichess` und `GET /masters`, alles andere 403 |
| Direkt | `http://rookhub-explorer:9002/` im Netz `rookhub-explorer_default` (Gateway, `explorer-sync`) |
| Host | `http://127.0.0.1:9002/` (nur localhost, direkt) |

Der Explorer hat keine Authentifizierung. Admin sind `/import/*`, `/compact` und auch `GET /player`
(`/personal`): das lädt die ganze Partiehistorie beliebiger Lichess-Konten dauerhaft in die DB. Diese
Routen (und `/monitor`) erreichen nur localhost und das Netz `rookhub-explorer_default`; die
rookhub-Container (PROD und DEV) sehen nur den Gateway (`rookhub/explorer-gateway.conf`). Den Explorer
nie selbst in die rookhub-Netze hängen und nie nach außen routen.

```sh
curl '127.0.0.1:9002/masters?fen=rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR%20b%20KQkq%20-%200%201&moves=5&topGames=0'
curl '127.0.0.1:9002/lichess?variant=standard&fen=…&ratings=1600,1800,2000&speeds=blitz,rapid,classical&moves=40&topGames=0&recentGames=0'
```

Gateway einführen, ohne den Explorer neu zu erstellen (ein volles `docker compose up -d` erstellt ihn
neu = RocksDB-Neustart), in `/opt/stacks/rookhub-explorer/`:

```sh
docker compose up -d explorer-gateway
docker network disconnect rookhub-schach_rookhub rookhub-explorer
docker network disconnect rookhub-schach-dev_rookhub-dev rookhub-explorer
```

Prüfen, wie ein rookhub-Container es sieht, erst **nach beiden** `network disconnect`: Vor dem
Deploy antwortet in den rookhub-Netzen der Explorer selbst, und im Übergangsfenster liefert Docker-DNS
für `rookhub-explorer` beide Container, ein Teil der Anfragen landet also direkt am Explorer. Deshalb
prüft der Befehl nur mit der harmlosen Route `GET /monitor` (über den Gateway `403`, direkt `200`) und
mehrfach, nie mit `/compact`, `/import/*` oder `/player`. Erwartet je Netz `10 403`; jede `200` heißt,
der Explorer hängt dort noch (disconnect wiederholen). `/masters?fen=…` liefert `200`.

```sh
for net in rookhub-schach_rookhub rookhub-schach-dev_rookhub-dev; do
  echo "$net:"
  docker run --rm --network "$net" --entrypoint sh rookhub-explorer:latest -c \
    'for i in 1 2 3 4 5 6 7 8 9 10; do
       curl -s -o /dev/null -w "%{http_code}\n" http://rookhub-explorer:9002/monitor
     done' | sort | uniq -c
done
```

## Datenumfang

- **lichess**: gewertete Partien von database.lichess.org mit Elo-**Schnitt ≥ 1600**, ohne Bullet und
  UltraBullet (`import-lichess --min-avg-rating 1600 --exclude-speed bullet --exclude-speed ultraBullet`).
  Sinnvolle Filter lokal: `ratings` 1600–2500, `speeds` blitz/rapid/classical/correspondence.
  Indexiert werden (wie bei Lichess) die ersten 50 Halbzüge. Monate ab `LICHESS_FROM` (Standard 2025-01).
- **masters**: Lumbra's GigaBase OTB (PGN, CC BY-NC-SA 4.0), Elo-Schnitt ≥ 2200 und ab 1952 (beides
  Regeln des Servers), alle Züge. IDs sind ein Inhalts-Hash → Re-Importe ergeben nur Dubletten.

## Nachimport

Cron (User `kahalm`), täglich 03:15:

```sh
docker run --rm --name rookhub-explorer-sync --user 1000:1000 --network rookhub-explorer_default \
  -v /mnt/disks/sdf/rookhub-explorer:/data rookhub-explorer:latest explorer-sync
```

`rookhub/explorer-sync` (im Image unter `/usr/local/bin`):

1. **Meister**: prüft, ob Lumbra „OTB partial <Jahr>" eine neue Version hat (MEGA-Link geändert),
   lädt sie per `megatools` und importiert sie. Im Januar zusätzlich das Vorjahr.
2. **Lichess**: jeder Monat ≥ `LICHESS_FROM`, der nicht in `state/lichess-imported.txt` steht, neueste
   zuerst: Download (fortsetzbar), sha256 gegen `sha256sums.txt`, gefilterter Import, Dump löschen.

**Importzeiten:** Standard ist rund um die Uhr. Ein Monat (~28 Mio. Partien, ~27 GB DB) schreibt
schneller, als RocksDB auf der HDD kompaktiert; während des Rückstands steigen die `/lichess`-Latenzen
auf Sekunden (rookhub fängt das mit 30 s Timeout + Wiederholung ab). Wer Importe tagsüber pausieren
will: `-e IMPORT_AVOID_UTC_HOURS="4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19"` (UTC-Stunden, hier
06–22 Uhr Sommerzeit). Rückstand prüfen:
`curl 127.0.0.1:9002/monitor/cf/lichess/rocksdb.estimate-pending-compaction-bytes`.

Alles idempotent; `flock` + fester Containername verhindern parallele Läufe. Einen Monat erneut
importieren: Zeile aus `state/lichess-imported.txt` löschen. Weiter zurück: `-e LICHESS_FROM=2024-01`.

Die Importer enden mit Exit 3, wenn der Server Partien abgelehnt hat (Zusammenfassung als letzte
Zeile, davor die letzten Ablehnungen, alles in `sync.log`). Solche Monate bzw. Lumbra-Versionen
landen in `state/lichess-rejected.txt` bzw. `state/lumbra-rejected.txt` statt in `*-imported.txt`
und werden nicht automatisch wiederholt (Zeile löschen = erneut importieren). Ein fehlender oder
unerwarteter Lumbra-Link gilt als Fehler: Der Lauf endet mit „Sync fertig — mit Fehlern" und Exit 1.

## Änderungen gegenüber upstream

- `import-pgn/src/bin/import-lichess.rs`: Filter `--min-avg-rating`, `--exclude-speed`.
- `import-pgn/src/bin/import-masters.rs`: neuer, schneller Meister-Importer (statt `import-master.py`).
- `src/main.rs`/`src/lila.rs`: Spieler-Blacklist nur mit Lichess-Token abfragen (ohne Token schlug
  der Abruf alle 5 s mit 401 fehl).
- `Cargo.toml`: rocksdb ohne `io-uring` (Docker blockiert io_uring per seccomp).
- `Dockerfile.rookhub`, `rookhub/explorer-sync`, `rookhub/explorer-gateway.conf`, diese Datei.
