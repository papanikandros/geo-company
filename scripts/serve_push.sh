#!/usr/bin/env bash
# Push a data version to a serve target and rebuild it there (serve-plan.md §5).
#
#   scripts/serve_push.sh user@host:/path/to/geo-company [SCOPE]
#   scripts/serve_push.sh root@badserver1:/opt/geo-company bremen
#   PUSH_BUILT=1 scripts/serve_push.sh badserver1:/opt/geo-company bremen   (see below)
#
# Copies ONLY the inputs of `geoextract serve build` for THAT SCOPE with rsync
# (resumable, checksummed), then runs the builder container on the target, which writes
# data/serve/<SCOPE>/<version>/ and switches the `current` symlink. Tiles are never
# uploaded: they are built where they are served. Nothing is deleted on the target.
#
# Env:
#   PUSH_BUILT=1            push the FINISHED version directory built here instead of the
#                           build inputs, and only switch `current` on the target. Use this
#                           when the target has no per-source parquets (665 MB for Bremen,
#                           much more DE-wide): the nested "full" tier and the card's raw
#                           records are complete only where those sources exist.
#   PUSH_SOURCES=0          skip the per-source parquets (src_*). The build still
#                           succeeds; company records lose their nested per-source
#                           detail (tier "full" / GET /v1/companies/{id}).
#   PUSH_OSM_STATES="a b"   extra OSM state slugs for a multi-state scope
#                           (e.g. SCOPE=bremen-hamburg → "bremen hamburg").
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
PUSH_SOURCES=${PUSH_SOURCES:-1}
PUSH_BUILT=${PUSH_BUILT:-0}
CONTAINER_UID_DEFAULT=10001

if [ "$PUSH_BUILT" = "1" ]; then
  VERSION=$(readlink "$DATA/serve/$SCOPE/current" || true)
  [ -n "$VERSION" ] || { echo "no $DATA/serve/$SCOPE/current — run: geoextract serve build --scope $SCOPE"; exit 1; }
  SRC="$DATA/serve/$SCOPE/$VERSION"
  echo "== pushing the built version $SCOPE/$VERSION ($(du -sh "$SRC" | cut -f1)) to $TARGET"
  ssh "$HOST" "mkdir -p '$REMOTE_DIR/data/serve/$SCOPE'"
  # unchanged files are hard-linked from the live version instead of sent again
  rsync -a --info=progress2 --partial --checksum --link-dest=../current \
    "$SRC/" "$TARGET/data/serve/$SCOPE/$VERSION.incoming/"
  # switch only when the transfer is complete: rename, repoint `current`, keep the old version
  ssh "$HOST" "cd '$REMOTE_DIR/data/serve/$SCOPE' && rm -rf '$VERSION.old' && \
    { [ -d '$VERSION' ] && mv '$VERSION' '$VERSION.old' || true; } && mv '$VERSION.incoming' '$VERSION' && \
    ln -sfn '$VERSION' current && chown -R ${CONTAINER_UID:-$CONTAINER_UID_DEFAULT}:${CONTAINER_UID:-$CONTAINER_UID_DEFAULT} '$VERSION' current 2>/dev/null || true; \
    ls -la current && du -sh '$VERSION'"
  echo "== done. Restart the API on the target — it holds the parquet files open:"
  echo "   ssh $HOST 'cd $REMOTE_DIR && docker compose restart api'"
  exit 0
fi

# The serve image runs as an unprivileged user (Dockerfile: useradd -u 10001), so the
# bind-mounted data dir must belong to it — a fresh checkout's is root-owned and the
# build dies on mkdir. paths.py `_ensured()` creates parents even to TEST for a file,
# so this is needed whether or not the optional inputs below are present.
CONTAINER_UID=10001

# Required inputs — the build fails without these.
required=(
  "geoextract/companies_merged_${SCOPE}_4326.parquet"
  "geoextract/companies_merged_${SCOPE}_summary.json"
)
for f in "${required[@]}"; do
  [ -f "$DATA/$f" ] || { echo "missing $DATA/$f — run the extract first" >&2; exit 1; }
done

# Optional inputs — `serve build` guards each with an existence check, so a missing file
# silently drops a layer or a column block instead of failing. Warn loudly.
optional=(
  "geoextract/register_only_${SCOPE}_4326.parquet"   # register-only companies layer
  "geoextract/landuse_${SCOPE}_4326.parquet"         # landuse underlay
  "geoextract/src_wikidata/wikidata_items.parquet"   # wikidata-only markers (DE-wide)
)

# Per-source parquets behind the merged table (build.py `source_files`): OSM is per state,
# every other source is one DE-wide file. Needed for the nested per-source detail.
includes=(
  --include='geoextract/'
  --include="geoextract/companies_merged_${SCOPE}_*"
  --include="geoextract/register_only_${SCOPE}_4326.parquet"
  --include="geoextract/landuse_${SCOPE}_4326.parquet"
  --include='geoextract/src_wikidata/'
  --include='geoextract/src_wikidata/wikidata_items.parquet'
)
if [ "$PUSH_SOURCES" = "1" ]; then
  includes+=(--include='geoextract/src_*/')
  # non-OSM sources: always DE-wide, regardless of scope
  includes+=(--include='geoextract/src_abwaerme/src_abwaerme_DE_4326.parquet')
  includes+=(--include='geoextract/src_ied/src_ied_DE_4326.parquet')
  includes+=(--include='geoextract/src_mastr/src_mastr_DE_4326.parquet')
  includes+=(--include='geoextract/src_overture/src_overture_DE_4326.parquet')
  optional+=("geoextract/src_overture/src_overture_DE_4326.parquet")
  if [ "$SCOPE" = "DE" ]; then
    includes+=(--include='geoextract/src_osm/src_osm_*_4326.parquet')
  else
    for st in $SCOPE ${PUSH_OSM_STATES:-}; do
      includes+=(--include="geoextract/src_osm/src_osm_${st}_4326.parquet")
      optional+=("geoextract/src_osm/src_osm_${st}_4326.parquet")
    done
  fi
else
  echo "[push] PUSH_SOURCES=0 — per-source parquets skipped; nested source detail will be absent" >&2
fi
includes+=(--exclude='*')

for f in "${optional[@]}"; do
  [ -f "$DATA/$f" ] || echo "[push] WARNING: $f absent — that layer/detail will be missing from the build" >&2
done

echo "[push] rsync inputs → $TARGET (scope $SCOPE)"
rsync -avh --partial --progress --checksum "${includes[@]}" "$DATA/" "$TARGET/data/"

# IMAGE_TARGET=full selects the image with tippecanoe compiled in — the builder cannot
# make tiles without it, and the API is happy to run from the same image.
echo "[push] build on target"
ssh "$HOST" "cd '$REMOTE_DIR' \
  && mkdir -p data/serve && chown -R ${CONTAINER_UID}:${CONTAINER_UID} data \
  && IMAGE_TARGET=full SCOPE='$SCOPE' docker compose --profile build run --rm builder \
  && IMAGE_TARGET=full SCOPE='$SCOPE' docker compose up -d && docker compose ps"
echo "[push] done — data/serve/$SCOPE/current now points at the new version on $HOST"
