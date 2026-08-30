# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

One installable Python package, **`geoextract`**, being built to spec from
**`GEOEXTRACT_SPEC.md` (v1.1, 2026-08-25)** — the single authoritative plan. Read it before
implementing anything; it contains the phase order (A0–A5, B1–B6, C1–C6, E1), the data
contract, source catalogue, gotchas, and a status block that must be updated on every phase
completion. The older `geo-company-extractor-plan.md` and
`company-classification-pipeline-plan.md` are **superseded** by the spec — consult them only
for background, never as instructions.

**The one and only goal (bottom-up):** gather as much information as possible about existing
companies at specific geolocations in Germany. Core fields: company name, address + exact
geolocation, website, company grounds surface (`grounds_area_m2`), NACE Rev. 2
classification. Anything further (legal form, HR number, financials) is an opportunistic
plus. **Out of scope:** size/financial enrichment, top-down allocation of regional
statistics, and the EnProGen export — that is the separate EnProGen project (top-down); the
two are independent for now and will be combined later. Do not re-add Part-D-style stages.

Pipeline: `geoextract extract` (OSM + register/POI adapters → canonical Company table via
entity resolution) → `geoextract classify` (websites/tags/registers → NACE codes written
back) → `geoextract export` (map parquet EPSG:3857 + summary JSON). Primary consumer: the
ensynergies Bokeh map, which reads `data/geoextract/companies_merged_*_3857.parquet`.

## Current code state (important — partially stale vs. spec)

The repo is **not** greenfield. It contains an earlier **OSM-only MVP** plus ensynergies
consumer files that predate the spec:

- `src/geoextract/` — pyrosm-based MVP (`models.py`, `config.py` + `config/default.yaml`,
  `download.py`, `extract_osm.py`, `area.py`, `export.py`, `cli.py`). It works but diverges
  from the spec; refactor it phase by phase rather than trusting it as the pattern:
  - **pyrosm → QuackOSM** everywhere (spec §4/A1; never Overpass for bulk). Modules must
    accept GeoDataFrames, not a live `pyrosm.OSM` object.
  - **`models.py` (per-row Pydantic) → `schema.py`** with `COMPANY_COLUMNS` +
    `conform()`/`validate_frame()` — frame-level validation (spec §3.4 forbids per-row).
  - **YAML config → plain-Python `config.py` constants** (no YAML).
  - **Metric CRS is EPSG:25832**, not the MVP's 32632. Export naming/layout follows spec §1
    (`data/geoextract/companies_merged_{scope}_{4326|3857}.parquet`), not
    `output/{scope}_companies_{date}.*`. OSM ids are `osm_way/123`, not `osm_w123`.
  - Best-preserved pieces: `area.py` logic (own polygon → smallest containing landuse zone,
    `representative_point()`), the `_3857` + `x`/`y` export pattern, tag-mapping heuristics.
- `scripts/` — ensynergies data-cache builders (map-consumer side, not part of the package).
  `build_ied_cache.py` / `build_abwarme_cache.py` are good seeds for the B1/B2 adapters.
  `build_companies_cache.py` is Overpass-based and broken here (imports a missing
  `src.overpass`) — retired, replaced by the geoextract pipeline.
- `app_bokeh_companies.py` — the map consumer (reference only; lives in ensynergies).
- `main.py` — leftover uv stub, delete when convenient.

## Environment & commands

Uses `uv` with Python 3.13 (pinned in `.python-version`); spec requires ≥ 3.12.

```bash
uv sync                      # install dependencies from pyproject.toml
uv add <package>             # add a dependency (updates pyproject.toml + lockfile)
uv run geoextract --help     # the CLI (entry point geoextract = geoextract.cli:main)
uv run <cmd>                 # run any command inside the project venv

uv run pytest tests/ -v                        # run tests (once tests exist)
uv run pytest tests/test_x.py::test_name -v    # run a single test
```

Env vars (never commit values; see spec §9): `GEOEXTRACT_DATA_DIR`, `LLM_BASE_URL` /
`LLM_MODEL` / `LLM_API_KEY` (OpenAI-compatible; Blablador first), `NOMINATIM_URL`.

## Non-negotiable working rules (from the spec)

- The **data contract (spec §3) is the interface** — additive changes only, never rename
  contract columns. Extra debug columns go after the contract columns.
- Every stage is **idempotent, cached, resumable**; every derived value carries provenance
  (`*_source` / `*_method`); estimates are labeled as estimates.
- **Validate on Bremen first**, then scale to all 16 states.
- **Verify visually after every stage**: `scripts/preview_layer.py <parquet>` renders a
  static HTML map into `data/previews/` (build it early; no Bokeh app needed).
- CRS regime (spec §2): store/exchange in EPSG:4326, compute areas/distances in EPSG:25832,
  export the map file in EPSG:3857 with scalar `x`/`y`. Sanity bounds: lat 45–56, lon 4–17;
  grounds area in (0, 5·10⁷ m²] — fail loudly outside.
- One git commit per completed phase (`phase(A1): osm extraction (bremen validated)`) and
  update the spec's status block in the same commit.
- Read spec **§10 Gotchas** before touching OSM/pandas/geocoding code (QuackOSM returns
  OGC:CRS84; pandas ≥ 3 string dtype; `representative_point()` not centroid; …).
- `data/` and `output/` are gitignored — never commit data.

## Data licensing

Outputs carry attribution obligations: OpenStreetMap is ODbL (**attribution required; the
merged table is an ODbL derivative database**), Overture CDLA-P-2.0, Foursquare OS Places
Apache-2.0, German agency data (BfEE/BNetzA/Destatis/thru.de) DL-DE-BY-2.0.
Handelsregister ≤ 60 req/h; public Nominatim 1 req/s with User-Agent + caching. Scraping for
classification: respect robots.txt, 1 req/s per domain, max home + 5 internal pages,
business emails only (GDPR).

## Golden working rules (user-imposed — NEVER violate)

1. **Never run `git commit`** — the user makes every commit; Claude may stage and prepare
   messages (`.commit-messages/`) only.
2. **Never implement anything without the user's explicit go** — plan/propose first.
3. **Never start any download on your own initiative** — retries/resumes/recovery included.
   The connection is METERED (~25 GB/month); state the expected volume and ask first.
   Beware library auto-downloads: open-mastr re-downloads AND DELETES its cache dir
   (`~/.open-MaStR/data/xml_download/`) when its date-derived zip name is missing — always
   pin `date=` and keep a `.bak` of the export outside that dir.
4. Spell out every abbreviation on first use.

## Status snapshot (2026-08-29 — details in Claude's project memory)

- A0–A5 + B1 (IED) + B2 (Abwärme/PfA) + B3 (Overture) done: DE table 4 083 494 companies,
  4 sources, 320 726 multi-source clusters. B4 (Foursquare) deferred as redundant.
- **B5 (MaStR): code complete + tested; DATA MISSING** — one verified 3.16 GB fetch of
  `Gesamtdatenexport_20260827` pending user go. Fetch: `data/raw/mastr/segfetch3.sh`
  (proven; only-206 + CRC-verified). Parse: `data/raw/mastr/download_probe.py`
  (date-pinned). Open question: `business_type` for consumer-only sites.
- Git: repo intentionally at ZERO commits (user is rewriting the baseline message);
  agreed commit split lives in Claude's memory (`git-recommit-plan`).
- Canonical preview: `data/previews/companies_merged_DE_industrial.html` — full data,
  never sampled, currently `--states hamburg,schleswig-holstein`; per-dataset toggle rows.
