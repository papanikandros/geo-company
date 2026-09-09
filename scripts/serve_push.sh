#!/usr/bin/env bash
# Push a data version to a serve target and rebuild it there (serve-plan.md §5).
#
#   scripts/serve_push.sh user@host:/srv/geo-company [SCOPE]
#
# Copies ONLY the inputs of `geoextract serve build` (merged parquet ≈ 0.7 GB for DE,
# per-source parquets, landuse, summary) with rsync — resumable, checksummed — then runs the
# builder container on the target, which writes data/serve/<SCOPE>/<version>/ and switches
# the `current` symlink. Tiles are never uploaded: they are built where they are served.
# The target needs: the repo checked out at the given path, docker compose, `docker compose
# build` done once. Nothing is deleted on the target.
set -euo pipefail
TARGET=${1:?usage: serve_push.sh user@host:/path/to/geo-company [SCOPE]}
SCOPE=${2:-DE}
HOST=${TARGET%%:*}
REMOTE_DIR=${TARGET#*:}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${GEOEXTRACT_DATA_DIR:-$ROOT/data}

inputs=(
  "geoextract/companies_merged_${SCOPE}_4326.parquet"
  "geoextract/companies_merged_${SCOPE}_summary.json"
)
for f in "${inputs[@]}"; do
  [ -f "$DATA/$f" ] || { echo "missing $DATA/$f — run the extract first" >&2; exit 1; }
done

echo "[push] rsync inputs → $TARGET (scope $SCOPE)"
rsync -avh --partial --progress --checksum \
  --include='geoextract/' \
  --include="geoextract/companies_merged_${SCOPE}_*" \
  --include='geoextract/src_*/' --include='geoextract/src_*/*.parquet' \
  --include='geoextract/landuse_*_4326.parquet' \
  --exclude='*' \
  "$DATA/" "$TARGET/data/"

echo "[push] build on target"
ssh "$HOST" "cd '$REMOTE_DIR' && SCOPE='$SCOPE' docker compose --profile build run --rm builder \
  && docker compose up -d && docker compose ps"
echo "[push] done — data/serve/$SCOPE/current now points at the new version on $HOST"
