"""Pipeline orchestration for the CLI — Part A complete: extract → resolve → geography →
confidence → export. Every stage is cached and idempotent (spec §0 principle 2)."""
from __future__ import annotations

import json
import time

import geopandas as gpd
import pandas as pd

from . import area, boundaries, config, download, export, paths, resolve
from .sources import abwaerme as abwaerme_source
from .sources import ied as ied_source
from .sources import osm as osm_source
from .sources import overture as overture_source


def _resolve_states(states: str) -> list[str]:
    if states.strip().lower() == "all":
        return list(config.STATES)
    return [config.resolve_state(s) for s in states.split(",") if s.strip()]


def _scope_name(slugs: list[str]) -> str:
    if set(slugs) == set(config.STATES):
        return "DE"
    return slugs[0] if len(slugs) == 1 else "-".join(sorted(slugs))


def extract_osm_state(slug: str, data_root, skip_download: bool = False) -> gpd.GeoDataFrame:
    """Download (unless cached) + extract one state's OSM businesses; cache the frame."""
    dest = paths.source_parquet(data_root, "osm", slug)
    if dest.exists():
        print(f"[skip] {dest.name} exists")
        return gpd.read_parquet(dest)

    pbf = paths.pbf_path(data_root, slug)
    if skip_download:
        if not pbf.exists():
            raise FileNotFoundError(f"--skip-download but {pbf} is missing")
    else:
        pbf = download.download_pbf(slug, data_root)

    t0 = time.time()
    gdf = osm_source.extract_osm(pbf, data_root)
    gdf.to_parquet(dest)
    n_web = int(gdf["website"].notna().sum())
    print(f"[osm/{slug}] {len(gdf)} businesses, {n_web} with website "
          f"({n_web / len(gdf):.0%}) in {time.time() - t0:.0f}s → {dest.name}")
    return gdf


def support_layers(slug: str, data_root) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Cached admin-boundary and landuse-zone layers for one state (from its PBF)."""
    b_path = paths.boundaries_parquet(data_root, slug)
    l_path = paths.landuse_parquet(data_root, slug)
    pbf = paths.pbf_path(data_root, slug)
    if b_path.exists():
        bounds = gpd.read_parquet(b_path)
    else:
        bounds = osm_source.extract_boundaries(pbf, data_root)
        bounds.to_parquet(b_path)
        print(f"[boundaries/{slug}] {len(bounds)} admin polygons → {b_path.name}")
    if l_path.exists():
        zones = gpd.read_parquet(l_path)
    else:
        zones = osm_source.extract_landuse(pbf, data_root)
        zones.to_parquet(l_path)
        print(f"[landuse/{slug}] {len(zones)} zones → {l_path.name}")
    return bounds, zones


def apply_geography(gdf: gpd.GeoDataFrame, bounds: gpd.GeoDataFrame,
                    zones: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """A2: fill state/district/district_ags and grounds area on a canonical frame."""
    gdf = boundaries.assign_admin(gdf, bounds)
    gdf = area.compute_grounds_area(gdf, zones)
    return gdf


def apply_intrinsic_industrial(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """§6.5 intrinsic industrial signal (pre-classifier): sources and OSM tag sets."""
    gdf = gdf.copy()

    def _row_signal(source, tags_json) -> bool:
        if any(s in config.INDUSTRIAL_SOURCES for s in str(source).split("+")):
            return True
        if isinstance(tags_json, str) and tags_json:
            try:
                tags = json.loads(tags_json)
            except json.JSONDecodeError:
                return False
            for key, values in config.INDUSTRIAL_TAGS.items():
                if str(tags.get(key)) in values:
                    return True
        return False

    tags_col = gdf["osm_tags"] if "osm_tags" in gdf.columns else pd.Series(
        [None] * len(gdf), index=gdf.index)
    signal = [
        _row_signal(src, tj) for src, tj in zip(gdf["source"], tags_col)
    ]
    gdf["is_industrial"] = pd.array(signal, dtype="boolean")
    return gdf


def run_extract(states: str, sources: str, data_dir=None, skip_download: bool = False) -> int:
    data_root = paths.data_root(data_dir)
    slugs = _resolve_states(states)
    scope = _scope_name(slugs)
    source_list = [s.strip() for s in sources.split(",") if s.strip()]
    runtimes: dict[str, float] = {}
    pbf_meta: dict[str, dict] = {}

    for src in source_list:
        if src not in ("osm", "ied", "abwaerme", "overture"):
            print(f"[warn] source {src!r} is Part B — not implemented yet, skipping")

    frames: list[gpd.GeoDataFrame] = []
    bounds_parts: list[gpd.GeoDataFrame] = []
    zones_parts: list[gpd.GeoDataFrame] = []
    if "osm" in source_list:
        for slug in slugs:
            t0 = time.time()
            frames.append(extract_osm_state(slug, data_root, skip_download=skip_download))
            bounds, zones = support_layers(slug, data_root)
            bounds_parts.append(bounds)
            zones_parts.append(zones)
            runtimes[f"extract_{slug}"] = time.time() - t0
            pbf_meta[slug] = download.read_meta(paths.pbf_path(data_root, slug))
    if "ied" in source_list:
        t0 = time.time()
        ied = ied_source.extract_ied(data_root)
        if scope != "DE":   # scoped runs (e.g. Bremen validation) take the state subset
            wanted = {config.SLUG_STATE_NAMES[s] for s in slugs}
            ied = ied[ied["state"].isin(wanted)].reset_index(drop=True)
            print(f"[ied] state subset {sorted(wanted)}: {len(ied)} installations")
        if len(ied):
            frames.append(ied)
        runtimes["extract_ied"] = time.time() - t0
        pbf_meta["ied"] = download.read_meta(
            paths.raw_dir(data_root) / "ied" / config.IED_URL.rsplit("/", 1)[-1])
    if "abwaerme" in source_list:
        t0 = time.time()
        abw = abwaerme_source.extract_abwaerme(data_root)
        if scope != "DE":
            wanted = {config.SLUG_STATE_NAMES[s] for s in slugs}
            abw = abw[abw["state"].isin(wanted)].reset_index(drop=True)
            print(f"[abwaerme] state subset {sorted(wanted)}: {len(abw)} sites")
        if len(abw):
            frames.append(abw)
        runtimes["extract_abwaerme"] = time.time() - t0
        pbf_meta["abwaerme"] = download.read_meta(
            paths.raw_dir(data_root) / "abwaerme" / "pfa_datentabelle.xlsx")
    if "overture" in source_list:
        t0 = time.time()
        ovt = overture_source.extract_overture(data_root)
        if scope != "DE":
            # region-derived state covers Foursquare/Microsoft rows; Meta rows have
            # none yet — fall back to the requested states' boundary bounding boxes
            wanted = {config.SLUG_STATE_NAMES[s] for s in slugs}
            in_state = ovt["state"].isin(wanted)
            in_bbox = pd.Series(False, index=ovt.index)
            for slug in slugs:
                b_path = paths.boundaries_parquet(data_root, slug)
                if b_path.exists():
                    x0, y0, x1, y1 = gpd.read_parquet(b_path).total_bounds
                    in_bbox |= (ovt["longitude"].between(x0, x1)
                                & ovt["latitude"].between(y0, y1))
            ovt = ovt[in_state | (ovt["state"].isna() & in_bbox)].reset_index(drop=True)
            print(f"[overture] state subset {sorted(wanted)}: {len(ovt)} places")
        if len(ovt):
            frames.append(ovt)
        runtimes["extract_overture"] = time.time() - t0
        pbf_meta["overture"] = {"release": config.OVERTURE_RELEASE}
    if not frames:
        raise SystemExit("nothing extracted — no implemented source requested")

    t0 = time.time()
    merged = resolve.resolve(frames)
    runtimes["resolve"] = time.time() - t0
    print(f"[resolve] {sum(len(f) for f in frames)} → {len(merged)} rows")

    t0 = time.time()
    bounds_all = gpd.GeoDataFrame(
        pd.concat(bounds_parts, ignore_index=True), crs=config.CRS_STORAGE)
    zones_all = gpd.GeoDataFrame(
        pd.concat(zones_parts, ignore_index=True), crs=config.CRS_STORAGE)
    merged = apply_geography(merged, bounds_all, zones_all)
    merged = apply_intrinsic_industrial(merged)
    merged = resolve.compute_confidence(merged)
    runtimes["geography"] = time.time() - t0

    written = export.write_merged(merged, data_root, scope)
    summary = export.write_summary(merged, data_root, scope, pbf_meta, runtimes)
    for p in [*written, summary]:
        print(f"[out] {p}")
    return 0


def run_export(scope: str, data_dir=None) -> int:
    """Re-derive the 3857 map file + summary from an existing merged 4326 parquet."""
    data_root = paths.data_root(data_dir)
    src = paths.merged_parquet(data_root, scope, "4326")
    if not src.exists():
        raise SystemExit(f"{src} not found — run `geoextract extract` first")
    gdf = gpd.read_parquet(src)
    for p in [*export.write_merged(gdf, data_root, scope),
              export.write_summary(gdf, data_root, scope)]:
        print(f"[out] {p}")
    return 0


def run_all(scope: str, sources: str, data_dir=None) -> int:
    states = "all" if scope.strip().upper() == "DE" else scope
    return run_extract(states=states, sources=sources, data_dir=data_dir)
