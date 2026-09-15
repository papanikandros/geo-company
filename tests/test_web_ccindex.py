"""Item 6 stage 3c — candidate labels from a sorted .de domain list (no network)."""
import numpy as np

from geoextract.web import ccindex


def test_candidates_from_domain_index():
    labels = np.array(sorted({"schwarzwaldmilch", "schwarzwald-milch", "schwarzwaldmilch-shop", "schwarzwaldmilch24",
                              "schwarzwaldmilch-lohnherstellung", "schwarzwaldverein", "schwarzwald", "unbescheiden",
                              "unbescheiden-erdarbeiten", "dold-holzwerke", "doldholzwerke-gmbh", "kiosk"}))
    idx = ccindex.DomainIndex(labels)
    c = ccindex.candidates("Schwarzwaldmilch GmbH", "GmbH", idx)
    assert c == ["schwarzwaldmilch"]                              # one distinctive token: exact label only
    c1 = ccindex.candidates("Schwarzwaldmilch Lohnherstellung", None, idx)
    assert c1[0] == "schwarzwaldmilch-lohnherstellung" and "schwarzwaldverein" not in c1 and "schwarzwald" not in c1
    c2 = ccindex.candidates("Dold Holzwerke GmbH & Co. KG", "GmbH & Co. KG", idx)
    assert c2 == ["dold-holzwerke", "doldholzwerke-gmbh"]
    assert ccindex.candidates("Kiosk", None, idx) == []          # too short / generic
    assert ccindex.candidates(None, None, idx) == []


def test_accept_rules_and_generic_names():
    labels = np.array(sorted({"logistik", "technik", "hansa-flex", "solarwerg"}))
    idx = ccindex.DomainIndex(labels)
    assert ccindex.candidates("TX Logistik", None, idx) == []                    # generic only
    assert ccindex.candidates("A&M Service & Technik GmbH", "GmbH", idx) == []
    assert ccindex.candidates("Hansa-Flex", None, idx) == ["hansa-flex"]
    rec = {"status": "ok", "imp_legal_name": "Karacalar GmbH"}
    assert ccindex.accept(rec, "name+plz", "Die Rümpel Profis", "ruempelprofis")
    assert not ccindex.accept(rec, "name", "Die Rümpel Profis", "ruempelprofis")   # legal name disagrees
    assert ccindex.accept({"status": "ok", "imp_legal_name": "SolarwerG GmbH"}, "name", "SolarwerG GmbH", "solarwerg")
    assert not ccindex.accept({"status": "ok", "imp_legal_name": "Gröpelingen Marketing e.V."}, "name", "Gröpelingen", "groepelingen")
    assert not ccindex.accept({"status": "ok", "imp_legal_name": "Hapag-Lloyd Reisebüro Südwest Presse GmbH"}, "name", "Hapag-Lloyd", "hapag-lloyd-reisen")
    assert ccindex.accept({"status": "ok", "imp_legal_name": None}, "name", "ThyssenKrupp Schulte", "thyssenkrupp-schulte")   # label is the key
    assert not ccindex.accept({"status": "ok", "imp_legal_name": None}, "name", "Blumenthal", "blumenthal")   # one token, no legal name
