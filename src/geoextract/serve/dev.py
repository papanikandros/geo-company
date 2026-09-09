"""S2 dev server — the map page + the two JSON endpoints it needs, standard library only.

``geoextract serve dev --scope bremen`` serves:

* ``/`` and ``/static/*``          the map page (``serve/web/``)
* ``/data/*``                     the serve version directory (PMTiles needs HTTP Range
                                  requests → implemented here; ``SimpleHTTPRequestHandler``
                                  has none)
* ``/v1/manifest``, ``/v1/ui``    the build's JSON files
* ``/v1/companies/{id}``          one company: every public field + the nested raw source
                                  records (from ``companies_full.parquet`` via DuckDB)
* ``/v1/search?q=``               name search over ``search.parquet`` (≤ 50 hits)

The production API (S3, FastAPI) replaces the ``/v1`` part with the full endpoint set; this
server exists so S2 can be verified locally without extra dependencies.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import mimetypes
import re
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import duckdb

from ..resolve import normalize_name

WEB_DIR = Path(__file__).parent / "web"
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def latest_version(data_root: Path, scope: str) -> Path:
    """The `current` symlink if the build set one, else the newest version directory."""
    base = data_root / "serve" / scope
    current = base / "current"
    if current.is_symlink() and (current / "manifest.json").exists():
        return current.resolve()
    versions = sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink()
                      and (p / "manifest.json").exists()) if base.exists() else []
    if not versions:
        raise FileNotFoundError(f"no serve build under {base} — run `geoextract serve build` first")
    return versions[-1]


def _jsonable(v):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "item"):   # numpy scalars
        try:
            return _jsonable(v.item())
        except (ValueError, TypeError):
            return str(v)
    return v


class Store:
    """DuckDB access to one serve version, guarded by a lock (the server is threaded)."""

    def __init__(self, version_dir: Path):
        self.dir = version_dir
        self.con = duckdb.connect()
        self.lock = threading.Lock()
        full = version_dir / "companies_full.parquet"
        self.table = full if full.exists() else version_dir / "companies_flat.parquet"
        self.con.execute(f"CREATE VIEW companies AS SELECT * FROM read_parquet('{self.table}')")
        self.con.execute(f"CREATE VIEW search AS SELECT * FROM read_parquet('{version_dir / 'search.parquet'}')")
        self.cols = [r[0] for r in self.con.execute("DESCRIBE companies").fetchall()]

    def company(self, cid: str) -> dict | None:
        with self.lock:
            row = self.con.execute("SELECT * FROM companies WHERE id = ?", [cid]).fetchone()
        if row is None:
            return None
        return {c: _jsonable(v) for c, v in zip(self.cols, row)}

    def search(self, q: str, limit: int = 50) -> list[dict]:
        key = normalize_name(q, strip_noise=True)
        raw = q.strip().lower()
        with self.lock:
            rows = self.con.execute(
                "SELECT id, name, latitude, longitude, state, district FROM search "
                "WHERE (? <> '' AND name_key LIKE '%' || ? || '%') OR lower(name) LIKE '%' || ? || '%' "
                "ORDER BY (lower(name) LIKE ? || '%') DESC, length(name) LIMIT ?",
                [key, key, raw, raw, limit]).fetchall()
        return [{"id": r[0], "name": r[1], "latitude": r[2], "longitude": r[3],
                 "state": r[4], "district": r[5]} for r in rows]


def make_handler(store: Store):
    version_dir = store.dir

    class Handler(SimpleHTTPRequestHandler):
        server_version = "geoextract-dev/0.1"

        def log_message(self, fmt, *args):   # quieter than the default
            pass

        def _send_json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path):
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            size = path.stat().st_size
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if path.suffix == ".pmtiles":
                ctype = "application/octet-stream"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            status = HTTPStatus.OK
            if rng:
                m = _RANGE_RE.match(rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    elif m.group(2):
                        start = max(0, size - int(m.group(2)))
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            p = unquote(url.path)
            if p in ("/", "/index.html"):
                return self._send_file(WEB_DIR / "index.html")
            if p.startswith("/static/"):
                target = (WEB_DIR / p[len("/static/"):]).resolve()
                if WEB_DIR.resolve() not in target.parents:
                    return self.send_error(HTTPStatus.FORBIDDEN)
                return self._send_file(target)
            if p.startswith("/data/"):
                target = (version_dir / p[len("/data/"):]).resolve()
                if version_dir.resolve() not in target.parents:
                    return self.send_error(HTTPStatus.FORBIDDEN)
                return self._send_file(target)
            if p == "/v1/manifest":
                return self._send_file(version_dir / "manifest.json")
            if p == "/v1/ui":
                return self._send_file(version_dir / "ui.json")
            if p.startswith("/v1/companies/"):
                rec = store.company(p[len("/v1/companies/"):])
                if rec is None:
                    return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return self._send_json(rec)
            if p == "/v1/search":
                q = parse_qs(url.query).get("q", [""])[0]
                return self._send_json(store.search(q) if len(q.strip()) >= 2 else [])
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def serve(data_root: Path, scope: str, version: str | None = None, port: int = 8765,
          host: str = "127.0.0.1") -> ThreadingHTTPServer:
    version_dir = (data_root / "serve" / scope / version) if version else latest_version(data_root, scope)
    store = Store(version_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    httpd.version_dir = version_dir   # type: ignore[attr-defined]
    return httpd


def main(data_root: Path, scope: str, version: str | None, port: int) -> int:
    httpd = serve(data_root, scope, version, port)
    print(f"[serve] dev server on http://127.0.0.1:{httpd.server_address[1]}/ "
          f"({httpd.version_dir})")   # type: ignore[attr-defined]
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
