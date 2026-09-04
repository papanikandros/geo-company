"""Splink vs heuristic entity resolution — benchmark on one state (todo item 5, 2026-09-04).

    uv run python scripts/splink_benchmark.py [--states bremen] [--threshold 0.9]

Same rows as `geoextract extract` (cached source parquets, state subset), same 100 m
blocking (cell + 8 neighbours). Heuristic = resolve.resolve (token_sort_ratio ≥ 80 within
50 m, ≥ 60 with polygon containment). Splink = Fellegi–Sunter model (EM-trained) on
Jaro–Winkler of the normalised name, metric distance bands and postcode. Output: pairwise
agreement between the two cluster sets, samples of the disagreements, a blocking-radius
sweep, and a markdown report under data/geoextract/eval/.
"""
from __future__ import annotations

import argparse
import itertools
import random
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from rapidfuzz import fuzz, process

from geoextract import config, paths, resolve
from geoextract.sources import abwaerme as abw_src
from geoextract.sources import ied as ied_src
from geoextract.sources import mastr as mastr_src
from geoextract.sources import overture as ovt_src


def load_frames(states: list[str], data_root: Path) -> list[gpd.GeoDataFrame]:
    """Mirror pipeline.run_extract's frame assembly for a state subset (cache only)."""
    wanted = {config.SLUG_STATE_NAMES[s] for s in states}
    frames = [gpd.read_parquet(paths.source_parquet(data_root, "osm", s)) for s in states]
    for fn in (ied_src.extract_ied, abw_src.extract_abwaerme, mastr_src.extract_mastr):
        df = fn(data_root)
        frames.append(df[df["state"].isin(wanted)].reset_index(drop=True))
    ovt = ovt_src.extract_overture(data_root)
    in_bbox = pd.Series(False, index=ovt.index)
    for s in states:
        x0, y0, x1, y1 = gpd.read_parquet(paths.boundaries_parquet(data_root, s)).total_bounds
        in_bbox |= ovt["longitude"].between(x0, x1) & ovt["latitude"].between(y0, y1)
    frames.append(ovt[ovt["state"].isin(wanted) | (ovt["state"].isna() & in_bbox)]
                  .reset_index(drop=True))
    return frames


def cluster_pairs(member_lists) -> set[tuple[str, str]]:
    out = set()
    for mems in member_lists:
        for a, b in itertools.combinations(sorted(mems), 2):
            out.add((a, b))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--states", default="bremen")
    ap.add_argument("--threshold", type=float, default=0.9,
                    help="Splink match probability for clustering")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    data_root = paths.data_root(args.data_dir)
    states = [s.strip() for s in args.states.split(",")]

    frames = load_frames(states, data_root)
    gdf = pd.concat(frames, ignore_index=True).drop_duplicates("id").reset_index(drop=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs)
    print(f"[bench] {len(gdf)} input rows from {len(frames)} frames")

    # ---------- heuristic ----------
    t0 = time.time()
    merged = resolve.resolve(frames)
    t_heur = time.time() - t0
    heur_members = [m.split("|") for m in merged["member_ids"].dropna().astype(str)]
    heur_pairs = cluster_pairs(heur_members)
    print(f"[bench] heuristic: {len(merged)} rows, {len(heur_members)} clusters, "
          f"{len(heur_pairs)} pairs, {t_heur:.1f}s")

    # ---------- Splink input ----------
    geom_m = gdf.geometry.to_crs(config.CRS_METRIC)
    reps = geom_m.representative_point()
    cell = config.BLOCK_GRID_M
    sp = pd.DataFrame({
        "unique_id": gdf["id"].astype(str),
        "name_full": [resolve.normalize_name(n) or None for n in gdf["name"]],
        "name_light": [resolve.normalize_name(n, strip_noise=False) or None for n in gdf["name"]],
        "x": reps.x.to_numpy(), "y": reps.y.to_numpy(),
        "cx": np.floor(reps.x.to_numpy() / cell).astype(int),
        "cy": np.floor(reps.y.to_numpy() / cell).astype(int),
        "postcode": gdf["address_postcode"].astype("string").astype(object).where(
            gdf["address_postcode"].notna(), None),
        "source": gdf["source"].astype(str),
    })
    sp["name_first"] = sp["name_full"].map(lambda s: s.split()[0] if isinstance(s, str) and s else None)

    import splink.comparison_level_library as cll
    import splink.comparison_library as cl
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on

    # blocking: same 100 m cell or one of its 8 neighbours (as the heuristic)
    neighbours = [f"l.cx = r.cx + ({dx}) and l.cy = r.cy + ({dy})"
                  for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
    dist_cmp = cl.CustomComparison(
        output_column_name="dist_m",
        comparison_levels=[
            cll.CustomLevel("sqrt(power(x_l - x_r, 2) + power(y_l - y_r, 2)) <= 25", "≤ 25 m"),
            cll.CustomLevel("sqrt(power(x_l - x_r, 2) + power(y_l - y_r, 2)) <= 50", "≤ 50 m"),
            cll.CustomLevel("sqrt(power(x_l - x_r, 2) + power(y_l - y_r, 2)) <= 100", "≤ 100 m"),
            cll.ElseLevel(),
        ],
    )
    settings = SettingsCreator(
        link_type="dedupe_only",
        unique_id_column_name="unique_id",
        blocking_rules_to_generate_predictions=neighbours,
        comparisons=[
            cl.JaroWinklerAtThresholds("name_full", [0.95, 0.88, 0.8]),
            cl.JaroWinklerAtThresholds("name_light", [0.95, 0.88]),
            dist_cmp,
            cl.ExactMatch("postcode").configure(term_frequency_adjustments=True),
        ],
        retain_intermediate_calculation_columns=True,
    )
    t0 = time.time()
    db_api = DuckDBAPI()
    linker = Linker(sp, settings, db_api)
    linker.training.estimate_probability_two_random_records_match(
        ["l.name_full = r.name_full and l.cx = r.cx and l.cy = r.cy"], recall=0.7)
    linker.training.estimate_u_using_random_sampling(max_pairs=2e6)
    # EM: block on name to learn distance/postcode; block on cell to learn the name params
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("name_full"))
    linker.training.estimate_parameters_using_expectation_maximisation(
        "l.cx = r.cx and l.cy = r.cy")
    pred = linker.inference.predict(threshold_match_probability=0.5)
    pred_df = pred.as_pandas_dataframe()
    clusters = linker.clustering.cluster_pairwise_predictions_at_threshold(pred, args.threshold)
    cl_df = clusters.as_pandas_dataframe()
    t_spl = time.time() - t0
    sizes = cl_df.groupby("cluster_id").size()
    spl_members = [g["unique_id"].tolist() for _, g in
                   cl_df[cl_df["cluster_id"].isin(sizes[sizes > 1].index)].groupby("cluster_id")]
    spl_pairs = cluster_pairs(spl_members)
    print(f"[bench] splink: {len(pred_df)} scored pairs ≥ 0.5, {len(spl_members)} clusters at "
          f"p ≥ {args.threshold}, {len(spl_pairs)} pairs, {t_spl:.1f}s (incl. training)")

    # ---------- agreement ----------
    both = heur_pairs & spl_pairs
    only_h, only_s = heur_pairs - spl_pairs, spl_pairs - heur_pairs
    prec = len(both) / len(spl_pairs) if spl_pairs else float("nan")
    rec = len(both) / len(heur_pairs) if heur_pairs else float("nan")
    print(f"[bench] pairs: both {len(both)} | heuristic-only {len(only_h)} | splink-only "
          f"{len(only_s)} | agreement vs heuristic: precision {prec:.3f} recall {rec:.3f}")

    # threshold sweep for splink
    sweep = []
    for thr in (0.5, 0.7, 0.8, 0.9, 0.95, 0.99):
        c = linker.clustering.cluster_pairwise_predictions_at_threshold(pred, thr).as_pandas_dataframe()
        sz = c.groupby("cluster_id").size()
        mem = [g["unique_id"].tolist() for _, g in
               c[c["cluster_id"].isin(sz[sz > 1].index)].groupby("cluster_id")]
        pr = cluster_pairs(mem)
        sweep.append((thr, len(mem), len(pr), len(pr & heur_pairs), len(pr - heur_pairs), len(heur_pairs - pr)))
    # blocking-radius sweep for the heuristic rule (name ≥ 80): how many pairs per radius
    names = np.array(sp["name_full"].fillna("").tolist(), dtype=object)
    pts = shapely.points(sp["x"].to_numpy(), sp["y"].to_numpy())
    tree = shapely.STRtree(pts)
    qi, tj = tree.query(shapely.buffer(pts, 1000.0), predicate="intersects")
    keep = (tj > qi) & (names[qi] != "") & (names[tj] != "")
    qi, tj = qi[keep], tj[keep]
    ratio = process.cpdist(names[qi], names[tj], scorer=fuzz.token_sort_ratio,
                           score_cutoff=config.DEDUP_NAME_RATIO, workers=-1)
    sim = ratio >= config.DEDUP_NAME_RATIO
    d = np.hypot(sp["x"].to_numpy()[qi[sim]] - sp["x"].to_numpy()[tj[sim]],
                 sp["y"].to_numpy()[qi[sim]] - sp["y"].to_numpy()[tj[sim]])
    radius = [(r, int((d <= r).sum())) for r in (25, 50, 100, 200, 500, 1000)]
    print("[bench] name-similar pairs (≥ 80) within radius:", radius)

    # ---------- samples + report ----------
    info = gdf.set_index(gdf["id"].astype(str))
    x = dict(zip(sp["unique_id"], sp["x"])); y = dict(zip(sp["unique_id"], sp["y"]))
    def describe(pair):
        a, b = pair
        dist = float(np.hypot(x[a] - x[b], y[a] - y[b]))
        return (f"{str(info.at[a, 'name'])[:38]!r} ({info.at[a, 'source']}) ↔ "
                f"{str(info.at[b, 'name'])[:38]!r} ({info.at[b, 'source']}), {dist:.0f} m, "
                f"ratio {fuzz.token_sort_ratio(resolve.normalize_name(info.at[a, 'name']), resolve.normalize_name(info.at[b, 'name'])):.0f}")
    rnd = random.Random(0)
    lines = [f"# Splink vs heuristic — {','.join(states)} ({len(gdf)} rows)", "",
             f"heuristic: {len(heur_members)} clusters / {len(heur_pairs)} pairs in {t_heur:.1f} s",
             f"splink (p ≥ {args.threshold}): {len(spl_members)} clusters / {len(spl_pairs)} pairs in {t_spl:.1f} s",
             f"agreement: both {len(both)}, heuristic-only {len(only_h)}, splink-only {len(only_s)} "
             f"→ precision {prec:.3f}, recall {rec:.3f} (Splink measured against the heuristic)", "",
             "## Splink threshold sweep", "| p ≥ | clusters | pairs | both | splink-only | heuristic-only |", "|---|---|---|---|---|---|"]
    lines += [f"| {t} | {c} | {p} | {b} | {so} | {ho} |" for t, c, p, b, so, ho in sweep]
    lines += ["", "## Blocking-radius sweep (name ratio ≥ 80)", "| radius m | pairs |", "|---|---|"]
    lines += [f"| {r} | {n} |" for r, n in radius]
    lines += ["", "## Model parameters (EM-trained)",
              "| comparison | level | m | u | match weight |", "|---|---|---|---|---|"]
    model = linker.misc.save_model_to_json()
    for comp in model["comparisons"]:
        for lvl in comp["comparison_levels"]:
            m, u = lvl.get("m_probability"), lvl.get("u_probability")
            if m is None or u is None or not u:
                continue
            lines.append(f"| {comp['output_column_name']} | {lvl.get('label_for_charts', lvl['sql_condition'])[:40]} "
                         f"| {m:.4f} | {u:.6f} | {np.log2(m / u):+.2f} |")
    lines += ["", f"## {min(25, len(only_s))} sample Splink-only pairs"]
    lines += [f"- {describe(p)}" for p in rnd.sample(sorted(only_s), min(25, len(only_s)))]
    lines += ["", f"## {min(25, len(only_h))} sample heuristic-only pairs"]
    lines += [f"- {describe(p)}" for p in rnd.sample(sorted(only_h), min(25, len(only_h)))]
    out_dir = data_root / "geoextract" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"splink_vs_heuristic_{'-'.join(states)}.md"
    out.write_text("\n".join(lines))
    print(f"[bench] report → {out}")


if __name__ == "__main__":
    main()
