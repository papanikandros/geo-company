"""MaStR (Marktstammdatenregister) adapter — spec §B5, source catalogue #6.

BNetzA bulk export (via open-mastr into ``data/raw/mastr/mastr.db``) → canonical
Company frame. Unit tables (user-approved selection 2026-08-27: all generation
technologies incl. nuclear, storage, gas producer/consumer/storage, large
electricity consumers) aggregate to ONE row per (operator, Lokation). Operators
that are natural persons are dropped (anonymized — not companies). Status filter
"In Betrieb" everywhere except nuclear (fleet shut down since 2023 — the sites
and operators still exist; status kept as debug). PV and Speicher only
≥ 100 kW (spec §B5 + amendment). The adapter does NOT do entity resolution.

Operator master data comes from ``market_actors``: legal name (Firmenname),
Rechtsform → legal_form, Registernummer/-gericht → hr_registration/hr_court,
contact columns, and the WZ economic-activity code (Hauptwirtdschaftszweig*,
register spelling) kept as debug for Part C's register classification.

business_type: from the operator's registered WZ section when known (80.8 %):
Energieversorgung → power, manufacturing/mining/water-waste/agriculture/logistics →
industrial, Handel → shop, Bau → craft, hospitality/health/education/culture → amenity,
other services → office; otherwise the technology rule ("power" for generation/storage,
"industrial" for pure consumer sites). Method recorded in mastr_business_type_method.
business_subtype is NULL for MaStR rows — never the technology (user decision
2026-09-03) and not the WZ either (user decision 2026-09-04: the operator's WZ 2025
classification lives in mastr_wz_abschnitt / mastr_wz_gruppe / mastr_wz_code and is shown
as its own "mastr_wz" line in the preview). MaStR WZ labels are WZ 2025 (sections A–V),
NOT WZ 2008. mastr_wz_code (3-digit group, "35.1") comes from the Destatis structure file
config.MASTR_WZ2025_XLSX + config.MASTR_WZ_GROUP_ALIASES.
One row per (operator, Lokation) — same-operator Lokationen are NOT merged (user decision
2026-09-01; cross-source entity resolution may still link them).

Capacities are captured PER UNIT, one-to-one, in mastr_tech_detail (JSON, grouped by
technology, every MaStR power/energy field verbatim under its MaStR column name) — no
sums across units or technologies, ever (user decision 2026-09-03: generation kW,
storage kWh and gas kW are different physical quantities).

Generation units below 50 kW are dropped (MaStR publishes exact coordinates only from
50 kW; below that are private households — user decision 2026-08-31). Unit coordinates
outside the sanity bbox or > 10 km outside their declared Landkreis (VG5000) are treated
as missing (acceptance checks 2026-08-31: ~1–3 % per technology, placeholder values such
as (5.0, 47.0)) — the site then takes the address geocode, with provenance.

Debug columns: mastr_tech_detail, mastr_units, mastr_techs, mastr_status,
mastr_commissioned, mastr_site_name, mastr_wz_* (labels), mastr_wz_code (WZ 2025 group
code), geocode_* (only for the
minority of sites without published coordinates), mastr_coord_method
(mastr_published | address_geocode | address_geocode_{bbox,district}_mismatch |
address_geocode_plz_town_only — the last one is NOT site-accurate),
mastr_coord_dropped_units, mastr_business_type_method, dedup_anchor.

Side product (not part of the Company table): ``extract_grid_layer`` builds
``grid_connections_DE_4326.parquet`` — grid connection points geolocated via
Lokation → linked units, attributed with network name/Sparte/voltage level.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from .. import config, geocode, paths, schema

# per-table quirks: unit-name column candidates, kW floor (on Bruttoleistung), status
# handling, and the per-unit capacity fields captured VERBATIM (MaStR column names; units
# per the MaStR data model: *leistung → kW, Speicherkapazitaet/Arbeitsgasvolumen → kWh).
_GEN_FIELDS = ["Bruttoleistung", "Nettonennleistung"]
_TABLE_SPEC: dict[str, dict] = {
    "wind_extended": {"names": ["NameWindpark", "NameStromerzeugungseinheit"],
                      "min_kw": config.MASTR_MIN_KW, "fields": _GEN_FIELDS},
    "solar_extended": {"names": ["NameStromerzeugungseinheit"],
                       "min_kw": config.MASTR_MIN_PV_KW, "fields": _GEN_FIELDS},
    "biomass_extended": {"names": ["NameStromerzeugungseinheit"],
                         "min_kw": config.MASTR_MIN_KW, "fields": _GEN_FIELDS},
    "hydro_extended": {"names": ["NameStromerzeugungseinheit"],
                       "min_kw": config.MASTR_MIN_KW, "fields": _GEN_FIELDS},
    "storage_extended": {"names": ["NameStromerzeugungseinheit"],
                         "min_kw": config.MASTR_MIN_STORAGE_KW,
                         "fields": _GEN_FIELDS + ["NutzbareSpeicherkapazitaet",
                                                  "LeistungsaufnahmeBeimEinspeichern"]},
    "combustion_extended": {"names": ["NameKraftwerk", "NameStromerzeugungseinheit"],
                            "min_kw": config.MASTR_MIN_KW, "fields": _GEN_FIELDS},
    "gsgk_extended": {"names": ["NameStromerzeugungseinheit"],
                      "min_kw": config.MASTR_MIN_KW, "fields": _GEN_FIELDS},
    "nuclear_extended": {"names": ["NameKraftwerk", "NameStromerzeugungseinheit"],
                         "keep_all_status": True, "fields": _GEN_FIELDS},
    "gas_producer": {"names": ["NameGaserzeugungseinheit"], "fields": ["Erzeugungsleistung"]},
    "gas_consumer": {"names": ["NameGasverbrauchsseinheit"],
                     "fields": ["MaximaleGasbezugsleistung"]},
    "gas_storage_extended": {"names": ["NameStromerzeugungseinheit", "NameGasspeichereinheit"],
                             "fields": ["MaximaleEinspeicherleistung",
                                        "MaximaleAusspeicherleistung",
                                        "MaximalNutzbaresArbeitsgasvolumen"]},
    "electricity_consumer": {"names": ["NameStromverbrauchseinheit"], "fields": []},
}
_CONSUMER_SUBTYPES = {"gas_consumption", "electricity_consumption"}
_STATUS_OK = {"In Betrieb", "InBetrieb", "35"}

_UNIT_BASE_COLS = [
    "EinheitMastrNummer", "AnlagenbetreiberMastrNummer", "LokationMastrNummer",
    "EinheitBetriebsstatus", "Bruttoleistung", "Laengengrad", "Breitengrad",
    "Strasse", "Hausnummer", "Postleitzahl", "Ort", "Bundesland", "Landkreis",
    "Gemeindeschluessel", "Inbetriebnahmedatum",
]


def _business_type_from_wz(section_label) -> str | None:
    """Map the operator's WZ section label (e.g. "Abschnitt C – Verarbeitendes Gewerbe")
    to the contract's business_type vocabulary; None when unknown/missing."""
    if section_label is None or pd.isna(section_label):
        return None
    label = str(section_label).lower()
    for keyword, business_type in config.MASTR_WZ_KEYWORD_TO_BUSINESS_TYPE:
        if keyword in label:
            return business_type
    return None


def _norm_wz_label(label) -> str:
    """Normalisation shared by the Destatis titles and the MaStR labels: NFKC, lowercase,
    unify dashes, drop punctuation, collapse whitespace."""
    t = unicodedata.normalize("NFKC", str(label)).lower().replace("–", "-").replace("—", "-")
    t = re.sub(r"\s*-\s*", "-", t)
    t = re.sub(r"[^\w]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _load_wz_groups(data_root: Path) -> dict[str, str] | None:
    """Normalised WZ 2025 group title → 3-digit code ("35.1"), from the Destatis structure
    file plus config.MASTR_WZ_GROUP_ALIASES. None (with a warning) when the file is absent —
    mastr_wz_code then stays NULL."""
    xlsx = data_root / config.MASTR_WZ2025_XLSX
    if not xlsx.exists():
        print(f"[mastr] WARNING: {xlsx} missing — mastr_wz_code stays NULL "
              f"(download per mastr-refactor-plan.md §A4)")
        return None
    wz = pd.read_excel(xlsx, sheet_name=config.MASTR_WZ2025_SHEET)
    wz.columns = ["level", "code", "title"]
    groups = wz[wz["level"] == 3]
    lut = {_norm_wz_label(t): str(c) for c, t in zip(groups["code"], groups["title"])}
    for label, code in config.MASTR_WZ_GROUP_ALIASES.items():
        lut[_norm_wz_label(label)] = code
    return lut


def _wz_group_code(label, lut: dict[str, str] | None) -> str | None:
    """3-digit WZ 2025 group code ("35.1") for one MaStR group label; None when unknown."""
    if label is None or pd.isna(label) or not lut:
        return None
    return lut.get(_norm_wz_label(str(label).strip()))


def _load_districts(data_root: Path) -> gpd.GeoDataFrame | None:
    shp = data_root / config.MASTR_DISTRICTS_SHP
    if not shp.exists():
        print(f"[mastr] WARNING: {shp} missing — point-in-declared-district check skipped "
              f"(only the bbox sanity check applies)")
        return None
    return gpd.read_file(shp)[["AGS", "geometry"]].to_crs(config.CRS_STORAGE)


def _drop_implausible_coords(units: pd.DataFrame,
                             districts: gpd.GeoDataFrame | None) -> pd.DataFrame:
    """Blank unit coordinates that cannot be right, so the site falls back to the
    register address. ``_coord_dropped`` records why: "bbox" (outside the LAT/LON sanity
    bounds) or "district" (> MASTR_DISTRICT_MISMATCH_KM outside the declared Landkreis).
    """
    units = units.copy()
    units["_coord_dropped"] = pd.Series(pd.NA, index=units.index, dtype="string")
    lat = pd.to_numeric(units["Breitengrad"], errors="coerce")
    lon = pd.to_numeric(units["Laengengrad"], errors="coerce")
    has = lat.notna() & lon.notna()
    bbox_bad = has & ~(lat.between(*config.LAT_BOUNDS) & lon.between(*config.LON_BOUNDS))
    units.loc[bbox_bad, "_coord_dropped"] = "bbox"

    if districts is not None and "Gemeindeschluessel" in units.columns:
        ags = units["Gemeindeschluessel"].astype("string").str.strip()
        cand = has & ~bbox_bad & ags.str.match(r"^\d{8}$", na=False)
        if cand.any():
            pts = gpd.GeoDataFrame(
                {"AGS": ags[cand].str[:5].astype(object)},
                geometry=gpd.points_from_xy(lon[cand], lat[cand]), crs=config.CRS_STORAGE,
                index=units.index[cand])
            poly = districts.set_index("AGS").geometry
            known = pts["AGS"].isin(poly.index)
            pts = pts[known]
            declared = gpd.GeoSeries(poly.loc[pts["AGS"]].values, crs=config.CRS_STORAGE,
                                     index=pts.index)
            with warnings.catch_warnings():  # degree buffer is deliberate (reference test does the same)
                warnings.simplefilter("ignore", UserWarning)
                buffered = declared.buffer(config.MASTR_DISTRICT_BUFFER_DEG)
            outside = ~pts.geometry.within(buffered, align=True)
            if outside.any():
                p_m = pts.geometry[outside].to_crs(config.CRS_METRIC)
                d_m = declared[outside].to_crs(config.CRS_METRIC)
                dist_km = p_m.distance(d_m, align=True) / 1000.0
                far = dist_km.index[dist_km > config.MASTR_DISTRICT_MISMATCH_KM]
                units.loc[far, "_coord_dropped"] = "district"

    dropped = units["_coord_dropped"].notna()
    if dropped.any():
        print(f"[mastr] coordinates dropped as implausible: "
              f"{units.loc[dropped, '_coord_dropped'].value_counts().to_dict()} "
              f"of {int(has.sum())} located units → address geocode fallback")
        units.loc[dropped, ["Laengengrad", "Breitengrad"]] = np.nan
    return units


def _table_cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _read_units(con: sqlite3.Connection, table: str, spec: dict) -> pd.DataFrame | None:
    have = set(_table_cols(con, table))
    if not have:
        print(f"[mastr] table {table} missing in the bulk db — skipped")
        return None
    name_col = next((c for c in spec.get("names", []) if c in have), None)
    fields = [c for c in spec.get("fields", []) if c in have]
    cols = [c for c in _UNIT_BASE_COLS if c in have]
    cols += [c for c in fields if c not in cols]
    if name_col:
        cols.append(name_col)
    df = pd.read_sql(f'SELECT {", ".join(f"`{c}`" for c in cols)} FROM {table}', con)
    df["_site_name"] = df[name_col].astype("string") if name_col else pd.NA
    # the kW floor applies to Bruttoleistung only (consumer tables have no floor)
    df["_kw"] = pd.to_numeric(df.get("Bruttoleistung"), errors="coerce")
    df["_tech"] = config.MASTR_TABLES[table]
    # per-unit capacity fields, verbatim, non-null only — the one-to-one record that
    # mastr_tech_detail carries; nothing is ever summed
    num = {c: pd.to_numeric(df[c], errors="coerce") for c in fields}
    df["_fields"] = [
        {c: float(num[c].iat[i]) for c in fields if pd.notna(num[c].iat[i])}
        for i in range(len(df))
    ]

    if not spec.get("keep_all_status") and "EinheitBetriebsstatus" in df.columns:
        n0 = len(df)
        df = df[df["EinheitBetriebsstatus"].astype(str).isin(_STATUS_OK)]
        if len(df) == 0 and n0 > 0:
            seen = pd.read_sql(
                f"SELECT DISTINCT EinheitBetriebsstatus FROM {table} LIMIT 8", con)
            raise ValueError(
                f"[mastr] status filter matched 0 of {n0} rows in {table} — "
                f"observed values: {seen.iloc[:, 0].tolist()} (adjust _STATUS_OK)")
    if "min_kw" in spec:
        df = df[df["_kw"] >= spec["min_kw"]]
    return df.reset_index(drop=True)


def _read_operators(con: sqlite3.Connection) -> pd.DataFrame:
    cols = ["MastrNummer", "Firmenname", "Personenart", "Rechtsform", "Registernummer",
            "Registergericht", "Webseite", "Email", "Telefon",
            "HauptwirtdschaftszweigAbschnitt", "HauptwirtdschaftszweigAbteilung",
            "HauptwirtdschaftszweigGruppe"]
    have = set(_table_cols(con, "market_actors"))
    cols = [c for c in cols if c in have]
    ops = pd.read_sql(
        f'SELECT {", ".join(f"`{c}`" for c in cols)} FROM market_actors', con)
    n0 = len(ops)
    natural = ops["Personenart"].astype(str).str.lower().str.contains("natürliche")
    ops = ops[~natural & ops["Firmenname"].notna()].reset_index(drop=True)
    print(f"[mastr] market actors: {n0} → {len(ops)} legal entities "
          f"({n0 - len(ops)} natural persons/unnamed dropped)")
    return ops


def _aggregate_sites(units: pd.DataFrame) -> pd.DataFrame:
    units = units.copy()
    units["_lok"] = units["LokationMastrNummer"].fillna(units["EinheitMastrNummer"])
    units["_key"] = units["AnlagenbetreiberMastrNummer"].astype(str) + "|" + units["_lok"].astype(str)

    def _first(s: pd.Series):
        nn = s.dropna()
        return nn.iloc[0] if len(nn) else None

    def _tech_detail(g: pd.DataFrame) -> str:
        """{"wind": [{"unit": "SEE…", "Bruttoleistung": 3000.0, …}, …], …} — one entry
        per unit, MaStR field names verbatim, units ordered by MaStR number."""
        detail: dict[str, list[dict]] = {}
        order = sorted(range(len(g)), key=lambda k: str(g["EinheitMastrNummer"].iat[k]))
        for k in order:
            rec = {"unit": g["EinheitMastrNummer"].iat[k]}
            rec.update(g["_fields"].iat[k] or {})
            detail.setdefault(g["_tech"].iat[k], []).append(rec)
        return json.dumps(dict(sorted(detail.items())), ensure_ascii=False)

    def _agg(g: pd.DataFrame) -> pd.Series:
        techs = sorted(g["_tech"].unique())
        lat = pd.to_numeric(g["Breitengrad"], errors="coerce")
        lon = pd.to_numeric(g["Laengengrad"], errors="coerce")
        return pd.Series({
            "operator": g["AnlagenbetreiberMastrNummer"].iloc[0],
            "lokation": g["_lok"].iloc[0],
            "techs": "+".join(techs),
            "is_consumer_only": set(techs) <= _CONSUMER_SUBTYPES,
            "tech_detail": _tech_detail(g),
            "units": int(len(g)),
            "site_name": _first(g["_site_name"]),
            "status": _first(g.get("EinheitBetriebsstatus")),
            "commissioned": str(_first(g.get("Inbetriebnahmedatum")) or "")[:4] or None,
            "latitude": float(lat.mean()) if lat.notna().any() else None,
            "longitude": float(lon.mean()) if lon.notna().any() else None,
            "street": _first(g.get("Strasse")),
            "housenumber": _first(g.get("Hausnummer")),
            "postcode": _first(g.get("Postleitzahl")),
            "city": _first(g.get("Ort")),
            "state": _first(g.get("Bundesland")),
            "district": _first(g.get("Landkreis")),
            "coord_dropped": int(g["_coord_dropped"].notna().sum()) if "_coord_dropped" in g else 0,
            "coord_dropped_why": _first(g["_coord_dropped"]) if "_coord_dropped" in g else None,
        })

    return (units.groupby("_key", sort=False)
            .apply(_agg, include_groups=False).reset_index(drop=True))


def extract_mastr(data_root: Path, force: bool = False) -> gpd.GeoDataFrame:
    """Aggregate the bulk db to company sites; geocode the coordinate-less rest."""
    dest = paths.source_parquet(data_root, "mastr", "DE")
    if dest.exists() and not force:
        print(f"[skip] {dest.name} exists")
        return gpd.read_parquet(dest)

    db = data_root / config.MASTR_DB
    if not db.exists():
        raise FileNotFoundError(
            f"{db} missing — run the open-mastr bulk download first "
            f"(data/raw/mastr/download_probe.py)")
    con = sqlite3.connect(db)

    parts = []
    for table, spec in _TABLE_SPEC.items():
        df = _read_units(con, table, spec)
        if df is not None and len(df):
            print(f"[mastr] {table}: {len(df)} units kept")
            parts.append(df)
    units = pd.concat(parts, ignore_index=True)
    ops = _read_operators(con)
    con.close()

    n0 = len(units)
    units = units[units["AnlagenbetreiberMastrNummer"].isin(set(ops["MastrNummer"]))]
    print(f"[mastr] {n0} units → {len(units)} operated by legal entities")
    units = _drop_implausible_coords(units, _load_districts(data_root))
    sites = _aggregate_sites(units)
    print(f"[mastr] {len(sites)} sites ({sites['operator'].nunique()} operators)")

    ops = ops.set_index("MastrNummer")
    for src, dst in [("Firmenname", "op_name"), ("Rechtsform", "op_rechtsform"),
                     ("Registernummer", "op_regnr"), ("Registergericht", "op_gericht"),
                     ("Webseite", "op_web"), ("Email", "op_email"), ("Telefon", "op_tel"),
                     ("HauptwirtdschaftszweigAbschnitt", "op_wz_abschnitt"),
                     ("HauptwirtdschaftszweigAbteilung", "op_wz_abteilung"),
                     ("HauptwirtdschaftszweigGruppe", "op_wz_gruppe")]:
        if src in ops.columns:
            sites[dst] = sites["operator"].map(ops[src])
        else:
            sites[dst] = None

    # geocode only sites without published coordinates. With a street → house/street
    # precision; without one (user decision 2026-09-01) → PLZ + town only, which is NOT
    # site-accurate: flagged in mastr_coord_method, geocode_precision postcode/city, and
    # excluded from dedup anchoring (GEOCODE_COARSE).
    has_street = sites["street"].notna()
    has_plz_town = sites["postcode"].notna() & sites["city"].notna()
    todo = sites["latitude"].isna() & (has_street | has_plz_town)
    precs = pd.Series([None] * len(sites), index=sites.index, dtype="object")
    if todo.any():
        print(f"[mastr] geocoding {int(todo.sum())} sites without coordinates "
              f"({int((todo & ~has_street).sum())} PLZ+town only)")
        coder = geocode.Geocoder(data_root)
        for i in sites.index[todo]:
            r = sites.loc[i]
            if pd.notna(r.street):
                street = f"{r.street} {r.housenumber}".strip() if pd.notna(r.housenumber) else r.street
            else:
                street = ""
            res = coder.geocode(street, r.postcode, r.city)
            sites.at[i, "latitude"] = res.latitude
            sites.at[i, "longitude"] = res.longitude
            precs.at[i] = res.precision
        coder.save()
        print(f"[geocode] precision distribution: "
              f"{precs[todo].value_counts(dropna=False).to_dict()}")

    # provenance of the site coordinate (spec: every derived value carries its method)
    why = sites["coord_dropped_why"].astype("string")
    coord_method = pd.Series(
        np.where(~todo, "mastr_published",
                 np.where(~has_street, "address_geocode_plz_town_only",
                          np.where(why.notna(),
                                   "address_geocode_" + why.fillna("") + "_mismatch",
                                   "address_geocode"))),
        index=sites.index, dtype="string")
    coord_method[sites["latitude"].isna()] = pd.NA

    # business_type: operator's registered economic activity first, technology rule as
    # fallback (user decision 2026-09-01 — see config.MASTR_WZ_KEYWORD_TO_BUSINESS_TYPE)
    bt_wz = sites["op_wz_abschnitt"].map(_business_type_from_wz)
    bt_tech = np.where(sites["is_consumer_only"], "industrial", "power")
    business_type = pd.Series(np.where(bt_wz.notna(), bt_wz, bt_tech),
                              index=sites.index, dtype="string")
    bt_method = pd.Series(np.where(bt_wz.notna(), "wz_section", "tech"),
                          index=sites.index, dtype="string")
    # WZ 2025 group code (debug column; the classification itself never enters the
    # contract fields — business_subtype stays NULL for MaStR rows)
    wz_groups = _load_wz_groups(data_root)
    wz_code = pd.Series([_wz_group_code(lbl, wz_groups) for lbl in sites["op_wz_gruppe"]],
                        index=sites.index, dtype="string")
    n_lbl = int(sites["op_wz_gruppe"].notna().sum())
    if n_lbl:
        n_code = int(wz_code.notna().sum())
        print(f"[mastr] WZ group code resolved for {n_code} of {n_lbl} sites with a group label")
        if n_code < n_lbl:
            miss = sites.loc[sites["op_wz_gruppe"].notna() & wz_code.isna(), "op_wz_gruppe"]
            print(f"[mastr] WARNING: unresolved WZ group labels (add to "
                  f"config.MASTR_WZ_GROUP_ALIASES): {miss.value_counts().head(10).to_dict()}")

    out = pd.DataFrame({
        "id": "mastr_" + sites["operator"].astype(str) + "_" + sites["lokation"].astype(str),
        "name": sites["op_name"].astype("string"),
        "business_type": business_type,
        "business_subtype": pd.Series(pd.NA, index=sites.index, dtype="string"),
        "address_street": sites["street"].astype("string"),
        "address_housenumber": sites["housenumber"].astype("string"),
        "address_postcode": sites["postcode"].astype("string"),
        "address_city": sites["city"].astype("string"),
        "state": sites["state"].astype("string"),
        "district": sites["district"].astype("string"),
        "website": sites["op_web"].astype("string"),
        "email": sites["op_email"].astype("string"),
        "phone": sites["op_tel"].astype("string"),
        "legal_form": sites["op_rechtsform"].astype("string"),
        "hr_registration": sites["op_regnr"].astype("string"),
        "hr_court": sites["op_gericht"].astype("string"),
        "latitude": pd.array(sites["latitude"], dtype="Float64"),
        "longitude": pd.array(sites["longitude"], dtype="Float64"),
        "source": "mastr",
        # --- debug columns (after the contract, spec §3.4) ---
        "mastr_tech_detail": sites["tech_detail"].astype("string"),
        "mastr_units": pd.array(sites["units"], dtype="Int64"),
        "mastr_techs": sites["techs"].astype("string"),
        "mastr_status": sites["status"].astype("string"),
        "mastr_commissioned": sites["commissioned"].astype("string"),
        "mastr_site_name": sites["site_name"].astype("string"),
        "mastr_wz_abschnitt": sites["op_wz_abschnitt"].astype("string"),
        "mastr_wz_abteilung": sites["op_wz_abteilung"].astype("string"),
        "mastr_wz_gruppe": sites["op_wz_gruppe"].astype("string"),
        "mastr_wz_code": wz_code,
        "geocode_precision": precs.astype("string"),
        "mastr_coord_method": coord_method,
        "mastr_coord_dropped_units": pd.array(sites["coord_dropped"], dtype="Int64"),
        "mastr_business_type_method": bt_method,
    })
    addr = out["address_street"].fillna("")
    hn = out["address_housenumber"].fillna("")
    plz_city = (out["address_postcode"].fillna("") + " " + out["address_city"].fillna("")).str.strip()
    out["address_full"] = ((addr + " " + hn).str.strip() + ", " + plz_city).str.strip(", ") \
        .replace("", None)

    n_miss = int(out["latitude"].isna().sum())
    if n_miss:
        print(f"[mastr] dropping {n_miss} sites without coordinates or geocode")
        out = out[out["latitude"].notna()].reset_index(drop=True)
    out["dedup_anchor"] = ~out["geocode_precision"].isin(sorted(config.GEOCODE_COARSE))

    gdf = gpd.GeoDataFrame(
        out, geometry=gpd.points_from_xy(out["longitude"], out["latitude"]),
        crs=config.CRS_STORAGE)
    gdf = schema.conform(gdf)
    schema.validate_frame(gdf, source="mastr")
    gdf.to_parquet(dest)
    print(f"[mastr] {len(gdf)} sites → {dest.name}")
    return gdf


def extract_grid_layer(data_root: Path, force: bool = False) -> gpd.GeoDataFrame:
    """Side product: grid connection points geolocated via Lokation → linked units.

    Written to data/geoextract/grid_connections_DE_4326.parquet — an infrastructure
    layer for the preview/map, NOT part of the Company table.
    """
    dest = paths.merged_parquet(data_root, "DE").parent / "grid_connections_DE_4326.parquet"
    if dest.exists() and not force:
        print(f"[skip] {dest.name} exists")
        return gpd.read_parquet(dest)

    db = data_root / config.MASTR_DB
    con = sqlite3.connect(db)
    gcs = pd.read_sql(
        "SELECT NetzanschlusspunktMastrNummer, NetzanschlusspunktBezeichnung, "
        "LokationMastrNummer, Lokationtyp, Spannungsebene, MaximaleEinspeiseleistung, "
        "MaximaleAusspeiseleistung, Netzanschlusskapazitaet, NetzMastrNummer "
        "FROM grid_connections", con)
    grids = pd.read_sql("SELECT MastrNummer, Bezeichnung, Sparte FROM grids", con)

    # locate each connection point at the mean coordinate of its Lokation's units
    coords = []
    for table in _TABLE_SPEC:
        if _table_cols(con, table):
            coords.append(pd.read_sql(
                f"SELECT LokationMastrNummer, Laengengrad, Breitengrad FROM {table} "
                f"WHERE Laengengrad IS NOT NULL", con))
    con.close()
    lok = (pd.concat(coords, ignore_index=True)
           .groupby("LokationMastrNummer")[["Laengengrad", "Breitengrad"]].mean())

    gcs = gcs.join(lok, on="LokationMastrNummer")
    gcs = gcs.merge(grids, left_on="NetzMastrNummer", right_on="MastrNummer", how="left")
    n0 = len(gcs)
    gcs = gcs[gcs["Laengengrad"].notna()].reset_index(drop=True)
    print(f"[mastr/grid] {n0} connection points, {len(gcs)} locatable via units")

    gdf = gpd.GeoDataFrame(
        gcs.rename(columns={
            "NetzanschlusspunktMastrNummer": "id",
            "NetzanschlusspunktBezeichnung": "name",
            "Bezeichnung": "netz_name", "Sparte": "sparte",
            "Spannungsebene": "spannungsebene",
            "MaximaleEinspeiseleistung": "max_einspeisung_kw",
            "MaximaleAusspeiseleistung": "max_ausspeisung_kw",
            "Netzanschlusskapazitaet": "kapazitaet_kw",
            "Laengengrad": "longitude", "Breitengrad": "latitude",
        }).drop(columns=["MastrNummer", "NetzMastrNummer"], errors="ignore"),
        geometry=gpd.points_from_xy(gcs["Laengengrad"], gcs["Breitengrad"]),
        crs=config.CRS_STORAGE)
    gdf.to_parquet(dest)
    print(f"[mastr/grid] → {dest.name}")
    return gdf
