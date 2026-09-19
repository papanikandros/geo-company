"""The canonical Company table contract — spec §3.

`COMPANY_COLUMNS` is the single source of truth: name → (dtype, nullable, description).
Validation is frame-level (never per-row Pydantic — too slow at ~850k rows). The contract
only ever grows additively; extra source-specific debug columns are allowed *after* the
contract columns.
"""
from __future__ import annotations

import pandas as pd

from . import config

# dtype strings are pandas dtypes; "string" is the nullable pandas string dtype.
# (name, (dtype, nullable, description)) — order here IS the contract column order.
COMPANY_COLUMNS: dict[str, tuple[str, bool, str]] = {
    # --- §3.1 identity, location, contact (Part A/B) ---
    "id": ("string", False, "stable id {source}_{key}, e.g. osm_way/123; winning source's id after merge"),
    "name": ("string", True, "business name"),
    "business_type": ("string", True, "primary OSM-style category: office|shop|craft|industrial|amenity|man_made|power"),
    "business_subtype": ("string", True, "value of that key, e.g. office=company → company"),
    "address_street": ("string", True, "street"),
    "address_housenumber": ("string", True, "house number"),
    "address_postcode": ("string", True, "postal code"),
    "address_city": ("string", True, "city"),
    "address_full": ("string", True, '"{street} {nr}, {plz} {city}"'),
    "state": ("string", True, "Bundesland official name (spatial join, admin_level 4)"),
    "district": ("string", True, "Landkreis / kreisfreie Stadt (admin_level 6)"),
    "district_ags": ("string", True, "5-digit Kreis AGS — regional join key"),
    "website": ("string", True, "website URL — the classifier seam"),
    "phone": ("string", True, "phone"),
    "email": ("string", True, "email"),
    "latitude": ("Float64", True, "WGS84 representative point"),
    "longitude": ("Float64", True, "WGS84 representative point"),
    "grounds_area_m2": ("Float64", True, "company grounds surface (m², EPSG:25832)"),
    "grounds_area_source": ("string", True, "own_polygon | landuse_zone | null"),
    "legal_form": ("string", True, "GmbH, AG, GmbH & Co. KG, e.K., …"),
    "hr_registration": ("string", True, "Handelsregister number"),
    "hr_court": ("string", True, "Handelsregister court"),
    "source": ("string", False, 'contributing sources joined with "+", e.g. osm+ied'),
    "source_count": ("Int64", False, "distinct sources in the dedup cluster"),
    "confidence_score": ("Float64", False, "0–1, weights per spec §5.4"),
    "merged_at": ("string", True, "ISO date of the merge snapshot"),
    # --- §3.2 classification (Part C) ---
    "nace_codes": ("string", True, "pipe-joined 4-digit NACE Rev.2 codes, best first (1–3)"),
    "nace_primary": ("string", True, "first/highest-confidence code"),
    "nace_section": ("string", True, "section letter of nace_primary, A–U"),
    "nace_confidence": ("Float64", True, "confidence of the primary code, 0–1"),
    "nace_method": ("string", True, "register_wz | intrinsic_source | osm_tag_rule | ai | traditional | null"),
    "nace_reasoning": ("string", True, "short evidence string (≤ 300 chars)"),
    "wz_code": ("string", True, "German WZ 2008 code as supplied by a register, untouched"),
    "is_industrial": ("boolean", False, "nace_section ∈ {B,C,D,E,F} or intrinsic industrial signal"),
    # --- §3.4 register verification (item 6 stage 2; additive, 2026-09-09) ---
    "register_match": ("string", True, "exact_hrb | name_plz_street | name_plz | name_city | ambiguous | none | n/a (unnamed)"),
    "hr_id": ("string", True, "register company id in hr_source (court code + number, OpenCorporates id, or LEI)"),
    "hr_source": ("string", True, "hr2022 (offeneregister.de 2022) | hr2019 (OpenCorporates 2019) | gleif"),
    "hr_status": ("string", True, "active | dissolved | unknown — at hr_snapshot_date"),
    "hr_dissolved_date": ("string", True, "ISO date of the register deletion, if dissolved"),
    "hr_snapshot_date": ("string", True, "date the register source is current to — a match is evidence as of this date"),
    "hr_objective": ("string", True, "Unternehmensgegenstand (registered business purpose) — Part C evidence"),
    "hr_capital": ("Float64", True, "registered capital (EUR) where the register has it"),
    "legal_name": ("string", True, "legal name as written in the site's Impressum (stage 1), else the register's"),
    "website_replaced": ("string", True, "the UNVERIFIED website a discovery route replaced (audit trail)"),
    "website_replaced_source": ("string", True, "where that replaced website had come from"),
}

CONTRACT_ORDER = list(COMPANY_COLUMNS)

DEFAULTS: dict[str, object] = {
    "source_count": 1,
    "confidence_score": 0.0,
    "is_industrial": False,
}


class SchemaError(ValueError):
    """Raised by validate_frame with a readable, actionable message."""


def conform(df: pd.DataFrame) -> pd.DataFrame:
    """Add missing contract columns (with defaults), cast dtypes, order columns.

    Contract columns come first in contract order; any extra (debug) columns keep their
    relative order after them. Returns a new frame; the input is not mutated.
    """
    df = df.copy()
    for col, (dtype, _nullable, _desc) in COMPANY_COLUMNS.items():
        if col not in df.columns:
            df[col] = DEFAULTS.get(col, pd.NA)
        try:
            df[col] = df[col].astype(dtype)
        except (TypeError, ValueError) as err:
            raise SchemaError(f"column {col!r} cannot be cast to {dtype}: {err}") from err
    extras = [c for c in df.columns if c not in COMPANY_COLUMNS and c != "geometry"]
    ordered = CONTRACT_ORDER + extras + (["geometry"] if "geometry" in df.columns else [])
    return df[ordered]


def validate_frame(df: pd.DataFrame, source: str) -> None:
    """Raise SchemaError listing every contract violation found in ``df``.

    Checks: missing columns, null/duplicate ids, required columns with nulls,
    coordinates outside Germany sanity bounds, insane grounds areas.
    """
    problems: list[str] = []

    missing = [c for c in COMPANY_COLUMNS if c not in df.columns]
    if missing:
        problems.append(f"missing contract columns: {missing}")

    if "id" in df.columns:
        n_null = int(df["id"].isna().sum())
        if n_null:
            problems.append(f"{n_null} null ids")
        dup = df["id"][df["id"].duplicated()]
        if len(dup):
            problems.append(f"{len(dup)} duplicate ids (e.g. {dup.head(3).tolist()})")

    for col in ("source", "source_count", "confidence_score", "is_industrial"):
        if col in df.columns:
            n_null = int(df[col].isna().sum())
            if n_null:
                problems.append(f"required column {col!r} has {n_null} nulls")

    if "latitude" in df.columns and "longitude" in df.columns:
        lat = pd.to_numeric(df["latitude"], errors="coerce")
        lon = pd.to_numeric(df["longitude"], errors="coerce")
        lat_lo, lat_hi = config.LAT_BOUNDS
        lon_lo, lon_hi = config.LON_BOUNDS
        bad = ((lat < lat_lo) | (lat > lat_hi) | (lon < lon_lo) | (lon > lon_hi)).fillna(False)
        if int(bad.sum()):
            sample = df.loc[bad, ["id", "latitude", "longitude"]].head(3).to_dict("records")
            problems.append(f"{int(bad.sum())} rows outside Germany bounds "
                            f"(lat {lat_lo}–{lat_hi}, lon {lon_lo}–{lon_hi}), e.g. {sample}")

    if "grounds_area_m2" in df.columns:
        area = pd.to_numeric(df["grounds_area_m2"], errors="coerce")
        bad = ((area <= 0) | (area > config.AREA_MAX_M2)).fillna(False)
        if int(bad.sum()):
            sample = df.loc[bad, ["id", "grounds_area_m2"]].head(3).to_dict("records")
            problems.append(f"{int(bad.sum())} rows with insane grounds_area_m2 "
                            f"(sane range (0, {config.AREA_MAX_M2:.0e}]), e.g. {sample}")

    if problems:
        raise SchemaError(
            f"schema validation failed for source {source!r} ({len(df)} rows):\n  - "
            + "\n  - ".join(problems)
        )
