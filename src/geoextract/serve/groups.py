"""Display groups shared by the serve build (tile properties, ui.json) and the map page.

Pure 1:1 reads of native source concepts — no classification (mirrors the canonical
preview `scripts/preview_layer.py`, user decisions 2026-08-27 / 2026-09-03):
IED Annex-I activity → chapter group, Abwärme heat quantity → band, MaStR → its
technologies, Overture → business type, OSM → business type category.
"""
from __future__ import annotations

import math
import re

PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
           "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
DS_LABELS = {"osm": "OSM", "ied": "IED", "abwaerme": "PfA", "overture": "OVT", "mastr": "MaStR"}
SRC_SHORT = {"osm": "OSM", "ied": "IED", "abwaerme": "PfA", "overture": "OVT", "mastr": "MaStR"}
MATCH_COLOURS = {"matched": "#0d8a72", "only": "#a31515"}
LU_COLOURS = {"industrial": "#8fa3b8", "commercial": "#c4b454", "retail": "#d09ec2"}
GRP_ORDER = ["energy", "metals", "minerals", "chemicals", "waste", "livestock", "other",
             "< 1 GWh/a", "1–10 GWh/a", "10–100 GWh/a", "> 100 GWh/a",
             "solar", "wind", "biomass", "hydro", "combustion", "geothermal", "nuclear",
             "storage", "gas_production", "gas_storage", "gas_consumption",
             "electricity_consumption"]
UNIT_OF = {"Bruttoleistung": "kW", "Nettonennleistung": "kW", "Erzeugungsleistung": "kW",
           "MaximaleGasbezugsleistung": "kW", "LeistungsaufnahmeBeimEinspeichern": "kW",
           "NutzbareSpeicherkapazitaet": "kWh", "MaximaleEinspeicherleistung": "kW",
           "MaximaleAusspeicherleistung": "kW", "MaximalNutzbaresArbeitsgasvolumen": "kWh"}

IED_ACTIVITY_LABELS = {
    "1.1": "Verfeuerung von Brennstoffen (≥ 50 MW)", "1.2": "Mineralöl- und Gasraffinerien",
    "1.3": "Kokereien", "1.4": "Vergasung/Verflüssigung von Brennstoffen",
    "2.1": "Rösten/Sintern von Metallerz", "2.2": "Roheisen- oder Stahlerzeugung",
    "2.3": "Verarbeitung von Eisenmetallen", "2.3(a)": "Warmwalzen von Eisenmetallen",
    "2.3(b)": "Schmieden mit Hämmern", "2.3(c)": "Schmelztauchbeschichten von Eisenmetallen",
    "2.4": "Eisenmetallgießereien", "2.5(a)": "Gewinnung von Nichteisen-Rohmetallen",
    "2.5(b)": "Schmelzen von Nichteisenmetallen",
    "2.6": "Oberflächenbehandlung von Metallen/Kunststoffen (elektrolytisch/chemisch)",
    "3.1(a)": "Zementklinkerherstellung", "3.1(b)": "Kalkherstellung",
    "3.1(c)": "Magnesiumoxidherstellung", "3.2": "Asbestverarbeitung", "3.3": "Glasherstellung",
    "3.4": "Schmelzen mineralischer Stoffe / Mineralfasern",
    "3.5": "Keramische Erzeugnisse (Ziegel, Fliesen, …)",
    "4.1": "Herstellung organischer Grundchemikalien", "4.1(a)": "Einfache Kohlenwasserstoffe",
    "4.1(b)": "Sauerstoffhaltige Kohlenwasserstoffe (Alkohole, Aldehyde, …)",
    "4.1(c)": "Schwefelhaltige Kohlenwasserstoffe",
    "4.1(d)": "Stickstoffhaltige Kohlenwasserstoffe (Amine, …)",
    "4.1(e)": "Phosphorhaltige Kohlenwasserstoffe", "4.1(f)": "Halogenhaltige Kohlenwasserstoffe",
    "4.1(g)": "Metallorganische Verbindungen", "4.1(h)": "Kunststoffe (Polymere, Chemiefasern)",
    "4.1(i)": "Synthetische Kautschuke", "4.1(j)": "Farbstoffe und Pigmente", "4.1(k)": "Tenside",
    "4.2": "Herstellung anorganischer Grundchemikalien", "4.2(a)": "Anorganische Gase",
    "4.2(b)": "Säuren", "4.2(c)": "Basen", "4.2(d)": "Salze", "4.2(e)": "Nichtmetalle, Metalloxide",
    "4.3": "Düngemittelherstellung", "4.4": "Pflanzenschutzmittel/Biozide",
    "4.5": "Arzneimittelherstellung", "4.6": "Explosivstoffherstellung",
    "5.1": "Beseitigung/Verwertung gefährlicher Abfälle", "5.2": "Abfallverbrennung",
    "5.2(a)": "Verbrennung nicht gefährlicher Abfälle", "5.2(b)": "Verbrennung gefährlicher Abfälle",
    "5.3(a)": "Beseitigung nicht gefährlicher Abfälle", "5.3(b)": "Verwertung nicht gefährlicher Abfälle",
    "5.4": "Deponien", "5.5": "Zeitweilige Lagerung gefährlicher Abfälle",
    "5.6": "Unterirdische Lagerung gefährlicher Abfälle",
    "6.1(a)": "Zellstoffherstellung", "6.1(b)": "Papier- und Pappeherstellung",
    "6.1(c)": "Holzwerkstoffplattenherstellung", "6.2": "Vorbehandlung/Färben von Textilfasern",
    "6.3": "Gerben von Häuten und Fellen", "6.4(a)": "Schlachthöfe", "6.4(b)": "Lebensmittelherstellung",
    "6.4(b)(i)": "Lebensmittel aus tierischen Rohstoffen",
    "6.4(b)(ii)": "Lebensmittel aus pflanzlichen Rohstoffen",
    "6.4(b)(iii)": "Lebensmittel aus tierischen und pflanzlichen Rohstoffen",
    "6.4(c)": "Milchbehandlung und -verarbeitung", "6.5": "Tierkörperbeseitigung",
    "6.6(a)": "Intensivhaltung von Geflügel", "6.6(b)": "Intensivhaltung von Mastschweinen",
    "6.6(c)": "Intensivhaltung von Säuen",
    "6.7": "Oberflächenbehandlung mit organischen Lösungsmitteln",
    "6.8": "Herstellung von Kohlenstoff/Elektrographit", "6.9": "CO₂-Abscheidung (CCS)",
    "6.10": "Holzschutzmittelbehandlung", "6.11": "Eigenständige Behandlung von Industrieabwasser",
}

_WZ_SECTION_RE = re.compile(r"^\s*Abschnitt\s+([A-Z])\s*[-–—]\s*(.+?)\s*$")


def _blank(v: object) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or not str(v).strip()


def activity_label(code: object) -> str | None:
    """"6.4(b)(ii)" → "6.4(b)(ii) – Lebensmittel aus pflanzlichen Rohstoffen" (probes
    "6.4(b)(ii)" → "6.4(b)" → "6.4")."""
    if _blank(code):
        return None
    c = str(code).strip()
    probe = c
    while probe:
        if probe in IED_ACTIVITY_LABELS:
            return f"{c} – {IED_ACTIVITY_LABELS[probe]}"
        if "(" not in probe:
            break
        probe = probe[: probe.rindex("(")]
    return c


def activity_group(code: object) -> str | None:
    """Annex-I chapter → group; 6.6 (intensive livestock) split out."""
    if _blank(code):
        return None
    c = str(code).strip()
    if c.startswith("6.6"):
        return "livestock"
    return {"1": "energy", "2": "metals", "3": "minerals", "4": "chemicals",
            "5": "waste", "6": "other"}.get(c[0])


def heat_band(mwh: object) -> str | None:
    try:
        v = float(mwh)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    if v < 1_000:
        return "< 1 GWh/a"
    if v < 10_000:
        return "1–10 GWh/a"
    if v < 100_000:
        return "10–100 GWh/a"
    return "> 100 GWh/a"


def wz_section(abschnitt: object) -> str | None:
    """"Abschnitt R – Gesundheits- und Sozialwesen" → "R – Gesundheits- und Sozialwesen"."""
    if _blank(abschnitt):
        return None
    return _WZ_SECTION_RE.sub(r"\1 – \2", str(abschnitt))


def techs_key(techs: object) -> str | None:
    """"solar+wind" → "+solar+wind+" so a tile filter can test "+solar+" as a substring
    without "storage" matching "gas_storage"."""
    if _blank(techs):
        return None
    parts = [t for t in str(techs).split("+") if t]
    return "+" + "+".join(parts) + "+" if parts else None
