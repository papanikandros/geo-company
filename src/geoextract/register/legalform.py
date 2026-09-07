"""Legal-form label from a company name (register division follows from it).

Canonical labels match the contract's ``legal_form`` examples ("GmbH", "AG",
"GmbH & Co. KG", "e.K.", …). Detection works on the normalised token stream (lowercase,
umlauts, dotted abbreviations collapsed) so "G.m.b.H.", "GmbH ＆ Co.KG" and "gGmbH" are all
recognised. ``register_division`` maps a label to the register that holds it:
HRB (capital companies), HRA (merchants and partnerships), GnR, PR, VR, GsR, or None.
"""
from __future__ import annotations

import math
import re

from .. import config

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_ABBREV_DOT_RE = re.compile(r"\b([a-z])\.\s?(?=[a-z]\b|[a-z]\.)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")

# (label, division) in evaluation order — longer / more specific forms first.
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b(gmbh|ggmbh|ug|ag|se) (und|u|and)? ?co ?(kg|kgaa|ohg)\b"), "GmbH & Co. KG", "HRA"),
    (re.compile(r"\bgmbh co ?kg\b"), "GmbH & Co. KG", "HRA"),
    (re.compile(r"\bcokg\b"), "GmbH & Co. KG", "HRA"),
    (re.compile(r"\bkgaa\b"), "KGaA", "HRB"),
    (re.compile(r"\bggmbh\b"), "gGmbH", "HRB"),
    (re.compile(r"\bgmbh\b|\bgesmbh\b|\bmbh\b"), "GmbH", "HRB"),
    (re.compile(r"\bug\b|haftungsbeschraenkt"), "UG (haftungsbeschränkt)", "HRB"),
    (re.compile(r"\bag\b"), "AG", "HRB"),
    (re.compile(r"\bse\b"), "SE", "HRB"),
    (re.compile(r"\bvvag\b"), "VVaG", "HRB"),
    (re.compile(r"\bohg\b"), "OHG", "HRA"),
    (re.compile(r"\bkg\b"), "KG", "HRA"),
    (re.compile(r"\bek\b|\bekfm\b|\bekfr\b|eingetragener? kauf(mann|frau)"), "e.K.", "HRA"),
    (re.compile(r"\bewiv\b"), "EWIV", "HRA"),
    (re.compile(r"\beg\b|genossenschaft"), "eG", "GnR"),
    (re.compile(r"\bpartg\b|\bpartgmbb\b|partnerschaftsgesellschaft"), "PartG", "PR"),
    (re.compile(r"\bev\b|eingetragener verein"), "e.V.", "VR"),
    (re.compile(r"\begbr\b"), "eGbR", "GsR"),
    (re.compile(r"\bgbr\b"), "GbR", ""),
    (re.compile(r"\bstiftung\b"), "Stiftung", ""),
    (re.compile(r"\bkdoer\b|koerperschaft des oeffentlichen rechts|\bador\b|anstalt des oeffentlichen rechts|zweckverband|eigenbetrieb|\bstadtwerke\b"),
     "Körperschaft/Anstalt öR", ""),
    (re.compile(r"\bltd\b|\blimited\b|\binc\b|\bllc\b|\bplc\b|\bsarl\b|\bsa\b|\bbv\b|\bnv\b|\bsro\b"),
     "foreign", ""),
]

_DIVISION = {p[1]: p[2] for p in _PATTERNS}


def _tokens(name: object) -> str:
    if name is None or (isinstance(name, float) and math.isnan(name)):
        return ""
    text = str(name).lower().translate(_UMLAUTS).replace("＆", "&").replace("&", " und ")
    text = text.replace("kg.", "kg ")
    for _ in range(3):
        text = _ABBREV_DOT_RE.sub(r"\1", text)
    text = re.sub(r"\bco\.\s*", "co ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def legal_form_from_name(name: object) -> str | None:
    """Canonical legal-form label found in ``name``, or None."""
    toks = _tokens(name)
    if not toks:
        return None
    for pattern, label, _division in _PATTERNS:
        if pattern.search(toks):
            return label
    return None


def register_division(legal_form: str | None) -> str | None:
    """Register division (HRA/HRB/GnR/PR/VR/GsR) a legal form is registered in; None if
    unregistered (GbR, Stiftung, public bodies, foreign) or unknown."""
    if not legal_form:
        return None
    return _DIVISION.get(legal_form) or None


__all__ = ["legal_form_from_name", "register_division"]
_ = config  # config imported for symmetry with sibling modules (thresholds live there)
