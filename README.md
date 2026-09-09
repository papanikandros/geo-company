# geoextract — companies at geolocations in Germany

`geoextract` builds one table of **existing companies at exact geolocations in Germany** from
open data, and serves it as an interactive map with a download API. Per company: name,
address and coordinates, website, company grounds surface (`grounds_area_m2`), the raw record
of every source that knows the company, and — Part C, in progress — the economic activity as
a WZ 2025 / NACE Rev. 2.1 code.

Sources merged today (4.14 M companies, 2026-09): OpenStreetMap, Overture Maps places, the
IED installation register (thru.de), the BfEE waste-heat platform (Plattform für Abwärme)
and the Marktstammdatenregister (MaStR). Register data (Handelsregister via OffeneRegister,
GLEIF) is loaded and will be joined next. The authoritative plan is `GEOEXTRACT_SPEC.md`;
working notes in `CLAUDE.md`.

## Quick start — use the served data

If somebody runs the server for you (see *Serving* below), you need nothing but a browser or
a parquet reader.

**Map:** open the site. Switch data layers on with the ⏻ buttons in the bottom toolbar, box-
or lasso-select companies, search by name, click a company for its card (all merged fields
plus the raw record of every source), download the listed rows as csv / parquet.

**Downloads:** ⇩ Downloads lists precomputed parquet extracts per state × sector. `flat` =
one row per company (the contract columns), `full` = the same plus one nested column per
source with that source's raw records. `sha256` per file is in `manifest.json`.

**API** (interactive docs at `/docs`):

```
GET  /v1/companies?state=Bremen&sector=industrial&format=parquet&tier=full
GET  /v1/companies?district=04011&industrial=true&format=geojson&limit=50000
GET  /v1/companies?bbox=8.7,53.0,8.9,53.2&fields=id,name,website&format=csv
POST /v1/companies/query          body: GeoJSON Polygon → same formats
GET  /v1/companies/{id}           every field + nested source records
GET  /v1/search?q=schwarzwaldmilch
GET  /v1/summary   /v1/extracts   /v1/manifest
```

Parameters: `state`, `district` (5-digit AGS or name), `sector` (business type today,
`nace_section` letter once Part C lands), `industrial=true`, `bbox=minx,miny,maxx,maxy`,
`tier=flat|full`, `format=json|geojson|csv|parquet`, `fields=` (column subset), `q=` (name
contains), `limit` / `offset`, `count_only=true`. JSON/GeoJSON answers are capped at 50 000
rows; csv and parquet stream the whole selection.

Python:

```python
import pandas as pd
url = "https://<host>/v1/companies?state=Bremen&industrial=true&format=parquet&tier=full"
df = pd.read_parquet(url)                      # nested source columns come back as lists of dicts
df["mastr"].dropna().iloc[0][0]["mastr_techs"]
```

DuckDB / SQL over a downloaded extract:

```sql
SELECT name, address_full, abwaerme[1].abw_heat_mwh_a AS heat_mwh
FROM 'bremen_is_industrial_full.parquet' WHERE abwaerme IS NOT NULL ORDER BY heat_mwh DESC;
```

R:

```r
library(arrow); df <- read_parquet("https://<host>/v1/companies?state=Bremen&format=parquet")
```

## Data contract

`src/geoextract/schema.py` is the single source of truth (spec §3). Contract columns in
order: `id, name, business_type, business_subtype, address_street, address_housenumber,
address_postcode, address_city, address_full, state, district, district_ags, website, phone,
latitude, longitude, grounds_area_m2, grounds_area_source, legal_form, hr_registration,
hr_court, source, source_count, confidence_score, merged_at, nace_codes, nace_primary,
nace_section, nace_confidence, nace_method, nace_reasoning, wz_code, is_industrial`, then
the website-hygiene columns `website_kind, website_host, website_listing, website_source`
and `member_ids`. `email` is never published. Coordinates are WGS84 (EPSG:4326); areas are
computed in EPSG:25832. `business_type` is an OSM-style category (office | shop | craft |
industrial | amenity | man_made | power); `is_industrial` is the intrinsic industrial
signal (register-backed or industrial tags), not a NACE verdict.

## Running the pipeline yourself

Python ≥ 3.12, [uv](https://docs.astral.sh/uv/). The connection matters: the OSM extracts
alone are ~4 GB, MaStR 3 GB, Overture is read from S3 per state.

```bash
uv sync                                   # core
uv sync --extra serve                     # + FastAPI / uvicorn for the served map
uv run geoextract extract --states bremen # OSM + IED + Abwärme + Overture + MaStR → merged table
uv run geoextract extract --states all    # Germany (~2.5 min merge once the sources are cached)
uv run geoextract export --scope DE       # map parquet (EPSG:3857) + summary JSON
uv run geoextract register build          # bulk register files → data/geoextract/register/
uv run geoextract web hygiene --scope DE  # website normalisation (also runs inside extract)
uv run pytest tests/ -q
```

Outputs: `data/geoextract/companies_merged_{scope}_4326.parquet` (GeoParquet, the exchange
format), `_3857.parquet` (map file with `x`/`y`), `_summary.json`. Preview any parquet as a
static map: `uv run --extra viz python scripts/preview_layer.py <parquet>`.

Environment: `GEOEXTRACT_DATA_DIR` (data root, default `./data`), `LLM_BASE_URL` /
`LLM_MODEL` / `LLM_API_KEY` (Part C), `NOMINATIM_URL`. `data/` is never committed.

## Serving the map + API

The served version of a scope lives in `data/serve/<scope>/<version>/` (flat + full parquet,
`sites.parquet`, extracts, `companies.pmtiles`, `search.parquet`, `props.parquet`, `ui.json`,
`manifest.json`) with `data/serve/<scope>/current` pointing at the version to serve.

Local, no docker (needs [tippecanoe](https://github.com/felt/tippecanoe) for the tiles):

```bash
uv run geoextract serve build --scope bremen          # ~15 s for Bremen, tiles included
uv run geoextract serve api --scope bremen --port 8791
# → http://127.0.0.1:8791/  (map)   http://127.0.0.1:8791/docs  (API)
```

With docker (identical on a laptop, a VPS or an institute server; Caddy serves the static
data with Range requests and gets TLS automatically for a hostname):

```bash
docker compose build                                  # serve-only image (≈ 220 MB of pulls)
uv run geoextract serve build --scope bremen          # tiles on the host (tippecanoe installed)
SCOPE=bremen docker compose up -d                     # http://localhost/
# server without tippecanoe on the host: build the "full" image and let the builder make tiles
IMAGE_TARGET=full docker compose build                # + tippecanoe compiled in (≈ 450 MB)
SCOPE=DE docker compose --profile build run --rm builder
SITE_ADDRESS=companies.example.org SCOPE=DE docker compose up -d   # production, HTTPS
```

Ship a new data version to a server without moving tiles (they are built there):

```bash
scripts/serve_push.sh user@host:/srv/geo-company DE   # rsync inputs (~0.7 GB) + remote build
```

## Licences and attribution

The merged table is a **derivative database of OpenStreetMap** (ODbL): attribution is
required and the table is offered under ODbL. Overture Maps: CDLA-Permissive-2.0. thru.de,
BfEE, BNetzA, Destatis: DL-DE-BY-2.0. OffeneRegister.de / OpenCorporates: CC-BY 4.0. GLEIF:
CC0. The full attribution string is `config.ATTRIBUTION` and is embedded in every
`manifest.json`, on the map page and in the API description. Scraping for classification
respects robots.txt, 1 request/s per domain and business contact data only.
