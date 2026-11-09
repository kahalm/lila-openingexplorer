#!/bin/bash
# Trockenlauf von rookhub/explorer-sync (Teil „masters“) gegen einen leeren Testordner, mit Stubs für
# curl, lumbra-fetch, 7z und import-masters — kein Netz, kein Explorer. S2-002: eine neue Lumbra-
# Version darf nur als gesehen gelten, wenn genau das dafür geladene Paket eingespielt ist.
#
# Aufruf (im Image, nie gegen /mnt/disks/sdf/rookhub-explorer):
#   docker run --rm --network none -v "$PWD":/src:ro rookhub-explorer:<tag> bash /src/tests/rookhub/test-explorer-sync.sh
set -uo pipefail
SRC=$(cd "$(dirname "$0")/../.." && pwd)
SYNC=$SRC/rookhub/explorer-sync
YEAR=$(date +%Y)
TITLE="OTB partial $YEAR"
NAME="LumbrasGigaBase_OTB_${YEAR}_partial_release.7z"
fails=0
check() { if eval "$2"; then echo "  OK   $1"; else echo "  FAIL $1"; fails=$((fails + 1)); fi; }

setup() {   # $1 = lumbra-fetch-Exit, $2 = import-masters-Exit
    W=$(mktemp -d)
    mkdir -p "$W/bin" "$W/data/state" "$W/drop"
    cat > "$W/bin/curl" <<STUB
#!/bin/bash
case "\$*" in
    *download-area*) echo "<p>$TITLE Version: $YEAR-12-01</p>" ;;
    *) echo ok ;;
esac
STUB
    cat > "$W/bin/lumbra-fetch" <<STUB
#!/bin/bash
[[ $1 == 0 ]] || exit $1
printf 'neues Paket %s' "\$RANDOM\$RANDOM" > "\$2/$NAME"
echo "\$2/$NAME"
STUB
    printf '#!/bin/bash\ncat "$3"\n' > "$W/bin/7z"
    printf '#!/bin/bash\ncat > /dev/null\nexit %s\n' "$2" > "$W/bin/import-masters"
    chmod +x "$W/bin/"*
    # Alte, schon eingespielte Datei im (nur lesenden) Drop-Ordner — gleicher Name, ohne Version.
    printf 'altes Paket' > "$W/drop/$NAME"
    echo "$(sha256sum "$W/drop/$NAME" | cut -d' ' -f1) $NAME" > "$W/data/state/lumbra-imported.txt"
}

run() {
    PATH="$W/bin:$PATH" DATA="$W/data" ENDPOINT=http://stub LUMBRA_DROP="$W/drop" \
        LUMBRA_AREA=https://stub/download-area/ "$@" bash "$SYNC" masters > "$W/log" 2>&1
}
seen() { grep -qxF "$TITLE@$YEAR-12-01" "$W/data/state/lumbra-seen.txt"; }

echo "== Download gesperrt (75), alte Datei im Drop =="
setup 75 0; run env
check "Version NICHT als gesehen" '! seen'
rm -rf "$W"

echo "== Download ok, Import ok =="
setup 0 0; run env
check "Version gesehen" 'seen'
check "Eingangsdatei nach Import gelöscht" '[[ ! -e $W/data/masters/inbox/$NAME ]]'
check "Drop-Datei bleibt liegen" '[[ -e $W/drop/$NAME ]]'
rm -rf "$W"

echo "== Download ok, Import scheitert, alte Datei im Drop bekannt =="
setup 0 1; run env
check "Version NICHT als gesehen" '! seen'
check "Lauf meldet Fehler" 'grep -q "fehlgeschlagen" "$W/log"'
rm -rf "$W"

echo "== LUMBRA_FETCH=0: nur alte Datei im Drop =="
setup 0 0; run env LUMBRA_FETCH=0
check "Version NICHT als gesehen" '! seen'
echo "== LUMBRA_FETCH=0: neue Datei von Hand in den Drop gelegt =="
printf 'von Hand geladen' > "$W/drop/$NAME"; run env LUMBRA_FETCH=0
check "Version gesehen" 'seen'
rm -rf "$W"

echo
if (( fails )); then echo "FAILED: $fails"; exit 1; fi
echo "Alle explorer-sync-Tests bestanden."
