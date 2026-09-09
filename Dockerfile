# geoextract serve image: FastAPI + DuckDB API, map page, and the serve build (tippecanoe).
# Runs identically on the VPS, the institute server and a laptop (docker-compose.yml).
#
#   docker compose build                          # target "runtime" (default): serve only, ≈ 220 MB
#   IMAGE_TARGET=full docker compose build        # + tippecanoe compiled in (≈ 450 MB, ~10 min)
#   docker compose --profile build run --rm builder   # merged parquet → data/serve/<scope>/<version>
#   docker compose up -d                          # caddy (:80/:443) + api
#
# Data never lives in the image: ./data is bind-mounted at /data (GEOEXTRACT_DATA_DIR).
# Stage order matters: without the buildx plugin, docker's legacy builder builds every stage
# up to the target in FILE order, so the slim "runtime" stage comes first and the tippecanoe
# compile stage only runs for the "full" target.

# --- stage 1: runtime (target "runtime": serve only, no tippecanoe — tiles built elsewhere) --
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 GEOEXTRACT_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends libsqlite3-0 zlib1g \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-serve.txt ./
RUN pip install --no-cache-dir -r requirements-serve.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
RUN useradd -r -u 10001 geoextract && mkdir -p /data && chown geoextract /data
USER geoextract
VOLUME ["/data"]
EXPOSE 8791
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8791/healthz').status == 200 else 1)"
ENTRYPOINT ["geoextract"]
CMD ["serve", "api", "--scope", "DE", "--host", "0.0.0.0", "--port", "8791"]

# --- stage 2: tippecanoe (C++; not on PyPI) — only for the "full" target ----------------
FROM debian:bookworm-slim AS tippecanoe
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git libsqlite3-dev zlib1g-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG TIPPECANOE_VERSION=2.79.0
RUN git clone --depth 1 --branch ${TIPPECANOE_VERSION} https://github.com/felt/tippecanoe.git /src \
    && make -C /src -j"$(nproc)" && make -C /src install

# --- stage 3: runtime + tippecanoe (target "full": the builder profile can make tiles) --------
FROM runtime AS full
COPY --from=tippecanoe /usr/local/bin/tippecanoe /usr/local/bin/tippecanoe-decode \
     /usr/local/bin/tile-join /usr/local/bin/
