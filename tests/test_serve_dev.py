"""Item 7 S2 — dev server routes on a synthetic serve build (no tiles, no browser)."""
import json
import threading
import urllib.request

import pytest
from test_serve_build import data_root  # noqa: F401  (fixture reuse)

from geoextract.serve import build, dev


@pytest.fixture
def server(data_root):  # noqa: F811
    build.build(data_root, "bremen", tiles=False)
    httpd = dev.serve(data_root, "bremen", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req) as r:
        return r.status, dict(r.headers), r.read()


def test_routes(server):
    status, _h, body = _get(server + "/")
    assert status == 200 and b"maplibre-gl" in body and b"static/app.js" in body
    status, h, _b = _get(server + "/static/app.js")
    assert status == 200 and "javascript" in h["Content-Type"]
    status, _h, body = _get(server + "/v1/manifest")
    assert status == 200 and json.loads(body)["companies"] == 3
    status, _h, body = _get(server + "/v1/ui")
    assert json.loads(body)["datasets"]["osm"]["n"] == 2
    status, _h, body = _get(server + "/v1/companies/osm_way%2F1")
    rec = json.loads(body)
    assert status == 200 and rec["name"] == "Weser Stahl GmbH" and "email" not in rec
    assert len(rec["mastr"]) == 2 and rec["abwaerme"][0]["abw_heat_mwh_a"] == 1234.0
    status, _h, body = _get(server + "/v1/search?q=weser")
    hits = json.loads(body)
    assert [h["id"] for h in hits] == ["osm_way/1"] and hits[0]["latitude"] > 50
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(server + "/v1/companies/nope")
    assert err.value.code == 404


def test_range_requests(server):
    status, h, body = _get(server + "/data/companies_flat.parquet")
    size = len(body)
    assert status == 200 and h["Accept-Ranges"] == "bytes" and int(h["Content-Length"]) == size
    status, h, part = _get(server + "/data/companies_flat.parquet", {"Range": "bytes=0-3"})
    assert status == 206 and part == body[:4] and h["Content-Range"] == f"bytes 0-3/{size}"
    status, h, tail = _get(server + "/data/companies_flat.parquet", {"Range": "bytes=-10"})
    assert status == 206 and tail == body[-10:]
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(server + "/data/../manifest.json")
    assert err.value.code in (403, 404)


def test_current_symlink(data_root):  # noqa: F811
    out = build.build(data_root, "bremen", tiles=False)
    current = out.parent / "current"
    assert current.is_symlink() and current.resolve() == out.resolve()
    assert dev.latest_version(data_root, "bremen") == out.resolve()
