"""Stage 3c — website candidates from the Common Crawl domain list (no search engine).

``geoextract web ccindex build`` turns the web-graph domain vertex file
(``data/raw/commoncrawl/cc-main-*-domain-vertices.txt.gz``, all registered domains the
crawl saw) into ``web/cc_de_domains.parquet``: the ``.de`` block, one row per registered
domain, sorted by label. ``geoextract web ccindex match`` then searches that list in the
direction domain guessing cannot: for a company without a verified site, which EXISTING
.de domains look like its name? Candidates (≤ 5 per company, best first):

1. the guessed labels of stage 3b that exist in the list (``schwarzwaldmilch``,
   ``schwarzwald-milch``, ``schwarzwaldmilch-gmbh``);
2. labels that START with the joined or hyphenated name key (``schwarzwaldmilch-shop``,
   ``schwarzwaldmilch24``);
3. labels that start with the first distinctive name token (≥ 6 characters) and continue
   with another name token or a common suffix (``-gmbh``, ``-online``, ``24``, the city).

Every candidate host is verified through the imprint stage (``web.impressum``): accepted
only with the verdict ``name+plz`` or ``name``; then ``website`` is written with
``website_source = cc``. A row keeps its unverified old website when nothing better is
found. Host fetches share the imprint cache, so nothing is fetched twice.
"""
from __future__ import annotations

import bisect
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config, paths
from ..register.legalform import legal_form_from_name, register_division
from .discover import candidates as guess_candidates
from .discover import name_tokens
from . import impressum

_SUFFIXES = {"gmbh", "kg", "ag", "ug", "ohg", "gbr", "ev", "shop", "online", "24", "group", "gruppe", "web", "home",
             "de", "germany", "deutschland", "service", "services", "logistik", "logistics", "technik", "bau",
             "bremen", "bremerhaven", "hb", "nord", "north", "official", "site", "portal", "info", "net"}
_NON_LABEL = re.compile(r"[^a-z0-9-]")


def index_parquet(data_root: Path) -> Path:
    return data_root / "geoextract" / "web" / "cc_de_domains.parquet"


def vertex_file(data_root: Path) -> Path | None:
    hits = sorted((data_root / "raw" / "commoncrawl").glob("cc-main-*-domain-vertices.txt.gz"))
    return hits[-1] if hits else None


def build(data_root: Path) -> Path:
    """Vertex file → sorted parquet of .de registered domains (label, domain, hosts)."""
    import gzip

    import duckdb
    src = vertex_file(data_root)
    if src is None:
        raise FileNotFoundError("no data/raw/commoncrawl/cc-main-*-domain-vertices.txt.gz")
    dest = index_parquet(data_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tsv")
    t0, n = time.time(), 0
    with gzip.open(src, "rt", encoding="utf8") as fh, open(tmp, "w", encoding="utf8") as out:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[1].startswith("de."):
                out.write(parts[1][3:] + "\t" + (parts[2] if len(parts) > 2 else "1") + "\n")
                n += 1
    con = duckdb.connect()
    con.execute(f"""COPY (SELECT column0 AS domain, column1::INTEGER AS hosts,
                        CASE WHEN position('.' IN column0) > 0 THEN split_part(column0, '.', 1) ELSE column0 END AS label
                    FROM read_csv('{tmp}', delim='\t', header=false, columns={{'column0':'VARCHAR','column1':'VARCHAR'}}, quote='')
                    ORDER BY label) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    tmp.unlink()
    print(f"[ccindex] {n} .de domains from {src.name} → {dest} ({time.time() - t0:.0f} s)")
    return dest


class DomainIndex:
    def __init__(self, labels: np.ndarray):
        self.labels = labels                     # sorted
        self.set = set(labels.tolist())

    @classmethod
    def load(cls, data_root: Path) -> "DomainIndex":
        df = pd.read_parquet(index_parquet(data_root), columns=["label"])
        return cls(np.array(sorted(set(df["label"].astype(str)))))

    def prefix(self, pre: str, limit: int = 200) -> list[str]:
        if not pre:
            return []
        lo = bisect.bisect_left(self.labels, pre)
        hi = bisect.bisect_left(self.labels, pre[:-1] + chr(ord(pre[-1]) + 1))
        return self.labels[lo:min(hi, lo + limit)].tolist()


def candidates(name: object, legal_form: object, index: DomainIndex, city: object = None, limit: int = 5) -> list[str]:
    """Existing .de labels that look like this company's name, best first."""
    toks = name_tokens(name)
    distinct = [t for t in toks if t not in impressum._HOST_GENERIC]
    if not toks or not distinct:                            # "TX Logistik", "A&M Service & Technik": nothing to search for
        return []
    joined, hyphen = "".join(toks), "-".join(toks)
    if len(joined) < 5:
        return []
    if len(toks) == 1:   # a single distinctive token: only its exact label, and only when it is long enough
        return [joined] if len(joined) >= 7 and joined in index.set else []
    out: list[str] = []
    seen: set[str] = set()

    def add(lbl: str) -> None:
        if lbl and lbl in index.set and lbl not in seen and not _NON_LABEL.search(lbl):
            seen.add(lbl)
            out.append(lbl)

    for h in guess_candidates(name, legal_form):           # 1. guessed labels that exist
        if h.endswith(".de"):
            add(h[:-3])
    for pre in (joined, hyphen):                            # 2. name key as a prefix
        for lbl in sorted(index.prefix(pre, 50), key=len):
            if len(lbl) <= len(pre) + 12:
                add(lbl)
    first = distinct[0]
    if len(first) >= 6 and len(out) < limit:                # 3. first distinctive token + another token / suffix
        others = set(toks[1:]) | _SUFFIXES
        if isinstance(city, str):
            others |= set(name_tokens(city))
        for lbl in sorted(index.prefix(first, 200), key=len):
            rest = lbl[len(first):].strip("-")
            rest_clean = re.sub(r"[-0-9]", "", rest)
            if rest == "" or rest in others or rest_clean in others or any(rest.startswith(o) for o in others if len(o) >= 4):
                add(lbl)
            if len(out) >= limit + 5:
                break
    return out[:limit]


_RESIDUAL_VERDICTS = {"mismatch", "dead", "parked", "redirect_offdomain", "no_impressum", "blocked", "plz", "robots"}


def accept(rec: dict, v: str, name: object, label: str) -> bool:
    """Acceptance for a DISCOVERED site (stricter than for a site a source already gave us).
    ``name+plz`` always. ``name`` only when the imprint's legal-name line names a registered
    company (HRA / HRB / GnR / PR legal form — clubs, foundations, city offices and slogans
    are out) whose distinctive tokens overlap the company name by at least half and cover
    the company's distinctive tokens; without a parsed legal name the host label must be the
    name key of a name with at least two distinctive tokens."""
    if v == "name+plz":
        return True
    if v != "name":
        return False
    toks = name_tokens(name)
    distinct = [t for t in toks if t not in impressum._HOST_GENERIC]
    ln = rec.get("imp_legal_name")
    if isinstance(ln, str) and ln:
        if register_division(legal_form_from_name(ln)) not in ("HRA", "HRB", "GnR", "PR"):
            return False
        a = set(distinct)
        b = {t for t in name_tokens(ln) if t not in impressum._HOST_GENERIC}
        if not a or not b:
            return False
        shared = a & b
        return len(shared) >= min(2, len(a)) and len(shared) / len(b) >= 0.5
    return len(distinct) >= 2 and label in ("".join(toks), "-".join(toks))


def match(data_root: Path, scope: str, industrial_only: bool = True, limit: int | None = None,
          workers: int = 8, dry_run: bool = False) -> pd.DataFrame:
    import geopandas as gpd

    from .. import export
    gdf = gpd.read_parquet(paths.merged_parquet(data_root, scope, "4326"))
    impressum.ensure_columns(gdf)
    prev = gdf["website_source"] == "cc"                    # idempotent: an earlier run of this route is redone
    if prev.any():
        for c in ["website", "website_host", "website_kind", "website_source", "website_verified", "website_verified_at",
                  "legal_name", "website_replaced", "website_replaced_source"] + impressum.EXTRA_COLUMNS:
            gdf.loc[prev, c] = pd.NA
        print(f"[ccindex] reset {int(prev.sum())} rows of an earlier run")
    verified = gdf["website_verified"].isin(["name+plz", "name"])
    need = gdf["name"].notna() & ~verified & (gdf["website"].isna() | gdf["website_verified"].isin(_RESIDUAL_VERDICTS))
    if industrial_only:
        need &= gdf["is_industrial"].fillna(False)
    rows = gdf[need]
    if limit:
        rows = rows.head(limit)
    t0 = time.time()
    index = DomainIndex.load(data_root)
    cand = {i: candidates(r["name"], r.get("legal_form"), index, r.get("address_city")) for i, r in rows.iterrows()}
    hosts = sorted({lbl + ".de" for hs in cand.values() for lbl in hs})
    n_with = sum(1 for hs in cand.values() if hs)
    print(f"[ccindex] {scope}: {len(rows)} rows without a verified site, {n_with} with candidates, "
          f"{len(hosts)} candidate hosts ({len(index.labels)} .de labels, {time.time() - t0:.0f} s)")
    if dry_run:
        for i, hs in list(cand.items())[:40]:
            if hs:
                print(f"  {str(rows.at[i, 'name'])[:40]:40} → {hs}")
        return rows
    recs = impressum.fetch_hosts(data_root, hosts, workers=workers, tag="ccindex")
    today = time.strftime("%Y-%m-%d")
    rank = {"name+plz": 0, "name": 1}
    n_fill, verdicts = 0, {}
    for i, hs in cand.items():
        best = None
        for lbl in hs:
            rec = recs.get(lbl + ".de")
            if rec is None:
                continue
            v = impressum.verdict(rec, rows.at[i, "name"], rows.at[i, "address_postcode"])
            verdicts[v] = verdicts.get(v, 0) + 1
            if v in rank and accept(rec, v, rows.at[i, "name"], lbl) and (best is None or rank[v] < rank[best[1]]):
                best = (lbl + ".de", v, rec)
        if best is None:
            continue
        host, v, rec = best
        n_fill += 1
        impressum.keep_replaced(gdf, i)
        gdf.at[i, "website"] = f"https://{host}"
        gdf.at[i, "website_host"] = host
        gdf.at[i, "website_kind"] = "own"
        gdf.at[i, "website_source"] = "cc"
        impressum.write_row(gdf, i, rec, v, today)
    print(f"[ccindex] candidate verdicts {verdicts}; verified sites written: {n_fill} of {len(rows)} rows "
          f"({time.time() - t0:.0f} s)")
    for p in [*export.write_merged(gdf, data_root, scope), export.write_summary(gdf, data_root, scope, None, {})]:
        print(f"[out] {p}")
    return gdf


__all__ = ["DomainIndex", "build", "candidates", "match"]
_ = config
