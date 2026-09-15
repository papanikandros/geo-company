"""S3 — the served API: FastAPI + DuckDB, read-only over one serve version (serve-plan.md §3).

``geoextract serve api --scope bremen --port 8791`` serves the map page, the version
directory (``/data/…``, Range requests for PMTiles) and:

GET  /v1/manifest, /v1/ui, /v1/summary, /v1/extracts
GET  /v1/companies?state=&district=&sector=&industrial=&bbox=minx,miny,maxx,maxy
                  &tier=flat|full&format=json|geojson|csv|parquet&limit=&offset=&count_only=
POST /v1/companies/query   body = GeoJSON Polygon / MultiPolygon / Feature (+ the same
                           query parameters) — draw-and-download
GET  /v1/companies/{id}    every public field + the nested raw record of every source
GET  /v1/search?q=&limit=  name search (prefix > substring, ≤ 50 hits)

Formats: ``json`` (records, nested sources as objects), ``geojson`` (points, tier-1
properties, ≤ SERVE_API_MAX_GEOJSON rows), ``csv`` (tier 1 flat; with ``tier=full`` the
nested source columns are JSON strings), ``parquet`` (typed, nested for ``tier=full``).
Every response carries ``X-Query-Ms``. Polygon queries prefilter on the polygon's bbox in
SQL and finish with a vectorised point-in-polygon test (shapely) — no DuckDB spatial
extension needed. The DuckDB connection is shared behind a lock (uvicorn workers = 1).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.middleware.cors import CORSMiddleware

from .. import config
from ..resolve import normalize_name
from .dev import WEB_DIR, _jsonable, latest_version

MAX_JSON = 50_000
DEFAULT_JSON = 1_000
FORMATS = ("json", "geojson", "csv", "parquet")
SOURCE_KEYS = list(config.SERVE_SOURCE_PREFIXES) + ["register"]


class Store:
    def __init__(self, version_dir: Path):
        self.dir = version_dir
        self.lock = threading.Lock()
        self.con = duckdb.connect()
        self.manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf8"))
        self.ui = json.loads((version_dir / "ui.json").read_text(encoding="utf8"))
        self.sector_col = self.ui.get("sector_column", "business_type")
        # view names are prefixed: "full" is a reserved word in DuckDB (FULL JOIN)
        for name in ("flat", "full", "search"):
            p = version_dir / (f"companies_{name}.parquet" if name != "search" else "search.parquet")
            if p.exists():
                self.con.execute(f"CREATE VIEW t_{name} AS SELECT * FROM read_parquet('{p}')")
        if not (version_dir / "companies_full.parquet").exists():
            self.con.execute("CREATE VIEW t_full AS SELECT * FROM t_flat")
        self.cols = {t: [r[0] for r in self.con.execute(f"DESCRIBE t_{t}").fetchall()] for t in ("flat", "full")}
        self.nested = [c for c in self.cols["full"] if c in SOURCE_KEYS]
        # id → (state, district) in memory with an index: a single-company lookup then hits
        # only the row group(s) of that state/district instead of scanning the nested file
        # (4 M rows DE-wide ≈ 1 s per card without it)
        self.con.execute("CREATE TABLE t_ids AS SELECT id, state, district FROM t_flat")
        self.con.execute("CREATE INDEX t_ids_id ON t_ids(id)")
        props = version_dir / "props.parquet"
        self.has_props = props.exists()
        if self.has_props:   # display properties → toggle-aware selections (see display_sql)
            self.con.execute(f"CREATE TABLE t_props AS SELECT * FROM read_parquet('{props}')")
            self.con.execute("CREATE INDEX t_props_id ON t_props(id)")

    # --- filters -------------------------------------------------------------------------
    def display_sql(self, display: dict | None) -> tuple[str, list] | None:
        """The map's toggle model (identical semantics to the page and the canonical preview:
        OR across datasets, AND within a dataset) as a subquery over t_props.
        display = {"osm": {"on": bool, "cats": [...]}, "reg": {"ied"|"abwaerme"|"overture"|"mastr":
        {"on": bool, "matched": bool, "only": bool, "groups": [...] | null}}}
        groups null = no group restriction; [] = none enabled."""
        if not display or not self.has_props:
            return None
        parts, params = [], []
        osm = display.get("osm") or {}
        if osm.get("on") and osm.get("cats"):
            parts.append("(regexp_matches(src, '(^|\\+)osm(\\+|$)') AND coalesce(bt, '∅') IN "
                         + "(" + ", ".join("?" * len(osm["cats"])) + "))")
            params += list(osm["cats"])
        gprop = {"ied": "ied", "abwaerme": "abw", "overture": "bt", "mastr": "mt"}
        for ds, cfg in (display.get("reg") or {}).items():
            if ds not in gprop or not cfg or not cfg.get("on"):
                continue
            matched, only = cfg.get("matched", True), cfg.get("only", True)
            if not matched and not only:
                continue
            c = [f"regexp_matches(src, '(^|\\+){ds}(\\+|$)')"]
            if not matched:
                c.append("coalesce(sc, 1) <= 1")
            if not only:
                c.append("coalesce(sc, 1) > 1")
            groups = cfg.get("groups")
            if groups is not None:
                if not groups:
                    continue
                if ds == "mastr":
                    c.append("(" + " OR ".join("contains(coalesce(mt, ''), ?)" for _ in groups) + ")")
                    params += [f"+{g}+" for g in groups]
                else:
                    default = "unmapped" if ds == "overture" else "∅"
                    c.append(f"coalesce({gprop[ds]}, '{default}') IN (" + ", ".join("?" * len(groups)) + ")")
                    params += list(groups)
            parts.append("(" + " AND ".join(c) + ")")
        if not parts:
            return "FALSE", []
        expr = "(" + " OR ".join(parts) + ")"
        hr = display.get("hr")           # register verdict classes (AND with the dataset rows)
        if isinstance(hr, list):
            if not hr:
                return "FALSE", []
            expr += " AND coalesce(hr, 'none') IN (" + ", ".join("?" * len(hr)) + ")"
            params += list(hr)
        return expr, params

    def where(self, state=None, district=None, sector=None, industrial=None, bbox=None,
              ids=None, display=None, q=None) -> tuple[str, list]:
        clauses, params = [], []
        disp = self.display_sql(display)
        if disp is not None:
            clauses.append(f"id IN (SELECT id FROM t_props WHERE {disp[0]})"); params += disp[1]
        if q and q.strip():
            clauses.append("lower(name) LIKE '%' || ? || '%'"); params.append(q.strip().lower())
        if state:
            clauses.append("state = ?"); params.append(state)
        if district:
            col = "district_ags" if district.isdigit() else "district"
            clauses.append(f"{col} = ?"); params.append(district)
        if sector:
            clauses.append(f'"{self.sector_col}" = ?'); params.append(sector)
        if industrial is not None:
            clauses.append("coalesce(is_industrial, false) = ?"); params.append(bool(industrial))
        if bbox:
            clauses.append("longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?")
            params += [bbox[0], bbox[2], bbox[1], bbox[3]]
        if ids is not None:
            clauses.append("list_contains(?, id)"); params.append(list(ids))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def count(self, where: str, params: list) -> int:
        with self.lock:
            return self.con.execute(f"SELECT count(*) FROM t_flat{where}", params).fetchone()[0]

    def select_list(self, tier: str, fields: list[str] | None) -> str:
        if not fields:
            return "*"
        bad = [f for f in fields if f not in self.cols[tier]]
        if bad:
            raise HTTPException(400, f"unknown fields {bad}")
        return ", ".join(f'"{f}"' for f in fields)

    def rows(self, tier: str, where: str, params: list, limit: int | None, offset: int,
             fields: list[str] | None = None):
        sql = f"SELECT {self.select_list(tier, fields)} FROM t_{tier}{where} ORDER BY state NULLS LAST, district NULLS LAST, id"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        with self.lock:
            cur = self.con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            data = cur.fetchall()
        return cols, data

    def export(self, tier: str, fmt: str, where: str, params: list, limit: int | None,
               offset: int, fields: list[str] | None = None) -> Path:
        """COPY the selection to a temp file (csv / parquet); nested columns → JSON in csv."""
        cols = fields or self.cols[tier]
        self.select_list(tier, fields)   # validates
        if fmt == "csv":
            sel = ", ".join(f'to_json("{c}") AS "{c}"' if c in self.nested else f'"{c}"' for c in cols)
        else:
            sel = "*"
        sql = f"SELECT {sel} FROM t_{tier}{where} ORDER BY state NULLS LAST, district NULLS LAST, id"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        fd, tmp = tempfile.mkstemp(suffix=f".{fmt}", prefix="geoextract_")
        os.close(fd)
        opts = "FORMAT CSV, HEADER" if fmt == "csv" else "FORMAT PARQUET, COMPRESSION ZSTD"
        with self.lock:
            self.con.execute(f"COPY ({sql}) TO '{tmp}' ({opts})", params)
        return Path(tmp)

    def company(self, cid: str) -> dict | None:
        with self.lock:
            key = self.con.execute("SELECT state, district FROM t_ids WHERE id = ?", [cid]).fetchone()
            if key is None:
                return None
            st, di = key
            where = ("state = ?" if st is not None else "state IS NULL") + " AND " + \
                    ("district = ?" if di is not None else "district IS NULL") + " AND id = ?"
            params = [v for v in (st, di) if v is not None] + [cid]
            row = self.con.execute(f"SELECT * FROM t_full WHERE {where}", params).fetchone()
        return None if row is None else {c: _jsonable(v) for c, v in zip(self.cols["full"], row)}

    def search(self, q: str, limit: int, display: dict | None = None) -> list[dict]:
        key = normalize_name(q, strip_noise=True)
        raw = q.strip().lower()
        disp = self.display_sql(display)
        extra, extra_params = "", []
        if disp is not None:
            extra, extra_params = f" AND id IN (SELECT id FROM t_props WHERE {disp[0]})", disp[1]
        tbl = [c for c in ("business_type", "nace_section", "address_full", "grounds_area_m2", "source", "website")
               if c in self.cols["flat"]]
        with self.lock:
            rows = self.con.execute(
                "SELECT s.id, s.name, s.latitude, s.longitude, s.state, s.district"
                + "".join(f', f."{c}"' for c in tbl) +
                " FROM t_search s LEFT JOIN t_flat f ON f.id = s.id "
                "WHERE ((? <> '' AND s.name_key LIKE '%' || ? || '%') OR lower(s.name) LIKE '%' || ? || '%')"
                + extra.replace("id IN", "s.id IN") +
                " ORDER BY (lower(s.name) LIKE ? || '%') DESC, length(s.name) LIMIT ?",
                [key, key, raw] + extra_params + [raw, limit]).fetchall()
        keys = ["id", "name", "latitude", "longitude", "state", "district"] + tbl
        return [{k: _jsonable(v) for k, v in zip(keys, r)} for r in rows]

    def summary(self) -> dict:
        with self.lock:
            by_state = self.con.execute(
                "SELECT state, count(*) FROM t_flat WHERE state IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
            by_sector = self.con.execute(
                f'SELECT "{self.sector_col}", count(*) FROM t_flat GROUP BY 1 ORDER BY 2 DESC').fetchall()
            totals = self.con.execute(
                "SELECT count(*), sum(CASE WHEN coalesce(is_industrial,false) THEN 1 ELSE 0 END), "
                "sum(CASE WHEN source_count >= 2 THEN 1 ELSE 0 END), "
                "sum(CASE WHEN website IS NOT NULL THEN 1 ELSE 0 END) FROM t_flat").fetchone()
        return {"version": self.manifest["version"], "companies": totals[0],
                "is_industrial": int(totals[1] or 0), "multi_source": int(totals[2] or 0),
                "with_website": int(totals[3] or 0),
                "by_state": {k: v for k, v in by_state},
                "by_sector": {str(k): v for k, v in by_sector}, "sector_column": self.sector_col}

    def ids_in_polygon(self, geom, where: str, params: list) -> list[str]:
        """bbox prefilter in SQL, then shapely contains_xy on the candidates."""
        import numpy as np
        from shapely import contains_xy

        minx, miny, maxx, maxy = geom.bounds
        w = (where + " AND " if where else " WHERE ") + \
            "longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?"
        with self.lock:
            cand = self.con.execute(
                f"SELECT id, longitude, latitude FROM t_flat{w}", params + [minx, maxx, miny, maxy]).fetchall()
        if not cand:
            return []
        ids = np.array([c[0] for c in cand], dtype=object)
        xs = np.array([c[1] for c in cand], dtype=float)
        ys = np.array([c[2] for c in cand], dtype=float)
        inside = contains_xy(geom, xs, ys)
        return ids[inside].tolist()


def _parse_bbox(text: str | None) -> list[float] | None:
    if not text:
        return None
    try:
        parts = [float(x) for x in text.split(",")]
    except ValueError as err:
        raise HTTPException(400, "bbox must be minx,miny,maxx,maxy") from err
    if len(parts) != 4 or parts[0] > parts[2] or parts[1] > parts[3]:
        raise HTTPException(400, "bbox must be minx,miny,maxx,maxy")
    return parts


def _geojson(cols: list[str], data: list[tuple]) -> dict:
    lon, lat = cols.index("longitude"), cols.index("latitude")
    feats = []
    for row in data:
        if row[lon] is None or row[lat] is None:
            continue
        props = {c: _jsonable(v) for c, v in zip(cols, row) if c not in ("longitude", "latitude") and v is not None}
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [row[lon], row[lat]]},
                      "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def create_app(version_dir: Path) -> FastAPI:
    store = Store(version_dir)
    app = FastAPI(title="geoextract", version=store.manifest["version"],
                  description="Companies at geolocations in Germany — merged open data. "
                              + store.manifest["licence"]["note"])
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
    app.state.store = store

    @app.middleware("http")
    async def timing(request: Request, call_next):
        t0 = time.perf_counter()
        resp = await call_next(request)
        resp.headers["X-Query-Ms"] = f"{1000 * (time.perf_counter() - t0):.1f}"
        path = request.url.path
        if path == "/" or path.startswith(("/static/", "/v1/")):
            # page, scripts and API answers must never be served stale from the browser cache
            # (a new index.html with a cached old app.js breaks the map silently)
            resp.headers["Cache-Control"] = "no-cache"
        elif path.startswith("/data/"):
            resp.headers["Cache-Control"] = "public, max-age=86400"   # tiles / extracts: immutable per version
        return resp

    @app.get("/v1/manifest")
    def manifest():
        return store.manifest

    @app.get("/v1/ui")
    def ui():
        return store.ui

    @app.get("/v1/summary")
    def summary():
        return store.summary()

    @app.get("/v1/extracts")
    def extracts(request: Request):
        base = str(request.base_url).rstrip("/") + "/data/"
        out = []
        for f, meta in store.manifest["files"].items():
            if not f.startswith("extracts/"):
                continue
            _, area, name = f.split("/", 2)
            stem, tier = name.replace(".parquet", "").rsplit("_", 1)
            out.append({"area": area, "sector": stem, "tier": tier, "url": base + f,
                        "bytes": meta["bytes"], "sha256": meta["sha256"]})
        return out

    def _display(text: str | None) -> dict | None:
        if not text:
            return None
        try:
            d = json.loads(text)
        except json.JSONDecodeError as err:
            raise HTTPException(400, "display must be JSON") from err
        if not isinstance(d, dict):
            raise HTTPException(400, "display must be a JSON object")
        return d

    def _fields(text: str | None) -> list[str] | None:
        return [f.strip() for f in text.split(",") if f.strip()] if text else None

    def _selection(request: Request, state, district, sector, industrial, bbox, tier, fmt,
                   limit, offset, count_only, ids=None, display=None, q=None, fields=None):
        if fmt not in FORMATS:
            raise HTTPException(400, f"format must be one of {FORMATS}")
        if tier not in ("flat", "full"):
            raise HTTPException(400, "tier must be flat or full")
        where, params = store.where(state, district, sector, industrial, bbox, ids, display, q)
        if count_only:
            return {"count": store.count(where, params)}
        if fmt in ("json", "geojson"):
            lim = min(limit or DEFAULT_JSON, MAX_JSON)
            if fmt == "geojson":
                cols, data = store.rows("flat", where, params, lim, offset)
                return JSONResponse(_geojson(cols, data), media_type="application/geo+json")
            cols, data = store.rows(tier, where, params, lim, offset, fields)
            return JSONResponse([{c: _jsonable(v) for c, v in zip(cols, row)} for row in data])
        tmp = store.export(tier, fmt, where, params, limit, offset, fields)
        stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d-%H%M%S")
        fname = f"geoextract_{store.manifest['scope']}_{tier}_{stamp}.{fmt}"
        media = "text/csv" if fmt == "csv" else "application/vnd.apache.parquet"
        return FileResponse(tmp, media_type=media, filename=fname,
                            background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))

    @app.get("/v1/companies")
    def companies(request: Request, state: str | None = None, district: str | None = None,
                  sector: str | None = None, industrial: bool | None = None,
                  bbox: str | None = None, tier: str = "flat", format: str = "json",
                  limit: int | None = Query(None, ge=1), offset: int = Query(0, ge=0),
                  count_only: bool = False, display: str | None = None, q: str | None = None,
                  fields: str | None = None):
        return _selection(request, state, district, sector, industrial, _parse_bbox(bbox), tier,
                          format, limit, offset, count_only, display=_display(display), q=q,
                          fields=_fields(fields))

    @app.post("/v1/companies/query")
    async def companies_query(request: Request, state: str | None = None, district: str | None = None,
                              sector: str | None = None, industrial: bool | None = None,
                              tier: str = "flat", format: str = "json",
                              limit: int | None = Query(None, ge=1), offset: int = Query(0, ge=0),
                              count_only: bool = False, display: str | None = None,
                              fields: str | None = None):
        from shapely.geometry import shape

        body = await request.json()
        geom_json = body.get("geometry", body) if isinstance(body, dict) else None
        if not geom_json or geom_json.get("type") not in ("Polygon", "MultiPolygon"):
            raise HTTPException(400, "body must be a GeoJSON Polygon / MultiPolygon or a Feature with one")
        geom = shape(geom_json)
        disp = _display(display) or (body.get("display") if isinstance(body, dict) else None)
        where, params = store.where(state, district, sector, industrial, display=disp)
        ids = store.ids_in_polygon(geom, where, params)
        return _selection(request, state, district, sector, industrial, None, tier, format, limit,
                          offset, count_only, ids=ids, display=disp, fields=_fields(fields))

    @app.get("/v1/companies/{cid:path}")
    def company(cid: str):
        rec = store.company(cid)
        if rec is None:
            raise HTTPException(404, "not found")
        return rec

    @app.get("/v1/search")
    def search(q: str = "", limit: int = Query(50, ge=1, le=500), display: str | None = None):
        return store.search(q, limit, _display(display)) if len(q.strip()) >= 2 else []

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return Response("ok", media_type="text/plain")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    app.mount("/data", StaticFiles(directory=version_dir), name="data")
    return app


def main(data_root: Path, scope: str, version: str | None, port: int, host: str = "127.0.0.1") -> int:
    import uvicorn

    version_dir = (data_root / "serve" / scope / version) if version else latest_version(data_root, scope)
    app = create_app(version_dir)
    print(f"[serve] api on http://{host}:{port}/ (docs at /docs) — {version_dir}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
