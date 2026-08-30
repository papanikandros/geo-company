"""Overture Maps places adapter — spec §B3, source catalogue #5.

Overture Maps Foundation (Linux Foundation project of Meta, Microsoft, Amazon, TomTom)
publishes a monthly conflated POI (point-of-interest) layer as GeoParquet on public S3
(Amazon Simple Storage Service) — provenance per row: Meta business listings, Microsoft/
Bing, Foursquare open data, AllThePlaces. License CDLA-Permissive-2.0. Read with DuckDB
straight from S3 with a Germany bounding-box filter (row-group pruning keeps the scan
in the low-GB range).

Filters (decided 2026-08-26): named places only, Overture confidence ≥ 0.5, country DE
or unset, not marked closed. `business_type` comes from a pure mapping of Overture's own
taxonomy ROOT (config.OVERTURE_ROOT_TYPES); the specific category slug goes to
`business_subtype`; unmapped roots stay NA and are logged. The adapter does NOT do
entity resolution itself.

Debug columns: ovt_confidence, ovt_dataset (provenance), ovt_root, ovt_brand_wikidata.
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

from .. import config, paths, schema


def _query(bbox: tuple[float, float, float, float]) -> str:
    lon0, lat0, lon1, lat1 = bbox
    return f"""
SELECT
    'ovt_' || id                          AS id,
    names."primary"                       AS name,
    categories."primary"                  AS business_subtype,
    taxonomy.hierarchy[1]                 AS ovt_root,
    confidence                            AS ovt_confidence,
    websites[1]                           AS website,
    emails[1]                             AS email,
    phones[1]                             AS phone,
    addresses[1].freeform                 AS address_street,
    addresses[1].postcode                 AS address_postcode,
    addresses[1].locality                 AS address_city,
    addresses[1].region                   AS ovt_region,
    sources[1].dataset                    AS ovt_dataset,
    brand.wikidata                        AS ovt_brand_wikidata,
    bbox.xmin                             AS longitude,
    bbox.ymin                             AS latitude
FROM read_parquet('{config.OVERTURE_S3}', hive_partitioning=1)
WHERE bbox.xmin BETWEEN {lon0} AND {lon1}
  AND bbox.ymin BETWEEN {lat0} AND {lat1}
  AND names."primary" IS NOT NULL
  AND confidence >= {config.OVERTURE_MIN_CONFIDENCE}
  AND (addresses[1].country = 'DE' OR addresses[1].country IS NULL)
  AND (operating_status IS NULL OR operating_status NOT ILIKE '%closed%')
"""


def extract_overture(data_root: Path, force: bool = False,
                     bbox: tuple[float, float, float, float] | None = None,
                     scope: str = "DE") -> gpd.GeoDataFrame:
    """DuckDB S3 scan → canonical frame; cached per scope."""
    dest = paths.source_parquet(data_root, "overture", scope)
    if dest.exists() and not force:
        print(f"[skip] {dest.name} exists")
        return gpd.read_parquet(dest)

    import duckdb
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    df = con.sql(_query(bbox or config.GERMANY_BBOX)).df()
    print(f"[overture] release {config.OVERTURE_RELEASE}: {len(df)} named places "
          f"(confidence ≥ {config.OVERTURE_MIN_CONFIDENCE})")

    df["business_type"] = df["ovt_root"].map(config.OVERTURE_ROOT_TYPES)
    unmapped = df.loc[df["business_type"].isna(), "ovt_root"].value_counts().head(8)
    if len(unmapped):
        print(f"[overture] taxonomy roots without business_type mapping: "
              f"{unmapped.to_dict()}")

    # region carries plain state names for Foursquare/Microsoft rows; Meta rows are
    # empty — the merge's geography stage fills those spatially later
    known = set(config.SLUG_STATE_NAMES.values())
    df["state"] = df["ovt_region"].where(df["ovt_region"].isin(known))
    df = df.drop(columns=["ovt_region"])

    df["address_full"] = (
        df["address_street"].fillna("") + ", " + df["address_postcode"].fillna("")
        + " " + df["address_city"].fillna("")).str.strip(", ").replace("", None)
    df["source"] = "overture"
    for col in ("name", "business_subtype", "website", "email", "phone",
                "address_street", "address_postcode", "address_city", "state",
                "ovt_root", "ovt_dataset", "ovt_brand_wikidata"):
        df[col] = df[col].astype("string")

    gdf = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs=config.CRS_STORAGE)
    gdf = schema.conform(gdf)
    schema.validate_frame(gdf, source="overture")
    gdf.to_parquet(dest)
    print(f"[overture] {len(gdf)} places → {dest.name}")
    return gdf
