# geo-company

Extract German companies (name, website, address, **company grounds surface area**) from open geodata, and classify them into NACE Rev. 2 sectors. See `geo-company-extractor-plan.md` and `company-classification-pipeline-plan.md` for the full design.

## Status: geoextract MVP (OSM-only, Hamburg)

A working end-to-end slice of the extractor: **download → extract OSM businesses → grounds area → export**. Pyrosm on a Geofabrik PBF; no multi-source dedup or classification yet (that's the next phase per the plan).

```bash
uv sync
uv run geoextract run                         # defaults to config/default.yaml (Hamburg)
uv run geoextract run --region germany/bremen # any pyrosm region
uv run geoextract run --skip-download          # reuse an existing PBF in ./data
```

Outputs (in `./output/`, filenames `{scope}_companies_{YYYYMMDD}.*`):
- `.csv` — flat table, lat/lng columns, no geometry
- `.geojson` / `.gpkg` — full geometry (EPSG:4326)
- `_3857.parquet` — Web-Mercator point cache with `x`/`y` columns, in the `app_bokeh_companies.py` convention (drop-in map layer; hover fields `name`/`business_type` map to the app's Name/Type)
- `_summary.json` + `ATTRIBUTION.txt`

### Hamburg result (reference)
~13,850 businesses; website coverage ~28%; districts resolved to Bezirke (admin_level 9); grounds area on ~5,850 of them.

### Known caveat — grounds-area over-attribution
Grounds area is the company's **own polygon** when it's mapped as one (median ~500 m² — often a building outline), else the **containing landuse zone** (median ~31,000 m²). A point-mapped shop sitting inside a large commercial/industrial zone therefore inherits that whole zone's area (a few hundred records exceed 100,000 m²). This is expected for the MVP; refining it (e.g. cap by nearest building, or only attribute zones to industrial features) is a later-phase improvement.

## Data licensing
OSM data © OpenStreetMap contributors, ODbL — attribution is required in outputs (written to `output/ATTRIBUTION.txt`).
