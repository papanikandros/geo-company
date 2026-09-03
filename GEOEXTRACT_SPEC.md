# geoextract — Company Extraction & NACE Classification for Germany

**Spec version:** 1.1 (2026-08-25)
**Purpose of this document:** a single, self-contained specification from which the whole
pipeline can be (re)built *from scratch* on any machine as a standalone Python package, and
later imported back into the `ensynergies` map project (DLR GitLab
`meno_rm/ensynergies`) without touching its internals. Everything a fresh engineer or an AI
coding agent needs is in here: goal, phases, data contract, sources, gotchas, environment,
conventions. Nothing in this file depends on having access to the ensynergies repo.

> **Status block — update this on every phase completion (it is the handoff record).**
>
> | Phase | Status | Date | Artifact / note |
> |---|---|---|---|
> | A0 Scaffolding | ☑ | 2026-08-25 | package `geoextract` (argparse CLI), `schema.py` contract + `conform`/`validate_frame`, plain-Python `config.py`, `paths.py`; 7 tests green |
> | A1 OSM extraction (one state) | ☑ | 2026-08-25 | Bremen: 11 358 businesses, 37 % with website, 14 s (DoD: ≥9 000 / ≥35 % / <5 min) |
> | A2 Boundaries + area | ☑ | 2026-08-25 | Bremen: district_ags 99.5 % (04011/04012), industrial median area 14 500 m²; own_polygon 2 257 / landuse_zone 4 962 rows |
> | A3 Entity resolution | ☑ | 2026-08-25 | Bremen OSM: 11 358 → 11 247 (99 clusters, all same-site node/way duplicates, largest checked by hand); 8 synthetic-duplicate tests; cross-source spot-check re-runs after B1/B2 |
> | A4 Export + CLI | ☑ | 2026-08-25 | `geoextract extract --states bremen` end-to-end → `companies_merged_bremen_{4326,3857}.parquet` + summary; preview HTML verified; §6.5 intrinsic is_industrial (1 253 rows) |
> | A5 OSM all 16 states | ☑ | 2026-08-25 | DE: 1 874 840 raw → 1 774 691 companies (83 375 duplicate ids from overlapping Geofabrik extracts dropped, e.g. Berlin ⊂ Brandenburg PBF); state coverage 99.3 % (AGS-prefix fallback for broken BB/ST level-4 relations); 2 offshore OOB features dropped loudly; extraction ≤ 46 s/state, merge ~13 min; sampled preview (100 k) verified |
> | B1 IED adapter | ☑ | 2026-08-25 | THRU.de 2026-04 workbook, Berichtsjahr 2024: 12 816 functional IED installations → 10 302 entities after A3 (2 514 same-site collapses); 857 ied+osm clusters DE-wide (8 %), Bremen clusters hand-verified, 0 false merges; resolver fixed en route (footprint blocking + containment threshold regardless of distance); `--sources` defaults to `osm,ied` |
> | B2 Abwärme adapter | ☑ | 2026-08-25 | BfEE v28: 23 936 potentials → 6 178 sites (3 659 companies) → geocoded via cached Nominatim §4.1 (72 % house / 20 % street / 341 coarse / 192 dropped) → 5 986 sites; DE merge: 979 of 5 932 abwaerme entities matched (17 %), 150 three-source clusters (abwaerme+ied+osm), coarse rows never merge + confidence ≤ 0.3 ✓; 201 TWh/a waste heat geolocated; Bremen clusters hand-verified incl. first triple (Mercedes-Benz) |
> | B3 Overture adapter | ☑ | 2026-08-27 | release 2026-08-19.0 via DuckDB/S3: 2 630 706 named DE places (confidence ≥ 0.5) → DE merge 4 524 348 raw → 4 083 494 companies in ~43 min; 319 906 overture clusters (12 % match), 100 four-source (first: Mercedes-Benz Werk Bremen); name 61→83 %, website 26→65 %; Bremen hand-verified (2 806 clusters, 0 false merges); `--sources` defaults to `osm,ied,abwaerme,overture`; preview OVT row (provenance filters) verified |
> | B4 FSQ OS Places adapter | ⊘ deferred | 2026-08-27 | likely redundant: FSQ is an Overture conflation input (25 % of our overture rows carry FSQ provenance; same category taxonomy, so no industrial gain — check-in data is thinnest exactly there); public S3 parquet withdrawn (bucket holds only LICENSE — access now gated: Places Portal Iceberg + token, or gated HF). Revisit only with a portal token + Bremen diff probe |
> | B5 MaStR adapter | ☑ | 2026-09-03 | Gesamtdatenexport_20260827 (3.16 GB, CRC-verified segmented fetch; parse 56 min → 11 GB sqlite) → 134 526 legal-entity units ≥ floors → 102 382 sites (59 186 operators); kW floor ≥ 50 kW all techs (PV/Speicher ≥ 100 kW = MaStR coordinate-publication thresholds); acceptance checks (port of verify-marktstammdaten) replicate Kotthoff/Tepe 2023: 3.07 % onshore wind outside declared Landkreis, coords wind 96.8 %/PV 4.1 %/storage 0.2 %; 848 implausible unit coords → address geocode (provenance mastr_coord_method), 292 street-less sites PLZ+town-only (never dedup anchors); business_type via operator WZ section (81 % coverage, ArcelorMittal/BASF/Mercedes → industrial); DE merge 4 626 730 raw → **4 164 518 companies**, 327 695 multi-source clusters, 9 914 mastr rows merged (10.9 %), **60 five-source clusters** (first: Dold Holzwerke, Schwarzwaldmilch, Badische Stahlwerke); Bremen 384 sites hand-verified; grid layer 263 863 connection points; `--sources` default += mastr |
> | B6 Handelsregister (optional) | ☐ | | |
> | C1 NACE reference data | ☐ | | |
> | C2 Website scraper | ☐ | | |
> | C3 Traditional classifier | ☐ | | |
> | C4 AI classifier | ☐ | | |
> | C5 Intrinsic/register classification | ☐ | | |
> | C6 Writeback | ☐ | | |
> | E1 Tests / README / CI | ☐ | | |

---

## 0. Goal and scope

Build **one installable Python package, `geoextract`**, with one CLI and three subcommands
that run in sequence on a shared table:

```
geoextract extract   # Part A+B: companies from geodata + registers → canonical Company table
geoextract classify  # Part C:   websites/tags/registers → NACE Rev. 2 codes written back
geoextract export    # map parquet (EPSG:3857) + summary JSON
```

**Primary consumer:** a Bokeh/Panel map (`app_bokeh_companies.py` in ensynergies) that
reads **one parquet file** and filters companies by NACE section / 4-digit code /
`is_industrial`.

**The one and only goal** is to gather as much information as possible about existing
companies at specific geolocations — **bottom-up**. Core fields, in priority order: company
name, address + exact geolocation, website, company grounds surface, NACE classification.
Everything beyond that (legal form, register number, financials) is a *plus* when a source
offers it cheaply, never a goal driving its own pipeline stage. The top-down approach
(regional statistics, energy profiles) lives in the separate **EnProGen** project; the two
projects are independent for now and will be combined later.

**In scope**
- All business-like entities in Germany from OSM (capture-all, then classify), plus
  register sources (IED, Abwärme/BfEE, MaStR), plus POI aggregators (Overture, Foursquare OS
  Places), optionally Handelsregister.
- Cross-source **entity resolution** (the same plant appears in OSM *and* IED *and* Abwärme).
- **Company grounds area** (own site polygon or containing landuse zone) — a size proxy.
- **NACE Rev. 2 / WZ 2008** classification at section and 4-digit level, with confidence.
- Bonus company facts where a source yields them for free (legal form, HR number, waste-heat
  quantities as debug columns) — opportunistic, no dedicated enrichment stage.

**Out of scope (for now)**
- Building footprints, floor area, building levels.
- Size/financial enrichment and top-down allocation of regional statistics (register
  financials, EU ETS/PRTR proxies, Destatis GENESIS, EnProGen export) — that is the
  top-down territory of the separate EnProGen project; re-add additively if/when the
  projects are combined.
- Real-time / incremental updates — this is a batch pipeline producing dated snapshots.
- Any UI — the package only produces files.

**Non-negotiable principles**
1. **The data contract (§3) is the interface.** The map only ever reads the
   contract columns. Internals may change freely; the contract only grows (additive).
2. **Every stage is idempotent, cached and resumable.** Re-running skips finished work.
3. **Every derived value carries provenance** (`*_source` / `*_method` columns).
4. **Estimates are labeled as estimates** and are overridden by measured/bottom-up data.
5. **Validate on one small Bundesland (Bremen) before running nationally.**
6. **Verify visually.** Every new layer/field is checked on a map render (a static HTML
   preview is enough when the Bokeh app is not available — see §8).

---

## 1. Architecture

```
geoextract/                          # standalone repo
├── pyproject.toml                   # package "geoextract", CLI entry point "geoextract"
├── README.md
├── GEOEXTRACT_SPEC.md               # this file (keep a copy in both repos)
├── src/geoextract/
│   ├── __init__.py
│   ├── cli.py                       # argparse/typer: extract | classify | export | run
│   ├── config.py                    # plain-Python constants (no YAML): CRS, URLs, knobs
│   ├── schema.py                    # COMPANY_COLUMNS contract + conform()/validate_frame()
│   ├── paths.py                     # data dir layout, cache file naming
│   ├── download.py                  # Geofabrik PBF etc., resumable, skip-if-exists
│   ├── boundaries.py                # admin_level 4/6 polygons → state/district/district_ags
│   ├── sources/                     # one adapter per source, each → canonical frame
│   │   ├── osm.py                   # QuackOSM over PBF
│   │   ├── ied.py                   # THRU.de / EEA Industrial Reporting xlsx
│   │   ├── abwaerme.py              # BfEE Plattform für Abwärme xlsx (+ geocoding)
│   │   ├── mastr.py                 # Marktstammdatenregister via open-MaStR
│   │   ├── overture.py              # Overture Maps places (parquet on S3)
│   │   ├── fsq.py                   # Foursquare OS Places (parquet on S3)
│   │   └── handelsregister.py       # optional, slow, scraping
│   ├── area.py                      # grounds_area_m2 (own polygon | landuse zone)
│   ├── resolve.py                   # entity resolution (blocking + fuzzy name + union-find)
│   ├── nace/
│   │   ├── loader.py                # NACE Rev.2 / WZ 2008 hierarchy (bundled JSON)
│   │   └── data/nace_rev2.json
│   ├── scraper/
│   │   ├── base.py
│   │   ├── traditional.py           # requests + BeautifulSoup + extruct (bulk default)
│   │   └── browser.py               # crawl4ai for JS-heavy minority
│   ├── classifier/
│   │   ├── base.py
│   │   ├── intrinsic.py             # register WZ codes + OSM tag → NACE rules
│   │   ├── traditional.py           # TF-IDF char n-gram beam baseline
│   │   ├── ai.py                    # embeddings retrieval + LLM re-rank (primary)
│   │   └── writeback.py             # join predictions back onto the Company table by id
│   └── export.py                    # map parquet, summary JSON
├── tests/                           # pytest; one test module per stage; fixtures = Bremen slice
└── scripts/preview_layer.py         # parquet → static HTML map (verification without the app)
```

**Data directory** (configurable, default `./data`, never committed):

```
data/
├── raw/                 # downloads as received (PBF, xlsx, csv, parquet) — cache, never edited
├── geoextract/
│   ├── src_osm/src_osm_{state}_4326.parquet          # per-source, per-state canonical frames
│   ├── src_ied/src_ied_DE_4326.parquet
│   ├── src_abwaerme/..., src_mastr/..., src_overture/..., src_fsq/..., src_hr/...
│   ├── boundaries_DE_4326.parquet                    # admin 4+6 polygons with AGS
│   ├── companies_merged_{scope}_4326.parquet         # after entity resolution (full geometry)
│   ├── companies_merged_{scope}_3857.parquet         # MAP CONTRACT (point geometry + x/y)
│   ├── companies_merged_{scope}_summary.json
│   └── classify/                                      # scraped text cache, predictions, LLM cache
└── previews/*.html
```

`{scope}` is `DE` or a state alias (`bremen`, `HH`, …). The map consumer looks for
`companies_merged_DE_3857.parquet` first and otherwise the newest `companies_merged_*_3857.parquet`.

---

## 2. CRS and geometry conventions

| Use | CRS | Why |
|---|---|---|
| Internal storage, all `*_4326` files, `latitude`/`longitude` | **EPSG:4326** | source-neutral |
| Area / distance math | **EPSG:25832** (ETRS89 / UTM 32N) | metric, official for Germany |
| Map export `*_3857` + `x`/`y` columns | **EPSG:3857** | Web-Mercator tiles in Bokeh |

- Point representation of polygons = `representative_point()` (guaranteed inside), never centroid.
- The `*_3857` map file contains **point geometry only** plus scalar `x`, `y` columns (the map
  cannot consume shapely geometries; it reads `x`/`y`).
- Germany sanity bounds for validation: lat 45–56, lon 4–17. Grounds area sane range
  (0, 5·10⁷ m²]. A mis-set CRS produces areas of fractions of a m² or billions — fail loudly.

---

## 3. Data contract — the canonical `Company` table

One row = one **site** (physical location), not one legal entity. Column order as listed.
`str` means pandas string dtype (pandas ≥ 3 default), nullable unless marked required.

### 3.1 Identity, location, contact (Part A/B fills these)

| column | dtype | req | description |
|---|---|---|---|
| `id` | str | ✔ | stable id `{source}_{key}`, e.g. `osm_way/123`, `ied_4711`, `abw_12`. After a merge the winning source's id is kept. |
| `name` | str | | business name |
| `business_type` | str | | primary OSM-style category: `office / shop / craft / industrial / amenity / man_made / power` |
| `business_subtype` | str | | value of that key, e.g. `office=company → company` |
| `address_street`, `address_housenumber`, `address_postcode`, `address_city` | str | | split address |
| `address_full` | str | | `"{street} {nr}, {plz} {city}"` |
| `state` | str | | Bundesland official name (spatial join, admin_level 4) |
| `district` | str | | Landkreis / kreisfreie Stadt (admin_level 6) |
| `district_ags` | str | | 5-digit Kreis AGS from `de:regionalschluessel[:5]` or `de:amtlicher_gemeindeschluessel` — regional join key (also the seam for a future EnProGen combination) |
| `website`, `phone`, `email` | str | | contact; `website` is the **classifier seam** |
| `latitude`, `longitude` | float64 | | WGS84 representative point |
| `grounds_area_m2` | float64 | | company grounds surface (m², EPSG:25832) |
| `grounds_area_source` | str | | `own_polygon` \| `landuse_zone` \| null |
| `legal_form` | str | | `GmbH`, `AG`, `GmbH & Co. KG`, `e.K.`, … |
| `hr_registration`, `hr_court` | str | | Handelsregister number / court |
| `source` | str | ✔ | contributing sources joined with `+`, e.g. `osm+ied` |
| `source_count` | int64 | ✔ | distinct sources in the dedup cluster |
| `confidence_score` | float64 | ✔ | 0–1, weights in §5.4 |
| `merged_at` | str | | ISO date of the merge snapshot; classifier runs pin to it |

### 3.2 Classification (Part C fills these)

| column | dtype | description |
|---|---|---|
| `nace_codes` | str | pipe-joined 4-digit NACE Rev. 2 codes, best first, e.g. `28.29\|33.12` (1–3 codes) |
| `nace_primary` | str | first/highest-confidence code |
| `nace_section` | str | section letter of `nace_primary`, `A`–`U` |
| `nace_confidence` | float64 | confidence of the primary code, 0–1 |
| `nace_method` | str | `register_wz` \| `intrinsic_source` \| `osm_tag_rule` \| `ai` \| `traditional` \| null |
| `nace_reasoning` | str | short evidence string from the classifier (optional, may be truncated to 300 chars) |
| `wz_code` | str | German WZ 2008 code **as supplied by a register** (IED/MaStR/HR), untouched |
| `is_industrial` | bool (req) | `nace_section ∈ {B,C,D,E,F}` **or** intrinsic industrial signal (§6.5). Default `False`. |

### 3.3 Map export extras (`*_3857` only)

`geometry` (Point, EPSG:3857), `x`, `y` (float64). Everything in §3.1–3.2 is carried through.

### 3.4 Schema implementation rules

- `schema.COMPANY_COLUMNS: dict[name → (dtype, nullable, description)]` is the single
  source of truth; `conform(df)` adds missing columns with defaults (`source_count=1`,
  `confidence_score=0.0`, `is_industrial=False`) and orders columns; `validate_frame(df,
  source)` raises with a readable message (missing columns, null/duplicate ids, coordinates
  outside Germany, insane areas).
- Validation is **frame-level**, not per-row Pydantic — at ~850 k rows per-row models are too slow.
- Extra source-specific debug columns are allowed **after** the contract columns.
- The former size/production columns (`size_class_*`, `employees`, `turnover_eur`,
  `energy_est_gj`, …) were removed in v1.1; if the EnProGen combination happens they return
  **additively** — never reuse their names for anything else.

---

## 4. Sources catalogue

| # | Source | What it gives | Access | Licence | Adapter notes |
|---|---|---|---|---|---|
| 1 | **OpenStreetMap** via Geofabrik per-state PBFs `https://download.geofabrik.de/europe/germany/{state}-latest.osm.pbf` | all business-like features, names, websites, addresses, site polygons, landuse zones, admin boundaries | bulk download (Bremen ~50 MB … Bayern ~1 GB; total ~4 GB) | ODbL | read with **QuackOSM**; never Overpass for bulk |
| 2 | **IED installations** — THRU.de `Anlagenliste_EU-Registry_gemaess_IE_RL_ab_2017.xlsx` (`https://thru.de/...`), EEA Industrial Reporting | ~9 k installations, operator, activity (Annex I), coordinates, status | xlsx download | DL-DE-BY-2.0 | filter latest `Berichtsjahr`; coordinates are given (WGS84) |
| 3 | **Abwärme platform** — BfEE `pfa_datentabelle_excel.xlsx` (`https://www.bfee-online.de/SharedDocs/Downloads/BfEE/DE/Effizienzpolitik/pfa_datentabelle_excel.xlsx`) | firms with waste heat, `Warmemenge_pro_Jahr`, temperature, address | xlsx | DL-DE-BY-2.0 | **no coordinates** → geocode (§4.1) |
| 4 | **MaStR** (Bundesnetzagentur) `https://www.marktstammdatenregister.de` | every generation unit; operator, technology, capacity, coordinates | `open-MaStR` package (bulk XML or SOAP) | DL-DE-BY-2.0 | filter out rooftop PV < 100 kW; keep plants/CHP/industrial generators |
| 5 | **Overture Maps** places theme | ~POI with names, categories, websites, confidence | parquet on S3, pin release (e.g. `2026-05-21.0`), read with DuckDB `httpfs` + bbox filter | CDLA-P-2.0 | category taxonomy → `business_type` map |
| 6 | **Foursquare OS Places** | POI with categories, websites | parquet on S3, DuckDB | Apache-2.0 | many closed venues — filter `date_closed` |
| 7 | **Handelsregister** (`handelsregister.de`; `bundesAPI/handelsregister` approach; bulk snapshot: OffeneRegister.de) | legal name, legal form, register number/court, seat, purpose | scraping, ≤ 60 req/h; or 2019 bulk dump | register data, ToS-restricted | **no coordinates, no size fields**; optional (`--include-hr`) |
| 8 | **NACE Rev. 2 / WZ 2008 reference** — Eurostat RAMON (`https://ec.europa.eu/eurostat/web/metadata/classifications`) and Destatis WZ 2008 with explanatory notes | 21 sections, 88 divisions, 272 groups, 615 classes + German 5-digit WZ subclasses | xlsx/csv | open | bundle as `nace_rev2.json`; WZ 2008 = NACE Rev. 2 at 4 digits |
| 9 | **Admin boundaries** | admin_level 4 (Bundesland) & 6 (Kreis) relations from the same PBFs, with `de:regionalschluessel` | from #1 | ODbL | alternative: BKG VG250 (DL-DE-BY-2.0) if OSM AGS is missing |

(Removed in v1.1, kept for the future EnProGen combination: Unternehmensregister/
Bundesanzeiger financials, EU ETS — EUTL, E-PRTR, Destatis GENESIS.)

### 4.1 Geocoding (address-only sources: Abwärme, HR)

- Small scopes: public **Nominatim** (≈1 req/s, cache every result on disk keyed by normalized
  address; set a descriptive `User-Agent`).
- Country scale: **self-hosted Nominatim or Photon** on the same PBFs, or **BKG Hauskoordinaten**
  (licensed). Never hammer the public endpoint with > 10 k addresses.
- Always store `geocode_source` and `geocode_precision` (`house` / `street` / `postcode` /
  `city`) in the per-source debug columns; rows geocoded to city level must not be used as
  dedup anchors.

---

## 5. Part A — Extraction core

### A0 Scaffolding
- `pyproject.toml` (`requires-python >= 3.12`, build via hatchling/setuptools, CLI script
  `geoextract = geoextract.cli:main`), `uv` lockfile, `ruff`, `pytest`.
- Dependencies (core): `geopandas>=1.1`, `shapely>=2.1`, `pyproj`, `pandas>=3.0`, `numpy>=2`,
  `pyarrow`, `duckdb`, `quackosm>=0.18`, `rapidfuzz`, `requests`, `tqdm`, `openpyxl`.
  Extras: `classify` (`beautifulsoup4`, `lxml`, `extruct`, `phonenumbers`, `scikit-learn`,
  `joblib`, `openai`), `ai` (`sentence-transformers`, `faiss-cpu`), `crawl` (`crawl4ai`),
  `mastr` (`open-mastr`).
- `config.py` holds: CRS constants, the 16 Geofabrik URLs + alias map (`HB→Bremen`,
  `NRW→Nordrhein-Westfalen`, …), OSM filters, dedup knobs, source priority, confidence
  weights, industrial signal sets, Overture release pin, Germany bbox `(5.87, 47.27, 15.04, 55.06)`.
- **DoD:** `geoextract --help` works; `tests/test_schema.py` passes.

### A1 OSM extraction (`sources/osm.py`) — validate on Bremen first
- `download.py`: per-state PBF, resumable (`Range` header), skip if present and size matches.
- QuackOSM `convert_pbf_to_geodataframe(pbf, tags_filter=OSM_BUSINESS_FILTER, keep_all_tags=True,
  explode_tags=False, working_directory=…)`. Capture-all filter:

  ```python
  OSM_BUSINESS_FILTER = {
    "office": True, "shop": True, "craft": True, "industrial": True,
    "amenity": [ "restaurant","cafe","fast_food","bar","pub","biergarten","food_court","bank",
      "pharmacy","fuel","car_wash","car_rental","driving_school","veterinary","dentist","doctors",
      "clinic","hospital","cinema","theatre","nightclub","marketplace","post_office",
      "coworking_space","internet_cafe","ice_cream","casino","childcare","kindergarten",
      "language_school","music_school","recycling" ],
    "man_made": [ "works","kiln","chimney","gasometer","silo","storage_tank","pipeline",
      "petroleum_well","mineshaft","wastewater_plant","water_works","pumping_station" ],
    "power": ["plant","generator","substation"],
  }
  ```
- Field mapping: `business_type/subtype` = first of `office, shop, craft, industrial, amenity,
  man_made, power` whose value ∉ {`no`,`vacant`}; website = first of `website, contact:website,
  url`; phone = `phone, contact:phone`; email = `email, contact:email`; address from `addr:*`.
  `id = "osm_" + feature_id` (`way/123`, `node/…`, `relation/…`). Keep the full polygon geometry.
- Separately extract `landuse ∈ {commercial, industrial, retail}` polygons (for A2) — they are
  **not** company records.
- **DoD (Bremen):** ≥ 9 000 rows, ≥ 35 % with website, all coordinates inside Bremen, runtime < 5 min.

### A2 Boundaries + grounds area (`boundaries.py`, `area.py`)
- Admin polygons: `boundary=administrative`, `admin_level ∈ {4, 6}`; `ags =
  de:regionalschluessel[:5]` else `de:amtlicher_gemeindeschluessel[:5]`. Spatial join
  (`predicate="within"` on representative point) fills `state`, `district`, `district_ags`.
  Berlin/Hamburg/Bremen: the level-4 polygon doubles as the Kreis (Bremen has two Kreise:
  Bremen `04011`, Bremerhaven `04012`).
- Area: polygon features → own area in EPSG:25832, `grounds_area_source=own_polygon`; point
  features → area of the **smallest** containing landuse zone, `landuse_zone`; else null.
- **DoD:** median `grounds_area_m2` for `business_type=industrial` in Bremen between 2 000 and
  200 000 m²; `district_ags` non-null for ≥ 99 % of rows.

### A3 Entity resolution (`resolve.py`)
- Normalize names: lowercase, umlaut transliteration (`ä→ae` …), strip legal forms
  (`gmbh & co. kg, gmbh, ag, kg, ohg, ug, e.k., e.v., se, gbr, mbh, inc, ltd, llc`), strip
  non-alphanumerics, collapse whitespace.
- Blocking: 100 m grid cells in EPSG:25832 (compare within cell + 8 neighbours).
- Match rule: distance ≤ **50 m** **and** `rapidfuzz.fuzz.token_sort_ratio ≥ 80`; for register
  sources with polygon sites, also "point inside the other's polygon and name ≥ 60".
- Cluster with union-find; one record per cluster; field-by-field fill in
  `SOURCE_PRIORITY = ["osm","ied","abwaerme","mastr","overture","fsq","handelsregister"]`
  (first non-null wins), geometry from the highest-priority source that has a polygon, else
  highest-priority point. `source = "+".join(sorted(set))`, `source_count`, keep all member ids
  in debug column `member_ids`.
- Single source → pass-through (same code path, no clustering).
- **DoD (Bremen, OSM+IED+Abwärme):** every IED/Abwärme site that has an OSM counterpart within
  50 m collapses (spot-check 20 by hand); no cluster merges two different companies (spot-check
  the 20 largest clusters); over-/under-merge rate documented in the summary JSON.

### 5.4 Confidence score
`multi_source 0.30 · has_website 0.15 · has_address (street+nr+plz+city) 0.20 ·
has_geometry_polygon 0.15 · has_phone 0.10 · has_grounds_area 0.10` — sum of satisfied weights.

### A4 Export + CLI (`export.py`, `cli.py`)
- `write_merged()` → `*_4326.parquet` (full geometry) and `*_3857.parquet` (points + x/y).
- `write_summary()` → JSON: total, by state, by business_type, multi-source clusters, field
  coverage (name/website/phone/email/address/area/nace), area distribution (min/p25/median/
  p75/max), average confidence, attribution string, licence note ("merged table is an ODbL
  derivative database").
- CLI: `geoextract extract --states Bremen [--sources osm,ied,abwaerme] [--data-dir ./data]`;
  `geoextract run --scope DE` chains everything. `--states all` = 16 states.
- **DoD:** `geoextract extract --states Bremen` end-to-end from an empty data dir; preview HTML
  renders the points on a basemap.

### A5 OSM all 16 states
- Loop states, cache per state, merge at the end. Memory: process one state at a time; the
  national merge is ~850 k rows — fine in pandas.
- **DoD:** `companies_merged_DE_3857.parquet` exists; summary JSON per state; runtime logged.

---

## 6. Part B — Register & aggregator adapters

Each adapter is `extract_<source>(config) → GeoDataFrame` in the canonical schema (EPSG:4326),
written to `data/geoextract/src_<source>/…`, then merged by A3. Adapters must not do entity
resolution themselves.

### B1 IED (`sources/ied.py`)
Columns: `Name.Betrieb → name`, `Name.Anlage` + IED activity → debug, address split, coordinates
(`Koordinaten.Anlage_geo_lat/lon_wgs84`), `ied_activity` (Annex I code, e.g. `2.2`) kept as debug
column for the classifier context and §6.5. `business_type=industrial`. One row per
installation; sites with several installations of the same operator within 50 m collapse in A3.

### B2 Abwärme (`sources/abwaerme.py`)
`Firmenname → name`, `Ort`, street address → geocode (§4.1); `Warmemenge_pro_Jahr`,
temperature level, medium kept as debug columns (`abw_heat_mwh_a`, …). Rows geocoded only to
city level get `confidence_score` capped at 0.3 and are excluded from dedup anchoring.

### B3 Overture (`sources/overture.py`)
DuckDB: `SELECT … FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*')
WHERE bbox.xmin BETWEEN … ` per state bbox; map `categories.primary` → `business_type`
via a bundled lookup; `websites[0]`, `phones[0]`, `addresses[0]`. `id="ovt_"+id`.

### B4 Foursquare OS Places (`sources/fsq.py`) — DEFERRED 2026-08-27, likely redundant
FSQ is already an Overture conflation input (25 % of our overture rows carry FSQ
provenance) and Overture's category taxonomy is FSQ-derived, so a standalone ingest adds
mainly the rows Overture's conflation rejected (stale check-in venues) and nothing for
industrial coverage. The public S3 parquet (`s3://fsq-os-places-us-east-1/release/dt=…`)
was withdrawn — access is now gated (Places Portal Iceberg catalog + token, or gated
Hugging Face). Revisit only with a portal token and a Bremen diff probe first.

### B5 MaStR (`sources/mastr.py`)
`open-mastr` bulk download → units with `Lage`/coordinates; keep technologies
`{Verbrennung, Biomasse, Wind, Wasser, Geothermie, Solarthermie, Speicher}` and PV only if
`Bruttoleistung ≥ 100 kW`; operator name → `name`; `business_type=power`,
`business_subtype=generator|plant`; capacity kept as debug `mastr_kw`.
Amendments (2026-08-27, user-approved): **Speicher also only ≥ 100 kW** (the register now
holds 2.76 M storage units, dominated by home batteries — unfiltered they would drown the
table); **drop `(natürliche Person)` operators unconditionally** (anonymized private
persons are not companies; also removes most small PV). Aggregate units → one row per
(operator, Lokation) with summed `mastr_kw`; units in operation only.
Amendments (2026-08-31 / 2026-09-01, user-approved after the acceptance checks on the
`Gesamtdatenexport_20260827` parse — `scripts/mastr_acceptance_checks.py`, a port of
Kotthoff/Tepe et al. 2023 `verify-marktstammdaten`; results replicate the paper: 3.07 % of
onshore wind outside its declared Landkreis, coordinate completeness wind 96.8 % / PV 4.1 % /
storage 0.2 %, unit→Lokation inflation 1.36×):
- **kW floor for all generation technologies: `Bruttoleistung ≥ 50 kW`** (`MASTR_MIN_KW`;
  PV + Speicher keep ≥ 100 kW). MaStR publishes exact coordinates only from 50 kW upward
  (100 % complete ≥ 50 kW, 0 % < 30 kW); sub-threshold units are private households.
  Consumer tables (large gas/electricity consumers) stay unfiltered.
- **Coordinate plausibility, unit level:** a published coordinate outside the §2 sanity bounds
  or > 10 km outside its declared Landkreis (BKG VG5000 polygon, 0.015° buffer; 848 of
  134 354 located units) is treated as missing → the site takes the address geocode.
  Provenance `mastr_coord_method ∈ {mastr_published, address_geocode,
  address_geocode_{bbox,district}_mismatch, address_geocode_plz_town_only}` +
  `mastr_coord_dropped_units`.
- **Street-less sites (0.3 %) get a PLZ + town geocode** — explicitly NOT site-accurate:
  `address_geocode_plz_town_only`, `geocode_precision` postcode/city, never a dedup anchor,
  confidence capped (§B2 coarse rule). The geocoder skips the street query when no street is
  given so such rows can never be labelled house precision.
- **`business_type` from the operator's registered WZ section** (market_actors, 80.8 %
  coverage; keyword map `MASTR_WZ_KEYWORD_TO_BUSINESS_TYPE`: Energieversorgung → power;
  manufacturing / mining / water-waste / agriculture / logistics → industrial; Handel → shop;
  Bau → craft; hospitality / health / education / culture → amenity; other services → office),
  technology rule (`consumer-only → industrial`, else `power`) only as fallback;
  `mastr_business_type_method ∈ {wz_section, tech}`. Reason: industrial self-generators
  (ArcelorMittal Bremen 240 MW, BASF Ludwigshafen 1 GW, Salzgitter, Brauerei Beck) were
  "power". Technology stays in `business_subtype`/`mastr_techs`; NACE is still Part C's job.
- **No consolidation of same-operator Lokationen** (8 381 operators have ≥ 2 sites within
  300 m, e.g. a plant's generation + gas-consumption Lokationen): one row per (operator,
  Lokation) stays; cross-source resolution may still link them.
- Side product `grid_connections_DE_4326.parquet` (263 863 connection points located via
  Lokation → unit coordinates; 94 k at medium voltage or above) — map layer, not part of the
  Company table.

### B6 Handelsregister (optional, `--include-hr`)
Per PLZ in scope, ≤ 60 req/h, parse name / legal form / register number / court / seat →
geocode (§4.1). Provides `legal_form`, `hr_registration`, `hr_court`. Slowest and most
fragile stage; build last.

### 6.5 Intrinsic industrial signal (used by `is_industrial` before any classifier runs)
`source` contains any of `{ied, abwaerme, mastr}` **or** OSM tags match
`landuse ∈ {industrial, quarry, port, depot, railway, landfill}`,
`man_made ∈ {works, kiln, chimney, gasometer, silo, storage_tank, pipeline, petroleum_well,
mineshaft, wastewater_plant, water_works, pumping_station}`,
`industrial ∈ {factory, oil, mine, warehouse, port, scrap_yard, slaughterhouse, depot}`,
`power ∈ {plant, generator, substation}`, `craft ∈ {metal_construction, electronics, joinery}`.

---

## 7. Part C — NACE classification

### C1 NACE reference data (`nace/`)
- `scripts/build_nace_data.py` downloads Eurostat NACE Rev. 2 (and Destatis WZ 2008 with
  German labels + explanatory notes) → `nace_rev2.json`:
  `{"sections": {"C": {"label": …, "label_de": …}}, "divisions": {…}, "groups": {…},
  "codes": {"28.29": {"label", "label_de", "section", "division", "group", "description",
  "includes", "excludes"}}}`. Bundle the JSON in the package; fallback to the bundled copy if
  the download fails.
- Loader API: `get_code`, `get_all_codes`, `children(node)`, `get_section_codes`,
  `validate_code`, `search_descriptions`.
- Hierarchy: Section (21) → Division (88) → Group (272) → Class (615).
  Industrial = sections **B, C, D, E, F**.

### C2 Website scraper (`scraper/`)
- **Bulk default:** `requests` + BeautifulSoup + `extruct` (Schema.org/JSON-LD:
  `Organization`, `LocalBusiness`, `numberOfEmployees`, `PostalAddress`, `Product`). Fetch
  home page + up to 5 internal pages matching `/(produkte|products|leistungen|services|
  ueber-uns|about|unternehmen|kontakt|impressum)/`. Timeout 10 s, 2 retries, respect
  `robots.txt`, rate-limit per domain (1 req/s), descriptive `User-Agent`, cache raw HTML +
  extracted text by normalized URL in `data/geoextract/classify/html/`.
- **Impressum parsing** (German legal-notice page) is the highest-yield step: company legal
  name, legal form, `HRB`/`HRA` number + court (→ `hr_registration`, `hr_court`), VAT id,
  address, sometimes employee counts.
- **Browser fallback** (`crawl4ai`) only for pages whose text extract < 300 chars.
- Output per company: `CompanyProfile {url, company_name, products[], services[],
  contact{emails, phones, addresses}, raw_text_summary (≤ 4 000 chars), impressum{…}}`, cached as
  JSON keyed by `id`.
- **DoD:** ≥ 85 % of Bremen websites yield ≥ 300 chars of text; Impressum found for ≥ 70 % of
  German `.de` sites.

### C3 Traditional classifier (`classifier/traditional.py`) — baseline / pre-filter
TF-IDF `char_wb` (2–5-grams) over NACE label+description (DE+EN) → cosine. **Beam** over the
hierarchy: top-k sections (k=5) → divisions within them (k=8) → classes (k=14) → final 1–3
with scores normalized to 0–1. Also a cheap rule layer: OSM `business_type/subtype` →
NACE lookup table (`shop=bakery → 47.24`, `craft=carpenter → 43.32`, `amenity=restaurant →
56.10`, `power=plant → 35.11`, …) bundled as `osm_tag_to_nace.csv`. Rules give
`nace_method=osm_tag_rule`, confidence 0.6.

### C4 AI classifier (`classifier/ai.py`) — primary path
- Embeddings: `sentence-transformers` multilingual model (e.g. `intfloat/multilingual-e5-base`
  or `paraphrase-multilingual-MiniLM-L12-v2`); FAISS `IndexFlatIP` over
  `"{code}: {label_de} / {label}. {description}"`; cached index on disk.
- Query text = `raw_text_summary` + **context**: OSM tags, address, IED activity, MaStR
  technology, Abwärme medium. (Cross-modal context is worth up to ~+20 pp — MONETA.)
- Retrieve **beam**: top-k sections → divisions → classes (or flat top-14 grouped by section);
  narrow beams (k≤10) underperform flat retrieval (GREEN), so use k ≥ 20 at the leaf level.
- LLM re-rank, **only from the retained candidates**, returns JSON
  `{"predictions":[{"code","label","confidence","reasoning"}]}` with 1–3 codes; validate codes
  against the loader; drop invalid; sort by confidence.
- LLM access through an **OpenAI-compatible client**: env `LLM_BASE_URL`, `LLM_MODEL`,
  `LLM_API_KEY`. First target: Helmholtz **Blablador**; later OpenRouter / any provider.
  Cache every (prompt-hash → response) on disk; bulk run is a one-shot job — quality over cost,
  but never re-pay for a cached prompt. Concurrency via a small thread pool with a
  configurable rate limit.
- **DoD:** on a hand-labelled Bremen sample of 200 companies: section accuracy ≥ 85 %,
  4-digit top-1 ≥ 60 %, top-3 ≥ 75 %. Record the evaluation in `tests/eval/`.

### C5 Intrinsic / register classification (`classifier/intrinsic.py`)
Priority order for `nace_primary` / `nace_method`:
1. `wz_code` from a register (`register_wz`, confidence 0.95);
2. IED Annex I activity → NACE lookup (bundled table, e.g. `2.2 → 24.10`, `6.1 → 17.11`,
   `3.1 → 23.51`) (`intrinsic_source`, 0.85); MaStR technology → `35.11`/`35.30`;
3. AI classifier if website text exists (`ai`);
4. OSM tag rule (`osm_tag_rule`);
5. traditional text baseline (`traditional`) when only a name/description exists.

### C6 Writeback (`classifier/writeback.py`)
Join predictions to the merged table **by `id`** (fallback: normalized website URL), pin to
`merged_at`, fill §3.2 columns, recompute `is_industrial`, rewrite both `*_4326` and `*_3857`
parquets and the summary JSON (`has_nace`, `by_section`). Never drop rows.

CLI: `geoextract classify --scope bremen [--mode ai|traditional] [--limit N] [--industrial-only]`.

---

## 8. Part E — Tests & verification

### E1 Tests, README, CI
- `pytest` with a frozen Bremen fixture (a 5 MB PBF cut with `osmium extract` committed under
  `tests/fixtures/`), per stage: schema, osm extraction, boundaries/area ranges, resolve
  (synthetic duplicates), nace loader, scraper on saved HTML, classifier beam on a labelled
  sample, writeback idempotency, export schema.
- README quickstart: install, env vars, `geoextract run --scope bremen`, where outputs land,
  how to preview.
- CI (GitLab/GitHub): lint + unit tests; no network in CI (mock downloads).

### 8.x Visual verification without the app
`scripts/preview_layer.py <parquet> [--color nace_section|is_industrial|business_type]` →
static HTML (folium or Bokeh standalone) in `data/previews/`. Check after every stage: points on
plausible places, polygons' areas plausible, industrial sites coloured correctly, no points in
the sea.

---

## 9. Environment and secrets

| variable | purpose |
|---|---|
| `GEOEXTRACT_DATA_DIR` | data root (default `./data`) |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | OpenAI-compatible LLM endpoint (Blablador first; OpenRouter later) |
| `NOMINATIM_URL` | geocoder base URL (public default; set to self-hosted at scale) |

Never commit values; provide `.env.example`. Python ≥ 3.12, `uv` for env + lock. Heavy runs
(A5, C4 nationwide) are long — design every loop to checkpoint per state / per 1 000
rows so a crash resumes.

---

## 10. Gotchas (learned the hard way — do not rediscover)

- **QuackOSM returns `OGC:CRS84`**, not EPSG:4326 → always `.to_crs("EPSG:4326")` right after loading.
- **pandas ≥ 3 string dtype:** test numeric columns with `pd.api.types.is_numeric_dtype(s)`,
  never `s.dtype == object`. Use `pd.NA`-aware ops; `fillna("")` before `.astype(str)` for text.
- QuackOSM feature ids look like `way/123` — keep the type prefix; node/way/relation ids collide.
- `representative_point()` not `centroid` (centroids of L-shaped sites fall outside).
- Overpass is fine for boundaries but **not** for bulk business extraction (rate limits,
  timeouts); mirrors rotate (`overpass.private.coffee`, `overpass-api.de`) — for bulk use PBFs.
- Nominatim public: 1 req/s, mandatory User-Agent, cache everything; Handelsregister ≤ 60 req/h.
- Geofabrik `*-latest` files change daily → record the file date (`Last-Modified`) in the
  summary JSON for reproducibility; pin Overture/FSQ releases explicitly.
- Beam width: hierarchical classification with narrow beams (k≤10) performs *worse* than flat
  retrieval; use k ≥ 20 at the leaf level and let the LLM pick from ≥ 14 candidates.
- Blablador / OpenAI-compatible endpoints: some models ignore `response_format`; always parse
  JSON defensively (extract first `{…}` block, retry once on failure).
- Map consumer reads with column pruning (`pyarrow.parquet.read_schema`) and tolerates missing
  optional columns — keep the contract additive; never rename existing columns.

---

## 11. Conventions for the implementer

- One git commit per completed phase, message `phase(A1): osm extraction (bremen validated)`.
- Update the **status block** at the top of this file in the same commit.
- Each phase ships with its test module and a one-line entry in the README status table.
- Log per-stage row counts and runtimes; write them into the summary JSON.
- Keep attribution: `"© OpenStreetMap contributors (ODbL); Overture Maps (CDLA-P-2.0);
  Foursquare OS Places (Apache-2.0); Destatis/BNetzA/BfEE (DL-DE-BY-2.0)"`. The merged table is
  an ODbL derivative database.
- Ask for no clarifications that this spec already answers; where it is silent, pick the
  simplest option that keeps the contract intact and note the decision under "Decisions" below.

### Decisions log
- 2026-08-20 — spec v1.0 created from `geo-company-extractor-plan.md`,
  `company-classification-pipeline-plan.md`, the Bremen MVP code in ensynergies
  (`src/geoextract/schema.py`, `config.py`, `resolve.py`, `area.py`, `export.py`), and the
  size/production research (Handelsregister has no size data; Unternehmensregister financials,
  ETS, GENESIS 42111/43531/52111/42131 as size/production sources).
- 2026-08-25 — spec v1.1: **Part D (size & production enrichment) and the EnProGen export
  removed.** Sole goal is bottom-up gathering of companies at specific geolocations: name,
  address + exact geolocation, website, grounds surface, NACE classification; anything
  further (legal form, HR number, financials) is opportunistic only. Top-down (regional
  statistics → energy/production) is the separate EnProGen project; the two run
  independently for now and will be combined later — removed columns/sources return
  additively then.

---

## 12. Importing back into ensynergies

Preferred: ensynergies adds the package as a dependency
(`uv add git+ssh://git@gitlab.dlr.de/<group>/geoextract.git`) and its
`app_bokeh_companies.py` keeps reading `data/geoextract/companies_merged_*_3857.parquet` —
**no code change** beyond pointing the data dir. Alternative: `git subtree add --prefix
src/geoextract <repo> main`. Data is regenerated with `geoextract run` or copied; it is never
versioned. The existing ensynergies modules `src/geoextract/*` and `src/classifier/*` are then
deleted in favour of the package.
