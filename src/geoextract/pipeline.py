"""Pipeline orchestration for the CLI — Part A complete: extract → resolve → geography →
confidence → export. Every stage is cached and idempotent (spec §0 principle 2)."""
from __future__ import annotations

import json
import time

import geopandas as gpd
import shapely
import pandas as pd

from . import area, boundaries, config, download, export, paths, resolve
from .sources import abwaerme as abwaerme_source
from .sources import ied as ied_source
from .sources import mastr as mastr_source
from .sources import osm as osm_source
from .sources import overture as overture_source
from .web import hygiene as web_hygiene


_resolve_states = config.resolve_states
_scope_name = config.scope_name


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
    """§6.5 intrinsic industrial signal (pre-classifier): register sources, OSM tag sets
    (power=generator/substation only for NAMED rows — item 4a), Overture industrial/power
    category slugs (item 4b)."""
    gdf = gdf.copy()
    ovt_signal_types = {"industrial", "power"}

    def _row_signal(source, tags_json, name, ovt_category) -> bool:
        if any(s in config.INDUSTRIAL_SOURCES for s in str(source).split("+")):
            return True
        if isinstance(ovt_category, str) and \
                config.OVERTURE_SUBTYPE_TYPES.get(ovt_category) in ovt_signal_types:
            return True
        if isinstance(tags_json, str) and tags_json:
            try:
                tags = json.loads(tags_json)
            except json.JSONDecodeError:
                return False
            for key, values in config.INDUSTRIAL_TAGS.items():
                if str(tags.get(key)) in values:
                    return True
            if isinstance(name, str) and name.strip():
                for key, values in config.INDUSTRIAL_TAGS_NAMED_ONLY.items():
                    if str(tags.get(key)) in values:
                        return True
        return False

    def _col(name):
        return gdf[name] if name in gdf.columns else pd.Series([None] * len(gdf), index=gdf.index)

    signal = [
        _row_signal(src, tj, nm, oc)
        for src, tj, nm, oc in zip(gdf["source"], _col("osm_tags"),
                                   _col("name").astype(object).where(_col("name").notna(), None),
                                   _col("ovt_category").astype(object).where(_col("ovt_category").notna(), None))
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
        if src not in ("osm", "ied", "abwaerme", "overture", "mastr"):
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
            # region-derived state covers Foursquare/Microsoft rows; Meta rows have none —
            # test those against the boundary POLYGON. The bounding box used before pulled
            # the whole rectangle around a state in (Bremen: 7 002 places from Delmenhorst,
            # Ganderkesee, Stuhr … — the "square" on the map).
            wanted = {config.SLUG_STATE_NAMES[s] for s in slugs}
            in_state = ovt["state"].isin(wanted)
            in_poly = pd.Series(False, index=ovt.index)
            unknown = ovt["state"].isna()
            if unknown.any():
                for slug in slugs:
                    b_path = paths.boundaries_parquet(data_root, slug)
                    if not b_path.exists():
                        continue
                    geom = gpd.read_parquet(b_path).union_all()
                    idx = ovt.index[unknown & ~in_poly]
                    hit = shapely.contains_xy(geom, ovt.loc[idx, "longitude"].to_numpy(),
                                              ovt.loc[idx, "latitude"].to_numpy())
                    in_poly.loc[idx] = hit
            ovt = ovt[in_state | (unknown & in_poly)].reset_index(drop=True)
            print(f"[overture] state subset {sorted(wanted)}: {len(ovt)} places")
        if len(ovt):
            frames.append(ovt)
        runtimes["extract_overture"] = time.time() - t0
        pbf_meta["overture"] = {"release": config.OVERTURE_RELEASE}
    if "mastr" in source_list:
        t0 = time.time()
        mst = mastr_source.extract_mastr(data_root)
        if scope != "DE":   # register carries Bundesland → direct state subset
            wanted = {config.SLUG_STATE_NAMES[s] for s in slugs}
            mst = mst[mst["state"].isin(wanted)].reset_index(drop=True)
            print(f"[mastr] state subset {sorted(wanted)}: {len(mst)} sites")
        if len(mst):
            frames.append(mst)
        runtimes["extract_mastr"] = time.time() - t0
        pbf_meta["mastr"] = {"db": str(config.MASTR_DB)}
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
    if scope != "DE":
        # the state extracts are rectangles around a state (Geofabrik) and a few sources
        # carry no state of their own: keep only what the geography stage placed inside
        wanted = {config.SLUG_STATE_NAMES[s] for s in slugs}
        keep = merged["state"].isin(wanted)
        if not keep.all():
            print(f"[scope] {int((~keep).sum())} rows outside {sorted(wanted)} dropped "
                  f"({merged.loc[~keep, 'source'].str.split('+').explode().value_counts().head(3).to_dict()})")
            merged = merged[keep].reset_index(drop=True)
    merged = apply_intrinsic_industrial(merged)
    runtimes["geography"] = time.time() - t0

    t0 = time.time()   # item 6 stage 0d: own sites only in `website` BEFORE confidence
    merged = web_hygiene.apply_hygiene(merged)
    merged = resolve.compute_confidence(merged)
    runtimes["web_hygiene"] = time.time() - t0
    rep = web_hygiene.hygiene_report(merged)
    print(f"[web] website kinds {rep['website_kind']}, propagated {rep['website_propagated']}")

    written = export.write_merged(merged, data_root, scope)
    summary = export.write_summary(merged, data_root, scope, pbf_meta, runtimes)
    for p in [*written, summary]:
        print(f"[out] {p}")
    return 0


def run_web_hygiene(scope: str, data_dir=None, propagate: bool = True) -> int:
    """Item 6 stage 0d on an existing merged table: rewrite 4326 + 3857 + summary."""
    data_root = paths.data_root(data_dir)
    scope = _scope_name(_resolve_states(scope)) if scope != "DE" else "DE"
    src = paths.merged_parquet(data_root, scope, "4326")
    if not src.exists():
        raise SystemExit(f"{src} missing — run `geoextract extract` first")
    t0 = time.time()
    gdf = gpd.read_parquet(src)
    before = int(gdf["website"].notna().sum())
    gdf = web_hygiene.apply_hygiene(gdf, propagate=propagate)
    gdf = resolve.compute_confidence(gdf)
    rep = web_hygiene.hygiene_report(gdf)
    print(f"[web] {scope}: website {before} → {rep['website_kept']} kept, "
          f"{rep['website_listing']} moved to website_listing, "
          f"{rep['website_propagated']} propagated; kinds {rep['website_kind']} "
          f"({time.time() - t0:.0f} s)")
    for host, n in rep["top_hosts"].items():
        print(f"[web]   {host}: {n}")
    for p in [*export.write_merged(gdf, data_root, scope),
              export.write_summary(gdf, data_root, scope, None, {"web_hygiene": time.time() - t0})]:
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
