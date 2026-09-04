"""Render a geoextract parquet as a static HTML map for visual verification — spec §8.x.

    uv run --extra viz python scripts/preview_layer.py <parquet> [--color <column>]

Writes data/previews/<stem>.html (folium standalone; open in any browser). Check after
every stage: points on plausible places, plausible areas, industrial sites coloured
correctly, no points in the sea.

Mirrors the interaction set of app_bokeh_companies.py: layer toggle buttons
(CheckboxButtonGroup), box + lasso select feeding a data table, CSV download of the
selection, hover tooltips, reset view.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from string import Template

import geopandas as gpd
import pandas as pd

PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
           "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

# attribute columns embedded per point (table + tooltip + CSV export)
ATTR_COLS = ["id", "name", "business_type", "business_type_source", "business_subtype",
             "business_subtype_source", "address_full",
             "website", "grounds_area_m2", "source", "confidence_score",
             "nace_primary", "is_industrial", "ied_activity",
             "abw_heat_mwh_a", "abw_temp_c", "geocode_precision",
             "ovt_confidence", "ovt_dataset", "mastr_techs", "mastr_units", "mastr_tech_detail",
             "mastr_wz_abschnitt", "mastr_wz_gruppe", "mastr_wz_code", "mastr_status",
             "mastr_coord_method"]

# IE-RL Anhang I — short German labels for the tooltip (regulatory text, no
# classification). Lookup tries the exact code, then strips trailing "(…)" groups:
# "6.4(b)(ii)" → "6.4(b)" → "6.4".
IED_ACTIVITY_LABELS = {
    "1.1": "Verfeuerung von Brennstoffen (≥ 50 MW)",
    "1.2": "Mineralöl- und Gasraffinerien",
    "1.3": "Kokereien",
    "1.4": "Vergasung/Verflüssigung von Brennstoffen",
    "2.1": "Rösten/Sintern von Metallerz",
    "2.2": "Roheisen- oder Stahlerzeugung",
    "2.3": "Verarbeitung von Eisenmetallen",
    "2.3(a)": "Warmwalzen von Eisenmetallen",
    "2.3(b)": "Schmieden mit Hämmern",
    "2.3(c)": "Schmelztauchbeschichten von Eisenmetallen",
    "2.4": "Eisenmetallgießereien",
    "2.5(a)": "Gewinnung von Nichteisen-Rohmetallen",
    "2.5(b)": "Schmelzen von Nichteisenmetallen",
    "2.6": "Oberflächenbehandlung von Metallen/Kunststoffen (elektrolytisch/chemisch)",
    "3.1(a)": "Zementklinkerherstellung",
    "3.1(b)": "Kalkherstellung",
    "3.1(c)": "Magnesiumoxidherstellung",
    "3.2": "Asbestverarbeitung",
    "3.3": "Glasherstellung",
    "3.4": "Schmelzen mineralischer Stoffe / Mineralfasern",
    "3.5": "Keramische Erzeugnisse (Ziegel, Fliesen, …)",
    "4.1": "Herstellung organischer Grundchemikalien",
    "4.1(a)": "Einfache Kohlenwasserstoffe",
    "4.1(b)": "Sauerstoffhaltige Kohlenwasserstoffe (Alkohole, Aldehyde, …)",
    "4.1(c)": "Schwefelhaltige Kohlenwasserstoffe",
    "4.1(d)": "Stickstoffhaltige Kohlenwasserstoffe (Amine, …)",
    "4.1(e)": "Phosphorhaltige Kohlenwasserstoffe",
    "4.1(f)": "Halogenhaltige Kohlenwasserstoffe",
    "4.1(g)": "Metallorganische Verbindungen",
    "4.1(h)": "Kunststoffe (Polymere, Chemiefasern)",
    "4.1(i)": "Synthetische Kautschuke",
    "4.1(j)": "Farbstoffe und Pigmente",
    "4.1(k)": "Tenside",
    "4.2": "Herstellung anorganischer Grundchemikalien",
    "4.2(a)": "Anorganische Gase",
    "4.2(b)": "Säuren",
    "4.2(c)": "Basen",
    "4.2(d)": "Salze",
    "4.2(e)": "Nichtmetalle, Metalloxide",
    "4.3": "Düngemittelherstellung",
    "4.4": "Pflanzenschutzmittel/Biozide",
    "4.5": "Arzneimittelherstellung",
    "4.6": "Explosivstoffherstellung",
    "5.1": "Beseitigung/Verwertung gefährlicher Abfälle",
    "5.2": "Abfallverbrennung",
    "5.2(a)": "Verbrennung nicht gefährlicher Abfälle",
    "5.2(b)": "Verbrennung gefährlicher Abfälle",
    "5.3(a)": "Beseitigung nicht gefährlicher Abfälle",
    "5.3(b)": "Verwertung nicht gefährlicher Abfälle",
    "5.4": "Deponien",
    "5.5": "Zeitweilige Lagerung gefährlicher Abfälle",
    "5.6": "Unterirdische Lagerung gefährlicher Abfälle",
    "6.1(a)": "Zellstoffherstellung",
    "6.1(b)": "Papier- und Pappeherstellung",
    "6.1(c)": "Holzwerkstoffplattenherstellung",
    "6.2": "Vorbehandlung/Färben von Textilfasern",
    "6.3": "Gerben von Häuten und Fellen",
    "6.4(a)": "Schlachthöfe",
    "6.4(b)": "Lebensmittelherstellung",
    "6.4(b)(i)": "Lebensmittel aus tierischen Rohstoffen",
    "6.4(b)(ii)": "Lebensmittel aus pflanzlichen Rohstoffen",
    "6.4(b)(iii)": "Lebensmittel aus tierischen und pflanzlichen Rohstoffen",
    "6.4(c)": "Milchbehandlung und -verarbeitung",
    "6.5": "Tierkörperbeseitigung",
    "6.6(a)": "Intensivhaltung von Geflügel",
    "6.6(b)": "Intensivhaltung von Mastschweinen",
    "6.6(c)": "Intensivhaltung von Säuen",
    "6.7": "Oberflächenbehandlung mit organischen Lösungsmitteln",
    "6.8": "Herstellung von Kohlenstoff/Elektrographit",
    "6.9": "CO₂-Abscheidung (CCS)",
    "6.10": "Holzschutzmittelbehandlung",
    "6.11": "Eigenständige Behandlung von Industrieabwasser",
}


_WZ_SECTION_RE = re.compile(r"^\s*Abschnitt\s+([A-Z])\s*[-–—]\s*(.+?)\s*$")


def _activity_label(code) -> str | None:
    if code is None or not str(code).strip():
        return None
    c = str(code).strip()
    probe = c
    while probe:
        if probe in IED_ACTIVITY_LABELS:
            return f"{c} – {IED_ACTIVITY_LABELS[probe]}"
        if "(" not in probe:
            break
        probe = probe[:probe.rindex("(")]
    return c


# Annex-I chapter → activity-group label (pure 1:1 read of ied_activity, no
# classification); 6.6 (intensive livestock) split out so it is one click to hide.
def _heat_band(mwh) -> str | None:
    """Abwärme heat-quantity band (native filter of the abwaerme dataset row)."""
    try:
        v = float(mwh)
    except (TypeError, ValueError):
        return None
    if v != v:   # NaN
        return None
    if v < 1_000:
        return "< 1 GWh/a"
    if v < 10_000:
        return "1–10 GWh/a"
    if v < 100_000:
        return "10–100 GWh/a"
    return "> 100 GWh/a"


def _activity_group(code) -> str | None:
    if code is None or (isinstance(code, float)) or not str(code).strip():
        return None
    c = str(code).strip()
    if c.startswith("6.6"):
        return "livestock"
    return {"1": "energy", "2": "metals", "3": "minerals", "4": "chemicals",
            "5": "waste", "6": "other"}.get(c[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet", type=Path)
    parser.add_argument("--color", default="business_type",
                        help="column to colour by (e.g. nace_section, is_industrial)")
    parser.add_argument("--landuse", type=Path, nargs="*", default=None,
                        help="landuse parquet(s) for the zone underlay (default: auto-detect "
                             "landuse_{state}_4326.parquet next to the input)")
    parser.add_argument("--states", default=None,
                        help="comma-separated state names to show (matches the `state` "
                             "column, case-insensitive; e.g. 'berlin,brandenburg,hamburg')")
    parser.add_argument("--district", "--districts", dest="districts", default=None,
                        help="comma-separated district names or 5-digit AGS codes to show "
                             "(matches `district` or `district_ags`, case-insensitive; "
                             "e.g. 'kiel,berlin' or '01002,11000') — the Kreis level that "
                             "the bottom-up/top-down combination will use")
    parser.add_argument("--sample", type=int, default=None,
                        help="opt-in row sample for country-scale files (a full DE embed "
                             "is ~400MB of HTML); prints what was sampled")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    import folium

    gdf = gpd.read_parquet(args.parquet).to_crs("EPSG:4326")
    state_slugs: list[str] = []
    if args.states and "state" in gdf.columns:
        state_slugs = [s.strip().lower() for s in args.states.split(",") if s.strip()]
        gdf = gdf[gdf["state"].str.lower().isin(state_slugs)]
        counts = gdf["state"].value_counts().to_dict()
        print(f"[info] state filter: {counts} ({len(gdf)} rows)")
        if not len(gdf):
            raise SystemExit(f"no rows match states {state_slugs!r}")
    district_slugs: list[str] = []
    if args.districts and "district" in gdf.columns:
        district_slugs = [d.strip().lower() for d in args.districts.split(",") if d.strip()]
        mask = (gdf["district"].str.lower().isin(district_slugs)
                | gdf["district_ags"].astype("string").isin(district_slugs))
        gdf = gdf[mask]
        print(f"[info] district filter: {gdf['district'].value_counts().to_dict()} "
              f"({len(gdf)} rows)")
        if not len(gdf):
            raise SystemExit(f"no rows match districts {district_slugs!r}")
    total_rows = len(gdf)
    if args.sample and total_rows > args.sample:
        gdf = gdf.sample(args.sample, random_state=0)
        print(f"[warn] SAMPLED preview: {args.sample} of {total_rows} rows "
              f"({args.sample / total_rows:.0%}) — not the full dataset")

    pts = gdf.geometry.representative_point()
    centre = [float(pts.y.median()), float(pts.x.median())]
    fmap = folium.Map(location=centre, zoom_start=11, tiles="OpenStreetMap")

    values = gdf[args.color].astype("string").fillna("∅") if args.color in gdf.columns \
        else pd.Series(["all"] * len(gdf), index=gdf.index)
    colour_of = {v: PALETTE[i % len(PALETTE)] for i, v in enumerate(sorted(values.unique()))}

    cols = [c for c in ATTR_COLS if c in gdf.columns]
    if args.color in gdf.columns and args.color not in cols:
        cols.append(args.color)
    attrs = gdf[cols].astype(object).where(gdf[cols].notna(), None)
    records = attrs.to_dict("records")

    def rings(geom):
        """Exterior rings as [[lat, lon], …] lists — the company ground surfaces."""
        if geom.geom_type == "Polygon":
            parts = [geom]
        elif geom.geom_type == "MultiPolygon":
            parts = list(geom.geoms)
        else:
            return None
        return [[[round(y, 5), round(x, 5)] for x, y in
                 p.simplify(5e-5).exterior.coords] for p in parts]

    n_surf = 0
    for rec, lat, lon, val, geom in zip(records, pts.y, pts.x, values, gdf.geometry):
        rec["lat"] = round(float(lat), 6)
        rec["lon"] = round(float(lon), 6)
        rec["_cat"] = str(val)
        # per-SOURCE group slots: each dataset row only ever filters by its own
        # native concept (a dual-register row has an IED activity AND a heat band,
        # each shown only in its own row)
        grp = {}
        ag = _activity_group(rec.get("ied_activity"))
        if ag:
            grp["ied"] = ag
        hb = _heat_band(rec.get("abw_heat_mwh_a"))
        if hb:
            grp["abwaerme"] = hb
        # the OVT row filters by business_type (provenance stays in hover/CSV via
        # ovt_dataset — user decision 2026-08-27: sub-source toggles not needed)
        if "overture" in str(rec.get("source") or ""):
            grp["overture"] = str(rec.get("business_type") or "unmapped")
        # the MaStR row filters by SINGULAR technology toggles: a site is a member of
        # every technology it has and shows if ANY enabled one matches (user decision
        # 2026-09-03)
        if "mastr" in str(rec.get("source") or ""):
            grp["mastr"] = str(rec.get("mastr_techs") or "unknown").split("+")
        # the operator's WZ 2025 classification on EVERY MaStR site (user decision
        # 2026-09-04) — also when a higher-priority source won business_type/subtype
        wz_sec = rec.get("mastr_wz_abschnitt")
        if wz_sec:
            sec = _WZ_SECTION_RE.sub(r"\1 – \2", str(wz_sec))
            grp_lbl = rec.get("mastr_wz_gruppe")
            code = rec.get("mastr_wz_code")
            rec["_wz"] = sec + (" / " + (f"{code} " if code else "") + str(grp_lbl) if grp_lbl else "")
        # per-unit capacities: embed the JSON as an object (hover blocks + CSV)
        td = rec.get("mastr_tech_detail")
        if isinstance(td, str) and td:
            try:
                rec["mastr_tech_detail"] = json.loads(td)
            except json.JSONDecodeError:
                pass
        if grp:
            rec["_grp"] = grp
        if rec.get("ied_activity") is not None:
            rec["_act"] = _activity_label(rec.get("ied_activity"))
        poly = rings(geom)
        if poly:
            rec["poly"] = poly
            n_surf += 1
    print(f"[info] {len(records)} companies, {n_surf} with ground surface polygons")

    # landuse zone underlay (the A2 grounds-area context layer)
    lu_paths = args.landuse
    # states whose landuse files to load: explicit filter, else derived from the
    # filtered rows (a district filter implies its state)
    lu_states = state_slugs or (
        sorted(gdf["state"].dropna().str.lower().unique()) if district_slugs
        and "state" in gdf.columns else [])
    if lu_paths is None:
        lu_paths = []
        if lu_states:
            tr = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
            for st in lu_states:
                p = args.parquet.parent / f"landuse_{st.translate(tr)}_4326.parquet"
                if p.exists():
                    lu_paths.append(p)
        parts = args.parquet.stem.split("_")   # companies_merged_{scope}_{4326|3857}
        if not lu_paths and not lu_states and len(parts) >= 4 and parts[0] == "companies":
            if parts[2] == "DE":
                lu_paths = sorted(args.parquet.parent.glob("landuse_*_4326.parquet"))
            else:
                for st in parts[2].split("-"):
                    p = args.parquet.parent / f"landuse_{st}_4326.parquet"
                    if p.exists():
                        lu_paths.append(p)
    landuse_records = []
    for p in lu_paths:
        lu = gpd.read_parquet(p).to_crs("EPSG:4326")
        areas = lu.geometry.to_crs("EPSG:25832").area   # spec §2: areas in 25832
        for geom, typ, a in zip(lu.geometry, lu["landuse"], areas):
            r = rings(geom)
            if r:
                landuse_records.append({"t": str(typ), "a": int(a), "poly": r})
    if landuse_records:
        print(f"[info] landuse underlay: {len(landuse_records)} zones "
              f"from {', '.join(p.name for p in lu_paths)}")
    else:
        print("[info] no landuse underlay (no landuse_*_4326.parquet found)")
    def _embed(obj) -> str:
        """JSON safe for embedding: no '</' inside <script>, and no jinja2 tag
        markers ('{%', '{{' — folium renders the page through jinja2; a brace in
        e.g. a URL would raise TemplateSyntaxError). \\u007b decodes back to '{'."""
        return (json.dumps(obj, ensure_ascii=False, default=str)
                .replace("</", "<\\/").replace("{%", "\\u007b%").replace("{{", "\\u007b\\u007b"))

    # drop null fields per record: sparse columns (register debug fields on 500k+
    # overture rows) cost ~40 % of the embed as literal `"col": null` pairs
    all_cols = cols + ["lat", "lon"]   # CSV export column order (poly excluded)
    records = [{k: v for k, v in rec.items() if v is not None} for rec in records]
    data_json = _embed(records)

    notes = []
    if state_slugs:
        notes.append("states: " + ", ".join(state_slugs) + " (full data)")
    if district_slugs:
        notes.append("districts: " + ", ".join(district_slugs) + " (full data)")
    if len(gdf) < total_rows:
        notes.append(f"sample: {len(gdf):,} of {total_rows:,} rows")
    sample_note = " — ".join(notes)
    page = Template(PAGE_TMPL).substitute(
        MAP_VAR=fmap.get_name(),
        SAMPLE_NOTE=json.dumps(sample_note),
        ALL_COLS=json.dumps(all_cols),
        DATA_JSON=data_json,
        LANDUSE_JSON=_embed(landuse_records),
        COLOURS_JSON=json.dumps(colour_of),
        COLOR_COL=json.dumps(args.color),
        CENTRE=json.dumps(centre),
        ZOOM=11,
        CSV_NAME=json.dumps(f"{args.parquet.stem}_selection.csv"),
    )
    fmap.get_root().html.add_child(folium.Element(page))

    out = args.out
    if out is None:
        previews = args.parquet.parents[1] / "previews" if args.parquet.parent.name \
            == "geoextract" else Path("data/previews")
        previews.mkdir(parents=True, exist_ok=True)
        out = previews / f"{args.parquet.stem}.html"
    fmap.save(str(out))
    print(f"[ok] {len(gdf)} points → {out}")


PAGE_TMPL = r"""
<style>
  #pv-toolbar {position:fixed;bottom:16px;left:calc(50% - 170px);transform:translateX(-50%);
    z-index:1000;background:#fff;padding:6px 8px;border:1px solid #ccc;border-radius:4px;
    box-shadow:0 1px 4px rgba(0,0,0,.3);font:12px sans-serif;white-space:nowrap;
    max-width:calc(95vw - 340px);overflow-x:auto}
  #pv-toolbar .pv-title {font-weight:bold;margin-right:8px}
  .pv-row {display:flex;align-items:center;gap:4px;margin-top:4px;white-space:nowrap}
  .pv-ds {font-weight:bold;color:#555;font-size:11px;min-width:38px;margin-right:4px}
  .pv-btn {font:12px sans-serif;padding:4px 10px;margin:0;cursor:pointer;
    border:1px solid #ccc;border-radius:3px;background:#f5f5f5;color:#aaa}
  .pv-btn.active {background:#e6e6e6;color:#000;box-shadow:inset 0 1px 2px rgba(0,0,0,.15)}
  .pv-row.pv-off .pv-btn:not(:first-of-type),
  .pv-row.pv-off .pv-ds {opacity:.45}
  .pv-sep {display:inline-block;width:1px;height:18px;background:#ddd;margin:0 4px}
  .pv-btn:not(.active) .pv-swatch {opacity:.25}
  .pv-swatch {display:inline-block;width:10px;height:10px;margin-right:5px;
    vertical-align:-1px;border-radius:2px}
  .pv-n {color:#888;font-size:10px;margin-left:2px}
  #pv-tools {position:fixed;top:16px;left:60px;z-index:1000;background:#fff;
    padding:4px;border:1px solid #ccc;border-radius:4px;
    box-shadow:0 1px 4px rgba(0,0,0,.3);font:12px sans-serif}
  #pv-tools button {font:12px sans-serif;padding:4px 10px;margin:0 2px;cursor:pointer;
    border:1px solid #ccc;border-radius:3px;background:#f5f5f5}
  #pv-tools button.active {background:#d0e2f4;border-color:#7aa8d8}
  #pv-panel {position:fixed;top:0;right:0;bottom:0;width:340px;z-index:1000;
    background:#fff;border-left:1px solid #ccc;box-shadow:-1px 0 4px rgba(0,0,0,.2);
    font:12px sans-serif;display:flex;flex-direction:column}
  #pv-panel-head {padding:8px 10px;border-bottom:1px solid #ddd}
  #pv-panel-head b {font-size:13px}
  #pv-count {color:#555;margin-left:6px}
  #pv-download {float:right;font:bold 12px sans-serif;padding:4px 12px;cursor:pointer;
    border:1px solid #3c763d;border-radius:3px;background:#5cb85c;color:#fff}
  #pv-download:hover {background:#449d44}
  #pv-download:disabled {background:#b8d8b8;border-color:#a8c8a8;cursor:not-allowed}
  #pv-table-wrap {flex:1;overflow:auto}
  #pv-table {border-collapse:collapse;width:100%}
  #pv-table th {position:sticky;top:0;background:#f0f0f0;text-align:left;
    padding:4px 6px;border-bottom:1px solid #ccc;font-size:11px}
  #pv-table td {padding:3px 6px;border-bottom:1px solid #eee;font-size:11px;
    max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #pv-table tr:hover {background:#eef4fb}
  #pv-note {padding:6px 10px;color:#777;border-top:1px solid #eee;font-size:11px}
  #pv-search {display:block;width:100%;box-sizing:border-box;margin-top:6px;padding:4px 6px;
    font:12px sans-serif;border:1px solid #bbb;border-radius:3px}
  #pv-table tr.pv-hit {cursor:pointer}
  .pv-link {color:#1a5fb4;text-decoration:underline}
  .leaflet-container {cursor:default}
</style>
<div id="pv-tools">
  <button id="pv-box" onclick="pvMode('box')" title="drag a rectangle to select">▭ Box select</button>
  <button id="pv-lasso" onclick="pvMode('lasso')" title="draw a free shape to select">✎ Lasso select</button>
  <button onclick="pvClear()" title="clear the selection (Esc)">✕ Clear</button>
  <button onclick="pvReset()" title="reset the view">⟲ Reset</button>
  <button id="pv-save" onclick="pvSaveImage()" title="save the current map view as PNG">📷 Image</button>
</div>
<div id="pv-toolbar"><span class="pv-title" id="pv-color-col"></span></div>
<div id="pv-panel">
  <div id="pv-panel-head">
    <button id="pv-download" onclick="pvDownload()">Download</button>
    <b>Companies</b><span id="pv-count"></span>
    <input id="pv-search" type="search" placeholder="search company name … (visible companies; click a row to zoom)"
           title="case-insensitive substring match on name among the currently visible companies; results replace the list">
  </div>
  <div id="pv-table-wrap"><table id="pv-table"></table></div>
  <div id="pv-note"></div>
</div>
<script>
window.addEventListener("load", function () {
  var DATA = $DATA_JSON;
  var COLOURS = $COLOURS_JSON;
  var COLOR_COL = $COLOR_COL;
  var CENTRE = $CENTRE, ZOOM = $ZOOM, CSV_NAME = $CSV_NAME;
  var map = window["$MAP_VAR"];
  var TABLE_CAP = 400;
  var esc = function (s) { return String(s).replace(/[&<>"]/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); };

  // CORS-enabled tiles so the PNG export canvas is not tainted (OSM allows CORS)
  map.eachLayer(function (l) {
    if (l instanceof L.TileLayer) { l.options.crossOrigin = "anonymous"; l.redraw(); }
  });

  // ---- landuse zone underlay, on its own pane beneath the company layers ----
  var LU = $LANDUSE_JSON;
  var LU_COLOURS = {industrial: "#8fa3b8", commercial: "#c4b454", retail: "#d09ec2"};
  var luGroup = null, luOn = false;
  function buildLanduse() {   // lazy: 150k national zones would dominate page load
    if (luGroup || !LU.length) return;
    map.createPane("pvLanduse").style.zIndex = 350;   // overlayPane is 400
    var luRenderer = L.canvas({padding: 0.5, pane: "pvLanduse"});
    luGroup = L.layerGroup();
    LU.forEach(function (z) {
      L.polygon(z.poly, {pane: "pvLanduse", renderer: luRenderer,
        color: LU_COLOURS[z.t] || "#999", weight: 1, dashArray: "3",
        fillColor: LU_COLOURS[z.t] || "#999", fillOpacity: 0.15})
        .bindTooltip("<b>landuse:</b> " + esc(z.t) + " | ~" + z.a.toLocaleString() + " m²")
        .addTo(luGroup);
    });
  }
  if (LU.length && LU.length <= 20000) {   // state scale: on by default; DE: on demand
    buildLanduse();
    luOn = true;
  }

  // ---- companies: typed arrays + ONE canvas — no per-row Leaflet objects ----
  // Membership is by CONTRIBUTING source: a merged ied+osm entity belongs to both
  // dataset rows and is drawn (once) if either row's filters want it. Each dataset
  // row only offers toggles for what that dataset really has.
  var N = DATA.length;
  var wx = new Float64Array(N), wy = new Float64Array(N);   // world px at zoom 0
  var srcsOf = new Array(N), catOf = new Array(N), grpOf = new Array(N);
  var OSM = {on: true, surfOn: true, cats: {}, nSurf: 0, present: false};
  var REG = {};   // register datasets (ied, abwaerme, …): match partners + groups
  var MATCH_COLOURS = {matched: "#0d8a72", only: "#a31515"};
  // display names for dataset rows ("abwaerme" is the BfEE Plattform für Abwärme)
  var DS_LABELS = {osm: "OSM", ied: "IED", abwaerme: "PfA", overture: "OVT", mastr: "MaStR"};
  function dsLabel(s) { return DS_LABELS[s] || s; }
  var polyW = {}, polyBB = {};   // world-coord surface rings + bboxes, by row index
  DATA.forEach(function (d, i) {
    var p = map.project([d.lat, d.lon], 0);
    wx[i] = p.x; wy[i] = p.y;
    var srcs = String(d.source || "?").split("+");
    srcsOf[i] = srcs; catOf[i] = d._cat; grpOf[i] = d._grp || null;   // {source: group}
    if (srcs.indexOf("osm") >= 0) {
      OSM.present = true;
      var c = OSM.cats[d._cat] || (OSM.cats[d._cat] = {n: 0, on: true, idx: []});
      c.n++; c.idx.push(i);
    }
    srcs.forEach(function (s) {
      if (s === "osm") return;
      var R = REG[s] || (REG[s] = {on: true, idx: [],
        match: {matched: {n: 0, on: true}, only: {n: 0, on: true}}, grps: {}});
      R.idx.push(i);
      R.match[srcs.length > 1 ? "matched" : "only"].n++;
      var gname = grpOf[i] && grpOf[i][s];   // only this source's own group concept
      // a list means multi-membership (MaStR technologies): count the row in each
      (Array.isArray(gname) ? gname : (gname ? [gname] : [])).forEach(function (gn) {
        var g = R.grps[gn] || (R.grps[gn] = {n: 0, on: true});
        g.n++;
      });
    });
    if (d.poly) {   // polygons only ever come from OSM geometries
      OSM.nSurf++;
      var bb = [Infinity, Infinity, -Infinity, -Infinity];
      polyW[i] = d.poly.map(function (ring) {
        return ring.map(function (pt) {
          var q = map.project(pt, 0);
          if (q.x < bb[0]) bb[0] = q.x;
          if (q.y < bb[1]) bb[1] = q.y;
          if (q.x > bb[2]) bb[2] = q.x;
          if (q.y > bb[3]) bb[3] = q.y;
          return [q.x, q.y];
        });
      });
      polyBB[i] = bb;
    }
  });
  function rgba(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }
  function matchOf(i) { return srcsOf[i].length > 1 ? "matched" : "only"; }
  function osmWants(i) {
    if (!OSM.on || srcsOf[i].indexOf("osm") < 0) return false;
    var c = OSM.cats[catOf[i]];
    return !!c && c.on;
  }
  function regWants(s, i) {
    var R = REG[s];
    if (!R || !R.on || srcsOf[i].indexOf(s) < 0) return false;
    if (!R.match[matchOf(i)].on) return false;
    var g = grpOf[i] && grpOf[i][s];
    if (Array.isArray(g)) {   // ANY enabled technology keeps the site visible
      for (var k = 0; k < g.length; k++) if (!R.grps[g[k]] || R.grps[g[k]].on) return true;
      return false;
    }
    if (g && R.grps[g] && !R.grps[g].on) return false;
    return true;
  }
  function visible(i) {   // OR across datasets, AND within a dataset's own filters
    if (osmWants(i)) return true;
    for (var s in REG) if (regWants(s, i)) return true;
    return false;
  }
  function setLayer(g, want) {
    if (want !== map.hasLayer(g)) { want ? map.addLayer(g) : map.removeLayer(g); }
  }
  // Below this zoom, polygons are sub-pixel — skipping them keeps pans smooth.
  var POLY_MIN_ZOOM = 10;
  var cnv = document.createElement("canvas");
  cnv.style.position = "absolute";
  cnv.style.pointerEvents = "none";
  map.getPanes().overlayPane.appendChild(cnv);
  var selected = null;          // Set of DATA indices, or null = no selection
  function draw() {
    var size = map.getSize(), z = map.getZoom(), s = Math.pow(2, z);
    cnv.width = size.x; cnv.height = size.y;
    L.DomUtil.setPosition(cnv, map.containerPointToLayerPoint([0, 0]));
    var o = map.project(map.containerPointToLatLng([0, 0]), z);
    var ctx = cnv.getContext("2d");
    if (z >= POLY_MIN_ZOOM && OSM.on && OSM.surfOn) {   // OSM ground surfaces
      var vx0 = o.x / s, vy0 = o.y / s, vx1 = (o.x + size.x) / s, vy1 = (o.y + size.y) / s;
      Object.keys(OSM.cats).forEach(function (cat) {
        var c = OSM.cats[cat];
        if (!c.on) return;
        ctx.fillStyle = rgba(COLOURS[cat] || "#666666", 0.18);
        ctx.strokeStyle = COLOURS[cat] || "#666";
        ctx.lineWidth = 1;
        for (var k = 0; k < c.idx.length; k++) {
          var i = c.idx[k], bb = polyBB[i];
          if (!bb || bb[2] < vx0 || bb[0] > vx1 || bb[3] < vy0 || bb[1] > vy1) continue;
          ctx.beginPath();
          polyW[i].forEach(function (ring) {
            for (var v = 0; v < ring.length; v++) {
              var x = ring[v][0] * s - o.x, y = ring[v][1] * s - o.y;
              if (v) ctx.lineTo(x, y); else ctx.moveTo(x, y);
            }
            ctx.closePath();
          });
          ctx.fill(); ctx.stroke();
        }
      });
    }
    var hasSel = !!selected;
    if (OSM.on) {   // points visible through OSM, batched per category colour
      Object.keys(OSM.cats).forEach(function (cat) {
        var c = OSM.cats[cat];
        if (!c.on) return;
        ctx.fillStyle = rgba(COLOURS[cat] || "#666666", hasSel ? 0.1 : 0.75);
        ctx.beginPath();
        for (var k = 0; k < c.idx.length; k++) {
          var i = c.idx[k];
          if (hasSel && selected.has(i)) continue;
          var x = wx[i] * s - o.x, y = wy[i] * s - o.y;
          if (x < -4 || y < -4 || x > size.x + 4 || y > size.y + 4) continue;
          ctx.moveTo(x + 3, y); ctx.arc(x, y, 3, 0, 6.2832);
        }
        ctx.fill();
      });
    }
    // points visible ONLY through a register dataset: match-status colours
    Object.keys(REG).forEach(function (s2) {
      var R = REG[s2];
      ["matched", "only"].forEach(function (m) {
        ctx.fillStyle = rgba(MATCH_COLOURS[m], hasSel ? 0.1 : 0.85);
        ctx.beginPath();
        for (var k = 0; k < R.idx.length; k++) {
          var i = R.idx[k];
          if (matchOf(i) !== m || !regWants(s2, i) || osmWants(i)) continue;
          if (hasSel && selected.has(i)) continue;
          var x = wx[i] * s - o.x, y = wy[i] * s - o.y;
          if (x < -5 || y < -5 || x > size.x + 5 || y > size.y + 5) continue;
          ctx.moveTo(x + 3.5, y); ctx.arc(x, y, 3.5, 0, 6.2832);
        }
        ctx.fill();
      });
    });
    // ring highlight on EVERY visible multi-source entity (matched with any partner),
    // regardless of which colour pass drew its dot
    ctx.strokeStyle = rgba(MATCH_COLOURS.matched, hasSel ? 0.25 : 1);
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (var ri = 0; ri < N; ri++) {
      if (srcsOf[ri].length < 2 || !visible(ri)) continue;
      if (hasSel && selected.has(ri)) continue;
      var rx = wx[ri] * s - o.x, ry = wy[ri] * s - o.y;
      if (rx < -7 || ry < -7 || rx > size.x + 7 || ry > size.y + 7) continue;
      ctx.moveTo(rx + 5.5, ry); ctx.arc(rx, ry, 5.5, 0, 6.2832);
    }
    ctx.stroke();
    if (hasSel) {                     // Bokeh-style firebrick selection on top
      ctx.fillStyle = "rgba(178,34,34,0.9)"; ctx.strokeStyle = "#000"; ctx.lineWidth = 1;
      ctx.beginPath();
      selected.forEach(function (i) {
        if (!visible(i)) return;
        var x = wx[i] * s - o.x, y = wy[i] * s - o.y;
        if (x < -8 || y < -8 || x > size.x + 8 || y > size.y + 8) return;
        ctx.moveTo(x + 6, y); ctx.arc(x, y, 6, 0, 6.2832);
      });
      ctx.fill(); ctx.stroke();
    }
    if (luGroup) setLayer(luGroup,
      luOn && z >= POLY_MIN_ZOOM && (!OSM.present || OSM.on));
  }
  map.on("moveend zoomend viewreset resize", draw);

  // ---- hover tooltip via spatial grid (rebuilt once per zoom level) ----
  var CELL = 24, grid = null, gridZ = -1;
  function hoverGrid(z) {
    if (gridZ === z && grid) return grid;
    gridZ = z; grid = {};
    var s = Math.pow(2, z);
    for (var i = 0; i < N; i++) {
      var key = ((wx[i] * s / CELL) | 0) + ":" + ((wy[i] * s / CELL) | 0);
      (grid[key] || (grid[key] = [])).push(i);
    }
    return grid;
  }
  // MaStR per-unit capacity fields, verbatim MaStR column names; unit hints follow the
  // MaStR data model (…leistung → kW, Speicherkapazitaet / Arbeitsgasvolumen → kWh)
  var UNIT_OF = {Bruttoleistung: "kW", Nettonennleistung: "kW", Erzeugungsleistung: "kW",
    MaximaleGasbezugsleistung: "kW", LeistungsaufnahmeBeimEinspeichern: "kW",
    MaximaleEinspeicherleistung: "kW", MaximaleAusspeicherleistung: "kW",
    NutzbareSpeicherkapazitaet: "kWh", MaximalNutzbaresArbeitsgasvolumen: "kWh"};
  var HOVER_UNIT_CAP = 10;   // per technology; the CSV export always holds every unit
  function fmtNum(v) { return typeof v === "number" ? v.toLocaleString("de-DE") : esc(v); }
  // one line per technology: "solar: Bruttoleistung 750 kW, Nettonennleistung 660 kW";
  // several units of one technology → one indented line per unit (never summed), no
  // unit ids (user decision 2026-09-04)
  function techLines(td) {
    if (!td || typeof td !== "object") return "";
    return Object.keys(td).sort().map(function (tech) {
      var units = td[tech] || [];
      var fld = function (u) {
        return Object.keys(u).filter(function (k) { return k !== "unit"; })
          .map(function (k) { return esc(k) + " " + fmtNum(u[k]) + (UNIT_OF[k] ? " " + UNIT_OF[k] : ""); })
          .join(", ") || "(no capacity field in MaStR)";
      };
      if (units.length === 1) return "&nbsp;&nbsp;" + esc(tech) + ": " + fld(units[0]);
      var lines = units.slice(0, HOVER_UNIT_CAP).map(function (u) { return "&nbsp;&nbsp;&nbsp;&nbsp;" + fld(u); });
      if (units.length > HOVER_UNIT_CAP)
        lines.push("&nbsp;&nbsp;&nbsp;&nbsp;… + " + (units.length - HOVER_UNIT_CAP) + " more units (see CSV)");
      return "&nbsp;&nbsp;" + esc(tech) + " (" + units.length + " units):<br>" + lines.join("<br>");
    }).join("<br>");
  }
  // which source supplied business_type / business_subtype (resolve records it)
  var SRC_SHORT = {osm: "osm", overture: "overture", abwaerme: "abw", ied: "ied", mastr: "mastr"};
  function srcTag(src) {
    if (!src) return "";
    var parts = String(src).split("+").map(function (x) { return SRC_SHORT[x] || x; });
    return " (" + esc(parts.join("+")) + ")";
  }
  function webUrl(w) {
    if (!w) return null;
    w = String(w).trim();
    if (!/^https?:\/\//i.test(w)) w = "https://" + w;
    return w;
  }
  function nameHtml(d) {   // blue link to the company website when there is one
    var u = webUrl(d.website), nm = esc(d.name == null ? "" : d.name);
    return u ? '<a class="pv-link" href="' + esc(u) + '" target="_blank" rel="noopener">' + nm + "</a>" : nm;
  }
  function hoverHtml(d) {
    var html = ["name", COLOR_COL === "business_type" ? null : "business_type", COLOR_COL,
                "business_subtype", "_wz", "grounds_area_m2", "nace_primary", "source", "_act",
                "abw_heat_mwh_a", "abw_temp_c", "ovt_confidence"]
      .filter(function (c2, j, a) { return c2 && c2 in d && d[c2] !== null && a.indexOf(c2) === j; })
      .map(function (c2) {
        var label = c2 === "_act" ? "ied_activity" : (c2 === "_wz" ? "mastr_wz" : c2);
        if (c2 === "name") return "<b>name:</b> " + nameHtml(d);
        var val = esc(d[c2]);
        if (c2 === "business_type") val += srcTag(d.business_type_source);
        if (c2 === "business_subtype") val += srcTag(d.business_subtype_source);
        return "<b>" + esc(label) + ":</b> " + val;
      }).join("<br>");
    var techs = techLines(d.mastr_tech_detail);
    if (techs) html += "<br><b>mastr_technologies:</b><br>" + techs;
    return html;
  }
  function nearest(latlng) {   // nearest VISIBLE company within 8 px, or -1
    var z = map.getZoom(), s = Math.pow(2, z);
    var g = hoverGrid(z), w = map.project(latlng, z);
    var gx = (w.x / CELL) | 0, gy = (w.y / CELL) | 0, best = -1, bd = 64;
    for (var dx = -1; dx <= 1; dx++) for (var dy = -1; dy <= 1; dy++) {
      var cell = g[(gx + dx) + ":" + (gy + dy)];
      if (!cell) continue;
      for (var k = 0; k < cell.length; k++) {
        var i = cell[k];
        if (!visible(i)) continue;
        var ddx = wx[i] * s - w.x, ddy = wy[i] * s - w.y, d2 = ddx * ddx + ddy * ddy;
        if (d2 < bd) { bd = d2; best = i; }
      }
    }
    return best;
  }
  // click on a company pins its card (a Leaflet popup, links clickable); clicking
  // another company replaces it; clicking empty map closes it
  var pinned = -1;
  var popup = L.popup({maxWidth: 420});
  popup.on("remove", function () { pinned = -1; });
  function pinCard(i) {
    pinned = i;
    var d = DATA[i];
    popup.setLatLng([d.lat, d.lon]).setContent(hoverHtml(d) || "—").openOn(map);
    if (map.hasLayer(hoverTip)) map.removeLayer(hoverTip);
  }
  map.on("click", function (e) {
    if (mode) return;
    var i = nearest(e.latlng);
    if (i >= 0) pinCard(i);
  });
  var hoverTip = L.tooltip();
  map.on("mousemove", function (e) {
    if (mode) return;   // selection drag has priority
    var best = nearest(e.latlng);
    if (best >= 0 && best !== pinned) {
      var d = DATA[best];
      hoverTip.setContent(hoverHtml(d) || "—").setLatLng([d.lat, d.lon]);
      if (!map.hasLayer(hoverTip)) hoverTip.addTo(map);
    } else if (map.hasLayer(hoverTip)) {
      map.removeLayer(hoverTip);
    }
  });

  // ---- bottom panel: one row of toggles per dataset (source layer) ----
  var bar = document.getElementById("pv-toolbar");
  var SAMPLE_NOTE = $SAMPLE_NOTE;
  document.getElementById("pv-color-col").textContent =
    COLOR_COL + (SAMPLE_NOTE ? " — " + SAMPLE_NOTE : "");
  function mkBtn(html, title, handler) {
    var b = document.createElement("button");
    b.className = "pv-btn active";
    b.innerHTML = html;
    b.title = title;
    b.onclick = function () { handler(b); };
    return b;
  }
  function swatch(colr) {
    return '<span class="pv-swatch" style="background:' + (colr || "#666") + '"></span>';
  }
  function nHtml(x) { return ' <span class="pv-n">' + x + "</span>"; }
  function addRow(name) {
    var row = document.createElement("div");
    row.className = "pv-row";
    var label = document.createElement("span");
    label.className = "pv-ds";
    label.textContent = name;
    row.appendChild(label);
    return row;
  }
  function masterBtn(row, state, name) {
    var b = mkBtn("⏻", "toggle the whole " + name + " dataset on/off", function (bb) {
      state.on = !state.on;
      bb.classList.toggle("active", state.on);
      row.classList.toggle("pv-off", !state.on);
      draw(); renderTable();
    });
    row.appendChild(b);
  }
  function sep() {
    var d = document.createElement("span");
    d.className = "pv-sep";
    return d;
  }
  if (OSM.present) {   // OSM row: categories + surfaces + landuse (all OSM-native)
    var orow = addRow("OSM");
    masterBtn(orow, OSM, "osm");
    Object.keys(OSM.cats).sort().forEach(function (cat) {
      var c = OSM.cats[cat];
      orow.appendChild(mkBtn(swatch(COLOURS[cat]) + esc(cat) + nHtml(c.n),
        "show/hide " + COLOR_COL + "=" + cat + " (osm)", function (b) {
          c.on = !c.on; b.classList.toggle("active", c.on);
          draw(); renderTable();
        }));
    });
    orow.appendChild(mkBtn('▨ surfaces' + nHtml(OSM.nSurf),
      "show/hide company ground surface polygons (rendered from zoom 10 — zoom in)",
      function (b) {
        OSM.surfOn = !OSM.surfOn; b.classList.toggle("active", OSM.surfOn);
        draw();
      }));
    if (LU.length) {
      var lb = mkBtn('▦ landuse' + nHtml(LU.length),
        "show/hide OSM landuse zones (rendered from zoom 10 — zoom in)", function (b) {
          luOn = !luOn;
          if (luOn) buildLanduse();
          b.classList.toggle("active", luOn);
          draw();
        });
      lb.classList.toggle("active", luOn);
      orow.appendChild(lb);
    }
    bar.appendChild(orow);
  }
  var GRP_ORDER = ["energy", "metals", "minerals", "chemicals", "waste",
                   "livestock", "other",
                   "< 1 GWh/a", "1–10 GWh/a", "10–100 GWh/a", "> 100 GWh/a",
                   "solar", "wind", "biomass", "hydro", "combustion", "geothermal", "nuclear",
                   "storage", "gas_production", "gas_storage", "gas_consumption",
                   "electricity_consumption"];
  Object.keys(REG).sort().forEach(function (s2) {   // register rows: match + groups
    var R = REG[s2];
    var row = addRow(dsLabel(s2));
    masterBtn(row, R, dsLabel(s2));
    // exactly two match buttons: matched (any partner dataset) and dataset-only;
    // the specific partners of an entity stay visible in its hover card (source: …)
    [["matched", "matched"], ["only", dsLabel(s2) + "-only"]].forEach(function (mm) {
      var st = R.match[mm[0]];
      row.appendChild(mkBtn(swatch(MATCH_COLOURS[mm[0]]) + esc(mm[1]) + nHtml(st.n),
        "show/hide " + dsLabel(s2) + " entities: " + mm[1], function (b) {
          st.on = !st.on; b.classList.toggle("active", st.on);
          draw(); renderTable();
        }));
    });
    var grps = GRP_ORDER.filter(function (g) { return g in R.grps; })
      .concat(Object.keys(R.grps).filter(function (g) {
        return GRP_ORDER.indexOf(g) < 0; }).sort());
    if (grps.length) row.appendChild(sep());
    var GRP_TIPS = {ied: "Annex-I activity group filter (from ied_activity, 1:1)",
                    abwaerme: "heat-quantity band filter (from abw_heat_mwh_a)",
                    overture: "business type filter",
                    mastr: "technology toggle — a site stays visible while ANY of its " +
                           "technologies is enabled (counts: sites having that technology)"};
    grps.forEach(function (g) {
      var st = R.grps[g];
      row.appendChild(mkBtn((COLOURS[g] ? swatch(COLOURS[g]) : "") + esc(g) + nHtml(st.n),
        GRP_TIPS[s2] || "group filter", function (b) {
          st.on = !st.on; b.classList.toggle("active", st.on);
          draw(); renderTable();
        }));
    });
    bar.appendChild(row);
  });

  // ---- selection (box + lasso), Bokeh-style highlight ----
  var mode = null, dragging = false, anchor = null, shape = null, lassoPts = [];

  function visibleIdx() {
    var out = [];
    for (var i = 0; i < N; i++) {
      if (visible(i)) out.push(i);
    }
    return out;
  }
  window.pvMode = function (m) {
    mode = (mode === m) ? null : m;
    ["box", "lasso"].forEach(function (id) {
      document.getElementById("pv-" + id).classList.toggle("active", mode === id);
    });
    if (mode) { map.dragging.disable(); map.getContainer().style.cursor = "crosshair"; }
    else { map.dragging.enable(); map.getContainer().style.cursor = ""; }
  };
  window.pvClear = function () { selected = null; draw(); renderTable(); };
  window.pvReset = function () { map.setView(CENTRE, ZOOM); };

  function inPolygon(pt, poly) {           // ray casting on [lat, lng] pairs
    var inside = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var yi = poly[i][0], xi = poly[i][1], yj = poly[j][0], xj = poly[j][1];
      if (((yi > pt[0]) !== (yj > pt[0])) &&
          (pt[1] < (xj - xi) * (pt[0] - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  map.on("mousedown", function (e) {
    if (!mode) return;
    dragging = true; anchor = e.latlng; lassoPts = [e.latlng];
    if (shape) { map.removeLayer(shape); shape = null; }
  });
  map.on("mousemove", function (e) {
    if (!mode || !dragging) return;
    if (mode === "box") {
      var b = L.latLngBounds(anchor, e.latlng);
      if (shape) shape.setBounds(b);
      else shape = L.rectangle(b, {color: "#555", weight: 1, dashArray: "4",
                                   fill: true, fillOpacity: 0.05}).addTo(map);
    } else {
      lassoPts.push(e.latlng);
      if (shape) shape.setLatLngs(lassoPts);
      else shape = L.polygon(lassoPts, {color: "#555", weight: 1, dashArray: "4",
                                        fill: true, fillOpacity: 0.05}).addTo(map);
    }
  });
  map.on("mouseup", function (e) {
    if (!mode || !dragging) return;
    dragging = false;
    var hit = new Set();
    if (mode === "box") {
      var b = L.latLngBounds(anchor, e.latlng);
      visibleIdx().forEach(function (i) {
        if (b.contains([DATA[i].lat, DATA[i].lon])) hit.add(i);
      });
    } else if (lassoPts.length > 2) {
      var poly = lassoPts.map(function (p) { return [p.lat, p.lng]; });
      visibleIdx().forEach(function (i) {
        if (inPolygon([DATA[i].lat, DATA[i].lon], poly)) hit.add(i);
      });
    }
    if (shape) { map.removeLayer(shape); shape = null; }
    selected = hit;
    draw(); renderTable();
  });

  // ---- data table (right panel) ----
  var ALL_COLS = $ALL_COLS;   // full column set (records drop their null fields)
  var TBL_COLS = ["name", COLOR_COL, "address_full", "grounds_area_m2", "source"]
    .filter(function (c, i, a) { return ALL_COLS.indexOf(c) >= 0 && a.indexOf(c) === i; });
  // name search (right panel): a non-empty query REPLACES the selection list with the
  // visible companies whose name contains the query (case-insensitive)
  var query = "";
  var searchBox = document.getElementById("pv-search");
  searchBox.addEventListener("input", function () {
    query = searchBox.value.trim().toLowerCase();
    renderTable();
  });
  function searchIdx() {
    var out = [];
    for (var i = 0; i < N; i++) {
      var nm = DATA[i].name;
      if (nm != null && String(nm).toLowerCase().indexOf(query) >= 0 && visible(i)) out.push(i);
    }
    return out;
  }
  function currentIdx() {   // table + CSV export: the search hits, else ONLY the selection
    if (query) return searchIdx();
    return selected ? Array.from(selected).sort(function (a, b) { return a - b; }) : [];
  }
  window.pvGoto = function (i) {   // click-to-zoom: centre on the company, pin its card
    var d = DATA[i];
    map.setView([d.lat, d.lon], Math.max(map.getZoom(), 16));
    pinCard(i);
  };
  document.getElementById("pv-table").addEventListener("click", function (e) {
    if (e.target.closest("a")) return;   // website link: open it, do not zoom
    var tr = e.target.closest("tr[data-i]");
    if (tr) pvGoto(+tr.getAttribute("data-i"));
  });
  function renderTable() {
    var idx = currentIdx();
    document.getElementById("pv-count").textContent = query
      ? idx.length + " match" + (idx.length === 1 ? "" : "es")
      : (selected ? idx.length + " selected" : "no selection");
    document.getElementById("pv-download").disabled = !idx.length;
    var rows = idx.slice(0, TABLE_CAP).map(function (i) {
      var d = DATA[i];
      return '<tr class="pv-hit" data-i="' + i + '" title="click to zoom to this company">' +
        TBL_COLS.map(function (c) {
          var v = d[c] == null ? "" : d[c];
          return '<td title="' + esc(v) + '">' + (c === "name" ? nameHtml(d) : esc(v)) + "</td>";
        }).join("") + "</tr>";
    });
    document.getElementById("pv-table").innerHTML = idx.length
      ? "<tr>" + TBL_COLS.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") +
        "</tr>" + rows.join("")
      : "";
    document.getElementById("pv-note").textContent = idx.length
      ? (idx.length > TABLE_CAP ? "table shows first " + TABLE_CAP + " rows — " : "") +
        "Download exports all " + idx.length + " rows as CSV — click a row to zoom"
      : (query ? "no visible company name contains \"" + query + "\""
               : "type a name above, or box-/lasso-select companies on the map — Esc deselects");
  }

  // ---- CSV download of the selection (all visible rows if nothing selected) ----
  window.pvDownload = function () {
    var idx = currentIdx();
    if (!idx.length) return;
    var cols = ALL_COLS;
    var q = function (v) {
      if (v === null || v === undefined) return "";
      v = typeof v === "object" ? JSON.stringify(v) : String(v);
      return /[",\n;]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    };
    var lines = [cols.join(",")].concat(idx.map(function (i) {
      return cols.map(function (c) { return q(DATA[i][c]); }).join(",");
    }));
    var blob = new Blob(["﻿" + lines.join("\n")], {type: "text/csv;charset=utf-8"});
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = CSV_NAME;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // ---- PNG export of the current view (the Bokeh "save" tool) ----
  window.pvSaveImage = function () {
    var cont = map.getContainer(), r = cont.getBoundingClientRect();
    var out = document.createElement("canvas");
    out.width = r.width; out.height = r.height;
    var ctx = out.getContext("2d");
    ctx.fillStyle = "#ddd"; ctx.fillRect(0, 0, out.width, out.height);
    cont.querySelectorAll("img.leaflet-tile").forEach(function (img) {
      if (!img.complete || !img.naturalWidth) return;
      var t = img.getBoundingClientRect();
      try { ctx.drawImage(img, t.left - r.left, t.top - r.top, t.width, t.height); }
      catch (e) {}
    });
    Array.prototype.slice.call(cont.querySelectorAll("canvas"))
      .sort(function (a, b) {    // composite panes bottom-up (landuse pane is 350)
        return (+getComputedStyle(a.parentElement).zIndex || 0) -
               (+getComputedStyle(b.parentElement).zIndex || 0);
      })
      .forEach(function (cv) {
        var t = cv.getBoundingClientRect();
        ctx.drawImage(cv, t.left - r.left, t.top - r.top, t.width, t.height);
      });
    var attr = "© OpenStreetMap contributors (ODbL)";   // required attribution
    ctx.font = "11px sans-serif";
    var w = ctx.measureText(attr).width;
    ctx.fillStyle = "rgba(255,255,255,.8)";
    ctx.fillRect(out.width - w - 12, out.height - 18, w + 12, 18);
    ctx.fillStyle = "#333";
    ctx.fillText(attr, out.width - w - 6, out.height - 5);
    try {
      out.toBlob(function (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = CSV_NAME.replace(/_selection\.csv$$/, "") + "_view.png";
        a.click();
        URL.revokeObjectURL(a.href);
      }, "image/png");
    } catch (e) {   // tainted canvas (a tile without CORS) — flag it on the button
      var b = document.getElementById("pv-save");
      b.textContent = "📷 blocked (CORS)";
      setTimeout(function () { b.textContent = "📷 Image"; }, 3000);
    }
  };

  // Esc: drop the selection and leave select mode
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (query) { query = ""; searchBox.value = ""; }
      pvClear(); if (mode) pvMode(mode);
    }
  });

  draw(); renderTable();
});
</script>
"""


if __name__ == "__main__":
    main()
