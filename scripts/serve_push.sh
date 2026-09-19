#!/usr/bin/env bash
# Push a data version to a serve target and rebuild it there (serve-plan.md §5).
#
#   scripts/serve_push.sh user@host:/path/to/geo-company [SCOPE]
#   scripts/serve_push.sh root@badserver1:/opt/geo-company bremen
#
# Copies ONLY the inputs of `geoextract serve build` for THAT SCOPE with rsync
# (resumable, checksummed), then runs the builder container on the target, which writes
# data/serve/<SCOPE>/<version>/ and switches the `current` symlink. Tiles are never
# uploaded: they are built where they are served. Nothing is deleted on the target.
#
# The target needs: the repo checked out at the given path, docker compose, the shared
# proxy stack up (it owns the `proxy` network), and — for the builder — the "full" image:
#   IMAGE_TARGET=full docker compose build
set -euo pipefail
TARGET=${1:?usage: serve_push.sh user@host:/path/to/geo-company [SCOPE]}
SCOPE=${2:-DE}
HOST=${TARGET%%:*}
REMOTE_DIR=${TARGET#*:}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${GEOEXTRACT_DATA_DIR:-$ROOT/data}

# Required inputs — the build fails without these.
required=(
  "geoextract/companies_merged_${SCOPE}_4326.parquet"
  "geoextract/companies_merged_${SCOPE}_summary.json"
)
for f in "${required[@]}"; do
  [ -f "$DATA/$f" ] || { echo "missing $DATA/$f — run the extract first" >&2; exit 1; }
done

# Optional inputs — `serve build` guards each with an existence check, so a missing file
# silently drops a map layer instead of failing. Warn loudly rather than ship a quiet gap.
optional=(
  "geoextract/register_only_${SCOPE}_4326.parquet"     # stage-4 register-only companies layer
  "geoextract/landuse_${SCOPE}_4326.parquet"           # landuse underlay
  "geoextract/src_wikidata/wikidata_items.parquet"     # wikidata-only markers (scope-independent)
)
for f in "${optional[@]}"; do
  [ -f "$DATA/$f" ] || echo "[push] WARNING: $f absent — that layer will be missing from the build" >&2
done

echo "[push] rsync inputs → $TARGET (scope $SCOPE)"
rsync -avh --partial --progress --checksum \
  --include='geoextract/' \
  --include="geoextract/companies_merged_${SCOPE}_*" \
  --include="geoextract/register_only_${SCOPE}_4326.parquet" \
  --include="geoextract/landuse_${SCOPE}_4326.parquet" \
  --include='geoextract/src_wikidata/' \
  --include='geoextract/src_wikidata/wikidata_items.parquet' \
  --exclude='*' \
  "$DATA/" "$TARGET/data/"

echo "[push] build on target"
ssh "$HOST" "cd '$REMOTE_DIR' && SCOPE='$SCOPE' docker compose --profile build run --rm builder \
  && SCOPE='$SCOPE' docker compose up -d && docker compose ps"
echo "[push] done — data/serve/$SCOPE/current now points at the new version on $HOST"
