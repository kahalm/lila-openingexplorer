#!/bin/sh

set -e

# Build first, so that a build error stops the script (the Lichess sample
# below is expected to end with an error).
cargo build --release --manifest-path import-pgn/Cargo.toml

##############

echo "Importing sample of Lichess games into db..."

curl \
    --range 0-50000000 \
    --remote-name \
    https://database.lichess.org/standard/lichess_db_standard_rated_2026-04.pgn.zst

# The sample is cut off after 50 MB: import-lichess imports everything up to
# that point and then fails with "incomplete frame", which is expected here.
cargo run --release --manifest-path import-pgn/Cargo.toml -- *.pgn.zst \
    || echo "import-lichess exited with $? (expected for the truncated sample)"

##############

echo "Importing sample of masters games into db..."

curl \
    --remote-name \
    https://theweekinchess.com/zips/twic1644g.zip

unzip twic1644g.zip

cargo run --release --manifest-path import-pgn/Cargo.toml --bin import-masters -- twic1644.pgn
