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
  DE table **4 164 518 companies**, 5 sources, 327 695 multi-source clusters, 60 five-source
  clusters. B4 (Foursquare) deferred as redundant.
- **B5 MaStR:** data present since 2026-08-31 (`data/raw/mastr/mastr.db`, 11 GB, zip + .bak
  kept); kW floors ≥ 50 kW (PV/storage ≥ 100 kW); B5.1 (2026-09-04): per-unit
  `mastr_tech_detail`, no kW sums; `business_subtype` NULL; WZ 2025 in `mastr_wz_*` +
  `mastr_wz_code` (Destatis file `data/raw/wz2025/`). Known gap: storage kWh empty in the
  20260827 parse (`storage_units` table has 0 rows → local re-parse of AnlagenStromSpeicher).
- **Resolve engine vectorised 2026-09-04:** DE merge 44.6 h → ~2.5 min end to end, output
  identical (see queue item 2). Reference copies of the previous DE merge:
  `companies_merged_DE_pre_b51_*` (delete when no longer needed).
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
4. **Industrial-signal 3-parter** (approved earlier): OSM power inflation, Overture
   subtype → industrial override (67 subtypes hidden under office), drop
   geographic_entities.
5. **Entity-resolution normalization pass** (prerequisite for 6 and for Splink): umlauts/ß,
   legal-form suffixes (GmbH, GmbH & Co. KG, e.K., …), Straße/Str. variants; then the
   Splink-vs-heuristic benchmark on Bremen and cluster-wide one-to-one matching.
6. **B6 Handelsregister — BEFORE Part C, no longer optional.** Purpose: verify that the
   entities in the merged table EXIST and cross-check them in every field a register
   offers (name, legal form, seat address, HR number/court, status incl. dissolved).
   - Preferred route: a **bulk register dump** matched locally (fast, no 60 req/h limit).
     Before any download: probe the candidate dumps for freshness and size and report
     both — `okfde/offeneregister.de` (OpenCorporates/OKFN dump; suspected stale, check
     date first), plus any newer open bulk source found in a fresh research pass. The
     `bundesAPI/handelsregister` CLI (≤ 60 req/h) is only for spot-checks / the residual.
   - Match on normalized name + city/PLZ (item 5), record `hr_*` provenance and a
     `register_match` status per row; a non-match is NOT proof of non-existence — most
     small businesses (sole traders, amenities) are not HR-registered.
   - Output feeds `confidence` and becomes the ground truth for Part C evaluation.
7. **Part C NACE classification — refactored against the 2026-08-30 research** before
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
8. **E1** tests / README / CI; then validation layers (sEEnergies/Hotmaps, Overture
   overlap stats, density QA) as time allows.

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
