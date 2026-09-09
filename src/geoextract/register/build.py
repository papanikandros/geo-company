"""Stage 0a–0c: bulk register files → uniform parquet tables (register-website-pipeline-plan.md).

Three sources, one shape, so stage 2 (matching) treats them alike:

* ``hr2022`` — ``data/raw/handelsregister/handelsregister.db`` (offeneregister.de, SQLite,
  2022-10-21): 2.19 M HRB companies 2001 – 2022-08 with name / address / reference-number
  history, dissolution date, Unternehmensgegenstand and capital. The ``Positions`` table
  (officers with birth dates) is personal data and is never read.
* ``hr2019`` — ``de_companies_ocdata.jsonl.bz2`` (OpenCorporates/OKFN, June 2017 – Jan 2019):
  all divisions (HRA/HRB/GnR/PR/VR), registered address as one string, status, previous names.
  ``officers`` are skipped.
* ``gleif`` — GLEIF golden copy (CC0, daily): German legal entities with LEI, legal address,
  registration-authority id (the HR number), ELF legal-form code, entity status.

Output (``data/geoextract/register/``):
``hr_companies_{source}.parquet`` — one row per company (current values) and
``hr_names_{source}.parquet`` — one row per name variant (current + previous) with the item 5
name keys, which is what the matcher blocks on. Both are idempotent caches (``force`` rebuilds).

Columns of hr_companies (all sources; missing → NA):
hr_id, hr_source, name, name_key_full, name_key_light, legal_form, register_division,
register_type, register_number, hr_registration ("HRB 519801"), court, court_code,
street, housenumber, postcode, city, street_key, state, status (active|dissolved|unknown),
founded, dissolved, first_seen, last_seen, snapshot_date, objective, capital_amount,
capital_currency, previous_names (pipe-joined), lei.
"""
from __future__ import annotations

import bz2
import glob
import io
import json
import math
import re
import sqlite3
import time
import zipfile
from pathlib import Path

import pandas as pd

from .. import config, paths
from ..resolve import normalize_name, normalize_street
from .legalform import legal_form_from_name, register_division

_REF_RE = re.compile(r"\b(HRA|HRB|GnR|PR|VR|GsR)\s*(\d+(?:\s*[A-Za-z]{1,3})?)")
_PLZ_CITY_RE = re.compile(r"(?<!\d)(\d{5})\s+([^\d,]+?)\.?\s*$")
_HOUSENUMBER_RE = re.compile(r"^(.*?[a-zäöüß.])\s+(\d+[\w\s./-]*)$")

COMPANY_COLUMNS = [
    "hr_id", "hr_source", "name", "name_key_full", "name_key_light", "legal_form",
    "register_division", "register_type", "register_number", "hr_registration", "court",
    "court_code", "street", "housenumber", "postcode", "city", "street_key", "state",
    "status", "founded", "dissolved", "first_seen", "last_seen", "snapshot_date", "objective",
    "capital_amount", "capital_currency", "previous_names", "lei",
]


# --- shared helpers ----------------------------------------------------------------------

def parse_reference(text: object) -> tuple[str | None, str | None]:
    """"Jena HRB 519801" / "HRB 12345 B" → ("HRB", "519801") / ("HRB", "12345 B")."""
    if text is None or (isinstance(text, float) and math.isnan(text)):
        return None, None
    m = _REF_RE.search(str(text))
    if not m:
        return None, None
    return m.group(1), " ".join(m.group(2).split())


def split_street(text: object) -> tuple[str | None, str | None]:
    """"Am Holunderstrauch 39" → ("Am Holunderstrauch", "39"); no number → (street, None)."""
    if text is None or (isinstance(text, float) and math.isnan(text)) or not str(text).strip():
        return None, None
    s = " ".join(str(text).split())
    m = _HOUSENUMBER_RE.match(s)
    if m:
        return m.group(1).strip(" ,"), m.group(2).strip()
    return s, None


def parse_address_line(text: object) -> tuple[str | None, str | None, str | None, str | None]:
    """"Waidmannstraße 1, 22769 Hamburg." → (street, housenumber, postcode, city)."""
    if text is None or (isinstance(text, float) and math.isnan(text)) or not str(text).strip():
        return None, None, None, None
    s = " ".join(str(text).split()).rstrip(".")
    m = _PLZ_CITY_RE.search(s)
    postcode = city = None
    if m:
        postcode, city = m.group(1), m.group(2).strip(" ,")
        s = s[: m.start()].rstrip(" ,")
    street, nr = split_street(s)
    return street, nr, postcode, city


def _finish(df: pd.DataFrame, source: str, snapshot: str) -> pd.DataFrame:
    """Add keys + derived columns, order columns, cast to nullable dtypes."""
    df = df.copy()
    df["hr_source"] = source
    df["snapshot_date"] = snapshot
    df["name_key_full"] = df["name"].map(lambda n: normalize_name(n, strip_noise=True))
    df["name_key_light"] = df["name"].map(lambda n: normalize_name(n, strip_noise=False))
    if "legal_form" not in df.columns or df["legal_form"].isna().all():
        df["legal_form"] = df["name"].map(legal_form_from_name)
    df["register_division"] = df["legal_form"].map(register_division)
    if "register_type" in df.columns:   # the register itself beats the name-derived division
        df["register_division"] = df["register_type"].where(
            df["register_type"].notna(), df["register_division"])
    df["street_key"] = [   # street + house number, as the item 5 key expects ("12 a" → "12a")
        normalize_street(f"{st} {nr}" if isinstance(nr, str) and nr else st)
        for st, nr in zip(df["street"], df["housenumber"])
    ]
    df["hr_registration"] = (
        df["register_type"].fillna("").astype(str) + " " + df["register_number"].fillna("").astype(str)
    ).str.strip().replace("", pd.NA)
    for c in COMPANY_COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[COMPANY_COLUMNS]
    for c in COMPANY_COLUMNS:
        if c not in ("capital_amount",):
            df[c] = df[c].astype("string")
    df["capital_amount"] = pd.to_numeric(df["capital_amount"], errors="coerce").astype("Float64")
    return df.reset_index(drop=True)


def _names_table(companies: pd.DataFrame, previous: pd.DataFrame | None) -> pd.DataFrame:
    """hr_names: every name variant (current + previous) with keys; the matcher's input."""
    cur = companies[["hr_id", "hr_source", "name", "postcode", "city", "street_key"]].copy()
    cur["is_current"] = True
    parts = [cur]
    if previous is not None and len(previous):
        prev = previous.drop(columns=[c for c in ("hr_source",) if c in previous.columns])
        prev = prev.merge(companies[["hr_id", "hr_source", "postcode", "city", "street_key"]],
                          on="hr_id", how="inner")
        prev["is_current"] = False
        parts.append(prev[cur.columns])
    names = pd.concat(parts, ignore_index=True)
    names = names[names["name"].notna() & (names["name"].astype(str).str.strip() != "")]
    names["name_key_full"] = names["name"].map(lambda n: normalize_name(n, strip_noise=True))
    names["name_key_light"] = names["name"].map(lambda n: normalize_name(n, strip_noise=False))
    names = names.drop_duplicates(["hr_id", "name_key_full", "name_key_light"]).reset_index(drop=True)
    for c in ("hr_id", "hr_source", "name", "postcode", "city", "street_key",
              "name_key_full", "name_key_light"):
        names[c] = names[c].astype("string")
    return names


def _write(companies: pd.DataFrame, names: pd.DataFrame, data_root: Path, source: str) -> Path:
    out_c = paths.register_parquet(data_root, f"hr_companies_{source}")
    out_n = paths.register_parquet(data_root, f"hr_names_{source}")
    companies.to_parquet(out_c, index=False)
    names.to_parquet(out_n, index=False)
    return out_c


# --- 0a: handelsregister.db (2022) -------------------------------------------------------

def _latest(df: pd.DataFrame, order_col: str) -> pd.DataFrame:
    """One row per companyId: the current one (isCurrent) with the latest validFrom."""
    if "isCurrent" in df.columns:
        cur = df[df["isCurrent"].astype(str).str.lower() == "true"]
        rest = df[~df.index.isin(cur.index)]
        df = pd.concat([rest.assign(_rank=0), cur.assign(_rank=1)])
    else:
        df = df.assign(_rank=1)
    df = df.sort_values(["companyId", "_rank", order_col], kind="stable")
    return df.drop_duplicates("companyId", keep="last").drop(columns="_rank")


def build_hr2022(data_root: Path, force: bool = False) -> Path:
    src = data_root / config.HR2022_DB
    out = paths.register_parquet(data_root, "hr_companies_hr2022")
    if out.exists() and not force:
        return out
    if not src.exists():
        raise FileNotFoundError(f"{src} — download handelsregister.db first (see CLAUDE.md item 6)")
    t0 = time.time()
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    q = lambda sql: pd.read_sql_query(sql, con)
    companies = q("SELECT companyId, firstSeenDate, lastSeenDate, foundedDate, dissolutionDate "
                  "FROM Companies")
    names_all = q("SELECT companyId, validFrom, name, isCurrent FROM Names")
    addresses = _latest(q("SELECT companyId, validFrom, address, zipCode, zipAndPlace, isCurrent "
                          "FROM Addresses"), "validFrom")
    refs = q("SELECT companyId, nativeReferenceNumber, courtName, courtCode, "
             "referenceNumberFirstSeen, validTill FROM ReferenceNumbers")
    objectives = _latest(q("SELECT companyId, validFrom, objective, isCurrent FROM Objectives"),
                         "validFrom")
    capital = _latest(q("SELECT companyId, validFrom, capitalAmount, capitalCurrency, isCurrent "
                        "FROM Capital"), "validFrom")
    con.close()
    print(f"[hr2022] read {len(companies)} companies, {len(names_all)} names, "
          f"{len(addresses)} addresses, {len(refs)} references in {time.time() - t0:.0f} s")

    names_cur = _latest(names_all, "validFrom")[["companyId", "name"]]
    prev = names_all.merge(names_cur.rename(columns={"name": "_cur"}), on="companyId")
    prev = prev[prev["name"] != prev["_cur"]][["companyId", "name"]].drop_duplicates()
    prev_joined = prev.groupby("companyId")["name"].agg(" | ".join)

    # current reference: open validTill, latest first-seen (chained ids keep the old court too)
    refs["_open"] = (refs["validTill"].fillna("") == "").astype(int)
    refs = refs.sort_values(["companyId", "_open", "referenceNumberFirstSeen"], kind="stable")
    ref_cur = refs.drop_duplicates("companyId", keep="last")
    parsed = ref_cur["nativeReferenceNumber"].map(parse_reference)
    ref_cur = ref_cur.assign(register_type=[p[0] for p in parsed],
                             register_number=[p[1] for p in parsed])

    df = companies.rename(columns={"companyId": "hr_id", "firstSeenDate": "first_seen",
                                   "lastSeenDate": "last_seen", "foundedDate": "founded",
                                   "dissolutionDate": "dissolved"})
    df = df.merge(names_cur.rename(columns={"companyId": "hr_id"}), on="hr_id", how="left")
    df = df.merge(addresses.rename(columns={"companyId": "hr_id"})[
        ["hr_id", "address", "zipCode", "zipAndPlace"]], on="hr_id", how="left")
    df = df.merge(ref_cur.rename(columns={"companyId": "hr_id", "courtName": "court",
                                          "courtCode": "court_code"})[
        ["hr_id", "court", "court_code", "register_type", "register_number"]], on="hr_id", how="left")
    df = df.merge(objectives.rename(columns={"companyId": "hr_id"})[["hr_id", "objective"]],
                  on="hr_id", how="left")
    df = df.merge(capital.rename(columns={"companyId": "hr_id", "capitalAmount": "capital_amount",
                                          "capitalCurrency": "capital_currency"})[
        ["hr_id", "capital_amount", "capital_currency"]], on="hr_id", how="left")
    df["previous_names"] = df["hr_id"].map(prev_joined)

    split = df["address"].map(split_street)
    df["street"] = [s[0] for s in split]
    df["housenumber"] = [s[1] for s in split]
    df["postcode"] = df["zipCode"].replace("", pd.NA)
    df["city"] = [
        (zp[len(z):].strip() if isinstance(zp, str) and isinstance(z, str) and zp.startswith(z)
         else (zp if isinstance(zp, str) and zp else None))
        for zp, z in zip(df["zipAndPlace"], df["zipCode"].fillna(""))
    ]
    for c in ("founded", "dissolved", "first_seen", "last_seen", "objective"):
        df[c] = df[c].replace("", pd.NA)
    df["status"] = df["dissolved"].notna().map({True: "dissolved", False: "active"})
    companies_out = _finish(df, "hr2022", config.HR2022_SNAPSHOT)
    names_out = _names_table(companies_out, prev.rename(columns={"companyId": "hr_id"})
                             .assign(hr_source="hr2022"))
    print(f"[hr2022] {len(companies_out)} companies, {len(names_out)} name variants, "
          f"{int(companies_out['status'].eq('dissolved').sum())} dissolved "
          f"({time.time() - t0:.0f} s)")
    return _write(companies_out, names_out, data_root, "hr2022")


# --- 0b: OpenCorporates / OKFN 2019 dump --------------------------------------------------

def _hr2019_records(path: Path):
    with bz2.open(path, "rt", encoding="utf8") as fh:
        for line in fh:
            d = json.loads(line)
            attrs = d.get("all_attributes") or {}
            prev = d.get("previous_names") or []
            yield {
                "hr_id": d.get("company_number"),
                "name": d.get("name"),
                "status_raw": d.get("current_status"),
                "address_line": d.get("registered_address"),
                "register_type": attrs.get("_registerArt"),
                "register_number": " ".join(
                    str(attrs.get("_registerNummer") or "").split()
                    + [str(attrs.get("_registerNummerSuffix") or "").strip()]).strip() or None,
                "court": attrs.get("registrar"),
                "state": attrs.get("federal_state"),
                "registered_office": attrs.get("registered_office"),
                "last_seen": (d.get("retrieved_at") or "")[:10] or None,
                "previous_names": " | ".join(
                    p.get("company_name", "") for p in prev if p.get("company_name")) or None,
            }


def build_hr2019(data_root: Path, force: bool = False) -> Path:
    src = data_root / config.HR2019_JSONL
    out = paths.register_parquet(data_root, "hr_companies_hr2019")
    if out.exists() and not force:
        return out
    if not src.exists():
        raise FileNotFoundError(f"{src} — download the 2019 dump first (260 MB, needs a go)")
    t0 = time.time()
    df = pd.DataFrame.from_records(_hr2019_records(src))
    print(f"[hr2019] parsed {len(df)} records in {time.time() - t0:.0f} s")
    parsed = df["address_line"].map(parse_address_line)
    df["street"] = [p[0] for p in parsed]
    df["housenumber"] = [p[1] for p in parsed]
    df["postcode"] = [p[2] for p in parsed]
    df["city"] = [p[3] for p in parsed]
    df["city"] = df["city"].where(df["city"].notna(), df["registered_office"])
    df["status"] = df["status_raw"].map(
        lambda s: "active" if s == "currently registered" else ("dissolved" if s == "removed" else "unknown"),
        na_action="ignore").fillna("unknown")
    df["dissolved"] = pd.NA
    companies_out = _finish(df, "hr2019", config.HR2019_SNAPSHOT)
    prev = df[["hr_id", "previous_names"]].dropna()
    prev = prev.assign(name=prev["previous_names"].str.split(r" \| ")).explode("name")
    prev = prev[["hr_id", "name"]].assign(hr_source="hr2019")
    names_out = _names_table(companies_out, prev)
    print(f"[hr2019] {len(companies_out)} companies, {len(names_out)} name variants, "
          f"divisions {companies_out['register_type'].value_counts().to_dict()} "
          f"({time.time() - t0:.0f} s)")
    return _write(companies_out, names_out, data_root, "hr2019")


# --- 0c: GLEIF golden copy ---------------------------------------------------------------

GLEIF_COLS = {
    "LEI": "lei",
    "Entity.LegalName": "name",
    "Entity.LegalAddress.FirstAddressLine": "street_line",
    "Entity.LegalAddress.AddressNumber": "address_number",
    "Entity.LegalAddress.City": "city",
    "Entity.LegalAddress.PostalCode": "postcode",
    "Entity.LegalAddress.Country": "country",
    "Entity.LegalAddress.Region": "region",
    "Entity.RegistrationAuthority.RegistrationAuthorityID": "ra_id",
    "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "ra_entity_id",
    "Entity.LegalForm.EntityLegalFormCode": "elf_code",
    "Entity.EntityStatus": "entity_status",
    "Entity.EntityCreationDate": "founded",
    "Registration.InitialRegistrationDate": "first_seen",
    "Registration.LastUpdateDate": "last_seen",
    "Registration.RegistrationStatus": "registration_status",
}


def gleif_zip(data_root: Path) -> Path | None:
    hits = sorted(glob.glob(str(data_root / config.GLEIF_ZIP_GLOB)))
    return Path(hits[-1]) if hits else None


def build_gleif(data_root: Path, force: bool = False) -> Path:
    out = paths.register_parquet(data_root, "hr_companies_gleif")
    if out.exists() and not force:
        return out
    src = gleif_zip(data_root)
    if src is None:
        raise FileNotFoundError(f"no {config.GLEIF_ZIP_GLOB} — download the golden copy first")
    t0 = time.time()
    with zipfile.ZipFile(src) as zf:
        member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(member) as raw:
            header = pd.read_csv(io.TextIOWrapper(raw, encoding="utf8"), nrows=0).columns
            missing = [c for c in GLEIF_COLS if c not in header]
            if missing:
                raise KeyError(f"GLEIF CSV lacks columns {missing}; header has {len(header)} columns")
        parts = []
        with zf.open(member) as raw:
            for chunk in pd.read_csv(io.TextIOWrapper(raw, encoding="utf8"),
                                     usecols=list(GLEIF_COLS), dtype="string",
                                     chunksize=200_000):
                parts.append(chunk[chunk["Entity.LegalAddress.Country"] == config.GLEIF_COUNTRY])
    df = pd.concat(parts, ignore_index=True).rename(columns=GLEIF_COLS)
    snapshot = re.search(r"(\d{8})-\d{4}-gleif", src.name)
    snapshot_date = (f"{snapshot.group(1)[:4]}-{snapshot.group(1)[4:6]}-{snapshot.group(1)[6:]}"
                     if snapshot else time.strftime("%Y-%m-%d"))
    print(f"[gleif] {len(df)} {config.GLEIF_COUNTRY} entities of the golden copy "
          f"({src.name}) in {time.time() - t0:.0f} s")
    df["hr_id"] = df["lei"]
    split = df["street_line"].map(split_street)
    df["street"] = [s[0] for s in split]
    df["housenumber"] = pd.Series([s[1] for s in split], index=df.index, dtype="string")
    df["housenumber"] = df["address_number"].where(df["address_number"].notna(), df["housenumber"])
    parsed = df["ra_entity_id"].map(parse_reference)
    df["register_type"] = [p[0] for p in parsed]
    df["register_number"] = [p[1] for p in parsed]
    df["court"] = pd.NA
    df["status"] = df["entity_status"].map(
        lambda s: "active" if s == "ACTIVE" else ("dissolved" if s == "INACTIVE" else "unknown"),
        na_action="ignore").fillna("unknown")
    df["state"] = pd.NA
    df["dissolved"] = pd.NA
    df["previous_names"] = pd.NA
    for c in ("founded", "first_seen", "last_seen"):
        df[c] = df[c].str[:10]
    companies_out = _finish(df, "gleif", snapshot_date)
    names_out = _names_table(companies_out, None)
    return _write(companies_out, names_out, data_root, "gleif")


BUILDERS = {"hr2022": build_hr2022, "hr2019": build_hr2019, "gleif": build_gleif}


def build(data_root: Path, sources: list[str] | None = None, force: bool = False) -> list[Path]:
    """Build every requested register table; skips sources whose raw file is absent."""
    written = []
    for name in sources or list(BUILDERS):
        try:
            written.append(BUILDERS[name](data_root, force=force))
        except FileNotFoundError as err:
            print(f"[register] skip {name}: {err}")
    return written


def load_companies(data_root: Path, sources: list[str] | None = None) -> pd.DataFrame:
    frames = []
    for name in sources or list(BUILDERS):
        p = paths.register_parquet(data_root, f"hr_companies_{name}")
        if p.exists():
            frames.append(pd.read_parquet(p))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COMPANY_COLUMNS)


def load_names(data_root: Path, sources: list[str] | None = None) -> pd.DataFrame:
    frames = []
    for name in sources or list(BUILDERS):
        p = paths.register_parquet(data_root, f"hr_names_{name}")
        if p.exists():
            frames.append(pd.read_parquet(p))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
