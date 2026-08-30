"""IED installations adapter — spec §B1, source catalogue #2.

THRU.de EU-Registry workbook → canonical Company frame (EPSG:4326, points).
One row per installation; A3 collapses same-operator installations within 50 m.
The adapter does NOT do entity resolution itself (spec §6).

Data facts (2026-04 workbook): 13 422 IED installations for Berichtsjahr 2024,
100 % with ETRS89 coordinates (≈ WGS84 across Germany), 100 % named + addressed.
Coordinate columns are name-shifting (`..._wgs84` vs `..._ETRS89`) — matched by prefix.
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

from .. import config, download, paths, schema

# Annex-I main activity + installation details kept as debug columns (after the
# contract): classifier context for C5 and §6.5.
_DEBUG_RENAMES = {
    "IE_RL_Haupttaetigkeit_Nr_Anh_I": "ied_activity",
    "Name.Anlage": "ied_installation_name",
    "Status.Anlage": "ied_status",
    "InspireID.Betrieb": "ied_site_id",
    "Muttergesellschaft": "ied_parent_company",
    "Berichtsjahr": "ied_report_year",
}


def _coord_col(df: pd.DataFrame, kind: str) -> str:
    """Find the lat/long column by prefix — THRU.de renames `_wgs84` ↔ `_ETRS89`."""
    prefix = f"Koordinaten.Anlage_geo_{kind}_"
    for col in df.columns:
        if str(col).startswith(prefix):
            return col
    found = [c for c in df.columns if "Koordinaten" in str(c)]
    raise KeyError(f"no column starting with {prefix!r}; present: {found}")


def _ied_id(inspire_url: str) -> str:
    """`https://registry.gdi-de.org/id/de.sh/50000083_515_0` → `ied_de.sh/50000083_515_0`.

    Keeps the WHOLE path after the registry prefix: some states (Bremen) nest deeper
    (`de.hb/de.hb.pf.bube-eureg.…/2000058/0/0-0001`) and only the full path is unique.
    """
    s = str(inspire_url).rstrip("/")
    prefix = "https://registry.gdi-de.org/id/"
    tail = s[len(prefix):] if s.startswith(prefix) else s.split("://")[-1]
    return f"ied_{tail}"


def extract_ied(data_root: Path, force: bool = False) -> gpd.GeoDataFrame:
    """Download (cached) + parse the IED workbook into the canonical schema; cached."""
    dest = paths.source_parquet(data_root, "ied", "DE")
    if dest.exists() and not force:
        print(f"[skip] {dest.name} exists")
        return gpd.read_parquet(dest)

    xlsx = download.download_file(
        config.IED_URL, paths.raw_dir(data_root) / "ied" / config.IED_URL.rsplit("/", 1)[-1],
        force=force)
    df = pd.read_excel(xlsx, sheet_name=config.IED_SHEET)

    year = int(df["Berichtsjahr"].max())
    df = df[df["Berichtsjahr"] == year]
    df = df[df["Anlage_Typ"] == "IED"]
    n_before = len(df)
    df = df[df["Status.Anlage"] == config.IED_KEEP_STATUS]
    print(f"[ied] Berichtsjahr {year}: {n_before} IED installations, "
          f"{n_before - len(df)} dropped by status filter → {len(df)} kept")

    lat_col, lon_col = _coord_col(df, "lat"), _coord_col(df, "long")
    df = df[df[lat_col].notna() & df[lon_col].notna()].reset_index(drop=True)

    def _s(col: str) -> pd.Series:
        return df[col].astype("string").str.strip()

    street, nr = _s("Adresse_Str"), _s("Adresse_Str_Nr")
    plz, city = _s("Adresse_PLZ"), _s("Adresse_Ort")
    out = pd.DataFrame({
        "id": df["InspireID.Anlage"].map(_ied_id),
        "name": _s("Name.Betrieb"),
        "business_type": "industrial",
        "address_street": street,
        "address_housenumber": nr,
        "address_postcode": plz,
        "address_city": city,
        "address_full": (street.fillna("") + " " + nr.fillna("")).str.strip()
                        + ", " + plz.fillna("") + " " + city.fillna(""),
        "state": df["Bundesland"].map(
            lambda c: config.SLUG_STATE_NAMES.get(
                config.STATE_ALIASES.get(str(c).lower(), ""))).astype("string"),
        "latitude": pd.to_numeric(df[lat_col]),
        "longitude": pd.to_numeric(df[lon_col]),
        "source": "ied",
    })
    for src_col, debug_col in _DEBUG_RENAMES.items():
        out[debug_col] = df[src_col].astype("string")

    gdf = gpd.GeoDataFrame(
        out, geometry=gpd.points_from_xy(out["longitude"], out["latitude"]),
        crs=config.CRS_STORAGE)
    gdf = schema.conform(gdf)
    schema.validate_frame(gdf, source="ied")
    gdf.to_parquet(dest)
    print(f"[ied] {len(gdf)} installations → {dest.name}")
    return gdf
