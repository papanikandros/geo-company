"""B5 acceptance checks on the parsed MaStR export (mastr.db).

Ports the location/plausibility tests of Kotthoff/Tepe et al. 2023
(github.com/FlorianK13/verify-marktstammdaten) from dbt/PostGIS to pandas/GeoPandas,
plus the unit->site clustering and geocoding-fallback diagnostics motivated by
Plinke et al. 2025. Read-only on the database; writes a markdown report and CSV samples
of failing rows.

Usage: uv run python scripts/mastr_acceptance_checks.py [--db PATH] [--out DIR]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VG5000_KRS = REPO / "data/raw/vg5000/vg5000_ebenen_1231/VG5000_KRS.shp"

TECHS = ["wind", "solar", "biomass", "hydro", "combustion", "gsgk", "nuclear", "storage"]
COLS = [
    "EinheitMastrNummer", "EinheitBetriebsstatus", "Laengengrad", "Breitengrad",
    "Gemeindeschluessel", "Landkreis", "Bundesland", "Postleitzahl", "Ort", "Strasse",
    "Hausnummer", "Nettonennleistung", "Bruttoleistung", "Inbetriebnahmedatum",
    "GeplantesInbetriebnahmedatum", "Lage", "Technologie", "Nabenhoehe", "Rotordurchmesser",
    "InAnspruchGenommeneFlaeche", "AnlagenbetreiberMastrNummer", "LokationMastrNummer",
]
# Paper reference values (Kotthoff/Tepe et al. 2023, 2024-03 snapshot) for comparison only.
PAPER_COORD_COMPLETENESS = {"wind": 0.97, "solar": 0.05, "storage": 0.0}
DISTRICT_BUFFER_DEG = 0.015  # same buffer as the reference dbt test
POWER_MAX_KW = {"wind": 22_000, "biomass": 150_000}
YEAR_RANGE = {"wind": (1980, 2030)}
YEAR_RANGE_DEFAULT = (1950, 2030)
RE_MASTR = re.compile(r"^[A-Z]{3}\d{12}$")
RE_AGS8 = re.compile(r"^\d{8}$")
RE_PLZ = re.compile(r"^\d{5}$")
BBOX = dict(lat=(45.0, 56.0), lon=(4.0, 17.0))  # spec §2 sanity bounds


def existing_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def load_table(con: sqlite3.Connection, tech: str) -> pd.DataFrame | None:
    table = f"{tech}_extended"
    have = existing_columns(con, table)
    if not have:
        return None
    cols = [c for c in COLS if c in have]
    q = f'SELECT {", ".join(f"\"{c}\"" for c in cols)} FROM "{table}"'
    df = pd.read_sql_query(q, con)
    for c in ("Laengengrad", "Breitengrad", "Nettonennleistung", "Bruttoleistung",
              "Nabenhoehe", "Rotordurchmesser", "InAnspruchGenommeneFlaeche"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("EinheitMastrNummer", "Gemeindeschluessel", "Postleitzahl", "Lage",
              "EinheitBetriebsstatus", "LokationMastrNummer", "AnlagenbetreiberMastrNummer"):
        if c in df:
            df[c] = df[c].astype("string")
    return df


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.2f}%" if d else "n/a"


def year_of(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.year


def district_check(df: pd.DataFrame, krs: gpd.GeoDataFrame, tech: str) -> tuple[dict, pd.DataFrame]:
    """Points must lie inside their declared district (AGS = first 5 digits of the 8-digit
    Gemeindeschluessel), buffered by DISTRICT_BUFFER_DEG in EPSG:4326 like the reference test."""
    sub = df.dropna(subset=["Laengengrad", "Breitengrad", "Gemeindeschluessel"]).copy()
    if tech == "wind" and "Lage" in sub and sub["Lage"].notna().any():
        sub = sub[sub["Lage"] == "Windkraft an Land"]
    # if Lage is unpopulated (lookup gap in this open-mastr version), offshore units are
    # excluded anyway below: they carry no 8-digit Gemeindeschluessel
    sub = sub[sub["Gemeindeschluessel"].str.match(r"^\d{8}$", na=False)]
    sub["AGS"] = sub["Gemeindeschluessel"].str[:5]
    if sub.empty:
        return {"tested": 0}, sub
    pts = gpd.GeoDataFrame(
        sub, geometry=gpd.points_from_xy(sub["Laengengrad"], sub["Breitengrad"]), crs="EPSG:4326"
    )
    buffered = krs[["AGS", "geometry"]].copy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # degree buffer is deliberate (reference test does the same)
        buffered["geometry"] = buffered.geometry.buffer(DISTRICT_BUFFER_DEG)
    merged = pts.merge(buffered.rename(columns={"geometry": "district_geom"}), on="AGS", how="left")
    unknown_ags = merged["district_geom"].isna()
    known = merged[~unknown_ags]
    inside = gpd.GeoSeries(known.geometry.values, crs="EPSG:4326").within(
        gpd.GeoSeries(known["district_geom"].values, crs="EPSG:4326")
    )
    fails = known[~inside.values].copy()
    if not fails.empty:
        # distance from point to (unbuffered) district polygon, metric CRS
        unbuf = krs.set_index("AGS").geometry.to_crs(25832)
        p_m = gpd.GeoSeries(fails.geometry.values, crs="EPSG:4326").to_crs(25832)
        d_m = unbuf.loc[fails["AGS"]].reset_index(drop=True)
        fails["dist_km"] = np.round(p_m.distance(d_m, align=False).values / 1000, 1)
    res = {
        "tested": int(len(known)),
        "unknown_ags": int(unknown_ags.sum()),
        "outside": int(len(fails)),
        "outside_pct": pct(len(fails), len(known)),
    }
    if not fails.empty:
        d = fails["dist_km"]
        res.update(
            dist_median_km=float(d.median()), dist_p90_km=float(d.quantile(0.9)),
            dist_max_km=float(d.max()), n_over_10km=int((d > 10).sum()),
            n_over_100km=int((d > 100).sum()),
        )
    keep = [c for c in ("EinheitMastrNummer", "EinheitBetriebsstatus", "Gemeindeschluessel",
                        "Landkreis", "Postleitzahl", "Laengengrad", "Breitengrad", "dist_km") if c in fails]
    return res, pd.DataFrame(fails[keep]).sort_values("dist_km", ascending=False) if not fails.empty else fails


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / "data/raw/mastr/mastr.db"))
    ap.add_argument("--out", default=str(REPO / "data/raw/mastr/acceptance"))
    args = ap.parse_args()
    out = Path(args.out)
    (out / "failures").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db)
    krs = gpd.read_file(VG5000_KRS)[["AGS", "GEN", "geometry"]].to_crs(4326)

    lines = [f"# MaStR acceptance report\n", f"db: `{args.db}`  \nVG5000 districts: {len(krs)}\n"]
    site_frames = []
    summary_rows = []

    for tech in TECHS:
        df = load_table(con, tech)
        if df is None:
            lines.append(f"\n## {tech}: table `{tech}_extended` NOT FOUND\n")
            continue
        n = len(df)
        lines.append(f"\n## {tech} — {n:,} rows\n")
        # --- status vocabulary
        if "EinheitBetriebsstatus" in df:
            vc = df["EinheitBetriebsstatus"].value_counts(dropna=False)
            lines.append("status: " + ", ".join(f"{k}={v:,}" for k, v in vc.items()) + "\n")
            ib = df[df["EinheitBetriebsstatus"] == "In Betrieb"]
        else:
            ib = df
        # --- coordinate completeness
        has = df[["Laengengrad", "Breitengrad"]].notna().all(axis=1) if "Laengengrad" in df else pd.Series(False, index=df.index)
        has_ib = has.loc[ib.index]
        ref = PAPER_COORD_COMPLETENESS.get(tech)
        lines.append(
            f"coordinates present: all={pct(int(has.sum()), n)}, In Betrieb={pct(int(has_ib.sum()), len(ib))}"
            + (f" (paper 2024-03 ref ≈ {ref:.0%})" if ref is not None else "") + "\n"
        )
        row = dict(tech=tech, rows=n, in_betrieb=len(ib), coord_all=pct(int(has.sum()), n),
                   coord_ib=pct(int(has_ib.sum()), len(ib)))
        # --- bbox sanity (spec §2)
        if has.any():
            c = df[has]
            oob = ~c["Breitengrad"].between(*BBOX["lat"]) | ~c["Laengengrad"].between(*BBOX["lon"])
            lines.append(f"coordinates outside DE sanity bbox (lat 45–56, lon 4–17): {int(oob.sum()):,} ({pct(int(oob.sum()), int(has.sum()))})\n")
            row["bbox_out"] = int(oob.sum())
            if oob.any():
                c[oob][[k for k in ("EinheitMastrNummer", "Bundesland", "Laengengrad", "Breitengrad") if k in c]].head(200).to_csv(out / "failures" / f"{tech}_bbox.csv", index=False)
        # --- point in declared district
        if has.any() and "Gemeindeschluessel" in df:
            res, fails = district_check(df, krs, tech)
            lines.append(f"point-in-declared-district (buffer {DISTRICT_BUFFER_DEG}°): {res}\n")
            row["district_out_pct"] = res.get("outside_pct")
            if len(fails):
                fails.head(500).to_csv(out / "failures" / f"{tech}_district.csv", index=False)
        # --- regex checks
        for col, rx, label in (("EinheitMastrNummer", RE_MASTR, "mastr_id"),
                               ("Gemeindeschluessel", RE_AGS8, "gemeindeschluessel"),
                               ("Postleitzahl", RE_PLZ, "plz")):
            if col in df:
                s = df[col].dropna()
                bad = ~s.str.match(rx.pattern)
                lines.append(f"regex {label}: {int(bad.sum()):,} bad of {len(s):,} non-null ({pct(int(bad.sum()), len(s))}); null={int(df[col].isna().sum()):,}\n")
        # --- value ranges
        if "Nettonennleistung" in df:
            p = df["Nettonennleistung"]
            neg = int((p < 0).sum()); null_ib = int(ib["Nettonennleistung"].isna().sum())
            msg = f"power (kW): negative={neg:,}, null among In Betrieb={null_ib:,}"
            if tech in POWER_MAX_KW:
                over = int((p > POWER_MAX_KW[tech]).sum()); msg += f", > {POWER_MAX_KW[tech]:,} kW: {over:,}"
            lines.append(msg + "\n")
        if "Inbetriebnahmedatum" in df:
            y = year_of(df["Inbetriebnahmedatum"])
            if "GeplantesInbetriebnahmedatum" in df:
                y = y.fillna(year_of(df["GeplantesInbetriebnahmedatum"]))
            lo, hi = YEAR_RANGE.get(tech, YEAR_RANGE_DEFAULT)
            yy = y.dropna()
            bad = ~yy.between(lo, hi)
            lines.append(f"installation year outside [{lo},{hi}]: {int(bad.sum()):,} of {len(yy):,}\n")
        if tech == "wind" and {"Nettonennleistung", "Rotordurchmesser"} <= set(df):
            w = df.dropna(subset=["Nettonennleistung", "Rotordurchmesser"])
            w = w[w["Rotordurchmesser"] > 0]
            ratio = w["Nettonennleistung"] / w["Rotordurchmesser"] ** 2  # kW/m² (0.16–0.7 per paper)
            bad = ~ratio.between(0.16, 0.7)
            lines.append(f"wind specific power outside 160–700 W/m²: {int(bad.sum()):,} of {len(w):,} ({pct(int(bad.sum()), len(w))})\n")
        if tech == "solar" and {"Bruttoleistung", "InAnspruchGenommeneFlaeche"} <= set(df):
            # ground-mounted proxy: only Freiflächen report a utilized area (Lage is unpopulated here)
            s = df[df["InAnspruchGenommeneFlaeche"].gt(0) & df["Bruttoleistung"].notna()]
            if "Lage" in df and df["Lage"].notna().any():
                s = s[s["Lage"] == "Freifläche"]
            dens = s["Bruttoleistung"] / s["InAnspruchGenommeneFlaeche"]  # kW per ha (50–1500 per paper)
            bad = ~dens.between(50, 1500)
            lines.append(f"ground-mounted PV density outside 50–1500 kW/ha: {int(bad.sum()):,} of {len(s):,} ({pct(int(bad.sum()), len(s))})\n")
        # --- geocoding fallback need (In Betrieb, no coords, but address present)
        addr_cols = [c for c in ("Strasse", "Postleitzahl", "Ort") if c in ib]
        if addr_cols:
            no_coord = ~has_ib
            has_addr = ib["Postleitzahl"].notna() & ib["Ort"].notna() if {"Postleitzahl", "Ort"} <= set(ib) else ib[addr_cols[0]].notna()
            need = int((no_coord & has_addr).sum())
            hopeless = int((no_coord & ~has_addr).sum())
            msg = f"geocoding fallback: In Betrieb without coords but with PLZ+Ort = {need:,}; without any address = {hopeless:,}"
            if tech in ("solar", "storage") and "Nettonennleistung" in ib:
                big = ib["Nettonennleistung"] >= 100
                msg += f"; among ≥100 kW units: need={int((no_coord & has_addr & big).sum()):,}, hopeless={int((no_coord & ~has_addr & big).sum()):,}, total ≥100 kW={int(big.sum()):,}"
            lines.append(msg + "\n")
            row["geocode_need_ib"] = need
        # --- site clustering inputs
        if "LokationMastrNummer" in ib:
            site_frames.append(ib[[c for c in ("EinheitMastrNummer", "LokationMastrNummer", "AnlagenbetreiberMastrNummer") if c in ib]].assign(tech=tech))
        summary_rows.append(row)

    # --- unit -> site clustering diagnostics (Plinke et al.: per-unit registration inflates site counts)
    if site_frames:
        allu = pd.concat(site_frames, ignore_index=True)
        lines.append("\n## Unit → site clustering (In Betrieb, all technologies)\n")
        n = len(allu)
        no_lok = int(allu["LokationMastrNummer"].isna().sum())
        lines.append(f"units: {n:,}; without LokationMastrNummer: {no_lok:,} ({pct(no_lok, n)})\n")
        per_lok = allu.dropna(subset=["LokationMastrNummer"]).groupby("LokationMastrNummer").size()
        lines.append(f"distinct Lokationen: {len(per_lok):,} → units/Lokation: mean={per_lok.mean():.2f}, median={per_lok.median():.0f}, p99={per_lok.quantile(0.99):.0f}, max={per_lok.max():,}; Lokationen with >1 unit: {int((per_lok > 1).sum()):,} ({pct(int((per_lok > 1).sum()), len(per_lok))})\n")
        lines.append(f"unit-count inflation vs Lokation count: {n / max(len(per_lok), 1):.2f}× (paper/Plinke: MaStR overcounts sites ~35%)\n")
        if "AnlagenbetreiberMastrNummer" in allu:
            key = allu.dropna(subset=["LokationMastrNummer", "AnlagenbetreiberMastrNummer"])
            per_site = key.groupby(["AnlagenbetreiberMastrNummer", "LokationMastrNummer"]).size()
            multi_op = key.groupby("LokationMastrNummer")["AnlagenbetreiberMastrNummer"].nunique()
            lines.append(f"distinct (operator, Lokation) sites: {len(per_site):,}; Lokationen shared by >1 operator: {int((multi_op > 1).sum()):,}\n")
        mixed = allu.dropna(subset=["LokationMastrNummer"]).groupby("LokationMastrNummer")["tech"].nunique()
        lines.append(f"Lokationen mixing technologies: {int((mixed > 1).sum()):,}\n")

    lines.append("\n## Summary table\n")
    summary = pd.DataFrame(summary_rows).fillna("")
    lines.append("| " + " | ".join(summary.columns) + " |\n|" + "---|" * len(summary.columns) + "\n")
    for _, r in summary.iterrows():
        lines.append("| " + " | ".join(str(v) for v in r.values) + " |\n")
    report = out / "acceptance_report.md"
    report.write_text("".join(lines), encoding="utf-8")
    print("".join(lines))
    print(f"\n[written] {report}")


if __name__ == "__main__":
    main()
