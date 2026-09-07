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

## Status snapshot (2026-09-04 — details in Claude's project memory)

- A0–A5 + B1 (IED) + B2 (Abwärme/PfA) + B3 (Overture) + B5 (MaStR, B5.1 refactor) done:
  DE table **4 139 625 companies** (2026-09-04, after items 4 + 5), 5 sources, `is_industrial`
  ≈ 370 k. B4 (Foursquare) deferred as redundant.
- **B5 MaStR:** data present since 2026-08-31 (`data/raw/mastr/mastr.db`, 11 GB, zip + .bak
  kept); kW floors ≥ 50 kW (PV/storage ≥ 100 kW); B5.1 (2026-09-04): per-unit
  `mastr_tech_detail`, no kW sums; `business_subtype` NULL; WZ 2025 in `mastr_wz_*` +
  `mastr_wz_code` (Destatis file `data/raw/wz2025/`). Storage kWh: the plant-level table
  (AnlagenStromSpeicher → open-mastr `storage_units`) was re-parsed 2026-09-07 from the kept
  zip (`data/raw/mastr/reparse_storage_units.py`, date-pinned, no download); the adapter
  fills `NutzbareSpeicherkapazitaet` from the plant via `SpeMastrNummer` (2 964 of 2 964
  units ≥ 100 kW). Zip backup: `data/raw/mastr/Gesamtdatenexport_20260827.zip.bak`.
- **Resolve engine vectorised 2026-09-04:** DE merge 44.6 h → ~2.5 min end to end, output
  identical (see queue item 2). Reference copies of earlier merges were deleted 2026-09-07;
  validate matcher changes by keeping a copy of the current merge before the change.
- Git: user makes every commit; Claude stages code + CLAUDE.md only (never spec/plan .md).
  Untracked-by-decision: old consumer scripts (`scripts/build_*`, `app_bokeh_companies.py`),
  plan .md files, `.commit-messages/`, `.playwright-mcp/`.
- Canonical preview: `data/previews/companies_merged_DE_industrial.html` — regenerate with
  `uv run --extra viz python scripts/preview_layer.py data/geoextract/companies_merged_DE_4326.parquet
  --states hamburg,schleswig-holstein --out data/previews/companies_merged_DE_industrial.html`
  (full data, never sampled; per-dataset toggle rows, MaStR per-technology toggles, name
  search, pinned cards, website links). ~16–40 MB: too big for SendUserFile, share via
  `python3 -m http.server` + ngrok from `data/previews/` (keep only that file there).

## Todo queue (ordered — user decisions 2026-09-03; this order is authoritative)

The "Research todos" section below holds the evidence and detail for each item; this queue
fixes the ORDER. Nothing starts without an explicit go (golden rule 2); every download
needs its own go with the volume stated first (golden rule 3).

1. **B5.1 MaStR refactor** — `mastr-refactor-plan.md` (per-unit kW one-to-one, no sums;
   `business_type` keeps its vocabulary + WZ 2025 section suffix
   `industrial (C – Verarbeitendes Gewerbe)`; `business_subtype` = WZ group code + label
   `35.1 Elektrizitätsversorgung` via `data/raw/wz2025/gliederung-wz2025.xlsx`; preview:
   singular tech toggles, per-tech hover, name search, click-to-zoom). Adapter + tests +
   Bremen check first; the DE re-run waits for item 2.
2. **Resolve-stage speedups — DONE 2026-09-04** (`resolve.py`: STRtree rectangle join for
   candidate pairs, rapidfuzz cpdist on all cores, shapely contains_xy, array union-find,
   one sorted groupby for cluster assembly). Cluster-for-cluster identical to the former
   loops: Bremen 37 349 rows and DE 4 164 518 rows, all member sets + geometries equal.
   Resolve time DE 160 560 s (44.6 h) → 88 s; Bremen 61 s → 0.7 s. A full DE re-run is now
   ~2.5 min (resolve 88 s + geography 23 s + I/O).
3. **DE re-run + canonical preview — DONE 2026-09-04** (4 164 518 companies, MaStR B5.1
   columns in, preview for Hamburg + Schleswig-Holstein regenerated). Left for the user:
   the resolve commit (`.commit-messages/resolve-speedups.txt`).
4. **Industrial-signal 3-parter — DONE 2026-09-04** (spec §6.5 + §B3 amended):
   a. `power=generator/substation` count only for NAMED rows (`INDUSTRIAL_TAGS_NAMED_ONLY`),
      `power=plant` unconditional → OSM-only power rows flagged 419 932 → 32 521.
   b. Overture slug override `OVERTURE_SUBTYPE_TYPES` (~65 → industrial, 5 → power; 74 001 DE
      rows re-typed) + debug `ovt_category` feeding the §6.5 signal; mapping re-applied on
      every cache load. `business_type=industrial` 30 418 → 94 050.
   c. Root `geographic_entities` dropped in the adapter (19 307 rows).
   DE table 4 164 518 → **4 145 466** companies; `is_industrial` 700 932 → **370 050**
   (105 627 register-backed). `man_made` structures (155 k) knowingly kept for now.
5. **Entity-resolution normalization pass — DONE 2026-09-04** (spec §A3 amended):
   token-based legal-form stripping incl. dotted spellings (46 961 `G.m.b.H.`-style names were
   missed before), title/connector noise, full+light key ensemble scoring (max), and
   `normalize_street` for B6. DE: multi-source clusters 351 823 → 355 133 (+4 058 new merges,
   336 borderline losses), table 4 141 720 companies; resolve 187 s (two scoring passes).
   Still open under item 5:
   - [x] **Splink benchmark on Bremen — DONE 2026-09-04** (`scripts/splink_benchmark.py`,
         report `data/geoextract/eval/splink_vs_heuristic_bremen.md`; splink 4.0.17 added).
         Same 40 419 rows, same 100 m blocking; EM-trained Fellegi–Sunter on Jaro–Winkler
         (full + light name key), metric distance bands, postcode. Result at p ≥ 0.9:
         4 192 pairs vs heuristic 3 818; both 3 112, Splink-only 1 080, heuristic-only 706
         (precision 0.74 / recall 0.82 vs the heuristic — the disagreements are mixed in BOTH
         directions, so neither is ground truth). Learned weights are sensible: exact name
         +12, ≤ 25 m +10.4, ≤ 100 m +8.6, postcode +4.2.
         Findings: (1) Splink catches identical-name pairs 50–100 m apart that the 50 m rule
         misses (Rossmann 81 m, Lidl 88 m, Homebox 63 m, "Gondel"/"Gondel Bremen" 6 m) —
         name-similar pairs grow 3 339 → 3 708 from 50 to 100 m; (2) Splink rejects dubious
         heuristic merges — short names at ratio 80 ("NK Beauty"/"Beauty Line",
         "GEW Bremen"/"NGG Bremen", "Budget"/"Avis Autovermietung") and polygon-containment
         merges at ratio 60–78 (Airbus/ArianeGroup 301 m, Klinikum/its pharmacy,
         Metropol/Musical Theater); (3) Splink's Jaro–Winkler is NOT token-order robust
         ("Dr. Volker Martin"/"Martin Volker Dr." missed) and it chains branch-like names
         (cambio station Kepler/Römer 134 m, Terminal 1/2). 6.6 s incl. training.
         **Three rule changes — DONE 2026-09-04 (user go):** (1) ratio ≥ 95 matches up to
         100 m; (2) containment path needs both names ≥ 2 tokens + ratio ≥ 70
         (`DEDUP_CONTAIN_RULE="ratio70"`; alternative `"token_subset"` measured: near-identical
         effect on Bremen, rejects same-suffix pairs like Metropol/Musical Theater, loses
         "KiGa girotondo" — switchable in config); (3) ratio 80–89 within 50 m needs ≥ 2
         shared tokens or ≥ 8 shared characters, ratio ≥ 90 stands alone. Bremen validated by
         hand: 246 gained pairs (all identical names 50–110 m: Rossmann, Lidl, Aral, Deutsche
         Bank …), 308 lost pairs (nearly all false: shopping-centre tenants sharing the
         centre's name, hospital ↔ its pharmacy/church, "Paros"/"Torros Döner"; known true
         losses: single-token "Homebox" inside "Homebox Germany", "Kraftwerk Hastedt").
         DE: 4 139 625 companies, 331 504 multi-source clusters; resolve 207 s.
         Splink stays an evaluation tool (`scripts/splink_benchmark.py`).
   - [ ] **One-to-one matching cluster-wide — recommend NOT enforcing** (measured 2026-09-04):
         5 956 DE clusters hold several rows of one register, almost all one operator's
         several MaStR Lokationen inside one plant polygon (AUMA Riester, Abwasserzweckverband
         Breisgauer Bucht, Schwarzwaldmilch) — legitimate under the 2026-09-01 "no
         consolidation of same-operator Lokationen, resolution may link them" decision.
         Revisit only if Splink shows chaining errors.
6. **Register + website pipeline — merged items 6 + 7 (user decision 2026-09-07), BEFORE
   sharing (item 7) and Part C.** Plan: `register-website-pipeline-plan.md` (authoritative
   for this item). Stages: 0 register build from `data/raw/handelsregister/handelsregister.db`
   (2.19 M HRB companies 2001 – 2022-08, downloaded 2026-09-07, CC-BY 4.0) + website hygiene
   (`website_kind`, directories → `website_listing`) → 1 Impressum extraction on own websites
   (legal name, form, HRB + court, VAT-ID; `website_verified`) → 2 register join (exact on
   HRB number, else name-key + PLZ + street; `register_match`, `hr_status` incl. dissolved,
   `hr_snapshot_date`, `hr_source`, `hr_objective`) → 3 URL discovery for named rows without
   a site (cluster propagation → domain guessing + DNS → Common Crawl host list → self-hosted
   SearXNG with the CBS/`SNStatComp/urlfinding` features, classifier retrained on Bremen) →
   4 two-way diff map ↔ register (register addresses geocoded OFFLINE against local OSM
   `addr:*`; `register_only_industrial` gap layer) → 5 outputs, eval, spec amendments.
   Facts behind it: DE table 63.4 % with `website` but only 30.8 % of 366 592 industrial rows;
   104 903 gelbeseiten listings; 689 247 unnamed rows; HRA-style names (GmbH & Co. KG, KG,
   e.K.) = 40 k industrial rows → the 2019 dump (260 MB) is needed for HRA/GnR/PR/VR, GLEIF
   (479 MB, CC0, 255 k DE) for fresh large-firm data; both downloads need a go. Research:
   `research/website-discovery/report.md` (F1 ceiling ≈ 0.82; municipality not PLZ in
   queries; Impressum = exact linkage; Kriesch 2024 Common-Crawl route). Network-heavy stages
   run on an unmetered machine. Bremen first at every stage; nothing starts without a go.
7. **Share the data state with colleagues (user idea 2026-09-07) — so they can run the NACE
   classification themselves.** Proposal (Claude's take, awaiting decision):
   - **Ship GeoParquet, not a Postgres dump.** The merged table is already the exchange
     format (spec §1, EPSG:4326, ~690 MB for 4.14 M rows). Anyone reads it in seconds with
     pandas/geopandas/DuckDB, no server. A Postgres/PostGIS dump would be several GB of
     text, needs a server install per colleague and buys nothing for classification work;
     keep it as an option only if someone needs concurrent multi-user writes.
   - **SQL for those who want it: one DuckDB file** (`geoextract db build` → single
     `.duckdb` with the spatial extension, ~1 GB, portable) or plain
     `SELECT … FROM 'companies_merged_DE_4326.parquet'` in DuckDB — zero setup.
   - **Distribution outside git**: `data/` stays gitignored (GitHub caps files at 100 MB).
     Publish the parquet + summary JSON + a checksum manifest as a **GitHub Release asset**
     (2 GB/file, free) or on the shared drive; add `scripts/fetch_data.py` (or `geoextract
     data pull`) that downloads and verifies them. Upload volume ≈ 0.7 GB from the metered
     connection — needs a go.
   - **Contribution path back**: colleagues do NOT regenerate the table; they deliver a
     small labels file (`nace_labels_<who>.parquet`: id, nace_codes, nace_primary,
     nace_confidence, nace_method, nace_reasoning) that C6 writeback ingests. Define that
     file contract first (it is the §3 nace_* columns keyed by `id`).
   - README quickstart (install, pull data, open in DuckDB/QGIS, label file format) and
     the licence block (ODbL derivative database — attribution required; CDLA/DL-DE notes
     from `config.ATTRIBUTION`).
8. **Part C NACE classification — refactored against the 2026-08-30 research** before
   any classify code is written: read INSEE GRAAL / codif-ape-train (adapt vs
   reimplement), Kühnemann 2020 + Beuter 2025; crawl4ai + ARGUS practices for fetching;
   tags/register hints as a first-class evidence path (54 % of firms have no findable
   website). Order inside C: C1 reference data → C5 intrinsic/register (free: MaStR WZ,
   IED activity, OSM tag rules, HR data from item 6) → C2 scraper → C3 baseline → C4 AI
   → C6 writeback.
   - **Target revision: always the LATEST — WZ 2025 / NACE Rev. 2.1** (user decision
     2026-09-03; the spec's "NACE Rev. 2 / WZ 2008" wording is superseded — amend spec §0/§3
     descriptions additively, no column renames). Older sources map forward via the
     Destatis 2008 → 2025 Umsteigeschlüssel (xlsx, ~176 KB, download needs go).
9. **E1** tests / README / CI; then validation layers (sEEnergies/Hotmaps, Overture
   overlap stats, density QA) as time allows.
10. **Later: deployment of the merged table to Cloudflare KV** (user idea 2026-09-04) — serve
   the company data from an edge key-value store so the map/API reads are ultra fast.
   Think through first: key design (per company id vs. per grid cell / district tiles),
   the 25 MB per-value limit (the DE table is ~700 MB → must be sharded), update flow after
   each re-run, and whether the Bokeh consumer needs a different reader.

## Research todos (moved from research-todos.md, 2026-08-31)

Source: research run in `research/geo-company-extraction/` (report.md has the full evidence; DOIs in
bibliography.bib). Each todo is a proposal — nothing here is implemented without explicit go
(golden rule 2). Ordered by expected value.

### Entity resolution (A-phase upgrade)

- [ ] **Evaluate Splink against the current heuristic clustering on Bremen.**
      `moj-analytical-services/splink` (MIT, native DuckDB backend — fits the QuackOSM stack).
      Fellegi–Sunter model with EM-learned match weights instead of hand-tuned distance/name
      thresholds. Benchmark: agreement with the existing 320 726 multi-source clusters, plus manual
      spot-check of disagreements. Reference for German-register practice: Destatis method test
      (Kramer 2026, 10.23889/ijpds.v11i5.3814). Full-text notes: notes/linacre2022.md, kramer2026.md.
      - [ ] Prerequisite (from Destatis): normalization pass BEFORE linkage — umlauts/ß, legal-form
            suffixes (GmbH, GmbH & Co. KG, e.K., …), Straße/Str. address variants.
      - [ ] Conservative validation baseline (Zhang & Pfoser recipe): stop-word-stripped name
            similarity ≥ 0.9 within ≤ 50 m; expect matched-pair coordinate agreement ~30–40 m.
      - [ ] Name metric: tuned Jaro–Winkler + token-order-robust ensemble suffices for German
            Latin-script names (Santos et al. — neural gain is mostly cross-alphabet); no NN matcher.
      - [ ] Blocking radius is a free parameter in the field (50–1000 m, Sun et al. review) — sweep it
            on Bremen rather than assuming.
- [ ] **Enforce one-to-one matching cluster-wide** (assignment/graph formulation à la Novack et al.
      2018, 10.3390/ijgi7030117) instead of greedy nearest-match — cheap even without Splink.
- [ ] Optional, if address noise becomes a matching problem: trial `openvenues/libpostal` (MIT) for
      address normalization before blocking.

### B5 MaStR (fetch + parse DONE 2026-08-31; mastr.db 11 GB, .bak kept)

- [ ] **B5.1 refactor (PLANNED, awaiting go — see `mastr-refactor-plan.md`):** per-unit
      kW fields one-to-one (NO sums, `mastr_tech_detail` JSON), drop `mastr_kw`,
      business_subtype without technology; preview: singular tech toggles (any-match),
      per-tech kW/kWh hover, name search + click-to-zoom in sidebar.

- [ ] Future re-parse of a newer export: trial open-mastr's parallel mode
      (`os.environ['NUMBER_OF_PROCESSES']`) — the single-process parse took 56 min for 63 GB XML.

- [x] **Adopt Tepe et al. 2023 as acceptance checks** — DONE 2026-08-31 (`scripts/mastr_acceptance_checks.py`, report in `data/raw/mastr/acceptance/`; paper replicated: 3.07 % wind outside district, coords 96.8/4.1/0.2 %): 90 SQL data tests, open code at
      github.com/FlorianK13/verify-marktstammdaten (notes/tepe2023.md). Run the location tests on our
      parsed export before merging into the DE table. Known failure modes to check for:
      >3% of wind units with coordinates outside their declared district (errors 40–300 km);
      coordinate completeness by technology 97% (wind) → 5% (solar) → 0% (storage) — plan geocoding
      fallback for coordinate-less units; implausible MW/ha densities (21.3% of ground-mounted PV).
      - [x] (measured 1.36× unit→Lokation inflation; adapter aggregates per (operator, Lokation)) Expect MaStR to OVERSHOOT real sites ~35% via per-unit registration (Plinke et al. biogas
            study) → cluster units to sites before counting companies; ~10% of real sites may be
            missing. Their aerial-validated Lower Saxony biogas register (doi:10.25835/in90p55t) is
            free ground truth for our site-clustering logic.

### C phase — NACE classification (before writing any classify code)

- [ ] **Read `InseeFrLab/GRAAL` (MIT)** — LLM + embeddings + RAG NACE classification module from the
      French statistical office; architecturally our planned classify stage. Decide: adapt vs
      reimplement. Also skim `InseeFrLab/codif-ape-train` for hierarchical NACE rev 2.1 handling.
- [ ] **Read the two key papers**: Kühnemann/van Delden/Windmeijer 2020 (NACE from web page texts,
      10.3233/sji-200675) and Beuter et al. 2025 (German business-activity descriptions,
      10.1007/978-3-032-10004-7_10). Expect: hierarchy + class imbalance are the hard parts; thin/no
      website text is the residual — same as our consumer-only-site open question.
- [ ] **Evaluate `crawl4ai` (Apache-2.0, already starred) for C-phase website fetching** — must respect
      robots.txt, 1 req/s per domain, home + max 5 internal pages (spec licensing rules).
      Do NOT vendor AGPL code (firecrawl, open-MaStR) into the package.
      Proven German-firm crawl practices to copy from ARGUS/ZEW (notes/kinne2020.md,
      github.com/datawizard1337/ARGUS): prefer short URLs when picking internal pages, per-page
      language detection with German preference, drop cross-domain redirects, Wayback Machine as
      fallback for dead URLs (Gök et al.).
- [ ] **Plan for the ~54% website gap**: only 46% of German firms have a findable URL, biased against
      small/young/rural firms (Kinne & Axenbeck, 2.52M-firm MUP population). NACE classification MUST
      keep tags/register hints as a first-class evidence path, not just a fallback — aligns with the
      B5 consumer-only-site open question.

### New source / validation opportunities

- [ ] **sEEnergies / Hotmaps industrial-sites adapter (B-phase candidate)**: georeferenced EU
      energy-intensive site DB (Manz et al. 2021, 10.3390/su13031439; open data hub). Check license
      terms + overlap with existing B1 IED clusters; likely more valuable as a validation layer than
      as a fifth source.
- [ ] **German Overture validation as a by-product**: Ballantyne & Berragan 2024 validated Overture
      places for the UK only (10.1177/23998083241263124; notes/ballantyne2024.md — <5% count deficit,
      6–8 m positional accuracy vs Geolytix). Our B3-vs-B1/B2 cluster-overlap stats are a publishable
      gap — write them down when we next regenerate the merged table.
      - [ ] Concrete rule for B3 QA now: stratify by `sources_dataset` — Microsoft-sourced places can
            be up to 100% brand/category-missing — and never filter on a single attribute.
- [ ] **Cheap completeness QA layer**: per-grid-cell expected-density proxy à la Herfort et al.
      (notes/herfort2023.md; W-European urban OSM near saturation, rural unproven) to flag cells where
      our company density is implausibly low.
- [ ] Opportunistic enrichment: `bundesAPI/handelsregister` CLI for HR numbers (≤60 req/h limit);
      check `okfde/offeneregister.de` dump freshness first.

### Research housekeeping

- [x] **Deep-reading pass** — DONE 2026-08-30 (29 MB): 12 papers read in full, page-referenced notes
      in research/geo-company-extraction/notes/, report.md updated (§1b). 6 papers remain
      abstract-only (MDPI/IOS Press/PMC bot-gating): Manz 2021, Novack 2018, Deng 2019, Kühnemann
      2020, food-outlet matcher 2024, LLM-POI 2026.
- [ ] Optional: retry the 6 bot-gated PDFs via browser (claude-in-chrome) or manual download if their
      full texts become load-bearing (Kühnemann 2020 is the most valuable — the CBS NACE-from-web
      methodology).
