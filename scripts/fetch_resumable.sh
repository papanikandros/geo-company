#!/usr/bin/env bash
# Resumable, single-copy download for large raw files on a metered line.
#
#   scripts/fetch_resumable.sh URL OUT EXPECT_BYTES ETAG [LOG]
#
# Never restarts from zero: `curl -C -` continues from the current size of OUT.part;
# `If-Range: ETAG` makes a changed server file FAIL (HTTP 200 → curl refuses to resume)
# instead of silently starting a second copy. On success OUT.part is renamed to OUT and a
# sha256 is written next to it. Exit 0 only when the size matches EXPECT_BYTES.
set -u
URL=$1; OUT=$2; EXPECT=$3; ETAG=$4; LOG=${5:-$OUT.log}
PART="$OUT.part"
mkdir -p "$(dirname "$OUT")"
if [ -f "$OUT" ] && [ "$(stat -c %s "$OUT")" -eq "$EXPECT" ]; then
  echo "$(date -Is) already complete: $OUT" >> "$LOG"; exit 0
fi
for attempt in $(seq 1 60); do
  have=$(stat -c %s "$PART" 2>/dev/null || echo 0)
  if [ "$have" -ge "$EXPECT" ]; then break; fi
  echo "$(date -Is) attempt $attempt resuming at $have" >> "$LOG"
  curl -sS -C - -H "If-Range: $ETAG" --speed-limit 10000 --speed-time 60 --max-time 7200 \
       -o "$PART" -w 'rc=%{http_code} got=%{size_download} avg=%{speed_download}B/s t=%{time_total}s\n' \
       "$URL" >> "$LOG" 2>&1
  echo "$(date -Is) curl exit $?" >> "$LOG"
  have=$(stat -c %s "$PART" 2>/dev/null || echo 0)
  [ "$have" -ge "$EXPECT" ] && break
  sleep 15
done
have=$(stat -c %s "$PART" 2>/dev/null || echo 0)
if [ "$have" -ne "$EXPECT" ]; then
  echo "$(date -Is) FAILED: size $have / $EXPECT" >> "$LOG"; exit 1
fi
mv "$PART" "$OUT"
( cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
echo "$(date -Is) complete: $OUT $have bytes" >> "$LOG"
