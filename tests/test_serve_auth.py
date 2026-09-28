"""The single-password gate (serve/auth.py): off without a password; with one, every
route needs the session cookie or the bearer token; /auth answers Caddy's forward_auth
for the static data; failures are throttled per client address."""
import pytest
from fastapi.testclient import TestClient
from test_serve_build import data_root  # noqa: F401  (fixture reuse)

from geoextract.serve import api, auth, build


@pytest.fixture
def app_dir(data_root):  # noqa: F811
    return build.build(data_root, "bremen", tiles=False)


@pytest.fixture
def gated(app_dir, monkeypatch):
    monkeypatch.setenv("GEOEXTRACT_WEB_PASSWORD", "open-sesame")
    monkeypatch.setenv("GEOEXTRACT_WEB_SECRET", "x" * 32)
    auth._attempts.clear(); auth._locked.clear()
    return TestClient(api.create_app(app_dir), raise_server_exceptions=True)


def test_gate_off_without_password(app_dir, monkeypatch):
    monkeypatch.delenv("GEOEXTRACT_WEB_PASSWORD", raising=False)
    c = TestClient(api.create_app(app_dir))
    assert c.get("/v1/summary").status_code == 200
    assert c.get("/auth").status_code == 204
    assert c.get("/login", follow_redirects=False).status_code == 303   # nothing to log into


def test_gate_blocks_everything_but_login_and_health(gated):
    assert gated.get("/healthz").status_code == 200
    assert gated.get("/login").status_code == 200
    assert gated.get("/v1/summary").status_code == 401
    assert gated.get("/auth").status_code == 401                        # Caddy then refuses /data/*
    r = gated.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bearer_token_opens_api_and_data(gated):
    hdr = {"Authorization": "Bearer open-sesame"}
    assert gated.get("/v1/summary", headers=hdr).status_code == 200
    assert gated.get("/auth", headers=hdr).status_code == 204
    assert gated.get("/auth", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_login_cookie_opens_api_and_data(gated):
    r = gated.post("/login", data={"password": "open-sesame"}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE_NAME in r.cookies
    assert gated.get("/v1/summary").status_code == 200                  # the client keeps the cookie
    assert gated.get("/auth").status_code == 204
    assert gated.post("/login", data={"password": "nope"}, follow_redirects=False).status_code == 401


def test_throttle_keys_on_the_proxy_appended_address(gated):
    # five failures lock the address; a client-written first value must not evade it
    for i in range(5):
        r = gated.get("/auth", headers={"Authorization": "Bearer wrong",
                                         "X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"})
        assert r.status_code == 401
    r = gated.get("/auth", headers={"Authorization": "Bearer open-sesame",
                                     "X-Forwarded-For": "10.0.0.99, 203.0.113.9"})
    assert r.status_code == 429 and "Retry-After" in r.headers
    # another real address is unaffected
    assert gated.get("/auth", headers={"Authorization": "Bearer open-sesame",
                                        "X-Forwarded-For": "198.51.100.1"}).status_code == 204
    assert auth.client_ip({"x-forwarded-for": "1.1.1.1, 2.2.2.2"}, "-") == "2.2.2.2"
