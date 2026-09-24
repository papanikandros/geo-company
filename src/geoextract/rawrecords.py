"""Verbatim source records — every field of every source row behind a map entity.

The adapters (``sources/*``) map each source to the data contract and keep only a few
debug columns, so the nested raw records of the full download inherited that selection.
This module writes, per source, a second table with the source rows VERBATIM — every
column under its original name, values as text — keyed by the id the adapter gave the
row. The serve build nests it as ``raw`` inside each source record; the map card renders
it generically. Nothing here touches the contract, the merge or the enrichment stages.

Layout of every raw table (``src_<source>/<source>_raw_DE.parquet``)::

    id      the adapter id (``ied_…``, ``mastr_…``) — for the register: hr_source + hr_id
    record  what the row is (``installation``, ``unit:wind_extended``, ``operator``,
            ``name``, ``address``, ``gleif`` …)
    key     the record's own identifier in the source (MaStR number, globalId, LEI …)
    fields  MAP<text, text> of every non-empty source column, original names

Sources covered: IED (the whole thru.de workbook, all report years), MaStR (the units a
site was built from, with their EEG/KWK/permit/storage-plant records, the operator, the
location and its grid connections — all from the parsed bulk db, offline), the register
(hr2022 with the full name/address/objective/capital histories, hr2019 records, GLEIF
rows with all 338 columns; officers stay out — spec rule, GDPR). Everything is offline.
"""
from __future__ import annotations

import bz2
import json
import sqlite3
import time
import zipfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import config, paths
from .register.build import gleif_zip

RAW_COLUMNS = ["id", "record", "key", "fields"]
_SCHEMA = pa.schema([("id", pa.string()), ("record", pa.string()), ("key", pa.string()),
                     ("fields", pa.map_(pa.string(), pa.string()))])


def raw_parquet(data_root: Path, source: str) -> Path:
    return paths.source_parquet(data_root, source, "DE").parent / f"{source}_raw_DE.parquet"


# --- helpers -----------------------------------------------------------------------------

def _text(v) -> str | None:
    """Source value → text, verbatim; empty / NaN → None (dropped from the map)."""
    if v is None:
        return None
    if isinstance(v, float):
        if v != v:                      # NaN
            return None
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    if isinstance(v, bytes):
        return v.decode("utf8", "replace")
    s = str(v).strip()
    return s if s and s.lower() not in {"nan", "none", "nat", "<na>"} else None


def rows_to_records(df: pd.DataFrame, id_col: str, record: str, key_col: str | None,
                    drop: tuple[str, ...] = ()) -> list[tuple]:
    """Every row of ``df`` → (id, record, key, fields) with all columns verbatim."""
    cols = [c for c in df.columns if c not in drop and c != id_col]
    out = []
    vals = df[cols].to_numpy(dtype=object)
    ids = df[id_col].to_numpy(dtype=object)
    keys = df[key_col].to_numpy(dtype=object) if key_col else [None] * len(df)
    for i in range(len(df)):
        fields = [(c, t) for c, v in zip(cols, vals[i]) if (t := _text(v)) is not None]
        out.append((_text(ids[i]), record, _text(keys[i]), fields))
    return out


def write_records(records: list[tuple], dest: Path) -> Path:
    tbl = pa.Table.from_arrays([
        pa.array([r[0] for r in records], pa.string()),
        pa.array([r[1] for r in records], pa.string()),
        pa.array([r[2] for r in records], pa.string()),
        pa.array([r[3] for r in records], pa.map_(pa.string(), pa.string())),
    ], schema=_SCHEMA)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, dest, compression="zstd")
    return dest


def _sql_in(con: sqlite3.Connection, name: str, values: set[str]) -> None:
    """A temp table of ids to join against — an IN (...) list would not scale."""
    con.execute(f"DROP TABLE IF EXISTS {name}")
    con.execute(f"CREATE TEMP TABLE {name} (v TEXT PRIMARY KEY)")
    con.executemany(f"INSERT OR IGNORE INTO {name} VALUES (?)", [(v,) for v in values if v])


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone() is not None


# --- IED ---------------------------------------------------------------------------------

def write_ied(data_root: Path) -> Path | None:
    """The whole thru.de workbook, every column, all report years, keyed by installation."""
    from .sources import ied
    xlsx = paths.raw_dir(data_root) / "ied" / config.IED_URL.rsplit("/", 1)[-1]
    if not xlsx.exists():
        print(f"[raw] ied: {xlsx} missing — skipped")
        return None
    df = pd.read_excel(xlsx, sheet_name=config.IED_SHEET)
    df["_id"] = df["InspireID.Anlage"].map(ied._ied_id)
    recs = rows_to_records(df, "_id", "installation", "InspireID.Anlage")
    dest = write_records(recs, raw_parquet(data_root, "ied"))
    print(f"[raw] ied: {len(recs)} installation rows ({df['_id'].nunique()} installations) → {dest.name}")
    return dest


# --- MaStR -------------------------------------------------------------------------------

_MASTR_SUBTABLES = {   # unit column → (table, its key column, record name)
    "EegMastrNummer": (None, "EegMastrNummer", "eeg"),             # table depends on the unit table
    "KwkMastrNummer": ("kwk", "KwkMastrNummer", "kwk"),
    "GenMastrNummer": ("permit", "GenMastrNummer", "permit"),
    "SpeMastrNummer": ("storage_units", "MastrNummer", "storage_plant"),
}


def _mastr_unit_sites(sites: pd.DataFrame) -> dict[str, str]:
    """unit MaStR number → site id, from the per-unit detail the adapter kept."""
    out: dict[str, str] = {}
    for sid, td in zip(sites["id"], sites["mastr_tech_detail"]):
        if not isinstance(td, str) or not td:
            continue
        for units in json.loads(td).values():
            for u in units:
                if u.get("unit"):
                    out[str(u["unit"])] = sid
    return out


def _mastr_fetch(con: sqlite3.Connection, table: str, key_col: str, keys: set[str]) -> pd.DataFrame:
    if not keys or not _table_exists(con, table):
        return pd.DataFrame()
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    if key_col not in cols:
        return pd.DataFrame()
    _sql_in(con, "sel", keys)
    return pd.read_sql(f"SELECT t.* FROM {table} t JOIN sel ON sel.v = t.`{key_col}`", con)


def write_mastr(data_root: Path) -> Path | None:
    """Every unit a site was built from, verbatim, plus the records hanging off it."""
    sites_p = paths.source_parquet(data_root, "mastr", "DE")
    db = data_root / config.MASTR_DB
    if not sites_p.exists() or not db.exists():
        print("[raw] mastr: site table or bulk db missing — skipped")
        return None
    t0 = time.time()
    sites = pd.read_parquet(sites_p, columns=["id", "mastr_tech_detail"])
    unit_site = _mastr_unit_sites(sites)
    con = sqlite3.connect(db)
    recs: list[tuple] = []
    link: dict[str, dict[str, set[str]]] = {k: {} for k in _MASTR_SUBTABLES}   # sub-key → site ids
    operators: dict[str, set[str]] = {}
    locations: dict[str, set[str]] = {}
    for table, tech in config.MASTR_TABLES.items():
        units = _mastr_fetch(con, table, "EinheitMastrNummer", set(unit_site))
        if units.empty:
            continue
        units["_site"] = units["EinheitMastrNummer"].map(unit_site)
        recs += rows_to_records(units, "_site", f"unit:{table}", "EinheitMastrNummer")
        for col, (_, _, _) in _MASTR_SUBTABLES.items():
            if col in units.columns:
                for k, sid in zip(units[col], units["_site"]):
                    if _text(k):
                        link[col].setdefault(str(k), set()).add(sid)
        if "AnlagenbetreiberMastrNummer" in units.columns:
            for k, sid in zip(units["AnlagenbetreiberMastrNummer"], units["_site"]):
                if _text(k):
                    operators.setdefault(str(k), set()).add(sid)
        if "LokationMastrNummer" in units.columns:
            for k, sid in zip(units["LokationMastrNummer"], units["_site"]):
                if _text(k):
                    locations.setdefault(str(k), set()).add(sid)
        # the EEG record of this technology's units
        eeg_table = table.replace("_extended", "_eeg")
        if "EegMastrNummer" in units.columns and _table_exists(con, eeg_table):
            keys = {str(k) for k in units["EegMastrNummer"] if _text(k)}
            eeg = _mastr_fetch(con, eeg_table, "EegMastrNummer", keys)
            if not eeg.empty:
                recs += _fan_out(eeg, "EegMastrNummer", f"eeg:{eeg_table}", link["EegMastrNummer"])
        print(f"[raw] mastr: {table}: {len(units)} units")
    for col, (table, key_col, record) in _MASTR_SUBTABLES.items():
        if table is None:
            continue
        sub = _mastr_fetch(con, table, key_col, set(link[col]))
        if not sub.empty:
            recs += _fan_out(sub, key_col, record, link[col])
    ops = _mastr_fetch(con, "market_actors", "MastrNummer", set(operators))
    if not ops.empty:
        recs += _fan_out(ops, "MastrNummer", "operator", operators)
    loc = _mastr_fetch(con, "locations_extended", "MastrNummer", set(locations))
    if not loc.empty:
        recs += _fan_out(loc, "MastrNummer", "location", locations)
    grid = _mastr_fetch(con, "grid_connections", "LokationMastrNummer", set(locations))
    if not grid.empty:
        recs += _fan_out(grid, "LokationMastrNummer", "grid_connection", locations,
                         key_col="NetzanschlusspunktMastrNummer")
    con.close()
    dest = write_records(recs, raw_parquet(data_root, "mastr"))
    kinds = pd.Series([r[1] for r in recs]).value_counts().to_dict()
    print(f"[raw] mastr: {len(recs)} records for {len(sites)} sites {kinds} "
          f"({time.time() - t0:.0f} s) → {dest.name}")
    return dest


def _fan_out(df: pd.DataFrame, link_col: str, record: str, targets: dict[str, set[str]],
             key_col: str | None = None) -> list[tuple]:
    """One record per (row, site) — a record shared by several sites appears under each."""
    df = df.copy()
    df["_sites"] = df[link_col].map(lambda k: sorted(targets.get(str(k), ())))
    df = df.explode("_sites")
    df = df[df["_sites"].notna()]
    return rows_to_records(df, "_sites", record, key_col or link_col)


# --- register ----------------------------------------------------------------------------

_HR2022_TABLES = {   # table → (record, key column)
    "Companies": ("company", "companyId"),
    "Names": ("name", "globalId"),
    "Addresses": ("address", "globalId"),
    "Objectives": ("objective", "globalId"),
    "Capital": ("capital", "globalId"),
    "ReferenceNumbers": ("reference", "stdRefNo"),
}   # Positions (officers) deliberately not read


def register_ids(data_root: Path) -> dict[str, set[str]]:
    """hr_source → hr_ids referenced by any merged table (DE + state scopes)."""
    import duckdb
    files = sorted((data_root / "geoextract").glob("companies_merged_*_4326.parquet"))
    files = [f for f in files if ".pre_" not in f.name]
    out: dict[str, set[str]] = {}
    if not files:
        return out
    con = duckdb.connect()
    for f in files:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{f}')").fetchall()]
        if not {"hr_id", "hr_source"} <= set(cols):
            continue
        for src, hid in con.execute(
                f"SELECT DISTINCT hr_source, hr_id FROM read_parquet('{f}') WHERE hr_id IS NOT NULL").fetchall():
            out.setdefault(src, set()).add(hid)
    return out


def _hr_id(source: str, hid) -> str:
    return f"{source}:{hid}"


def write_register(data_root: Path, ids: dict[str, set[str]] | None = None) -> Path | None:
    """Full register rows (with histories) for every hr_id a merged table references."""
    ids = register_ids(data_root) if ids is None else ids
    if not ids:
        print("[raw] register: no merged table with hr_id — skipped")
        return None
    t0 = time.time()
    recs: list[tuple] = []
    # hr2022: handelsregister.db, all tables except officers, full histories
    db = data_root / config.HR2022_DB
    if ids.get("hr2022") and db.exists():
        con = sqlite3.connect(db)
        _sql_in(con, "sel", ids["hr2022"])
        for table, (record, key_col) in _HR2022_TABLES.items():
            if not _table_exists(con, table):
                continue
            df = pd.read_sql(f"SELECT t.* FROM {table} t JOIN sel ON sel.v = t.companyId", con)
            df["_id"] = df["companyId"].map(lambda h: _hr_id("hr2022", h))
            recs += rows_to_records(df, "_id", record, key_col if key_col in df.columns else None)
        con.close()
        print(f"[raw] register: hr2022 {len(recs)} rows for {len(ids['hr2022'])} companies")
    # hr2019: the OKFN dump, one flattened record per company (+ previous names); no officers
    jsonl = data_root / config.HR2019_JSONL
    n19 = 0
    if ids.get("hr2019") and jsonl.exists():
        want = ids["hr2019"]
        with bz2.open(jsonl, "rt", encoding="utf8") as fh:
            for line in fh:
                d = json.loads(line)
                cid = d.get("company_number")
                if cid not in want:
                    continue
                flat: dict = {}
                for k, v in d.items():
                    if k in ("officers", "previous_names"):
                        continue
                    if isinstance(v, dict):
                        for k2, v2 in v.items():
                            if isinstance(v2, dict):
                                for k3, v3 in v2.items():
                                    flat[f"{k}.{k2}.{k3}"] = v3
                            else:
                                flat[f"{k}.{k2}"] = v2
                    else:
                        flat[k] = v
                rid = _hr_id("hr2019", cid)
                recs.append((rid, "hr2019", cid,
                             [(k, t) for k, v in flat.items() if (t := _text(v)) is not None]))
                for p in d.get("previous_names") or []:
                    recs.append((rid, "hr2019_previous_name", cid,
                                 [(k, t) for k, v in p.items() if (t := _text(v)) is not None]))
                n19 += 1
        print(f"[raw] register: hr2019 {n19} of {len(want)} companies found")
    # GLEIF: the golden-copy row, all columns
    src = gleif_zip(data_root)
    if ids.get("gleif") and src is not None:
        want = ids["gleif"]
        n = 0
        with zipfile.ZipFile(src) as z:
            name = z.namelist()[0]
            with z.open(name) as raw:
                for chunk in pd.read_csv(raw, dtype="string", chunksize=200_000, low_memory=False):
                    hit = chunk[chunk["LEI"].isin(want)]
                    if len(hit):
                        hit = hit.assign(_id=hit["LEI"].map(lambda h: _hr_id("gleif", h)))
                        recs += rows_to_records(hit, "_id", "gleif", "LEI")
                        n += len(hit)
        print(f"[raw] register: gleif {n} of {len(want)} entities found")
    dest = write_records(recs, paths.register_parquet(data_root, "register_raw"))
    print(f"[raw] register: {len(recs)} records ({time.time() - t0:.0f} s) → {dest.name}")
    return dest


WRITERS = {"ied": write_ied, "mastr": write_mastr, "register": write_register}


def build(data_root: Path, sources: list[str]) -> list[Path]:
    out = []
    for s in sources:
        if s not in WRITERS:
            raise SystemExit(f"unknown raw source {s!r}; known: {', '.join(WRITERS)}")
        p = WRITERS[s](data_root)
        if p:
            out.append(p)
    return out
