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
  Grundstock = einmaliger Import von „OTB Complete“ (siehe [Neuaufbau](#neuaufbau)), der Sync holt
  danach nur das laufende Jahr.

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

## Neuaufbau

Die RocksDB unter `db/` liegt in keinem Backup (`rookhub/scripts/backup-db.sh` sichert nur MariaDB).
Ist sie weg, reicht `explorer-sync` allein **nicht**: Bei den Meistern holt der Sync nur „OTB
partial“ des laufenden Jahres (im Januar zusätzlich das Vorjahr). Der historische Bestand stammt aus
einem einmaligen Import von Lumbras „OTB Complete“; fehlt er, liefert `/masters` ohne jede
Fehlermeldung nur Partien des laufenden Jahres. Reihenfolge (Zeiten gemessen beim Erstaufbau
2026-09-22 bis 2026-09-28):

1. **Leere DB:** Image bauen (siehe Betrieb), Datenverzeichnis für uid 1000 anlegen, Stack starten;
   der Explorer legt `db/` beim Start selbst an.

   ```sh
   d=/mnt/disks/sdf/rookhub-explorer
   sudo install -d -o 1000 -g 1000 "$d" "$d/masters"
   cd /opt/stacks/rookhub-explorer && docker compose up -d
   ```

2. **Meister-Grundstock „OTB Complete“:** Quelle ist
   <https://lumbrasgigabase.com/en/download-in-pgn-format-en/>, Paket „OTB Complete“ (Slug
   `otb-complete`, `LumbrasGigaBase_OTB_Complete.7z`, ~1,5 GB, entpackt 8,6 GB PGN). Der Knopf leitet
   auf einen MEGA-Link weiter, den `megatools` aus dem Image lädt (~6 min). Immer die aktuelle Fassung
   laden: Lumbra aktualisiert monatlich, und die Kopie in `masters/` (Stand 2026-07) liegt auf derselben
   Platte wie die DB. Eine vorhandene alte Kopie vorher löschen, `megatools` überschreibt nicht. Der
   Import streamt das Archiv, ohne es zu entpacken: 71 min, danach ~6,5 GB in `db/` (2026-09-22:
   `imported: 3715729, duplicate: 51616, rejected: 0, skipped (filter/illegal): 6588143`).

   ```sh
   docker run --rm --user 1000:1000 -v /mnt/disks/sdf/rookhub-explorer:/data rookhub-explorer:latest \
     megatools dl --no-progress --path /data/masters 'https://mega.nz/file/…'
   docker run --rm --name rookhub-explorer-masters-import --user 1000:1000 \
     --network rookhub-explorer_default -v /mnt/disks/sdf/rookhub-explorer:/data \
     rookhub-explorer:latest bash -c 'set -o pipefail
       7z x -so /data/masters/LumbrasGigaBase_OTB_Complete.7z \
         | import-masters --endpoint http://rookhub-explorer:9002 /dev/stdin 2>&1 | tail -n 20'
   ```

   Die letzte Zeile ist die Zusammenfassung. Exit 3 heißt, der Server hat Partien abgelehnt (Gründe
   stehen davor), jeder andere Exit ≠ 0 heißt abgebrochen: einfach wiederholen, bereits Importiertes
   zählt dann nur als Dublette. Mit einer älteren Complete-Kopie fehlt, was Lumbra seit ihrem Stand
   nachgetragen hat. Dann für jedes Jahr von ihrem Stand bis zum Vorjahr das Jahrespaket (z. B. „OTB
   2025“) genauso importieren.

3. **`explorer-sync`:** den Cron-Befehl aus [Nachimport](#nachimport) einmal von Hand starten (oder
   auf 03:15 warten). „OTB partial“ ergibt jetzt fast nur Dubletten (2026-09-22:
   `imported: 0, duplicate: 44400`), danach folgen die Lichess-Monate ab `LICHESS_FROM`, neueste
   zuerst: je Monat 10 bis 60 min Download samt Prüfsumme und 3 bis 10 h Import, für 20 Monate
   (2026-08 bis 2025-01) knapp 6 Tage.

**Plattenbedarf** (Stand 2026-09-29): `db/` 491 GB, davon Meister ~6,5 GB und Lichess ~24 GB je Monat.
Dazu kommen während des Syncs ein Monats-Dump (~30 GB, nach dem Import gelöscht) und die
Complete-Datei (1,6 GB). Für den Stand bis 2025-01 also mindestens 550 GB frei, jeder weitere Monat
braucht ~25 GB mehr.

## Änderungen gegenüber upstream

- `import-pgn/src/bin/import-lichess.rs`: Filter `--min-avg-rating`, `--exclude-speed`.
- `import-pgn/src/bin/import-masters.rs`: neuer, schneller Meister-Importer (statt `import-master.py`).
- `src/main.rs`/`src/lila.rs`: Spieler-Blacklist nur mit Lichess-Token abfragen (ohne Token schlug
  der Abruf alle 5 s mit 401 fehl).
- `Cargo.toml`: rocksdb ohne `io-uring` (Docker blockiert io_uring per seccomp).
- `Dockerfile.rookhub`, `rookhub/explorer-sync`, `rookhub/explorer-gateway.conf`, diese Datei.
