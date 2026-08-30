"""OSM adapter — QuackOSM over a Geofabrik PBF → canonical Company frame — spec §A1.

Also extracts the two OSM-derived support layers (they are NOT company records):
landuse zones (for the A2 grounds-area fallback) and admin boundaries (for A2 joins).

Gotchas honoured (spec §10): QuackOSM returns OGC:CRS84 → always to_crs(EPSG:4326);
feature ids keep their `way/123` type prefix; `representative_point()` not centroid.
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import quackosm

from .. import config, paths, schema


def _convert(pbf: Path, tags_filter: dict, data_root: Path) -> gpd.GeoDataFrame:
    gdf = quackosm.convert_pbf_to_geodataframe(
        str(pbf),
        tags_filter=tags_filter,
        keep_all_tags=True,
        explode_tags=False,
        working_directory=str(paths.quackosm_workdir(data_root)),
    )
    return gdf.to_crs(config.CRS_STORAGE)


def _first_tag(tags: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = tags.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _business_type(tags: dict) -> tuple[str | None, str | None]:
    for key in config.BUSINESS_KEYS:
        value = tags.get(key)
        if value is not None and str(value).strip() \
                and str(value) not in config.BUSINESS_VALUE_BLACKLIST:
            return key, str(value)
    return None, None


def extract_osm(pbf: Path, data_root: Path) -> gpd.GeoDataFrame:
    """Business features from one state PBF → canonical frame (full geometry kept).

    Debug columns: `osm_tags` (JSON of all tags — classifier context and §6.5 signals).
    """
    gdf = _convert(pbf, config.OSM_BUSINESS_FILTER, data_root)
    if len(gdf) == 0:
        raise RuntimeError(f"no business features from {pbf} — check the tag filter")

    feature_ids = gdf.index.to_numpy()
    tag_dicts = [t if isinstance(t, dict) else {} for t in gdf["tags"]]

    records = []
    for fid, tags in zip(feature_ids, tag_dicts):
        btype, bsub = _business_type(tags)
        street = _first_tag(tags, ["addr:street"])
        hnr = _first_tag(tags, ["addr:housenumber"])
        plz = _first_tag(tags, ["addr:postcode"])
        city = _first_tag(tags, ["addr:city"])
        full = ", ".join(
            part for part in (
                " ".join(x for x in (street, hnr) if x),
                " ".join(x for x in (plz, city) if x),
            ) if part
        ) or None
        records.append({
            "id": f"osm_{fid}",
            "name": _first_tag(tags, ["name"]),
            "business_type": btype,
            "business_subtype": bsub,
            "address_street": street,
            "address_housenumber": hnr,
            "address_postcode": plz,
            "address_city": city,
            "address_full": full,
            "website": _first_tag(tags, config.WEBSITE_TAGS),
            "phone": _first_tag(tags, config.PHONE_TAGS),
            "email": _first_tag(tags, config.EMAIL_TAGS),
            "source": "osm",
            "osm_tags": json.dumps(tags, ensure_ascii=False),
        })

    out = gpd.GeoDataFrame(records, geometry=gdf.geometry.values, crs=config.CRS_STORAGE)
    # Features whose only business value was blacklisted (office=no, shop=vacant) are noise.
    out = out[out["business_type"].notna()].reset_index(drop=True)

    reps = out.geometry.representative_point()
    out["longitude"] = reps.x
    out["latitude"] = reps.y

    # Geofabrik state extracts include offshore sea scraps, so a handful of mapped
    # features can sit outside the Germany sanity bounds (spec §2). Drop true
    # stragglers loudly; anything beyond a sliver stays for validate_frame to fail on.
    lat_lo, lat_hi = config.LAT_BOUNDS
    lon_lo, lon_hi = config.LON_BOUNDS
    oob = ((out["latitude"] < lat_lo) | (out["latitude"] > lat_hi) |
           (out["longitude"] < lon_lo) | (out["longitude"] > lon_hi))
    if 0 < int(oob.sum()) <= max(5, len(out) // 1000):
        print(f"[warn] dropping {int(oob.sum())} out-of-bounds feature(s): "
              f"{out.loc[oob, 'id'].tolist()}")
        out = out[~oob].reset_index(drop=True)

    out = schema.conform(out)
    schema.validate_frame(out, source="osm")
    return out


def extract_landuse(pbf: Path, data_root: Path) -> gpd.GeoDataFrame:
    """landuse ∈ {commercial, industrial, retail} polygons — grounds-area fallback zones."""
    gdf = _convert(pbf, {"landuse": config.LANDUSE_ZONE_VALUES}, data_root)
    landuse = [t.get("landuse") if isinstance(t, dict) else None for t in gdf["tags"]]
    out = gpd.GeoDataFrame(
        {"landuse": landuse}, geometry=gdf.geometry.values, crs=config.CRS_STORAGE
    )
    out = out[out.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    return out.reset_index(drop=True)


def extract_boundaries(pbf: Path, data_root: Path) -> gpd.GeoDataFrame:
    """admin_level 4/6 administrative polygons with name + 5-digit Kreis AGS — spec §A2."""
    levels = [config.ADMIN_LEVEL_STATE, config.ADMIN_LEVEL_DISTRICT]
    gdf = _convert(pbf, {"admin_level": levels}, data_root)

    rows = []
    for tags, geom in zip(gdf["tags"], gdf.geometry.values):
        if not isinstance(tags, dict) or tags.get("boundary") != "administrative":
            continue
        level = str(tags.get("admin_level", ""))
        if level not in levels:
            continue
        ags = None
        for key in ("de:regionalschluessel", "de:amtlicher_gemeindeschluessel"):
            value = tags.get(key)
            if value and str(value).strip():
                ags = str(value).strip()[:5]
                break
        rows.append({"name": tags.get("name"), "admin_level": level, "ags": ags,
                     "geometry": geom})

    out = gpd.GeoDataFrame(rows, crs=config.CRS_STORAGE)
    out = out[out.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    return out.reset_index(drop=True)
