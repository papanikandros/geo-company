/* geoextract map page — identical interaction set to scripts/preview_layer.py (the canonical
   preview), on vector tiles + the S3 API:
   • bottom toolbar: one row per dataset — OSM categories / surfaces / landuse; register rows
     with matched / -only + native group toggles; MaStR singular technology toggles
     (OR across datasets, AND within a dataset)
   • points: category colour when visible through OSM, match colour (green matched / red
     dataset-only) when visible only through a register, green ring on every multi-source
     entity, firebrick selection with the rest dimmed
   • top-left tools: Box select, Lasso select, Clear, Reset, Image
   • right panel: selection / search table (name, business_type, address, area, source),
     click-to-zoom, Download of all listed rows
   • hover card (preview fields) and click-to-pin card (+ every public field + the raw record
     of every source)
   Selections are computed by the API over the FULL data with the same toggle semantics
   (display filter), so they are exact at any zoom. */
(function () {
  "use strict";
  var DATA = window.GEOEXTRACT_DATA || "data";
  var API = window.GEOEXTRACT_API || "v1";
  var BASEMAP = window.GEOEXTRACT_BASEMAP || "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  var TABLE_CAP = 500, HL_CAP = 50000;
  var esc = function (s) {
    return String(s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; });
  };
  var $ = function (id) { return document.getElementById(id); };
  var protocol = new pmtiles.Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);

  Promise.all([fetch(API + "/manifest").then(function (r) { return r.json(); }),
               fetch(API + "/ui").then(function (r) { return r.json(); })])
    .then(function (res) { init(res[0], res[1]); })
    .catch(function (e) { document.body.innerHTML = "<p style='padding:20px'>cannot load manifest/ui: " + esc(e) + "</p>"; });

  function init(manifest, ui) {
    var PAL = ui.palette, DS = ui.datasets, LBL = ui.labels || {}, MATCH = ui.match_colours;
    var LU = ui.lu_colours, UNIT_OF = ui.unit_of || {}, IED_LABELS = ui.ied_labels || {};
    var COLOR_COL = ui.sector_column === "nace_section" ? "nace_section" : "business_type";
    var SRC_SHORT = LBL;
    $("pv-color-col").textContent = COLOR_COL + " — " + manifest.scope + " (full data, version " + manifest.version + ")";

    // ---- filters box (served-map additions) -----------------------------------------------
    ui.states.forEach(function (s) {
      var o = document.createElement("option"); o.value = s.name; o.textContent = s.name + " (" + s.n.toLocaleString("de-DE") + ")";
      $("sel-state").appendChild(o);
    });
    function fillDistricts(state) {
      var sel = $("sel-district"); sel.innerHTML = "<option value=''>all districts</option>";
      ui.districts.filter(function (d) { return !state || d.state === state; }).forEach(function (d) {
        var o = document.createElement("option"); o.value = d.ags; o.textContent = d.name + " (" + d.n.toLocaleString("de-DE") + ")";
        sel.appendChild(o);
      });
    }
    fillDistricts("");
    ui.sectors.forEach(function (s) { var o = document.createElement("option"); o.value = s; o.textContent = s; $("sel-sector").appendChild(o); });

    // ---- map -----------------------------------------------------------------------------
    var bbox = ui.bbox;
    var tileMeta = manifest.files && manifest.files["companies.pmtiles"];
    var tileVersion = tileMeta ? tileMeta.sha256.slice(0, 12) : manifest.generated_at;
    var map = new maplibregl.Map({
      container: "map",
      style: {version: 8,
        sources: {base: {type: "raster", tiles: [BASEMAP], tileSize: 256, maxzoom: 19,
                         attribution: "© OpenStreetMap contributors"},
                  companies: {type: "vector", tiles: ["pmtiles://" + new URL(DATA + "/companies.pmtiles?v=" + tileVersion, location.href).href + "/{z}/{x}/{y}"],
                              minzoom: ui.tile_minzoom || 4, maxzoom: ui.tile_maxzoom || 14}},
        layers: [{id: "base", type: "raster", source: "base"}]},
      bounds: [[bbox[0], bbox[1]], [bbox[2], bbox[3]]], fitBoundsOptions: {padding: 20},
      attributionControl: false, preserveDrawingBuffer: true, boxZoom: false
    });
    window.gxMap = map;
    map.on("error", function (e) { console.error("map error", e && e.error ? e.error.message || e.error : e); });
    map.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-left");
    map.addControl(new maplibregl.ScaleControl({unit: "metric"}), "bottom-left");
    map.addControl(new maplibregl.AttributionControl({compact: true, customAttribution:
      "© OpenStreetMap contributors (ODbL) · Overture (CDLA-P-2.0) · BfEE/BNetzA/Destatis (DL-DE-BY-2.0) · " +
      "OffeneRegister/OpenCorporates (CC-BY 4.0) · GLEIF (CC0) · <a href='docs'>API</a>"}), "bottom-right");

    function paletteExpr(prop) {
      var e = ["match", ["coalesce", ["get", prop], "∅"]];
      Object.keys(PAL).forEach(function (k) { e.push(k, PAL[k]); });
      e.push("#666666");
      return e;
    }

    // ---- filter model (the preview's semantics) --------------------------------------------
    var OSM = {on: false, surfOn: true, luOn: true, cats: {}};   // every dataset starts OFF: empty map
    if (DS.osm) Object.keys(DS.osm.cats).forEach(function (c) { OSM.cats[c] = true; });
    var REG = {};
    ["ied", "abwaerme", "overture", "mastr"].forEach(function (k) {
      if (!DS[k]) return;
      REG[k] = {on: false, match: {matched: true, only: true}, grps: {}};
      Object.keys(DS[k].groups).forEach(function (g) { REG[k].grps[g] = true; });
    });
    var GPROP = {ied: "ied", abwaerme: "abw", overture: "bt", mastr: "mt"};
    var HR_LABELS = {matched_active: "matched · active", matched_dissolved: "matched · dissolved", ambiguous: "ambiguous", none: "no match"};
    var WD_LABELS = {e: "entity item", o: "operator item", b: "brand item", w: "website from Wikidata"};
    var WD = {on: false, matched: true, only: true};   // Wikidata row: matched = dots with an entity/operator item, only = items with coordinates but no map entity
    var WD_MATCHED = ["e", "o"];
    var WD_COLOUR = "#2b6cb0";
    var HR_COLOURS = {matched_active: "#0d8a72", matched_dissolved: "#b8860b", ambiguous: "#7f7f7f", none: "#a31515"};
    var HR = {on: false, matched: true, only: true};   // Register row: matched = validation of the map dots, only = register companies with no map entity
    var HR_MATCHED = ["matched_active", "matched_dissolved"];
    function srcHas(k) { return ["in", k, ["get", "src"]]; }
    function osmWants() {
      if (!OSM.on || !DS.osm) return null;
      var on = Object.keys(OSM.cats).filter(function (c) { return OSM.cats[c]; });
      if (!on.length) return null;
      return ["all", srcHas("osm"), ["in", ["coalesce", ["get", "bt"], "∅"], ["literal", on]]];
    }
    function regWants(k) {
      var R = REG[k];
      if (!R || !R.on || (!R.match.matched && !R.match.only)) return null;
      var parts = ["all", srcHas(k)];
      if (!R.match.matched) parts.push(["<=", ["get", "sc"], 1]);
      if (!R.match.only) parts.push([">", ["get", "sc"], 1]);
      var on = Object.keys(R.grps).filter(function (g) { return R.grps[g]; }), all = Object.keys(R.grps);
      if (on.length < all.length) {
        if (!on.length) return null;
        if (k === "mastr") parts.push(["any"].concat(on.map(function (t) { return ["in", "+" + t + "+", ["coalesce", ["get", "mt"], ""]]; })));
        else if (k === "overture") parts.push(["in", ["coalesce", ["get", "bt"], "unmapped"], ["literal", on]]);
        else parts.push(["in", ["coalesce", ["get", GPROP[k]], "∅"], ["literal", on]]);
      }
      return parts;
    }
    function regionExpr() {   // served-map filters (state / district / sector / industrial)
      var f = [];
      if ($("sel-state").value) f.push(["==", ["get", "st"], $("sel-state").value]);
      if ($("sel-district").value) f.push(["==", ["get", "ags"], $("sel-district").value]);
      if ($("sel-sector").value) f.push(["==", ["coalesce", ["get", COLOR_COL === "nace_section" ? "nace" : "bt"], "∅"], $("sel-sector").value]);
      if ($("chk-ind").checked) f.push(["==", ["get", "ind"], 1]);
      return f;
    }
    function hrWants() {   // Register row: the register-matched companies, added like any other dataset
      if (!HR.on || !HR.matched || !ui.register) return null;
      return ["in", ["coalesce", ["get", "hr"], "none"], ["literal", HR_MATCHED]];
    }
    function wdWants() {   // Wikidata row: companies with an entity or operator item
      if (!WD.on || !WD.matched || !ui.wikidata) return null;
      return ["any"].concat(WD_MATCHED.map(function (k) { return ["in", "+" + k + "+", ["coalesce", ["get", "wd"], ""]]; }));
    }
    function visibleExpr() {   // OR across datasets (incl. Register + Wikidata), AND within; plus the region filters
      var any = ["any"];
      var o = osmWants(); if (o) any.push(o);
      Object.keys(REG).forEach(function (k) { var w = regWants(k); if (w) any.push(w); });
      var h = hrWants(); if (h) any.push(h);
      var wdw = wdWants(); if (wdw) any.push(wdw);
      var f = ["all"].concat(regionExpr());
      f.push(any.length > 1 ? any : false);
      return f;
    }
    function displayModel() {   // the same model for the API (selections over the full data)
      var m = {osm: {on: OSM.on && !!DS.osm, cats: Object.keys(OSM.cats).filter(function (c) { return OSM.cats[c]; })}, reg: {}};
      Object.keys(REG).forEach(function (k) {
        var R = REG[k], on = Object.keys(R.grps).filter(function (g) { return R.grps[g]; });
        m.reg[k] = {on: R.on, matched: R.match.matched, only: R.match.only,
                    groups: on.length < Object.keys(R.grps).length ? on : null};
      });
      if (HR.on && HR.matched && ui.register) m.hr = HR_MATCHED;
      if (WD.on && WD.matched && ui.wikidata) m.wd = WD_MATCHED;
      return m;
    }
    function regionParams() {
      var p = [];
      if ($("sel-state").value) p.push("state=" + encodeURIComponent($("sel-state").value));
      if ($("sel-district").value) p.push("district=" + encodeURIComponent($("sel-district").value));
      if ($("sel-sector").value) p.push("sector=" + encodeURIComponent($("sel-sector").value));
      if ($("chk-ind").checked) p.push("industrial=true");
      p.push("display=" + encodeURIComponent(JSON.stringify(displayModel())));
      return p;
    }

    // ---- selection state -------------------------------------------------------------------
    var selected = null;      // {geometry, count, ids: [...]} or null
    var query = "";
    var listed = [];          // rows shown in the table (selection rows or search hits)

    function anyLayerOn() {
      return (OSM.on && !!DS.osm) || Object.keys(REG).some(function (k) { return REG[k].on; }) ||
        (HR.on && !!ui.register) || (WD.on && !!ui.wikidata);
    }
    function applyFilter() {
      if (!map.getLayer("points")) return;
      $("pv-hint").hidden = anyLayerOn();
      var vis = visibleExpr(), o = osmWants() || false;
      var hasSel = !!selected;
      map.setFilter("points", vis);
      map.setFilter("points-hit", vis);
      map.setFilter("points-ring", ["all", vis, [">", ["get", "sc"], 1]]);
      map.setPaintProperty("points", "circle-color",
        ["case", o, paletteExpr("bt"), ["case", [">", ["get", "sc"], 1], MATCH.matched, MATCH.only]]);
      map.setPaintProperty("points", "circle-radius", ["case", o, 3, 3.5]);
      map.setPaintProperty("points", "circle-opacity", hasSel ? 0.1 : ["case", o, 0.75, 0.85]);
      map.setPaintProperty("points-ring", "circle-stroke-opacity", hasSel ? 0.25 : 1);
      if (map.getLayer("register-only")) {
        var roOn = HR.on && HR.only ? ["has", "id"] : false;
        map.setFilter("register-only", roOn);
        map.setFilter("register-only-hit", roOn);
      }
      if (map.getLayer("wikidata-only")) {
        var woOn = WD.on && WD.only ? ["has", "id"] : false;
        map.setFilter("wikidata-only", woOn);
        map.setFilter("wikidata-only-hit", woOn);
      }
      var surf = OSM.surfOn && OSM.on ? "visible" : "none";
      map.setLayoutProperty("sites-fill", "visibility", surf);
      map.setLayoutProperty("sites-line", "visibility", surf);
      var lu = OSM.luOn ? "visible" : "none";
      map.setLayoutProperty("landuse-fill", "visibility", lu);
      map.setLayoutProperty("landuse-line", "visibility", lu);
      map.getSource("selection").setData(hasSel && selected.features ? selected.features : EMPTY_FC);
    }
    var EMPTY_FC = {type: "FeatureCollection", features: []};
    function showError(msg) {
      console.error(msg);
      $("pv-note").textContent = "error: " + msg;
    }
    window.addEventListener("error", function (e) { showError(e.message); });
    window.addEventListener("unhandledrejection", function (e) { showError(e.reason && e.reason.message ? e.reason.message : String(e.reason)); });

    $("pv-collapse").onclick = function () {
      var c = $("pv-toolbar").classList.toggle("collapsed");
      $("pv-collapse").textContent = c ? "▸" : "▾";
    };
    function updateInView() {
      if (!map.getLayer("points")) return;
      var n = map.queryRenderedFeatures({layers: ["points"]}).length;
      $("pv-inview").textContent = n.toLocaleString("de-DE") + " in view";
    }
    map.on("idle", updateInView);
    map.on("load", function () {
      map.addLayer({id: "landuse-fill", type: "fill", source: "companies", "source-layer": "landuse", minzoom: 10,
        paint: {"fill-color": ["match", ["get", "landuse"], "industrial", LU.industrial, "commercial", LU.commercial, "retail", LU.retail, "#999999"], "fill-opacity": 0.15}});
      map.addLayer({id: "landuse-line", type: "line", source: "companies", "source-layer": "landuse", minzoom: 10,
        paint: {"line-color": ["match", ["get", "landuse"], "industrial", LU.industrial, "commercial", LU.commercial, "retail", LU.retail, "#999999"], "line-width": 1, "line-dasharray": [3, 2]}});
      map.addLayer({id: "sites-fill", type: "fill", source: "companies", "source-layer": "sites", minzoom: 10,
        paint: {"fill-color": paletteExpr("bt"), "fill-opacity": 0.18}});
      map.addLayer({id: "sites-line", type: "line", source: "companies", "source-layer": "sites", minzoom: 10,
        paint: {"line-color": paletteExpr("bt"), "line-width": 1}});
      map.addLayer({id: "points", type: "circle", source: "companies", "source-layer": "points",
        paint: {"circle-color": paletteExpr("bt"), "circle-radius": 3, "circle-opacity": 0.75}});
      map.addLayer({id: "points-ring", type: "circle", source: "companies", "source-layer": "points",
        paint: {"circle-radius": 5.5, "circle-opacity": 0, "circle-stroke-width": 1.5, "circle-stroke-color": MATCH.matched, "circle-stroke-opacity": 1}});
      map.addSource("selection", {type: "geojson", data: EMPTY_FC});
      map.addLayer({id: "points-sel", type: "circle", source: "selection",
        paint: {"circle-radius": 6, "circle-color": "rgba(178,34,34,0.9)", "circle-stroke-width": 1, "circle-stroke-color": "#000"}});
      map.addLayer({id: "points-hit", type: "circle", source: "companies", "source-layer": "points",
        paint: {"circle-radius": 8, "circle-opacity": 0}});
      if (ui.register_only) {   // register-only companies: hollow squares-ish markers (black ring, no fill)
        map.addLayer({id: "register-only", type: "circle", source: "companies", "source-layer": "register", filter: false,
          paint: {"circle-radius": 4, "circle-color": "#ffffff", "circle-opacity": 0.6, "circle-stroke-width": 1.5,
                  "circle-stroke-color": ["case", ["==", ["get", "ind"], 1], "#111111", "#888888"]}});
        map.addLayer({id: "register-only-hit", type: "circle", source: "companies", "source-layer": "register", filter: false,
          paint: {"circle-radius": 8, "circle-opacity": 0}});
        map.on("mousemove", "register-only-hit", function (e) {
          var f = e.features[0]; if (!f || mode) return;
          map.getCanvas().style.cursor = "pointer";
          hover.setLngLat(f.geometry.coordinates).setHTML("<div class='card'><b>register only:</b> " + esc(f.properties.name || "—") +
            (f.properties.lf ? " (" + esc(f.properties.lf) + ")" : "") + "<br><span class='k'>" + esc(f.properties.reg || "") +
            " · " + esc(f.properties.src || "") + " · geocode " + esc(f.properties.geo || "") + "</span>" +
            (f.properties.obj ? "<br>" + esc(f.properties.obj) : "") + "</div>").addTo(map);
        });
        map.on("mouseleave", "register-only-hit", function () { map.getCanvas().style.cursor = ""; hover.remove(); });
      }
      if (ui.wikidata && ui.wikidata.only) {   // wikidata-only companies: blue ring, no fill
        map.addLayer({id: "wikidata-only", type: "circle", source: "companies", "source-layer": "wikidata", filter: false,
          paint: {"circle-radius": 4, "circle-color": "#ffffff", "circle-opacity": 0.6, "circle-stroke-width": 1.5, "circle-stroke-color": WD_COLOUR}});
        map.addLayer({id: "wikidata-only-hit", type: "circle", source: "companies", "source-layer": "wikidata", filter: false,
          paint: {"circle-radius": 8, "circle-opacity": 0}});
        map.on("mousemove", "wikidata-only-hit", function (e) {
          var f = e.features[0]; if (!f || mode) return;
          map.getCanvas().style.cursor = "pointer";
          hover.setLngLat(f.geometry.coordinates).setHTML("<div class='card'><b>wikidata only:</b> " + esc(f.properties.name || "—") +
            "<br><span class='k'>" + esc(f.properties.id || "") + " · " + esc(f.properties.role || "") + " item of a map company" +
            (f.properties.ind ? " · " + esc(f.properties.ind) : "") + "</span>" +
            (f.properties.web ? "<br><a class='pv-link' href='" + esc(webUrl(f.properties.web)) + "' target='_blank' rel='noopener'>" + esc(f.properties.web) + "</a>" : "") + "</div>").addTo(map);
        });
        map.on("mouseleave", "wikidata-only-hit", function () { map.getCanvas().style.cursor = ""; hover.remove(); });
      }
      map.addLayer({id: "points-pin", type: "circle", source: "companies", "source-layer": "points",
        filter: ["==", ["get", "id"], ""], paint: {"circle-radius": 9, "circle-opacity": 0, "circle-stroke-width": 2.5, "circle-stroke-color": "#111"}});
      applyFilter();
      renderTable();
    });
    buildBar();   // the toolbar needs only the model — it shows even before the tiles are drawn
    ["sel-state", "sel-district", "sel-sector", "chk-ind"].forEach(function (id) {
      $(id).addEventListener("change", function () {
        if (id === "sel-state") {
          fillDistricts($("sel-state").value);
          var s = ui.states.filter(function (x) { return x.name === $("sel-state").value; })[0];
          if (s) map.fitBounds([[s.bbox[0], s.bbox[1]], [s.bbox[2], s.bbox[3]]], {padding: 20});
        }
        if (id === "sel-district") {
          var d = ui.districts.filter(function (x) { return x.ags === $("sel-district").value; })[0];
          if (d) map.fitBounds([[d.bbox[0], d.bbox[1]], [d.bbox[2], d.bbox[3]]], {padding: 20});
        }
        applyFilter(); refreshList();
      });
    });

    // ---- bottom toolbar: one row per dataset ------------------------------------------------
    function mkBtn(html, title, handler) {
      var b = document.createElement("button"); b.className = "pv-btn active"; b.innerHTML = html; b.title = title;
      b.onclick = function () { handler(b); };
      return b;
    }
    function swatch(colr) { return '<span class="pv-swatch" style="background:' + (colr || "#666") + '"></span>'; }
    function nHtml(x) { return ' <span class="pv-n">' + x.toLocaleString("de-DE") + "</span>"; }
    function addRow(name) {
      var row = document.createElement("div"); row.className = "pv-row";
      var label = document.createElement("span"); label.className = "pv-ds"; label.textContent = name; row.appendChild(label);
      return row;
    }
    function masterBtn(row, state, name) {
      var b = mkBtn("⏻", "toggle the whole " + name + " dataset on/off", function (bb) {
        state.on = !state.on; bb.classList.toggle("active", state.on); row.classList.toggle("pv-off", !state.on);
        applyFilter(); refreshList();
      });
      b.classList.toggle("active", state.on); row.classList.toggle("pv-off", !state.on);
      row.appendChild(b);
    }
    function sep() { var d = document.createElement("span"); d.className = "pv-sep"; return d; }
    function buildBar() {
      var bar = $("pv-toolbar-body");
      if (DS.osm) {
        var orow = addRow("OSM"); masterBtn(orow, OSM, "osm");
        Object.keys(DS.osm.cats).sort().forEach(function (cat) {
          orow.appendChild(mkBtn(swatch(PAL[cat]) + esc(cat) + nHtml(DS.osm.cats[cat]), "show/hide " + COLOR_COL + "=" + cat + " (osm)",
            function (b) { OSM.cats[cat] = !OSM.cats[cat]; b.classList.toggle("active", OSM.cats[cat]); applyFilter(); refreshList(); }));
        });
        orow.appendChild(mkBtn("▨ surfaces" + nHtml(DS.osm.nSurf), "show/hide company ground surface polygons (rendered from zoom 10 — zoom in)",
          function (b) { OSM.surfOn = !OSM.surfOn; b.classList.toggle("active", OSM.surfOn); applyFilter(); }));
        orow.appendChild(mkBtn("▦ landuse", "show/hide OSM landuse zones (rendered from zoom 10 — zoom in)",
          function (b) { OSM.luOn = !OSM.luOn; b.classList.toggle("active", OSM.luOn); applyFilter(); }));
        bar.appendChild(orow);
      }
      var TIPS = {ied: "Annex-I activity group filter (from ied_activity, 1:1)", abwaerme: "heat-quantity band filter (from abw_heat_mwh_a)",
                  overture: "business type filter", mastr: "technology toggle — a site stays visible while ANY of its technologies is enabled (counts: sites having that technology)"};
      Object.keys(REG).sort().forEach(function (k) {
        var R = REG[k], D = DS[k], row = addRow(LBL[k] || k);
        masterBtn(row, R, LBL[k] || k);
        [["matched", "matched"], ["only", (LBL[k] || k) + "-only"]].forEach(function (mm) {
          row.appendChild(mkBtn(swatch(MATCH[mm[0]]) + esc(mm[1]) + nHtml(D.match[mm[0]]), "show/hide " + (LBL[k] || k) + " entities: " + mm[1],
            function (b) { R.match[mm[0]] = !R.match[mm[0]]; b.classList.toggle("active", R.match[mm[0]]); applyFilter(); refreshList(); }));
        });
        var grps = ui.grp_order.filter(function (g) { return g in D.groups; })
          .concat(Object.keys(D.groups).filter(function (g) { return ui.grp_order.indexOf(g) < 0; }).sort());
        if (grps.length) row.appendChild(sep());
        grps.forEach(function (g) {
          row.appendChild(mkBtn((PAL[g] ? swatch(PAL[g]) : "") + esc(g) + nHtml(D.groups[g]), TIPS[k] || "group filter",
            function (b) { R.grps[g] = !R.grps[g]; b.classList.toggle("active", R.grps[g]); applyFilter(); refreshList(); }));
        });
        bar.appendChild(row);
      });
      if (ui.register) {   // Register row: "matched" validates the dots that are on, "register-only" adds the register companies with no map entity
        var hrow = addRow("Register"); masterBtn(hrow, HR, "Register (Handelsregister via OffeneRegister / GLEIF)");
        hrow.appendChild(mkBtn(swatch(HR_COLOURS.matched_active) + "matched" + nHtml(ui.register.matched || 0),
          "show/hide the companies matched to a register company (active or dissolved — see the card)",
          function (b) { HR.matched = !HR.matched; b.classList.toggle("active", HR.matched); applyFilter(); refreshList(); }));
        if (ui.register_only) hrow.appendChild(mkBtn(swatch("#111111") + "register-only" + nHtml(ui.register.only || 0),
          "active register companies with a Bremen address that no map row matched (geocoded offline against OSM addresses; black ring = industrial business purpose)",
          function (b) { HR.only = !HR.only; b.classList.toggle("active", HR.only); applyFilter(); refreshList(); }));
        bar.appendChild(hrow);
      }
      if (ui.wikidata) {   // Wikidata row: "matched" validates the dots that are on, "wikidata-only" adds items with coordinates but no map entity
        var wrow = addRow("Wikidata"); masterBtn(wrow, WD, "Wikidata");
        wrow.appendChild(mkBtn(swatch(WD_COLOUR) + "matched" + nHtml(ui.wikidata.matched || 0),
          "show/hide the companies with a Wikidata item for the entity itself or its operator",
          function (b) { WD.matched = !WD.matched; b.classList.toggle("active", WD.matched); applyFilter(); refreshList(); }));
        if (ui.wikidata.only) wrow.appendChild(mkBtn(swatch(WD_COLOUR) + "wikidata-only" + nHtml(ui.wikidata.only),
          "Wikidata items (operator or brand of a map company) with coordinates in the scope but no map entity of their own",
          function (b) { WD.only = !WD.only; b.classList.toggle("active", WD.only); applyFilter(); refreshList(); }));
        bar.appendChild(wrow);
      }
    }

    // ---- cards -----------------------------------------------------------------------------
    var cardCache = {};
    function getCard(id) {
      if (cardCache[id]) return Promise.resolve(cardCache[id]);
      return fetch(API + "/companies/" + encodeURIComponent(id)).then(function (r) { return r.json(); })
        .then(function (rec) { cardCache[id] = rec; return rec; });
    }
    function webUrl(w) { if (!w) return null; w = String(w).trim(); return /^https?:\/\//i.test(w) ? w : "https://" + w; }
    function fmt(v) {
      if (v === null || v === undefined) return "";
      if (typeof v === "number") return v.toLocaleString("de-DE");
      if (typeof v === "boolean") return v ? "true" : "false";
      return esc(v);
    }
    function nameHtml(rec) {
      var u = webUrl(rec.website), nm = esc(rec.name == null ? "" : rec.name);
      return u ? '<a class="pv-link" href="' + esc(u) + '" target="_blank" rel="noopener">' + nm + "</a>" : nm;
    }
    function srcTag(src) { return src ? " (" + esc(String(src).split("+").map(function (x) { return SRC_SHORT[x] || x; }).join("+")) + ")" : ""; }
    function activityLabel(code) {
      if (!code) return null;
      var c = String(code).trim(), probe = c;
      while (probe) {
        if (IED_LABELS[probe]) return c + " – " + IED_LABELS[probe];
        if (probe.indexOf("(") < 0) break;
        probe = probe.slice(0, probe.lastIndexOf("("));
      }
      return c;
    }
    function techLines(td) {
      if (!td) return "";
      var obj = td; if (typeof td === "string") { try { obj = JSON.parse(td); } catch (e) { return esc(td); } }
      return Object.keys(obj).sort().map(function (tech) {
        var units = Array.isArray(obj[tech]) ? obj[tech] : [];
        var lines = units.slice(0, 12).map(function (u) {
          return "<div class='units'>" + Object.keys(u).filter(function (k) { return k !== "unit"; })
            .map(function (k) { return esc(k) + " " + fmt(u[k]) + (UNIT_OF[k] ? " " + UNIT_OF[k] : ""); }).join(", ") + "</div>";
        }).join("");
        return "<div><b>" + esc(tech) + "</b> (" + units.length + " unit" + (units.length === 1 ? "" : "s") + ")" + lines + "</div>";
      }).join("");
    }
    function first(rec, key) { return Array.isArray(rec[key]) && rec[key].length ? rec[key][0] : null; }
    function summaryHtml(rec) {   // the preview's hover card, field for field (+ register / wikidata verdict lines)
      var lines = ["<b>name:</b> " + nameHtml(rec)];
      if (rec.register_match && rec.register_match !== "n/a") lines.push("<b>register:</b> " + esc(rec.register_match) +
        (rec.hr_status ? " · " + esc(rec.hr_status) : "") + (rec.hr_name ? " · " + esc(rec.hr_name) : ""));
      if (rec.wd_id || rec.wd_operator_id) lines.push("<b>wikidata:</b> " + esc(rec.wd_id || "") + (rec.wd_operator_id ? " operator " + esc(rec.wd_operator_id) : "") +
        (rec.wd_industry ? " · " + esc(rec.wd_industry) : ""));
      if (rec.business_type) lines.push("<b>business_type:</b> " + esc(rec.business_type) + srcTag(rec.business_type_source));
      if (rec.business_subtype) lines.push("<b>business_subtype:</b> " + esc(rec.business_subtype) + srcTag(rec.business_subtype_source));
      var ms = rec.mastr && rec.mastr.filter(function (m) { return m.mastr_wz_abschnitt; })[0];
      if (ms) {
        var sec = String(ms.mastr_wz_abschnitt).replace(/^\s*Abschnitt\s+([A-Z])\s*[-–—]\s*/, "$1 – ");
        lines.push("<b>mastr_wz:</b> " + esc(sec + (ms.mastr_wz_gruppe ? " / " + (ms.mastr_wz_code ? ms.mastr_wz_code + " " : "") + ms.mastr_wz_gruppe : "")));
      }
      if (rec.grounds_area_m2 != null) lines.push("<b>grounds_area_m2:</b> " + fmt(rec.grounds_area_m2));
      if (rec.nace_primary) lines.push("<b>nace_primary:</b> " + esc(rec.nace_primary));
      if (rec.source) lines.push("<b>source:</b> " + esc(rec.source));
      var ied = first(rec, "ied"); if (ied && ied.ied_activity) lines.push("<b>ied_activity:</b> " + esc(activityLabel(ied.ied_activity)));
      var abw = first(rec, "abwaerme");
      if (abw) {
        if (abw.abw_heat_mwh_a != null) lines.push("<b>abw_heat_mwh_a:</b> " + fmt(abw.abw_heat_mwh_a));
        if (abw.abw_temp_c != null) lines.push("<b>abw_temp_c:</b> " + fmt(abw.abw_temp_c));
      }
      var ovt = first(rec, "overture"); if (ovt && ovt.ovt_confidence != null) lines.push("<b>ovt_confidence:</b> " + fmt(ovt.ovt_confidence));
      var html = lines.join("<br>");
      if (rec.mastr) {
        var merged = {};
        rec.mastr.forEach(function (m) {
          var td = m.mastr_tech_detail; if (typeof td === "string") { try { td = JSON.parse(td); } catch (e) { td = null; } }
          if (td) Object.keys(td).forEach(function (t) { merged[t] = (merged[t] || []).concat(td[t]); });
        });
        var techs = techLines(merged);
        if (techs) html += "<br><b>mastr_technologies:</b><br>" + techs;
      }
      return html;
    }
    var SOURCE_KEYS = ["osm", "ied", "abwaerme", "mastr", "overture"];
    function fieldsHtml(rec, skip) {
      return Object.keys(rec).filter(function (k) {
        return skip.indexOf(k) < 0 && rec[k] !== null && rec[k] !== undefined && rec[k] !== "" && k !== "latitude" && k !== "longitude";
      }).map(function (k) {
        if (k === "mastr_tech_detail") return "<div><span class='k'>" + esc(k) + ":</span>" + techLines(rec[k]) + "</div>";
        if (k === "website" || k === "website_listing") {
          return "<div><span class='k'>" + esc(k) + ":</span> <a class='pv-link' href='" + esc(webUrl(rec[k])) + "' target='_blank' rel='noopener'>" + esc(rec[k]) + "</a></div>";
        }
        return "<div><span class='k'>" + esc(k) + ":</span> " + fmt(rec[k]) + "</div>";
      }).join("");
    }
    function cardHtml(rec) {
      var html = "<div class='card'>" + summaryHtml(rec);
      // the raw register row behind hr_id and the raw Wikidata items behind wd_* (nested in tier 2);
      // the merged record keeps the derived hr_* / wd_* fields
      if (Array.isArray(rec.register) && rec.register.length) {
        html += "<details><summary>register — raw record</summary>" + rec.register.map(function (r) {
          return "<div class='rec'><span class='k'>" + esc(r.hr_source || "") + " " + esc(r.hr_id || "") + "</span>" + fieldsHtml(r, ["hr_id", "hr_source"]) + "</div>";
        }).join("") + "</details>";
      }
      if (Array.isArray(rec.wikidata) && rec.wikidata.length) {
        html += "<details><summary>wikidata — raw record" + (rec.wikidata.length > 1 ? "s (" + rec.wikidata.length + ")" : "") + "</summary>" +
          rec.wikidata.map(function (r) {
            return "<div class='rec'><span class='k'>" + esc(r.role || "") + " <a class='pv-link' href='https://www.wikidata.org/wiki/" + esc(r.qid || "") +
              "' target='_blank' rel='noopener'>" + esc(r.qid || "") + "</a></span>" + fieldsHtml(r, ["role", "qid"]) + "</div>";
          }).join("") + "</details>";
      }
      html += "<details><summary>merged record — all fields</summary>" + fieldsHtml(rec, SOURCE_KEYS.concat(["name", "register", "wikidata"])) + "</details>";
      SOURCE_KEYS.forEach(function (k) {
        if (!Array.isArray(rec[k]) || !rec[k].length) return;
        var recs = rec[k].map(function (r, i) {
          return "<div class='rec'><span class='k'>#" + (i + 1) + " " + esc(r.id || "") + "</span>" + fieldsHtml(r, ["id"]) + "</div>";
        }).join("");
        var title = (LBL[k] || k) + " — raw record" + (rec[k].length > 1 ? "s (" + rec[k].length + ")" : "");
        html += "<details><summary>" + esc(title) + "</summary>" + recs + "</details>";
      });
      return html + "</div>";
    }

    // ---- hover + pin -------------------------------------------------------------------------
    var hover = new maplibregl.Popup({closeButton: false, closeOnClick: false, className: "hover", offset: 8, maxWidth: "420px"});
    var pin = new maplibregl.Popup({closeButton: true, closeOnClick: true, maxWidth: "440px", offset: 8});
    var pinnedId = null, hoverId = null, hoverTimer = null;
    pin.on("close", function () { pinnedId = null; map.setFilter("points-pin", ["==", ["get", "id"], ""]); });
    map.on("mousemove", "points-hit", function (e) {
      if (mode) return;
      var f = e.features[0]; if (!f) return;
      map.getCanvas().style.cursor = "pointer";
      if (f.properties.id === pinnedId) { hover.remove(); return; }
      if (f.properties.id === hoverId) return;
      hoverId = f.properties.id; clearTimeout(hoverTimer);
      var ll = f.geometry.coordinates;
      var quick = "<div class='card'><b>name:</b> " + esc(f.properties.name || "—") +
        (f.properties.bt ? "<br><b>business_type:</b> " + esc(f.properties.bt) : "") + "<br><b>source:</b> " + esc(f.properties.src) +
        (f.properties.hr ? "<br><b>register:</b> " + esc(HR_LABELS[f.properties.hr] || f.properties.hr) : "") +
        (f.properties.wd ? "<br><b>wikidata:</b> " + esc(String(f.properties.wd).split("+").filter(Boolean).map(function (k) { return WD_LABELS[k] || k; }).join(", ")) : "") + "</div>";
      hover.setLngLat(ll).setHTML(quick).addTo(map);
      hoverTimer = setTimeout(function () {
        getCard(hoverId).then(function (rec) { if (hoverId === rec.id && pinnedId !== rec.id) hover.setHTML("<div class='card'>" + summaryHtml(rec) + "</div>"); });
      }, 220);
    });
    map.on("mouseleave", "points-hit", function () { map.getCanvas().style.cursor = mode ? "crosshair" : ""; hover.remove(); hoverId = null; clearTimeout(hoverTimer); });
    map.on("click", "points-hit", function (e) {
      if (mode) return;
      var f = e.features[0]; if (f) pinCard(f.properties.id, f.geometry.coordinates);
    });
    function pinCard(id, lngLat) {
      pinnedId = id; hover.remove(); hoverId = null;
      map.setFilter("points-pin", ["==", ["get", "id"], id]);
      pin.setLngLat(lngLat).setHTML("<div class='card'>loading …</div>").addTo(map);
      getCard(id).then(function (rec) { if (pinnedId === id) pin.setHTML(cardHtml(rec)); });
    }

    // ---- selection: box + lasso (API-computed over the full data) ----------------------------
    var mode = null, dragging = false, anchor = null, lassoPts = [];
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.id = "selshape"; $("map").appendChild(svg);
    var shape = null;
    function setMode(m) {
      mode = (mode === m) ? null : m;
      $("pv-box").classList.toggle("active", mode === "box");
      $("pv-lasso").classList.toggle("active", mode === "lasso");
      if (mode) { map.dragPan.disable(); map.getCanvas().style.cursor = "crosshair"; hover.remove(); }
      else { map.dragPan.enable(); map.getCanvas().style.cursor = ""; }
    }
    $("pv-box").onclick = function () { setMode("box"); };
    $("pv-lasso").onclick = function () { setMode("lasso"); };
    $("pv-clear").onclick = function () { clearSelection(); };
    $("pv-reset").onclick = function () { map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], {padding: 20}); };
    document.addEventListener("keydown", function (e) {   // Esc: leave select mode, drop selection, close cards
      if (e.key !== "Escape") return;
      if (mode) setMode(mode);
      clearSelection();
      hover.remove(); hoverId = null; clearTimeout(hoverTimer);
      if (pinnedId !== null) pin.remove();
    });
    function pt(e) { var r = $("map").getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
    var cc = map.getCanvasContainer();
    cc.addEventListener("mousedown", function (e) {
      if (!mode || e.button !== 0) return;
      dragging = true; anchor = pt(e); lassoPts = [anchor]; e.preventDefault();
      svg.innerHTML = ""; shape = document.createElementNS("http://www.w3.org/2000/svg", mode === "box" ? "rect" : "polygon");
      shape.setAttribute("fill", "rgba(85,85,85,.05)"); shape.setAttribute("stroke", "#555"); shape.setAttribute("stroke-dasharray", "4");
      svg.appendChild(shape);
    });
    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var p = pt(e);
      if (mode === "box") {
        shape.setAttribute("x", Math.min(anchor[0], p[0])); shape.setAttribute("y", Math.min(anchor[1], p[1]));
        shape.setAttribute("width", Math.abs(p[0] - anchor[0])); shape.setAttribute("height", Math.abs(p[1] - anchor[1]));
      } else {
        lassoPts.push(p); shape.setAttribute("points", lassoPts.map(function (q) { return q.join(","); }).join(" "));
      }
    });
    window.addEventListener("mouseup", function (e) {
      if (!dragging) return;
      dragging = false; svg.innerHTML = ""; shape = null;
      var p = pt(e), ring;
      if (mode === "box") {
        if (Math.abs(p[0] - anchor[0]) < 3 || Math.abs(p[1] - anchor[1]) < 3) return;
        ring = [anchor, [p[0], anchor[1]], p, [anchor[0], p[1]], anchor];
      } else {
        if (lassoPts.length < 3) return;
        ring = lassoPts.concat([lassoPts[0]]);
      }
      var coords = ring.map(function (q) { var ll = map.unproject(q); return [ll.lng, ll.lat]; });
      runSelection({type: "Polygon", coordinates: [coords]});
    });
    function post(geom, extra) {
      return fetch(API + "/companies/query?" + regionParams().concat(extra).join("&"),
        {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(geom)});
    }
    function asJson(r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error("API " + r.status + ": " + t.slice(0, 200)); });
      return r.json();
    }
    function runSelection(geom) {
      query = ""; $("pv-search").value = "";
      selected = {geometry: geom, count: null, features: null};
      $("pv-count").textContent = "selecting …"; $("pv-table").innerHTML = ""; $("pv-note").textContent = "";
      var t0 = performance.now();
      Promise.all([
        post(geom, ["format=json", "limit=" + TABLE_CAP]).then(asJson),
        post(geom, ["format=json", "limit=" + HL_CAP, "fields=id,longitude,latitude"]).then(asJson)
      ]).then(function (res) {
        if (!selected || selected.geometry !== geom) return;
        listed = res[0];
        selected.features = {type: "FeatureCollection", features: res[1].map(function (x) {
          return {type: "Feature", geometry: {type: "Point", coordinates: [x.longitude, x.latitude]}, properties: {id: x.id}};
        })};
        if (res[1].length < HL_CAP) { selected.count = res[1].length; applyFilter(); renderTable(); }
        else post(geom, ["count_only=true"]).then(asJson).then(function (c) {
          if (selected && selected.geometry === geom) { selected.count = c.count; applyFilter(); renderTable(); }
        });
        console.log("selection in " + (performance.now() - t0).toFixed(0) + " ms");
      }).catch(function (e) { selected = null; applyFilter(); renderTable(); showError(e.message); });
    }
    function clearSelection() { selected = null; listed = []; applyFilter(); renderTable(); }
    function refreshList() {   // toggles changed: recompute the selection / search with the new display model
      if (selected) runSelection(selected.geometry); else if (query) runSearch(); else renderTable();
    }

    // ---- right panel: table, search, download --------------------------------------------------
    var TBL_COLS = ["name", COLOR_COL, "address_full", "grounds_area_m2", "source"];
    var searchTimer = null;
    $("pv-search").addEventListener("input", function () {
      query = $("pv-search").value.trim(); clearTimeout(searchTimer);
      if (query.length < 2) { query = ""; if (!selected) { listed = []; renderTable(); } return; }
      searchTimer = setTimeout(runSearch, 200);
    });
    function runSearch() {
      selected = null; applyFilter();
      var q = query;
      fetch(API + "/search?" + regionParams().concat(["q=" + encodeURIComponent(q), "limit=" + TABLE_CAP]).join("&"))
        .then(asJson).then(function (rows) { if (q === query) { listed = rows; renderTable(); } }).catch(function (e) { showError(e.message); });
    }
    function renderTable() {
      var n = query ? listed.length : (selected ? selected.count : 0);
      $("pv-count").textContent = query ? listed.length + " match" + (listed.length === 1 ? "" : "es") + (listed.length >= TABLE_CAP ? "+" : "")
        : (selected ? (n == null ? "…" : n.toLocaleString("de-DE") + " selected") : "no selection");
      $("pv-download").disabled = !(query ? listed.length : (selected && selected.count));
      var rows = listed.slice(0, TABLE_CAP).map(function (d, i) {
        return '<tr class="pv-hit" data-i="' + i + '" title="click to zoom to this company">' + TBL_COLS.map(function (c) {
          var v = d[c] == null ? "" : d[c];
          return '<td title="' + esc(v) + '">' + (c === "name" ? nameHtml(d) : fmt(v)) + "</td>";
        }).join("") + "</tr>";
      });
      $("pv-table").innerHTML = listed.length
        ? "<tr>" + TBL_COLS.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr>" + rows.join("") : "";
      $("pv-note").textContent = listed.length
        ? ((selected && selected.count > TABLE_CAP) || (query && listed.length >= TABLE_CAP) ? "table shows first " + TABLE_CAP + " rows — " : "") +
          "Download exports all " + (selected ? selected.count.toLocaleString("de-DE") : listed.length) + " rows — click a row to zoom"
        : (query ? "no visible company name contains \"" + query + "\""
           : (!OSM.on && !Object.keys(REG).some(function (k) { return REG[k].on; })
              ? "switch a data layer on in the toolbar below (⏻), then box-/lasso-select or search — Esc deselects"
              : "type a name above, or box-/lasso-select companies on the map — Esc deselects"));
    }
    $("pv-table").addEventListener("click", function (e) {
      if (e.target.closest("a")) return;
      var tr = e.target.closest("tr[data-i]"); if (!tr) return;
      var d = listed[+tr.getAttribute("data-i")]; if (!d) return;
      map.jumpTo({center: [d.longitude, d.latitude], zoom: Math.max(map.getZoom(), 16)});
      map.once("idle", function () { pinCard(d.id, [d.longitude, d.latitude]); });
    });
    $("pv-download").onclick = function () {
      var f = $("pv-format").value, fmtParam = f === "csv" ? ["format=csv", "tier=flat"] : ["format=parquet", "tier=" + (f === "parquet-full" ? "full" : "flat")];
      var name = "geoextract_" + manifest.scope + "_selection." + (f === "csv" ? "csv" : "parquet");
      var req = query ? fetch(API + "/companies?" + regionParams().concat(fmtParam, ["q=" + encodeURIComponent(query)]).join("&"))
                      : post(selected.geometry, fmtParam);
      $("pv-download").disabled = true;
      req.then(function (r) { return r.blob(); }).then(function (blob) {
        var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click(); URL.revokeObjectURL(a.href);
      }).finally(function () { $("pv-download").disabled = false; });
    };

    // ---- PNG export of the current view ------------------------------------------------------
    $("pv-save").onclick = function () {
      var cv = map.getCanvas(), out = document.createElement("canvas");
      out.width = cv.width; out.height = cv.height;
      var ctx = out.getContext("2d");
      try {
        ctx.drawImage(cv, 0, 0);
        var attr = "© OpenStreetMap contributors (ODbL)";
        ctx.font = "11px sans-serif"; var w = ctx.measureText(attr).width;
        ctx.fillStyle = "rgba(255,255,255,.8)"; ctx.fillRect(out.width - w - 12, out.height - 18, w + 12, 18);
        ctx.fillStyle = "#333"; ctx.fillText(attr, out.width - w - 6, out.height - 5);
        out.toBlob(function (blob) {
          var a = document.createElement("a"); a.href = URL.createObjectURL(blob);
          a.download = "geoextract_" + manifest.scope + "_view.png"; a.click(); URL.revokeObjectURL(a.href);
        }, "image/png");
      } catch (e) {
        $("pv-save").textContent = "📷 blocked (CORS)"; setTimeout(function () { $("pv-save").textContent = "📷 Image"; }, 3000);
      }
    };

    // ---- precomputed downloads panel ---------------------------------------------------------------
    $("btn-extracts").onclick = function () { $("extracts").hidden = !$("extracts").hidden; renderExtracts(); };
    $("ex-close").onclick = function () { $("extracts").hidden = true; };
    function renderExtracts() {
      var files = Object.keys(manifest.files).filter(function (f) { return f.indexOf("extracts/") === 0; }).sort(), byDir = {};
      files.forEach(function (f) {
        var p = f.split("/"), m = p[2].replace(".parquet", "").match(/^(.*)_(flat|full)$/); if (!m) return;
        (byDir[p[1] + "|" + m[1]] = byDir[p[1] + "|" + m[1]] || {dir: p[1], sector: m[1]})[m[2]] = f;
      });
      var link = function (f) { return f ? "<a href='" + DATA + "/" + f + "' download>" + (manifest.files[f].bytes / 1e6).toFixed(1) + " MB</a>" : ""; };
      $("ex-body").innerHTML = "<table><tr><th>state / scope</th><th>sector</th><th>flat</th><th>full</th></tr>" +
        Object.keys(byDir).sort().map(function (k) { var e = byDir[k];
          return "<tr><td>" + esc(e.dir) + "</td><td>" + esc(e.sector) + "</td><td>" + link(e.flat) + "</td><td>" + link(e.full) + "</td></tr>"; }).join("") +
        "</table><p class='sub'>Also: <a href='" + DATA + "/companies_flat.parquet'>companies_flat.parquet</a>, <a href='" + DATA + "/companies_full.parquet'>companies_full.parquet</a>, " +
        "<a href='" + DATA + "/sites.parquet'>sites.parquet</a> (polygons), <a href='" + DATA + "/manifest.json'>manifest.json</a> (sha256, licence), API docs at <a href='docs'>/docs</a>.</p>";
    }
  }
})();
