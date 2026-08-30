"""Entity resolution: blocking + fuzzy name match + union-find — spec §A3.

The same plant can appear in OSM *and* IED *and* Abwärme. Records match when they are
≤ 50 m apart and their normalized names reach token_sort_ratio ≥ 80 (or, when one record
has a polygon containing the other's point, ratio ≥ 60). Clusters merge field-by-field in
SOURCE_PRIORITY order. A single-source frame goes through the same code path unchanged.
"""
from __future__ import annotations

import datetime as _dt
import re

import geopandas as gpd
import numpy as np
import pandas as pd
from rapidfuzz import fuzz

from . import config, schema

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_LEGAL_FORMS_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in
                      sorted(config.LEGAL_FORM_SUFFIXES, key=len, reverse=True)) + r")\b"
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_name(name: object) -> str:
    """lowercase → umlaut transliteration → strip legal forms → alnum only → collapse ws."""
    if name is None or (isinstance(name, float) and np.isnan(name)) or pd.isna(name):
        return ""
    text = str(name).lower().translate(_UMLAUTS)
    text = _LEGAL_FORMS_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[rj] = ri


def _priority(source: str) -> int:
    primary = str(source).split("+")[0]
    try:
        return config.SOURCE_PRIORITY.index(primary)
    except ValueError:
        return len(config.SOURCE_PRIORITY)


def _candidate_pairs(bounds: np.ndarray):
    """Blocking: 100 m grid; each row registers in every cell its bbox covers.
    Yields candidate index pairs (streaming — nothing is materialized).

    Points cover one cell; polygons cover their footprint. Registering the footprint
    keeps the §A3 polygon-containment match reachable for large sites (refineries,
    steelworks) whose representative point lies far from the contained point —
    with centre-only blocking those pairs were never even considered.
    """
    cell = config.BLOCK_GRID_M
    cx0 = np.floor(bounds[:, 0] / cell).astype(np.int64)
    cy0 = np.floor(bounds[:, 1] / cell).astype(np.int64)
    cx1 = np.floor(bounds[:, 2] / cell).astype(np.int64)
    cy1 = np.floor(bounds[:, 3] / cell).astype(np.int64)
    max_span = 40  # cap cells per axis — a degenerate multi-km geometry registers coarsely
    grid: dict[tuple[int, int], list[int]] = {}
    for i in range(len(bounds)):
        for gx in range(cx0[i], min(cx1[i], cx0[i] + max_span) + 1):
            for gy in range(cy0[i], min(cy1[i], cy0[i] + max_span) + 1):
                grid.setdefault((gx, gy), []).append(i)

    # Streaming half-neighbourhood sweep: within-cell pairs plus four of the eight
    # neighbour directions covers every unordered cell pair exactly once, with O(1)
    # extra memory (a materialized pair set costs tens of GB at 4.5M rows). Rows
    # registered in several cells (polygon footprints) can yield a pair twice —
    # union-find is idempotent, so that only costs a duplicate ratio check.
    half = ((1, 0), (0, 1), (1, 1), (1, -1))
    for (gx, gy), members in grid.items():
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                yield members[a], members[b]
        for dx, dy in half:
            other = grid.get((gx + dx, gy + dy))
            if not other:
                continue
            for i in members:
                for j in other:
                    if i != j:
                        yield i, j


def resolve(frames: list[gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    """Merge canonical per-source frames into one deduplicated Company frame."""
    merged_at = _dt.datetime.now(_dt.UTC).date().isoformat()
    gdf = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0].copy()
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs)
    # Overlapping extracts (Geofabrik's Brandenburg PBF contains Berlin) yield the same
    # feature twice under the same prefixed id — identical id means identical entity.
    n0 = len(gdf)
    gdf = gdf.drop_duplicates(subset="id", keep="first")
    if len(gdf) < n0:
        print(f"[resolve] dropped {n0 - len(gdf)} duplicate-id rows from overlapping extracts")
    gdf = gdf.reset_index(drop=True)

    geom_m = gdf.geometry.to_crs(config.CRS_METRIC)
    reps = geom_m.representative_point()
    xs, ys = reps.x.to_numpy(), reps.y.to_numpy()
    names = [normalize_name(n) for n in gdf["name"]]
    is_poly = geom_m.geom_type.isin(["Polygon", "MultiPolygon"]).to_numpy()

    # rows geocoded only to postcode/city centroids must not anchor dedup (spec §B2):
    # dozens of companies share one centroid, so proximity there is meaningless
    anchors = (gdf["dedup_anchor"].fillna(True).to_numpy()
               if "dedup_anchor" in gdf.columns else None)

    uf = _UnionFind(len(gdf))
    for i, j in _candidate_pairs(geom_m.bounds.to_numpy()):
        if anchors is not None and not (anchors[i] and anchors[j]):
            continue
        if not names[i] or not names[j]:
            continue  # never merge on geometry alone
        ratio = fuzz.token_sort_ratio(names[i], names[j])
        if ratio < config.DEDUP_POLYGON_NAME_RATIO:
            continue
        dist = float(np.hypot(xs[i] - xs[j], ys[i] - ys[j]))
        if dist <= config.DEDUP_DISTANCE_M and ratio >= config.DEDUP_NAME_RATIO:
            uf.union(i, j)
            continue
        # remaining path needs containment: far pairs at any ratio ≥ 60, and close
        # pairs whose ratio sits in [60, 80) — the polygon is the extra evidence
        # (spec §A3: containment relaxes the name threshold, regardless of distance)
        if ((is_poly[i] and geom_m.iloc[i].contains(reps.iloc[j]))
                or (is_poly[j] and geom_m.iloc[j].contains(reps.iloc[i]))):
            uf.union(i, j)

    roots = np.array([uf.find(i) for i in range(len(gdf))])
    gdf["_root"] = roots
    gdf["merged_at"] = merged_at

    singleton_mask = pd.Series(roots).groupby(roots).transform("size").to_numpy() == 1
    out_rows = [gdf[singleton_mask]]

    fill_cols = [c for c in gdf.columns
                 if c not in ("geometry", "_root", "source", "source_count", "merged_at")]

    cluster_records = []
    cluster_geoms = []
    for _, cluster in gdf[~singleton_mask].groupby("_root"):
        order = sorted(cluster.index, key=lambda idx: _priority(cluster.at[idx, "source"]))
        ranked = cluster.loc[order]
        record: dict = {}
        for col in fill_cols:
            non_null = ranked[col].dropna()
            record[col] = non_null.iloc[0] if len(non_null) else pd.NA
        sources = sorted({s for src in ranked["source"] for s in str(src).split("+")})
        record["source"] = "+".join(sources)
        record["source_count"] = len(sources)
        record["merged_at"] = merged_at
        record["member_ids"] = "|".join(ranked["id"].astype(str))
        poly_members = ranked[ranked.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
        geom = poly_members.geometry.iloc[0] if len(poly_members) else ranked.geometry.iloc[0]
        cluster_records.append(record)
        cluster_geoms.append(geom)

    if cluster_records:
        clusters = gpd.GeoDataFrame(cluster_records, geometry=cluster_geoms, crs=gdf.crs)
        reps4326 = clusters.geometry.representative_point()
        clusters["longitude"] = reps4326.x
        clusters["latitude"] = reps4326.y
        out_rows.append(clusters)

    out = pd.concat(out_rows, ignore_index=True)
    out = gpd.GeoDataFrame(out.drop(columns=["_root"], errors="ignore"),
                           geometry="geometry", crs=gdf.crs)
    out = schema.conform(out)
    schema.validate_frame(out, source="merged")
    return out


def compute_confidence(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """§5.4: sum of satisfied weights → confidence_score."""
    gdf = gdf.copy()
    w = config.CONFIDENCE_WEIGHTS
    has_address = (
        gdf["address_street"].notna() & gdf["address_housenumber"].notna()
        & gdf["address_postcode"].notna() & gdf["address_city"].notna()
    )
    score = (
        (gdf["source_count"] >= 2).fillna(False).astype(float) * w["multi_source"]
        + gdf["website"].notna().astype(float) * w["has_website"]
        + has_address.astype(float) * w["has_address"]
        + gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).astype(float)
        * w["has_geometry_polygon"]
        + gdf["phone"].notna().astype(float) * w["has_phone"]
        + gdf["grounds_area_m2"].notna().astype(float) * w["has_grounds_area"]
    )
    if "geocode_precision" in gdf.columns:   # spec §B2: coarse geocodes cap at 0.3
        coarse = gdf["geocode_precision"].isin(sorted(config.GEOCODE_COARSE)).fillna(False)
        score = score.where(~coarse, score.clip(upper=config.GEOCODE_COARSE_CONFIDENCE_CAP))
    gdf["confidence_score"] = score.round(2).astype("Float64")
    return gdf
