"""Plain-Python configuration constants (no YAML) — spec §A0.

Everything tunable lives here: CRS regime, Geofabrik state URLs + aliases, OSM filters,
dedup knobs, source priority, confidence weights, intrinsic industrial signals, release
pins, sanity bounds.
"""
from __future__ import annotations

import os

# --- CRS regime (spec §2) ---------------------------------------------------------------
CRS_STORAGE = "EPSG:4326"   # internal storage, all *_4326 files, latitude/longitude
CRS_METRIC = "EPSG:25832"   # ETRS89 / UTM 32N — all area/distance math
CRS_MAP = "EPSG:3857"       # Web-Mercator map export + x/y columns

# --- Germany sanity bounds (spec §2) ----------------------------------------------------
GERMANY_BBOX = (5.87, 47.27, 15.04, 55.06)  # lon_min, lat_min, lon_max, lat_max
LAT_BOUNDS = (45.0, 56.0)
LON_BOUNDS = (4.0, 17.0)
AREA_MAX_M2 = 5e7           # grounds area sane range: (0, 5e7]

# --- Geofabrik per-state PBFs (spec §4 #1) ----------------------------------------------
GEOFABRIK_BASE = "https://download.geofabrik.de/europe/germany"

# slug → official Bundesland name (OSM admin_level=4 `name`)
STATES: dict[str, str] = {
    "baden-wuerttemberg": "Baden-Württemberg",
    "bayern": "Bayern",
    "berlin": "Berlin",
    "brandenburg": "Brandenburg",
    "bremen": "Bremen",
    "hamburg": "Hamburg",
    "hessen": "Hessen",
    "mecklenburg-vorpommern": "Mecklenburg-Vorpommern",
    "niedersachsen": "Niedersachsen",
    "nordrhein-westfalen": "Nordrhein-Westfalen",
    "rheinland-pfalz": "Rheinland-Pfalz",
    "saarland": "Saarland",
    "sachsen": "Sachsen",
    "sachsen-anhalt": "Sachsen-Anhalt",
    "schleswig-holstein": "Schleswig-Holstein",
    "thueringen": "Thüringen",
}

# any user-supplied spelling → slug (official names, license-plate codes, common shorthands)
STATE_ALIASES: dict[str, str] = {
    **{slug: slug for slug in STATES},
    **{name.lower(): slug for slug, name in STATES.items()},
    "bw": "baden-wuerttemberg",
    "by": "bayern",
    "be": "berlin",
    "bb": "brandenburg",
    "hb": "bremen",
    "hh": "hamburg",
    "he": "hessen",
    "mv": "mecklenburg-vorpommern",
    "ni": "niedersachsen",
    "nds": "niedersachsen",
    "nrw": "nordrhein-westfalen",
    "nw": "nordrhein-westfalen",
    "rp": "rheinland-pfalz",
    "rlp": "rheinland-pfalz",
    "sl": "saarland",
    "sn": "sachsen",
    "st": "sachsen-anhalt",
    "sh": "schleswig-holstein",
    "th": "thueringen",
}


def resolve_state(value: str) -> str:
    """Any spelling of a Bundesland → Geofabrik slug. Raises on unknown."""
    slug = STATE_ALIASES.get(value.strip().lower())
    if slug is None:
        raise ValueError(f"unknown state {value!r}; known: {', '.join(sorted(STATES))}")
    return slug


def resolve_states(states: str) -> list[str]:
    """"bremen,hamburg" / "all" → Geofabrik slugs."""
    if states.strip().lower() == "all":
        return list(STATES)
    return [resolve_state(s) for s in states.split(",") if s.strip()]


def scope_name(states: str | list[str]) -> str:
    """Scope label used in file names: "DE" for all states, one slug, or sorted slugs joined."""
    if isinstance(states, str):
        if states.strip() == "DE":
            return "DE"
        states = resolve_states(states)
    if set(states) == set(STATES):
        return "DE"
    return states[0] if len(states) == 1 else "-".join(sorted(states))


def geofabrik_url(slug: str) -> str:
    return f"{GEOFABRIK_BASE}/{slug}-latest.osm.pbf"


# --- IED installations (spec §B1, source catalogue #2) ----------------------------------
# THRU.de EU-Registry workbook. The upload path is version-stamped — bump deliberately
# when THRU.de publishes a new release.
IED_URL = ("https://thru.de/wp-content/uploads/2026/04/"
           "Anlagenliste_EU-Registry_gemaess_IE_RL_ab_2017.xlsx")
IED_SHEET = "2017-2024_EUReg_Anlagen"
IED_KEEP_STATUS = "In Betrieb (functional)"  # drop disused / decommissioned / notRegulated


# --- Abwärme platform (spec §B2, source catalogue #3) -----------------------------------
# BfEE waste-heat platform (EnEfG §17). The plain URL serves an HTML page; the
# `__blob=publicationFile` variant serves the workbook. `v=` is the release — bump
# deliberately.
ABWAERME_URL = ("https://www.bfee-online.de/SharedDocs/Downloads/BfEE/DE/Effizienzpolitik/"
                "pfa_datentabelle_excel.xlsx?__blob=publicationFile&v=28")
ABWAERME_SHEET = "Abwärmepotentiale"

# --- Overture Maps places theme (spec §B3, source catalogue #5) -------------------------
# Monthly GeoParquet releases on public S3 (no auth), license CDLA-Permissive-2.0.
# Pin the release — bump deliberately.
OVERTURE_RELEASE = "2026-08-19.0"
OVERTURE_S3 = (f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}"
               "/theme=places/type=place/*")
OVERTURE_MIN_CONFIDENCE = 0.5      # Overture's own conflation confidence; drops ~11 %
GERMANY_BBOX = (5.5, 47.0, 15.5, 55.5)   # lon_min, lat_min, lon_max, lat_max

# taxonomy root → contract business_type (pure mapping of Overture's own hierarchy —
# no per-row classification; unmapped roots stay business_type=NA and are logged)
OVERTURE_ROOT_TYPES = {
    "shopping": "shop",
    "retail": "shop",
    "automotive": "shop",
    "pets": "shop",
    "beauty_and_spa": "shop",
    "lifestyle_services": "shop",
    "food_and_drink": "amenity",
    "eat_and_drink": "amenity",
    "health_care": "amenity",
    "health_and_medical": "amenity",
    "community_and_government": "amenity",
    "public_service_and_government": "amenity",
    "education": "amenity",
    "arts_and_entertainment": "amenity",
    "cultural_and_historic": "amenity",
    "sports_and_recreation": "amenity",
    "active_life": "amenity",
    "attractions_and_activities": "amenity",
    "travel_and_transportation": "amenity",
    "lodging": "amenity",
    "accommodation": "amenity",
    "religious_organization": "amenity",
    "services_and_business": "office",
    "professional_services": "office",
    "business_to_business": "office",
    "financial_service": "office",
    "financial_services": "office",
    "real_estate": "office",
    "mass_media": "office",
    "home_service": "craft",
    "home_services": "craft",
    "industrial": "industrial",
    "manufacturing": "industrial",
}
# Taxonomy roots that are not businesses at all — dropped in the adapter (todo item 4c,
# 2026-09-04): lakes, rivers, mountains, bridges, beaches … (19 307 DE rows).
OVERTURE_DROP_ROOTS = {"geographic_entities"}
# Category-slug override consulted BEFORE the root mapping (todo item 4b, 2026-09-04).
# Overture has no industrial taxonomy root: its manufacturing / processing / extraction /
# utility / waste / logistics categories sit under services_and_business (→ office) or
# community_and_government (→ amenity). Pure slug → business_type mapping, still no
# per-row classification. Rows hitting this table also carry the §6.5 intrinsic
# industrial signal (they have no OSM tags to carry it otherwise).
OVERTURE_SUBTYPE_TYPES: dict[str, str] = {
    # manufacturing & processing (NACE C)
    "industrial_equipment": "industrial", "metal_supplier": "industrial",
    "business_manufacturing_and_supply": "industrial", "metal_fabricator": "industrial",
    "commercial_industrial": "industrial", "industrial_company": "industrial",
    "plastic_company": "industrial", "plastic_manufacturer": "industrial",
    "machine_shop": "industrial", "chemical_plant": "industrial",
    "masonry_concrete": "industrial", "jewelry_and_watches_manufacturer": "industrial",
    "jewelry_manufacturer": "industrial", "auto_manufacturers_and_distributors": "industrial",
    "pharmaceutical_companies": "industrial", "furniture_manufacturers": "industrial",
    "appliance_manufacturer": "industrial", "welders": "industrial",
    "b2b_machinery_and_tools": "industrial", "wood_and_pulp": "industrial",
    "leather_products_manufacturer": "industrial", "biotechnology_company": "industrial",
    "glass_manufacturer": "industrial", "b2b_rubber_and_plastics": "industrial",
    "saw_mill": "industrial", "mattress_manufacturing": "industrial",
    "metal_plating_service": "industrial", "steel_fabricators": "industrial",
    "aircraft_manufacturer": "industrial", "paper_mill": "industrial",
    "textile_mill": "industrial", "casting_molding_and_machining": "industrial",
    "packaging_contractors_and_service": "industrial",
    "metal_materials_and_experts": "industrial", "motorcycle_manufacturer": "industrial",
    "sheet_metal": "industrial", "plastic_injection_molding_workshop": "industrial",
    "flour_mill": "industrial", "mills": "industrial", "commercial_printer": "industrial",
    "coal_and_coke": "industrial",
    # extraction & refining (NACE B / C19)
    "mining": "industrial", "oil_and_gas": "industrial", "oil_refiners": "industrial",
    "oil_and_gas_field_equipment_and_services": "industrial",
    "b2b_oil_and_gas_extraction_and_services": "industrial",
    "b2b_energy_and_mining": "industrial",
    # water, waste, recycling (NACE E)
    "water_treatment_equipment_and_services": "industrial",
    "b2b_cleaning_and_waste_management": "industrial", "recycling_center": "industrial",
    "scrap_metals": "industrial", "hazardous_waste_disposal": "industrial",
    "industrial_cleaning_services": "industrial",
    # logistics depots (OSM counts warehouse/depot/port/railway as industrial, §6.5)
    "warehouses": "industrial", "freight_and_cargo_service": "industrial",
    "railroad_freight": "industrial", "motor_freight_trucking": "industrial",
    # energy supply (NACE D)
    "energy_company": "power", "power_plants_and_power_plant_service": "power",
    "wind_energy": "power", "electric_utility_provider": "power",
    "public_utility_company": "power",
}

# --- MaStR — Marktstammdatenregister (spec §B5, source catalogue #6) --------------------
# BNetzA bulk export via open-mastr into data/raw/mastr/mastr.db (sqlite). Units in
# operation only; operators that are natural persons are dropped (anonymized, not
# companies — user-approved amendment 2026-08-27). Unit rows aggregate to one site per
# (operator, Lokation).
MASTR_DB = "raw/mastr/mastr.db"            # relative to the data root
MASTR_MIN_PV_KW = 100.0                    # spec §B5: PV only ≥ 100 kW
MASTR_MIN_STORAGE_KW = 100.0               # amendment 2026-08-27: same rule for Speicher
# All other generation tables: MaStR publishes exact coordinates only from 50 kW upward
# (verified on the 2026-08-27 export: 100 % coordinate completeness ≥ 50 kW for every
# technology, 0 % below 30 kW). Sub-50 kW units are private households, not companies
# (user decision 2026-08-31). Consumer tables (gas/electricity) stay unfiltered.
MASTR_MIN_KW = 50.0
# Coordinate plausibility (acceptance checks 2026-08-31, after Kotthoff/Tepe et al. 2023):
# a unit coordinate farther than MASTR_DISTRICT_MISMATCH_KM outside its declared Landkreis
# (VG5000 polygon buffered by MASTR_DISTRICT_BUFFER_DEG, like the reference test) or outside
# the LAT/LON sanity bounds is treated as missing → address geocode, provenance flagged.
MASTR_DISTRICTS_SHP = "raw/vg5000/vg5000_ebenen_1231/VG5000_KRS.shp"   # BKG, DL-DE-BY-2.0
MASTR_DISTRICT_BUFFER_DEG = 0.015
MASTR_DISTRICT_MISMATCH_KM = 10.0
# business_type from the OPERATOR's registered economic activity (WZ section label in
# market_actors, 80.8 % coverage) — user decision 2026-09-01: a steelworks with an on-site
# power plant is industrial, not power. Keyword match on the section label (robust against
# the WZ 2008 → WZ 2025 letter shift), first hit wins; no hit → technology rule
# (consumer-only → industrial, else power).
MASTR_WZ_KEYWORD_TO_BUSINESS_TYPE: list[tuple[str, str]] = [
    ("energieversorgung", "power"),
    ("verarbeitendes gewerbe", "industrial"),
    ("bergbau", "industrial"),
    ("wasserversorgung", "industrial"),           # water, sewage, waste
    ("land- und forstwirtschaft", "industrial"),  # no agriculture value in the vocabulary
    ("verkehr und lagerei", "industrial"),        # transport & logistics depots
    ("baugewerbe", "craft"),
    ("handel", "shop"),
    ("gastgewerbe", "amenity"),
    ("gesundheits", "amenity"),
    ("erziehung", "amenity"),
    ("kunst", "amenity"),
    ("sport", "amenity"),
    ("abschnitt", "office"),                      # every remaining service/admin section
]
# WZ 2025 (Klassifikation der Wirtschaftszweige, Destatis, DL-DE-BY-2.0) structure file —
# level/code/title. MaStR carries the operator's WZ section/division/group only as German
# LABELS; this file turns the group label into its 3-digit code ("35.1") for
# business_subtype ("35.1 Elektrizitätsversorgung") and the mastr_wz_code debug column.
# Downloaded 2026-09-03 (user go); see mastr-refactor-plan.md §A4.
MASTR_WZ2025_XLSX = "raw/wz2025/gliederung-wz2025.xlsx"
MASTR_WZ2025_SHEET = "WZ 2025 Struktur"
# MaStR group labels that differ from the Destatis titles (NACE 2.1 DE wording / typos):
# measured 2026-09-03 — 274 of 287 labels match exactly after normalisation, these 13
# cover the rest (100 % of operators with a group). Keys are matched after the same
# normalisation as the file titles.
MASTR_WZ_GROUP_ALIASES: dict[str, str] = {
    "Auswärtige Angelegenheiten, Verteidigung, Rechtspflege/Justiz, öffentliche Sicherheit und Ordnung": "84.2",
    "Call Center": "82.2",
    "Erbringung von Dienstleistungen für den Unterricht": "85.6",
    "Erbringung von Dienstleistungen für kunstschaffende und darstellende Künste": "90.3",
    "Grundschulen/Volksschulen": "85.2",
    "Herstellung von Beförderungsmitteln": "30.9",
    "Herstellung von Bekleidung und Bekleidungszubehör": "14.2",
    "Herstellung von Mess- und Kontrollinstrumenten sowie Uhren": "26.5",
    "Oberflächenveredlung und Wärmebehandlung; Metallbearbeitung": "25.5",
    "Sammeln von wild wachsenden Produkten, ohne Holz": "02.3",
    "Soziale Betreuung von älterern Menschen und von Menschen mit Behinderung": "88.1",
    "Säge-, Hobel- und Holzimprägnierwerke; Bearbeitung und Veredlung von Holz": "16.1",
    "Verlegen von Büchern und Zeitschriften; sonstiges Verlagswesen, ohne Software": "58.1",
}
# open-mastr unit tables to read (technology → mastr_techs value; NOT business_subtype —
# user decision 2026-09-03, technology lives only in mastr_* columns).
# nuclear keeps ALL operating statuses (fleet shut down since 2023 — the sites and
# their operators still exist; user decision 2026-08-27); everything else In Betrieb only.
MASTR_TABLES = {
    "wind_extended": "wind",
    "solar_extended": "solar",
    "biomass_extended": "biomass",
    "hydro_extended": "hydro",
    "storage_extended": "storage",
    "combustion_extended": "combustion",
    "gsgk_extended": "geothermal",         # Geothermie/Grubengas/Druckentspannung
    "nuclear_extended": "nuclear",
    "gas_producer": "gas_production",
    "gas_consumer": "gas_consumption",     # large industrial gas consumers
    "gas_storage_extended": "gas_storage",
    "electricity_consumer": "electricity_consumption",  # large industrial consumers
}

# --- Geocoding (spec §4.1) --------------------------------------------------------------
# Public Nominatim by default; set NOMINATIM_URL for a self-hosted instance. Every
# result is cached on disk — each address is fetched once, ever. Public endpoint
# policy: ≤ 1 req/s, descriptive User-Agent, never > 10 k addresses.
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
NOMINATIM_USER_AGENT = "geoextract/0.1 (EnergiaConsult company-extraction pipeline)"
NOMINATIM_MIN_INTERVAL_S = 1.1
# geocode_precision values that are too coarse to anchor dedup (spec §B2):
GEOCODE_COARSE = {"postcode", "city"}
GEOCODE_COARSE_CONFIDENCE_CAP = 0.3


# --- OSM capture-all business filter (spec §A1) -----------------------------------------
OSM_BUSINESS_FILTER: dict[str, bool | list[str]] = {
    "office": True,
    "shop": True,
    "craft": True,
    "industrial": True,
    "amenity": [
        "restaurant", "cafe", "fast_food", "bar", "pub", "biergarten", "food_court", "bank",
        "pharmacy", "fuel", "car_wash", "car_rental", "driving_school", "veterinary",
        "dentist", "doctors", "clinic", "hospital", "cinema", "theatre", "nightclub",
        "marketplace", "post_office", "coworking_space", "internet_cafe", "ice_cream",
        "casino", "childcare", "kindergarten", "language_school", "music_school", "recycling",
    ],
    "man_made": [
        "works", "kiln", "chimney", "gasometer", "silo", "storage_tank", "pipeline",
        "petroleum_well", "mineshaft", "wastewater_plant", "water_works", "pumping_station",
    ],
    "power": ["plant", "generator", "substation"],
}

# priority order when a feature carries several business keys — first present wins
BUSINESS_KEYS = ["office", "shop", "craft", "industrial", "amenity", "man_made", "power"]
BUSINESS_VALUE_BLACKLIST = {"no", "vacant"}

WEBSITE_TAGS = ["website", "contact:website", "url"]
PHONE_TAGS = ["phone", "contact:phone"]
EMAIL_TAGS = ["email", "contact:email"]

# landuse zones used for the grounds-area fallback (spec §A2) — not company records
LANDUSE_ZONE_VALUES = ["commercial", "industrial", "retail"]

# admin boundary levels (spec §A2)
ADMIN_LEVEL_STATE = "4"
ADMIN_LEVEL_DISTRICT = "6"

# Geofabrik slug → official Bundesland name (the `state` contract column).
SLUG_STATE_NAMES = {
    "baden-wuerttemberg": "Baden-Württemberg", "bayern": "Bayern", "berlin": "Berlin",
    "brandenburg": "Brandenburg", "bremen": "Bremen", "hamburg": "Hamburg",
    "hessen": "Hessen", "mecklenburg-vorpommern": "Mecklenburg-Vorpommern",
    "niedersachsen": "Niedersachsen", "nordrhein-westfalen": "Nordrhein-Westfalen",
    "rheinland-pfalz": "Rheinland-Pfalz", "saarland": "Saarland", "sachsen": "Sachsen",
    "sachsen-anhalt": "Sachsen-Anhalt", "schleswig-holstein": "Schleswig-Holstein",
    "thueringen": "Thüringen",
}

# Official AGS state prefixes (first two digits of a Kreis AGS) → state name.
# Fallback for states whose level-4 relation is broken/absent in the clipped PBF
# (seen for Brandenburg and Sachsen-Anhalt in Geofabrik extracts).
AGS_STATE_NAMES = {
    "01": "Schleswig-Holstein", "02": "Hamburg", "03": "Niedersachsen", "04": "Bremen",
    "05": "Nordrhein-Westfalen", "06": "Hessen", "07": "Rheinland-Pfalz",
    "08": "Baden-Württemberg", "09": "Bayern", "10": "Saarland", "11": "Berlin",
    "12": "Brandenburg", "13": "Mecklenburg-Vorpommern", "14": "Sachsen",
    "15": "Sachsen-Anhalt", "16": "Thüringen",
}

# --- Entity resolution knobs (spec §A3) -------------------------------------------------
DEDUP_DISTANCE_M = 50.0        # max distance for a match
DEDUP_NAME_RATIO = 80.0        # rapidfuzz token_sort_ratio threshold
DEDUP_POLYGON_NAME_RATIO = 60.0  # candidate floor for the polygon-containment path
# Rule changes after the Splink benchmark (todo item 5, user go 2026-09-04):
# (1) near-identical names match farther than 50 m (identical branches 50–100 m apart
#     were missed: Rossmann 81 m, Lidl 88 m)
DEDUP_NEAR_IDENTICAL_RATIO = 95.0
DEDUP_NEAR_IDENTICAL_DISTANCE_M = 100.0
# (2) polygon-containment path: both names ≥ 2 tokens and, by DEDUP_CONTAIN_RULE,
#     "ratio70" → token_sort_ratio ≥ 70 (as approved) or "token_subset" → the shorter
#     name's tokens all occur in the longer one (measured alternative, see spec §A3)
DEDUP_CONTAIN_MIN_TOKENS = 2
DEDUP_CONTAIN_NAME_RATIO = 70.0
DEDUP_CONTAIN_RULE = "ratio70"
# (3) ratio-80 matches within 50 m need substance: ≥ 2 shared tokens, or shared tokens
#     of ≥ 8 characters in total ("NK Beauty"/"Beauty Line", "GEW Bremen"/"NGG Bremen"
#     share one short generic token only)
DEDUP_MIN_SHARED_TOKENS = 2
DEDUP_MIN_SHARED_CHARS = 8
DEDUP_SUBSTANCE_MAX_RATIO = 90.0   # ≥ 90 = spelling variant ("Erotic"/"Erotik Gigant",
                                   # "Veronika's"/"Veronikas Treff") — no token check needed
BLOCK_GRID_M = 100.0           # blocking grid cell size in EPSG:25832

# Name normalisation for entity resolution (todo item 5, 2026-09-04; Destatis practice,
# Kramer 2026): applied as TOKENS after dots inside abbreviations are removed
# ("G.m.b.H." → "gmbh", "e.K." → "ek"), so every spelling variant is caught.
LEGAL_FORM_TOKENS = {
    "gmbh", "ggmbh", "gesmbh", "mbh", "ag", "kg", "kgaa", "ohg", "ug", "ek", "ev",
    "se", "gbr", "eg", "cokg", "co", "inc", "ltd", "llc", "haftungsbeschraenkt",
    "gemeinnuetzige", "gemeinnuetzig",
}
# titles, owner prefixes and connectors that vary between sources ("Dr. med. H. Müller"
# vs "Zahnarzt Müller", "Müller & Söhne" vs "Müller und Söhne")
NAME_NOISE_TOKENS = {"dr", "med", "dent", "vet", "dipl", "ing", "inh", "prof", "und", "u", "and"}
# kept for reference/compat (old regex-based stripper); no longer used by normalize_name
LEGAL_FORM_SUFFIXES = [
    "gmbh & co. kg", "gmbh & co kg", "gmbh", "ag", "kg", "ohg", "ug",
    "e.k.", "e.v.", "se", "gbr", "mbh", "inc", "ltd", "llc",
]

SOURCE_PRIORITY = ["osm", "ied", "abwaerme", "mastr", "overture", "fsq", "handelsregister"]

# Register join (item 6 stage 2, register/match.py): a shared name token decides an 80–89
# pair only if it is rare among the register names of the scope (idf = ln(N / df)):
# idf ≥ 11 ≈ fewer than ~20 of 1.1 M Bremen name variants carry it ("nacke", "korschek");
# place names and trade words ("worpswede" ≈ 8, "rechtsanwaltsgesellschaft" ≈ 10) do not.
REGISTER_IDF_RARE = 11.0
REGISTER_IDF_COMMON = 9.0      # two shared tokens this rare also count

# Stage 4 (register/reconcile.py): interim industrial flag on the register's business purpose
# (Unternehmensgegenstand) until Part C delivers WZ codes — substrings on the umlaut-folded
# lower-case text; the exclude list catches trading / holding / real-estate uses of the words.
REGISTER_INDUSTRIAL_KEYWORDS = (
    "herstellung", "produktion", "fertigung", "verarbeitung", "erzeugung", "recycling",
    "entsorgung", "abfall", "kraftwerk", "energieerzeugung", "stromerzeugung", "windpark",
    "solarpark", "photovoltaik", "biogas", "raffinerie", "giesserei", "walzwerk", "schmiede",
    "brauerei", "molkerei", "schlacht", "muehle", "saegewerk", "druckerei", "chemie",
    "pharma", "metallbau", "maschinenbau", "anlagenbau", "schiffbau", "werft", "bergbau",
    "steinbruch", "kies", "zement", "beton", "asphalt", "logistik", "spedition",
    "lagerung", "hafen", "wasserversorgung", "abwasser", "fernwaerme",
)
REGISTER_INDUSTRIAL_EXCLUDE = (
    "verwaltung von", "beteiligung", "vermietung", "immobilien", "grundstueck", "holding",
    "handel mit", "grosshandel", "einzelhandel", "vertrieb von", "beratung", "vermittlung",
    "softwareentwicklung", "software", "planung", "ingenieur", "architekt",
)

# Item 6c (sources/wikidata.py) + stage 3b (web/discover.py): identify ourselves per the
# Wikimedia User-Agent policy; the same string is used for the one-page verification fetches.
WIKIDATA_USER_AGENT = "geoextract/0.1 (wikidata enrichment; https://github.com/EnergiaConsult/geo-company)"

# --- Register + website pipeline (queue item 6, register-website-pipeline-plan.md) ----------
# Stage 0a–0c: bulk register files (never the 60 req/h portal). Paths relative to the data root.
HR2022_DB = "raw/handelsregister/handelsregister.db"        # offeneregister.de, 2022-10-21, CC-BY 4.0
HR2022_SNAPSHOT = "2022-08-01"                               # last announcement date in the file
HR2019_JSONL = "raw/handelsregister/de_companies_ocdata.jsonl.bz2"   # OpenCorporates/OKFN, CC-BY 4.0
HR2019_SNAPSHOT = "2019-01-31"
GLEIF_ZIP_GLOB = "raw/gleif/*-gleif-goldencopy-lei2-golden-copy.csv.zip"   # GLEIF golden copy, CC0
GLEIF_COUNTRY = "DE"
REGISTER_ATTRIBUTION = (
    "Handelsregister data: OffeneRegister.de (Open Knowledge Foundation Deutschland) and "
    "OpenCorporates, CC-BY 4.0; GLEIF LEI golden copy, CC0."
)

# Stage 0d: website hygiene. `website` keeps the company's OWN site (or its chain's site);
# directory listings, social profiles and parcel-network pages move to `website_listing`.
WEBSITE_DIRECTORY_HOSTS = {
    "gelbeseiten.de", "11880.com", "dasoertliche.de", "dastelefonbuch.de", "goyellow.de",
    "cylex.de", "cylex-branchenbuch.de", "meinestadt.de", "yelp.de", "yelp.com", "golocal.de",
    "stadtbranchenbuch.com", "branchenbuch24.com", "firmenwissen.de", "northdata.de",
    "northdata.com", "unternehmensregister.de", "handelsregister.de", "companyhouse.de",
    "kununu.com", "wikipedia.org", "wikidata.org", "parkopedia.de", "parkopedia.com",
    "google.com", "google.de", "goo.gl", "maps.app.goo.gl", "bing.com", "tripadvisor.de",
    "tripadvisor.com", "booking.com", "hrs.de", "lieferando.de", "jameda.de", "doctolib.de",
    "sanego.de", "immobilienscout24.de", "kleinanzeigen.de", "ebay-kleinanzeigen.de",
    "openstreetmap.org", "osm.org", "wheelmap.org", "foursquare.com", "opentable.de",
    "quandoo.de", "thefork.de", "speisekarte.de", "restaurant-kritik.de", "marktplatz-mittelstand.de",
    "wer-zu-wem.de", "dialo.de", "hotfrog.de", "yellowmap.de", "branchen-info.net",
    # confirmed on the DE table 2026-09-07 (learned rule + name sample)
    "sellwerk.de", "mon.de", "webadresse.de", "kosmetikstudios-24.de", "studiobookr.com",
    "planity.com", "treatwell.de", "kinderaerzte-im-netz.de", "arzt-auskunft.de",
    "wonderl.ink", "bit.ly", "tinyurl.com", "florist.fleurop.de",
}
# host-label tokens too generic to prove a brand ("huk-vor-ort" → only "huk" counts)
WEBSITE_BRAND_STOP_TOKENS = {
    "de", "com", "net", "org", "eu", "www", "online", "vor", "ort", "agentur", "betreuer",
    "shop", "portal", "web", "service", "filialen", "standorte", "stores", "store", "hotel",
    "hotels", "markt", "maerkte", "gesundheit", "group", "gruppe", "info", "app", "home",
    "mein", "meine", "dein", "deine", "unser",
}
# hosts that fall under a directory parent but are genuinely a company's own site
WEBSITE_OWN_HOST_EXCEPTIONS = {"sites.google.com", "business.site", "jimdosite.com", "jimdofree.com"}
# operator / agent networks whose per-location pages ARE the company's official presence but
# whose names rarely carry the host label (confirmed on the DE table 2026-09-07)
WEBSITE_CHAIN_HOSTS = {
    "dvag.de", "allfinanz-dvag.de", "korian.de", "all.accor.com", "ihg.com",
    "helios-gesundheit.de", "axa-betreuer.de", "huk-vor-ort.de", "signal-iduna-agentur.de",
    "vertretung.allianz.de", "agentur.lvm.de", "agentur.barmenia.de", "wuerttembergische.de",
    "ruv.de", "vlh.de", "johanniter.de", "drk.de", "malteser.de", "awo.org", "caritas.de",
    "diakonie.de", "asb.de", "fressnapf.de", "tedi.com", "kik.de", "nkd.com",
}
WEBSITE_SOCIAL_HOSTS = {
    "facebook.com", "fb.com", "fb.me", "instagram.com", "linktr.ee", "twitter.com", "x.com",
    "linkedin.com", "xing.com", "youtube.com", "youtu.be", "tiktok.com", "pinterest.com",
    "pinterest.de", "threads.net", "wa.me", "whatsapp.com", "t.me", "telegram.me", "vimeo.com",
    "flickr.com", "tumblr.com", "snapchat.com", "twitch.tv", "discord.gg", "discord.com",
    "facebook.de", "m.me",
}
WEBSITE_PARCEL_HOSTS = {
    "paketshop.myhermes.de", "myhermes.de", "dpd.com", "gls-pakete.de", "gls-group.eu",
    "ups.com", "fedex.com", "packstation.de",
}
# learned rules on the merged table: a host shared by many rows is a chain (kept in `website`)
# unless it looks like a directory — many distinct names whose keys do not contain the host's
# brand label (gelbeseiten.de: 104 903 rows / 102 779 names; edeka.de: 7 332 rows, brand in name)
WEBSITE_CHAIN_MIN_ROWS = 20
WEBSITE_DIRECTORY_MIN_ROWS = 100
WEBSITE_DIRECTORY_NAME_RATIO = 0.9      # distinct names / rows
WEBSITE_DIRECTORY_MAX_BRAND_SHARE = 0.3  # share of name keys containing the brand label
WEBSITE_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref")

# --- Serve build (queue item 7, serve-plan.md S1) -------------------------------------------
SERVE_PUBLIC_DROP = {"email", "geometry"}          # never in a public download
SERVE_EXTRA_PUBLIC = ["website_kind", "website_host", "website_listing", "website_source",
                      "hr_name", "hr_matched_name", "register_score", "hr_candidates",
                      "wd_id", "wd_operator_id", "wd_brand_id", "wd_website", "wd_industry", "wd_lei",
                      "wd_legal_form", "wd_parent", "wd_inception", "wd_dissolved",
                      "member_ids"]                 # debug columns that ARE public (tier 1)
# columns of a raw source record that are merge bookkeeping, not the source's own data
SERVE_RAW_DROP = {"email", "geometry", "source", "source_count", "confidence_score",
                  "merged_at", "nace_codes", "nace_primary", "nace_section", "nace_confidence",
                  "nace_method", "nace_reasoning", "wz_code", "is_industrial", "dedup_anchor"}
SERVE_SOURCE_PREFIXES = {"osm": "osm_", "ied": "ied_", "abwaerme": "abw_", "mastr": "mastr_",
                         "overture": "ovt_"}         # member id prefix → source key
SERVE_SECTOR_COLUMN = "business_type"               # until Part C: nace_section
SERVE_ROW_GROUP = 100_000
SERVE_TILE_MINZOOM = 4
SERVE_TILE_MAXZOOM = 14
SERVE_TILE_ALLPOINTS_ZOOM = 11                      # from here every point is kept
SERVE_TILE_SITES_MINZOOM = 12
SERVE_TILE_LANDUSE_MINZOOM = 10
SERVE_TILE_POINT_PROPS = ["id", "name", "business_type", "source", "source_count",
                          "is_industrial", "nace_section", "state", "district_ags", "website",
                          "register_match", "hr_status"]

# --- Confidence score weights (spec §5.4) -----------------------------------------------
CONFIDENCE_WEIGHTS = {
    "multi_source": 0.30,
    "has_website": 0.15,
    "has_address": 0.20,        # street + housenumber + postcode + city all present
    "has_geometry_polygon": 0.15,
    "has_phone": 0.10,
    "has_grounds_area": 0.10,
}

# --- Intrinsic industrial signal (spec §6.5) --------------------------------------------
INDUSTRIAL_SOURCES = {"ied", "abwaerme", "mastr"}
INDUSTRIAL_TAGS: dict[str, set[str]] = {
    "landuse": {"industrial", "quarry", "port", "depot", "railway", "landfill"},
    "man_made": {"works", "kiln", "wastewater_plant", "water_works"},   # production / utility plants
    "industrial": {"factory", "oil", "mine", "warehouse", "port", "scrap_yard",
                   "slaughterhouse", "depot"},
    "power": {"plant"},
    "craft": {"metal_construction", "electronics", "joinery"},
}
# Tags that count only when the row has a NAME (user decision 2026-09-04, todo item 4a):
# anonymous rooftop panels (power=generator, 277 k solar) and street cabinets
# (power=substation minor_distribution) are infrastructure objects, not companies. A
# register merge (INDUSTRIAL_SOURCES) makes the row industrial regardless.
# Structures (tanks, silos, pipelines, chimneys, shafts, wells, pumping stations) joined
# the named-only group on 2026-09-16: an anonymous tank is an object on somebody's site,
# a named one ("Getreidesilo Hansa", "Pumpwerk Seehausen") is a site worth keeping.
INDUSTRIAL_TAGS_NAMED_ONLY: dict[str, set[str]] = {
    "power": {"generator", "substation"},
    "man_made": {"storage_tank", "silo", "pipeline", "chimney", "gasometer", "mineshaft",
                 "petroleum_well", "pumping_station"},
}
INDUSTRIAL_NACE_SECTIONS = {"B", "C", "D", "E", "F"}

# (Overture release pin lives with the B3 constants above.)

# --- Attribution (spec §11) -------------------------------------------------------------
ATTRIBUTION = (
    "© OpenStreetMap contributors (ODbL); Overture Maps (CDLA-P-2.0); "
    "Foursquare OS Places (Apache-2.0); Destatis/BNetzA/BfEE (DL-DE-BY-2.0)"
)
LICENCE_NOTE = "merged table is an ODbL derivative database"
