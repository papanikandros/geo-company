"""Entity resolution: blocking + fuzzy name match + union-find — spec §A3.

Vectorised (2026-09-04, todo item 2): candidate pairs come from an STRtree join of the
rows' blocking-cell rectangles (same semantics as the former per-cell sweep: same cell or
an 8-neighbour, polygons registering their capped footprint), names are scored with
rapidfuzz's multithreaded pairwise scorer, containment with shapely.contains_xy, and
clusters are assembled with a single groupby instead of a per-cluster Python loop.

The same plant can appear in OSM *and* IED *and* Abwärme. Records match when they are
≤ 50 m apart and their normalized names reach token_sort_ratio ≥ 80 (or, when one record
has a polygon containing the other's point, ratio ≥ 60). Clusters merge field-by-field in
SOURCE_PRIORITY order. A single-source frame goes through the same code path unchanged.
"""
from __future__ import annotations

import datetime as _dt
import re
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from rapidfuzz import fuzz, process

from . import config, schema

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
# dots/spaces inside abbreviations: "g.m.b.h." → "gmbh", "e. k." → "ek", "co. kg" → "cokg"
_ABBREV_DOT_RE = re.compile(r"\b([a-z])\.\s?(?=[a-z]\b|[a-z]\.)")
_CO_KG_RE = re.compile(r"\bco\.?\s*kg\b")
_STREET_RE = re.compile(r"(?<=[a-z])str\.?(?=\s|$)|\bstr\.?(?=\s|$)|strasse|straße")


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and np.isnan(value)) or pd.isna(value)


def normalize_name(name: object, strip_noise: bool = True) -> str:
    """Company-name key for matching (todo item 5, 2026-09-04): lowercase → umlaut
    transliteration → collapse dotted abbreviations (G.m.b.H., e.K., Co. KG) → alnum
    tokens → drop legal-form tokens anywhere (config.LEGAL_FORM_TOKENS) and, with
    ``strip_noise``, title/connector noise (config.NAME_NOISE_TOKENS). The matcher scores
    both keys and keeps the higher score: stripping a title shared by both names can push
    a true pair below the threshold ("Dr. med J Heidt" / "Dr. med. Joachim Heidt")."""
    if _is_missing(name):
        return ""
    text = str(name).lower().translate(_UMLAUTS)
    text = _CO_KG_RE.sub(" cokg ", text)
    for _ in range(3):   # "g.m.b.h." needs several passes of the pairwise collapse
        text = _ABBREV_DOT_RE.sub(r"\1", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    drop = config.LEGAL_FORM_TOKENS | (config.NAME_NOISE_TOKENS if strip_noise else set())
    return " ".join(t for t in text.split() if t not in drop)


def normalize_street(street: object) -> str:
    """Street key for address matching (B6 prep): lowercase, umlauts, "Str." / "-str." /
    "straße" → "strasse", punctuation to spaces, house-number letters glued ("12 a" → "12a")."""
    if _is_missing(street):
        return ""
    text = str(street).lower().translate(_UMLAUTS)
    text = re.sub(r"-\s*str(\.|asse|aße)?(?=\s|$)", "strasse", text)   # "Bahnhof-Str." → glued
    text = _STREET_RE.sub("strasse", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    text = re.sub(r"\b(\d+)\s+([a-z])\b", r"\1\2", text)
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


# category fields whose supplying source is recorded as <col>_source (debug columns)
PROVENANCE_COLS = ("business_type", "business_subtype")


def _priority(source: str) -> int:
    primary = str(source).split("+")[0]
    try:
        return config.SOURCE_PRIORITY.index(primary)
    except ValueError:
        return len(config.SOURCE_PRIORITY)


MAX_SPAN = 40   # cap cells per axis — a degenerate multi-km geometry registers coarsely
QUERY_BATCH = 50_000


def _cell_boxes(bounds: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Blocking as metric rectangles. Each row registers in every BLOCK_GRID_M cell its
    bbox covers (points: one cell; polygons: their footprint, capped at MAX_SPAN per
    axis). Two rows are candidates when some cell of one is the same as, or an 8-neighbour
    of, some cell of the other — i.e. their cell rectangles are within one cell of each
    other. Returned: the cell rectangles (tree side) and the same rectangles dilated by one
    cell (query side), both shrunk by a tiny epsilon so that rectangles two cells apart
    only touch and do not count as intersecting.
    """
    cell = config.BLOCK_GRID_M
    cx0 = np.floor(bounds[:, 0] / cell)
    cy0 = np.floor(bounds[:, 1] / cell)
    cx1 = np.minimum(np.floor(bounds[:, 2] / cell), cx0 + MAX_SPAN)
    cy1 = np.minimum(np.floor(bounds[:, 3] / cell), cy0 + MAX_SPAN)
    eps = cell * 1e-3
    x0, y0 = cx0 * cell + eps, cy0 * cell + eps
    x1, y1 = (cx1 + 1) * cell - eps, (cy1 + 1) * cell - eps
    boxes = shapely.box(x0, y0, x1, y1)
    queries = shapely.box(x0 - cell, y0 - cell, x1 + cell, y1 + cell)
    return boxes, queries


def _matching_edges(geom_m: gpd.GeoSeries, names: list[str], anchors: np.ndarray | None,
                    is_poly: np.ndarray, names_light: list[str] | None = None) -> np.ndarray:
    """All (i, j) pairs (i < j) that satisfy the §A3 match rules — vectorised per batch:
    STRtree rectangle join for candidates, rapidfuzz pairwise scoring on all cores
    (max over the full and the light name key when ``names_light`` is given), numpy
    distance test, shapely contains_xy for the polygon-containment path."""
    n = len(geom_m)
    reps = geom_m.representative_point()
    xs, ys = reps.x.to_numpy(), reps.y.to_numpy()
    geoms = geom_m.to_numpy()
    names_arr = np.array(names, dtype=object)
    light_arr = np.array(names_light, dtype=object) if names_light is not None else None
    has_name = np.array([bool(nm) for nm in names], dtype=bool)
    if light_arr is not None:
        has_name |= np.array([bool(nm) for nm in names_light], dtype=bool)
    ok_row = has_name if anchors is None else (has_name & anchors)

    boxes, queries = _cell_boxes(geom_m.bounds.to_numpy())
    tree = shapely.STRtree(boxes)
    edges: list[np.ndarray] = []
    n_cand = 0
    t0 = time.time()
    for start in range(0, n, QUERY_BATCH):
        stop = min(start + QUERY_BATCH, n)
        qi, tj = tree.query(queries[start:stop], predicate="intersects")
        i = qi + start
        j = tj
        keep = (j > i) & ok_row[i] & ok_row[j]      # each unordered pair once; §A3 gates
        i, j = i[keep], j[keep]
        n_cand += len(i)
        if not len(i):
            continue
        ratio = process.cpdist(names_arr[i], names_arr[j], scorer=fuzz.token_sort_ratio,
                               score_cutoff=config.DEDUP_POLYGON_NAME_RATIO, workers=-1)
        if light_arr is not None:
            ratio = np.maximum(ratio, process.cpdist(
                light_arr[i], light_arr[j], scorer=fuzz.token_sort_ratio,
                score_cutoff=config.DEDUP_POLYGON_NAME_RATIO, workers=-1))
        sim = ratio >= config.DEDUP_POLYGON_NAME_RATIO
        i, j, ratio = i[sim], j[sim], ratio[sim]
        dist = np.hypot(xs[i] - xs[j], ys[i] - ys[j])
        # token evidence for the surviving pairs (full key; light key when a name is
        # nothing but noise tokens)
        shared, shared_chars, min_tok, subset = _token_evidence(
            names_arr, light_arr, i, j)
        # (1) near-identical names up to 100 m; (2) ratio ≥ 80 within 50 m with substance
        accept = ((ratio >= config.DEDUP_NEAR_IDENTICAL_RATIO)
                  & (dist <= config.DEDUP_NEAR_IDENTICAL_DISTANCE_M))
        substance = ((shared >= config.DEDUP_MIN_SHARED_TOKENS)
                     | (shared_chars >= config.DEDUP_MIN_SHARED_CHARS)
                     | (ratio >= config.DEDUP_SUBSTANCE_MAX_RATIO))
        accept |= ((dist <= config.DEDUP_DISTANCE_M) & (ratio >= config.DEDUP_NAME_RATIO)
                   & substance)
        # remaining path needs containment: the polygon is the extra evidence (spec §A3);
        # both names ≥ 2 tokens, plus the configured name rule
        if config.DEDUP_CONTAIN_RULE == "token_subset":
            name_ok = subset
        else:
            name_ok = ratio >= config.DEDUP_CONTAIN_NAME_RATIO
        rest = ~accept & (min_tok >= config.DEDUP_CONTAIN_MIN_TOKENS) & name_ok
        if rest.any():
            ri, rj = i[rest], j[rest]
            contain = np.zeros(len(ri), dtype=bool)
            pi = is_poly[ri]
            if pi.any():
                contain[pi] = shapely.contains_xy(geoms[ri[pi]], xs[rj[pi]], ys[rj[pi]])
            pj = is_poly[rj] & ~contain
            if pj.any():
                contain[pj] = shapely.contains_xy(geoms[rj[pj]], xs[ri[pj]], ys[ri[pj]])
            accept[np.flatnonzero(rest)[contain]] = True
        if accept.any():
            edges.append(np.stack([i[accept], j[accept]], axis=1))
        if (start // QUERY_BATCH) % 20 == 19:
            print(f"[resolve] {stop}/{n} rows blocked, {n_cand:,} candidate pairs, "
                  f"{sum(len(e) for e in edges):,} matches, {time.time() - t0:.0f}s")
    out = np.concatenate(edges) if edges else np.empty((0, 2), dtype=np.int64)
    print(f"[resolve] {n_cand:,} candidate pairs → {len(out):,} matching pairs "
          f"in {time.time() - t0:.0f}s")
    return out


def _token_evidence(names_arr: np.ndarray, light_arr: np.ndarray | None,
                    i: np.ndarray, j: np.ndarray):
    """Per pair: shared token count, characters in the shared tokens, the smaller token
    count of the two names, and whether the shorter name's tokens are all contained in the
    longer one. Evaluated on the full AND the light key (the score is the max of both, so
    the evidence must be too): shared tokens / chars take the stronger key."""
    keys = [names_arr] + ([light_arr] if light_arr is not None else [])
    n = len(i)
    shared = np.zeros(n, dtype=np.int32)
    chars = np.zeros(n, dtype=np.int32)
    min_tok = np.zeros(n, dtype=np.int32)
    subset = np.zeros(n, dtype=bool)
    for arr in keys:
        cache: dict[int, set[str]] = {}
        for k, (a, b) in enumerate(zip(i.tolist(), j.tolist())):
            ta = cache.get(a)
            if ta is None:
                ta = cache[a] = set(arr[a].split()) if arr[a] else set()
            tb = cache.get(b)
            if tb is None:
                tb = cache[b] = set(arr[b].split()) if arr[b] else set()
            common = ta & tb
            shared[k] = max(shared[k], len(common))
            chars[k] = max(chars[k], sum(len(t) for t in common))
            min_tok[k] = max(min_tok[k], min(len(ta), len(tb)))
            subset[k] |= bool(ta) and bool(tb) and (ta <= tb or tb <= ta)
    return shared, chars, min_tok, subset


def _components(n: int, edges: np.ndarray) -> np.ndarray:
    """Connected components of the match graph → root = smallest member index."""
    uf = _UnionFind(n)
    for a, b in edges.tolist():
        uf.union(a, b)
    roots = np.fromiter((uf.find(k) for k in range(n)), dtype=np.int64, count=n)
    smallest = np.full(n, n, dtype=np.int64)
    np.minimum.at(smallest, roots, np.arange(n))
    return smallest[roots]


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
    n = len(gdf)

    geom_m = gdf.geometry.to_crs(config.CRS_METRIC)
    names = [normalize_name(nm) for nm in gdf["name"]]
    names_light = [normalize_name(nm, strip_noise=False) for nm in gdf["name"]]
    is_poly = geom_m.geom_type.isin(["Polygon", "MultiPolygon"]).to_numpy()

    # rows geocoded only to postcode/city centroids must not anchor dedup (spec §B2):
    # dozens of companies share one centroid, so proximity there is meaningless
    anchors = (gdf["dedup_anchor"].fillna(True).to_numpy(dtype=bool)
               if "dedup_anchor" in gdf.columns else None)

    edges = _matching_edges(geom_m, names, anchors, is_poly, names_light)
    roots = _components(n, edges)
    size = np.bincount(roots, minlength=n)[roots]
    singleton_mask = size == 1
    gdf["merged_at"] = merged_at

    # provenance of the category fields (which member supplied the value) — for a
    # singleton that is its own source; for clusters it is recorded below
    singles = gdf[singleton_mask].copy()
    for col in PROVENANCE_COLS:
        singles[col + "_source"] = singles["source"].where(singles[col].notna(), pd.NA) \
            .astype("string")
    out_rows = [singles]

    fill_cols = [c for c in gdf.columns
                 if c not in ("geometry", "source", "source_count", "merged_at")]

    if not singleton_mask.all():
        # rank members within each cluster by SOURCE_PRIORITY (stable on original order),
        # then take the first non-null value per column — vectorised groupby, no per-
        # cluster Python loop (that loop cost ~15 ms per cluster: 44 h on the DE table)
        multi = gdf[~singleton_mask].copy()
        multi["_root"] = roots[~singleton_mask]
        multi["_prio"] = [_priority(s) for s in multi["source"]]
        multi["_idx"] = np.flatnonzero(~singleton_mask)
        multi["_ispoly"] = is_poly[~singleton_mask]
        ranked = multi.sort_values(["_root", "_prio", "_idx"], kind="stable")
        plain = pd.DataFrame(ranked.drop(columns="geometry"))
        grp = plain.groupby("_root", sort=True)
        clusters = grp[fill_cols].first()          # first non-null in priority order
        for col in PROVENANCE_COLS:
            src = plain.loc[plain[col].notna()].groupby("_root")["source"].first()
            clusters[col + "_source"] = src.reindex(clusters.index).astype("string")
        clusters["source"] = grp["source"].agg(
            lambda s: "+".join(sorted({x for src in s for x in str(src).split("+")})))
        clusters["source_count"] = clusters["source"].str.count(r"\+") + 1
        clusters["merged_at"] = merged_at
        clusters["member_ids"] = grp["id"].agg(lambda s: "|".join(map(str, s)))
        # geometry: first polygon member in priority order, else the first member's point
        by_geom = ranked.sort_values(["_root", "_ispoly", "_prio", "_idx"],
                                     ascending=[True, False, True, True], kind="stable")
        pick = by_geom.groupby("_root", sort=True)["_idx"].first()
        geoms = gdf.geometry.iloc[pick.to_numpy()].to_numpy()
        clusters = gpd.GeoDataFrame(clusters.reset_index(drop=True), geometry=geoms, crs=gdf.crs)
        reps4326 = clusters.geometry.representative_point()
        clusters["longitude"] = reps4326.x
        clusters["latitude"] = reps4326.y
        out_rows.append(clusters)

    out = pd.concat(out_rows, ignore_index=True)
    out = gpd.GeoDataFrame(out.drop(columns=["_root", "_prio", "_idx", "_ispoly"],
                                    errors="ignore"), geometry="geometry", crs=gdf.crs)
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
