"""Abwärme (waste-heat) platform adapter — spec §B2, source catalogue #3.

BfEE `pfa_datentabelle_excel.xlsx` (EnEfG §17 reports) → canonical Company frame.
The workbook has one row per waste-heat POTENTIAL; this adapter aggregates to one
row per SITE (company + street + PLZ) and geocodes the address (spec §4.1 — the
source has no coordinates). The adapter does NOT do entity resolution itself.

Data facts (v28 workbook): 23 936 potential rows → 3 704 companies at 6 222 sites;
addresses 100 % complete; e-mail/phone present. `business_type` stays null — the
EnEfG obligation says nothing about what kind of business reports.

Debug columns: abw_heat_mwh_a (sum), abw_power_kw (sum of max thermal power),
abw_temp_c (heat-weighted mean), abw_potentials (count), abw_potential_names,
abw_site_name, geocode_source, geocode_precision, dedup_anchor (False for
postcode/city-precision rows — they must not anchor dedup, spec §B2).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from .. import config, download, geocode, paths, schema

_COLS = {
    "uid": "Unternehmens- ID",
    "name": "Firmenname",
    "site": "Standortname",
    "street": "Straße und Hausnummer",
    "plz": "PLZ",
    "city": "Ort",
    "pot_name": "Name des Abwärmepotentials",
    "heat_kwh": "Wärmemenge pro Jahr (in kWh/a)",
    "power_kw": "Maximale thermische Leistung (in kW)",
    "temp_c": "Durchschnittliches Temperaturniveau (in °C)",
    "email": "E-Mail-Adresse",
    "phone": "Telefonnummer",
}


def _site_id(uid, street, plz) -> str:
    digest = hashlib.md5(f"{street}|{plz}".lower().encode("utf-8")).hexdigest()[:8]
    return f"abw_{uid}_{digest}"


def read_workbook(xlsx: Path) -> pd.DataFrame:
    """Parse the two-row-header sheet and normalize the column names we use."""
    df = pd.read_excel(xlsx, sheet_name=config.ABWAERME_SHEET, header=1)
    df.columns = [str(c).replace("\n", " ") for c in df.columns]
    missing = [v for v in _COLS.values() if v not in df.columns]
    if missing:
        raise KeyError(f"Abwärme workbook is missing expected columns: {missing}")
    # some cells carry zero-width/BOM characters (e.g. "​HIT-Pack …")
    name_col = _COLS["name"]
    df[name_col] = (df[name_col].astype("string")
                    .str.replace(r"[​‌‍﻿]", "", regex=True)
                    .str.strip())
    return df


def aggregate_sites(df: pd.DataFrame) -> pd.DataFrame:
    """One row per site (uid + street + PLZ), potentials aggregated."""
    c = _COLS
    df = df.copy()
    for col in (c["heat_kwh"], c["power_kw"], c["temp_c"]):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["_key"] = (df[c["uid"]].astype(str) + "|"
                  + df[c["street"]].astype(str).str.lower().str.strip() + "|"
                  + df[c["plz"]].astype(str).str.strip())

    def _agg(g: pd.DataFrame) -> pd.Series:
        heat = g[c["heat_kwh"]]
        temp = g[c["temp_c"]]
        weights = heat.where(heat > 0)
        if weights.notna().any() and temp.notna().any():
            mask = weights.notna() & temp.notna()
            temp_avg = float(np.average(temp[mask], weights=weights[mask])) \
                if mask.any() else float(temp.mean())
        else:
            temp_avg = float(temp.mean()) if temp.notna().any() else None
        names = [str(x) for x in g[c["pot_name"]].dropna().unique()]
        return pd.Series({
            "uid": g[c["uid"]].iloc[0],
            "name": g[c["name"]].iloc[0],
            "site_name": g[c["site"]].dropna().iloc[0] if g[c["site"]].notna().any() else None,
            "street": g[c["street"]].iloc[0],
            "plz": str(g[c["plz"]].iloc[0]).strip(),
            "city": g[c["city"]].iloc[0],
            "email": g[c["email"]].dropna().iloc[0] if g[c["email"]].notna().any() else None,
            "phone": g[c["phone"]].dropna().iloc[0] if g[c["phone"]].notna().any() else None,
            "heat_mwh_a": round(float(heat.sum()) / 1000.0, 1) if heat.notna().any() else None,
            "power_kw": round(float(g[c["power_kw"]].sum()), 1)
                if g[c["power_kw"]].notna().any() else None,
            "temp_c": round(temp_avg, 1) if temp_avg is not None else None,
            "potentials": int(len(g)),
            "potential_names": "|".join(names[:5]),
        })

    return df.groupby("_key", sort=False).apply(_agg, include_groups=False).reset_index(drop=True)


def extract_abwaerme(data_root: Path, force: bool = False) -> gpd.GeoDataFrame:
    """Download (cached) + aggregate + geocode the Abwärme platform; cached."""
    dest = paths.source_parquet(data_root, "abwaerme", "DE")
    if dest.exists() and not force:
        print(f"[skip] {dest.name} exists")
        return gpd.read_parquet(dest)

    xlsx = download.download_file(
        config.ABWAERME_URL, paths.raw_dir(data_root) / "abwaerme" / "pfa_datentabelle.xlsx",
        force=force)
    raw = read_workbook(xlsx)
    sites = aggregate_sites(raw)
    print(f"[abwaerme] {len(raw)} potential rows → {len(sites)} sites "
          f"({sites['name'].nunique()} companies)")

    coder = geocode.Geocoder(data_root)
    lats, lons, precs, states = [], [], [], []
    n_cached0 = len(coder.cache)
    for k, r in enumerate(sites.itertuples(index=False)):
        res = coder.geocode(r.street, r.plz, r.city)
        lats.append(res.latitude); lons.append(res.longitude)
        precs.append(res.precision); states.append(res.state)
        if (k + 1) % 250 == 0:
            print(f"[geocode] {k + 1}/{len(sites)} addresses "
                  f"({len(coder.cache) - n_cached0} fresh lookups)")
    coder.save()
    prec_counts = pd.Series(precs).value_counts(dropna=False).to_dict()
    print(f"[geocode] precision distribution: {prec_counts}")

    out = pd.DataFrame({
        "id": [_site_id(u, s, p) for u, s, p in zip(sites.uid, sites.street, sites.plz)],
        "name": sites["name"].astype("string"),
        "address_street": sites.street.astype("string"),
        "address_postcode": sites.plz.astype("string"),
        "address_city": sites.city.astype("string"),
        "address_full": (sites.street.astype("string") + ", "
                         + sites.plz.astype("string") + " " + sites.city.astype("string")),
        "state": pd.Series(states, dtype="string"),
        "email": sites.email.astype("string"),
        "phone": sites.phone.astype("string"),
        "latitude": pd.array(lats, dtype="Float64"),
        "longitude": pd.array(lons, dtype="Float64"),
        "source": "abwaerme",
        # --- debug columns (after the contract, spec §3.4) ---
        "abw_heat_mwh_a": pd.array(sites.heat_mwh_a, dtype="Float64"),
        "abw_power_kw": pd.array(sites.power_kw, dtype="Float64"),
        "abw_temp_c": pd.array(sites.temp_c, dtype="Float64"),
        "abw_potentials": pd.array(sites.potentials, dtype="Int64"),
        "abw_potential_names": sites.potential_names.astype("string"),
        "abw_site_name": sites.site_name.astype("string"),
        "geocode_source": "nominatim",
        "geocode_precision": pd.Series(precs, dtype="string"),
    })
    n_miss = int(out["latitude"].isna().sum())
    if n_miss:
        print(f"[abwaerme] dropping {n_miss} sites that could not be geocoded at all")
        out = out[out["latitude"].notna()].reset_index(drop=True)
    out["dedup_anchor"] = ~out["geocode_precision"].isin(sorted(config.GEOCODE_COARSE))

    gdf = gpd.GeoDataFrame(
        out, geometry=gpd.points_from_xy(out["longitude"], out["latitude"]),
        crs=config.CRS_STORAGE)
    gdf = schema.conform(gdf)
    schema.validate_frame(gdf, source="abwaerme")
    gdf.to_parquet(dest)
    print(f"[abwaerme] {len(gdf)} sites → {dest.name}")
    return gdf
