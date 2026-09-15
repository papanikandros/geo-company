"""Item 6 stage 3b — domain candidates and page verification (mocked session)."""
from geoextract.web import discover


def test_candidates_and_tokens():
    assert discover.name_tokens("Schwarzwaldmilch GmbH") == ["schwarzwaldmilch"]
    assert discover.candidates("Schwarzwaldmilch GmbH") == ["schwarzwaldmilch.de", "schwarzwaldmilch.com", "schwarzwaldmilch-gmbh.de"]
    assert discover.candidates("Dold Holzwerke GmbH & Co. KG")[:2] == ["doldholzwerke.de", "dold-holzwerke.de"]
    assert discover.candidates("Kiosk") == []                  # too short / generic
    assert discover.candidates("Praxis") == []
    assert discover.candidates(None) == []


class _FakeResp:
    def __init__(self, status, text, url):
        self.status_code, self._text, self.url, self.encoding = status, text, url, "utf-8"
        self.text = text
    def iter_content(self, n):
        yield self._text.encode("utf-8")


class _FakeSession:
    def __init__(self, pages):
        self.pages = pages
    def get(self, url, timeout=None, stream=False, allow_redirects=True):
        host = url.split("/")[2]
        if url.endswith("robots.txt"):
            return _FakeResp(404, "", url)
        page = self.pages.get(host)
        if page is None:
            raise discover.requests.ConnectionError(url)
        return _FakeResp(200, page["html"], page.get("final", url))


def test_verify_rules():
    s = _FakeSession({
        "weserstahl.de": {"html": "<html><title>Weser Stahl GmbH</title><body>Stahlbau in 28237 Bremen</body></html>"},
        "hansadruck.de": {"html": "<html><body>Hansa Druck – Druckerei</body></html>"},
        "parked.de": {"html": "<html><body>Diese Domain ist zu verkaufen</body></html>"},
        "moved.de": {"html": "<html><body>Weser Stahl</body></html>", "final": "https://other-company.de/"},
    })
    assert discover.verify(s, "weserstahl.de", "Weser Stahl GmbH", "28237", "Bremen")["verified"] == "name+plz"
    assert discover.verify(s, "hansadruck.de", "Hansa Druck GmbH", "28195", "Bremen")["verified"] == "name"
    assert discover.verify(s, "parked.de", "Weser Stahl GmbH", "28237", "Bremen")["verified"] == "mismatch"
    assert discover.verify(s, "moved.de", "Weser Stahl GmbH", "28237", "Bremen")["verified"] == "redirect_offdomain"
    assert discover.verify(s, "nothere.de", "Weser Stahl GmbH", "28237", "Bremen")["verified"] == "dead"
