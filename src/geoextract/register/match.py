"""Stage 2 (probabilistic part) — join the merged Company table to the register tables.

For every NAMED map row, find the register company it most likely is, using the item 5
name-key ensemble (full + light key, rapidfuzz token_sort_ratio, best of both) inside two
blocking schemes:

* postcode block — map ``address_postcode`` = register ``postcode`` (all register sources
  that carry a postcode: hr2022 93 %, gleif 100 %, hr2019 25 %);
* city block — for rows where either side lacks a postcode: same normalised city AND same
  first token of the light name key (keeps Berlin-sized cities tractable).

Acceptance (item 5 rules): ratio ≥ 90 stands alone; 80–89 needs substance — equal street
keys, or ≥ 2 shared tokens, or shared tokens of ≥ 8 characters. Match types:
``name_plz_street`` > ``name_plz`` > ``name_city``. Best candidate per map row by score, then
street match, then active status, then source priority (hr2022, gleif, hr2019), then current
name; two different register companies within 5 points and no street decider → ``ambiguous``
(both kept in ``hr_candidates``). A register company may match several map rows (branches);
one-to-one is NOT enforced. ``exact_hrb`` (Impressum register number) is reserved for stage 1.

Written back (additive): ``register_match``, ``register_score``, ``hr_id``, ``hr_source``,
``hr_name`` (register legal name), ``hr_status`` (active | dissolved | unknown),
``hr_dissolved_date``, ``hr_snapshot_date``, ``hr_objective`` (Unternehmensgegenstand),
``hr_capital``, ``hr_candidates`` (debug JSON); contract columns ``legal_form``,
``hr_registration`` ("HRB 12345", normalised) and ``hr_court`` are filled where empty.
A non-match is never proof of non-existence.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from .. import config
from ..resolve import normalize_name, normalize_street
from . import build as register_build
from .legalform import legal_form_from_name

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_CITY_NOISE = {"hansestadt", "freie", "stadt", "landeshauptstadt", "am", "an", "der", "im", "bei"}
SOURCE_PRIORITY = {"hr2022": 0, "gleif": 1, "hr2019": 2}
MATCH_RANK = {"name_plz_street": 0, "name_plz": 1, "name_city": 2}
_REG_NO_RE = re.compile(r"\b(HRA|HRB|GnR|PR|VR|GsR)\s*(\d+(?:\s*[A-Za-z]{1,3})?)")


def normalize_city(city: object) -> str:
    """"Freie Hansestadt Bremen" → "bremen"; "Bremerhaven" → "bremerhaven"."""
    if city is None or (isinstance(city, float) and math.isnan(city)):
        return ""
    text = _NON_ALNUM.sub(" ", str(city).lower().translate(_UMLAUTS))
    toks = [t for t in text.split() if t not in _CITY_NOISE]
    return toks[0] if toks else ""


def normalize_registration(text: object) -> str | None:
    """"541657" (MaStR) / "HRB 541657" / "Bremen HRB 541657 HB" → "HRB 541657 HB"; bare number kept."""
    if text is None or (isinstance(text, float) and math.isnan(text)):
        return None
    s = str(text).strip()
    if not s:
        return None
    m = _REG_NO_RE.search(s)
    if m:
        return f"{m.group(1)} {' '.join(m.group(2).split())}"
    return s


def _substance(a: str, b: str, idf: dict[str, float]) -> bool:
    """80–89 pairs need a DISTINCTIVE shared token: the resolve stage's "≥ 8 shared characters"
    rule worked within 50 m, but without spatial proximity place names ("worpswede") and
    trade words ("rechtsanwaltsgesellschaft") produce false joins. Token rarity in the
    register names of the scope (inverse document frequency) decides instead: one shared
    token with idf ≥ REGISTER_IDF_RARE, or two shared tokens with idf ≥ REGISTER_IDF_COMMON."""
    shared = set(a.split()) & set(b.split())
    if not shared:
        return False
    vals = sorted((idf.get(t, 0.0) for t in shared), reverse=True)
    return vals[0] >= config.REGISTER_IDF_RARE or (len(vals) >= 2 and vals[1] >= config.REGISTER_IDF_COMMON)


def token_idf(keys: pd.Series) -> dict[str, float]:
    """idf per token over the register name keys: log(N / df)."""
    n = max(len(keys), 1)
    df = pd.Series([t for k in keys for t in set(k.split())]).value_counts()
    return {t: float(np.log(n / c)) for t, c in df.items()}


def _score_block(map_full, map_light, reg_full, reg_light, cutoff: float) -> np.ndarray:
    """Best-of-both-keys token_sort_ratio matrix (map rows × register rows)."""
    a = process.cdist(map_full, reg_full, scorer=fuzz.token_sort_ratio, score_cutoff=cutoff, workers=-1)
    b = process.cdist(map_light, reg_light, scorer=fuzz.token_sort_ratio, score_cutoff=cutoff, workers=-1)
    return np.maximum(a, b)


def _candidates_for_block(mi: np.ndarray, ri: np.ndarray, M: pd.DataFrame, R: pd.DataFrame,
                          kind: str, out: list, idf: dict[str, float]) -> None:
    """Append (map_idx, reg_idx, score, type) tuples for one block."""
    if not len(mi) or not len(ri):
        return
    scores = _score_block(M["kf"].values[mi].tolist(), M["kl"].values[mi].tolist(),
                          R["kf"].values[ri].tolist(), R["kl"].values[ri].tolist(),
                          config.DEDUP_NAME_RATIO)
    rows, cols = np.nonzero(scores >= config.DEDUP_NAME_RATIO)
    if not len(rows):
        return
    m_street = M["sk"].values[mi]
    r_street = R["sk"].values[ri]
    m_kf, r_kf, m_kl, r_kl = M["kf"].values[mi], R["kf"].values[ri], M["kl"].values[mi], R["kl"].values[ri]
    for r, c in zip(rows, cols):
        sc = float(scores[r, c])
        street = bool(m_street[r]) and m_street[r] == r_street[c]
        if sc < config.DEDUP_SUBSTANCE_MAX_RATIO and not (
                street or _substance(m_kf[r], r_kf[c], idf) or _substance(m_kl[r], r_kl[c], idf)):
            continue
        if kind == "city" and sc < config.DEDUP_SUBSTANCE_MAX_RATIO and not _substance(m_kf[r], r_kf[c], idf):
            continue   # city blocks have no postcode evidence: 80–89 needs the distinctive token
        mtype = "name_plz_street" if (kind == "plz" and street) else ("name_plz" if kind == "plz" else "name_city")
        out.append((int(mi[r]), int(ri[c]), sc, mtype))


def match_frame(map_df: pd.DataFrame, reg_names: pd.DataFrame, reg_companies: pd.DataFrame,
                ambiguity_window: float = 5.0) -> pd.DataFrame:
    """Pure function: map frame (needs id, name, address_postcode, address_city, district,
    address_street, address_housenumber) + register names/companies → per-map-row result
    frame indexed like map_df with the register columns."""
    t0 = time.time()
    M = pd.DataFrame(index=map_df.index)
    M["kf"] = map_df["name"].map(lambda n: normalize_name(n, strip_noise=True)).fillna("")
    M["kl"] = map_df["name"].map(lambda n: normalize_name(n, strip_noise=False)).fillna("")
    M["plz"] = map_df["address_postcode"].astype("string").fillna("")
    city_src = map_df["address_city"].astype("string")
    if "district" in map_df.columns:
        city_src = city_src.fillna(map_df["district"].astype("string"))
    M["city"] = city_src.map(normalize_city).fillna("")
    M["sk"] = [normalize_street(f"{s} {n}" if isinstance(n, str) and n else s)
               for s, n in zip(map_df.get("address_street", pd.Series(index=map_df.index, dtype="string")),
                               map_df.get("address_housenumber", pd.Series(index=map_df.index, dtype="string")))]
    named = (M["kf"] != "") | (M["kl"] != "")

    R = pd.DataFrame({
        "hr_id": reg_names["hr_id"].astype(str), "hr_source": reg_names["hr_source"].astype(str),
        "name": reg_names["name"].astype(str),
        "kf": reg_names["name_key_full"].fillna("").astype(str), "kl": reg_names["name_key_light"].fillna("").astype(str),
        "plz": reg_names["postcode"].astype("string").fillna("").astype(str),
        "city": reg_names["city"].map(normalize_city).fillna("").astype(str),
        "sk": reg_names["street_key"].fillna("").astype(str),
        "is_current": reg_names["is_current"].fillna(True).astype(bool),
    }).reset_index(drop=True)
    R = R[(R["kf"] != "") | (R["kl"] != "")].reset_index(drop=True)
    R["first"] = R["kl"].str.split().str[0].fillna("")
    M["first"] = M["kl"].str.split().str[0].fillna("")
    idf = token_idf(R["kf"])

    cands: list = []
    # postcode blocks
    m_idx = np.flatnonzero(named & (M["plz"] != ""))
    r_by_plz = {k: v.values for k, v in R[R["plz"] != ""].groupby("plz").groups.items()}
    m_by_plz = pd.Series(m_idx, index=m_idx).groupby(M["plz"].values[m_idx]).groups
    n_pairs = 0
    for plz, mi in m_by_plz.items():
        ri = r_by_plz.get(plz)
        if ri is None:
            continue
        mi = np.asarray(list(mi)); ri = np.asarray(ri)
        n_pairs += len(mi) * len(ri)
        _candidates_for_block(mi, ri, M, R, "plz", cands, idf)
    # city blocks (either side without postcode), sub-blocked on the first light-key token
    m_nop = np.flatnonzero(named & (M["plz"] == "") & (M["city"] != ""))
    r_nop = R[(R["plz"] == "") & (R["city"] != "")]
    m_all_city = np.flatnonzero(named & (M["city"] != ""))
    if len(r_nop):
        r_groups = {k: np.asarray(v) for k, v in r_nop.groupby(["city", "first"]).groups.items()}
        for (city, first), mi in pd.Series(m_all_city, index=m_all_city).groupby([M["city"].values[m_all_city], M["first"].values[m_all_city]]).groups.items():
            ri = r_groups.get((city, first))
            if ri is None:
                continue
            mi = np.asarray(list(mi))
            n_pairs += len(mi) * len(ri)
            _candidates_for_block(mi, ri, M, R, "city", cands, idf)
    if len(m_nop):
        r_with = R[(R["plz"] != "") & (R["city"] != "")]
        r_groups = {k: np.asarray(v) for k, v in r_with.groupby(["city", "first"]).groups.items()}
        for (city, first), mi in pd.Series(m_nop, index=m_nop).groupby([M["city"].values[m_nop], M["first"].values[m_nop]]).groups.items():
            ri = r_groups.get((city, first))
            if ri is None:
                continue
            mi = np.asarray(list(mi))
            n_pairs += len(mi) * len(ri)
            _candidates_for_block(mi, ri, M, R, "city", cands, idf)

    comp = reg_companies.set_index(["hr_source", "hr_id"])
    result = pd.DataFrame(index=map_df.index)
    result["register_match"] = np.where(named, "none", "n/a")
    for c in ("register_score", "hr_id", "hr_source", "hr_name", "hr_matched_name", "hr_status", "hr_dissolved_date",
              "hr_snapshot_date", "hr_objective", "hr_capital", "hr_candidates",
              "_legal_form", "_hr_registration", "_hr_court"):
        result[c] = pd.NA
    if not cands:
        print(f"[register] no candidates ({n_pairs} pairs scored, {time.time() - t0:.0f} s)")
        return result
    C = pd.DataFrame(cands, columns=["mi", "ri", "score", "mtype"])
    C["hr_id"] = R["hr_id"].values[C["ri"]]
    C["hr_source"] = R["hr_source"].values[C["ri"]]
    C["is_current"] = R["is_current"].values[C["ri"]]
    C["matched_key"] = R["kf"].values[C["ri"]]
    keyed = comp.reindex(list(zip(C["hr_source"], C["hr_id"])))
    C["status"] = keyed["status"].values
    C["legal_form"] = keyed["legal_form"].values
    C["matched_name"] = R["name"].values[C["ri"]]
    C["mrank"] = C["mtype"].map(MATCH_RANK)
    C["srank"] = C["hr_source"].map(SOURCE_PRIORITY).fillna(9)
    C["active"] = (C["status"] == "active").astype(int)
    # one row per (map row, register company): keep its best name variant
    map_lf = map_df["name"].map(legal_form_from_name)
    C["lf_match"] = [int(isinstance(lf, str) and lf == map_lf.iloc[m]) for lf, m in zip(C["legal_form"], C["mi"])]
    C = C.sort_values(["mi", "score", "lf_match", "mrank", "active", "srank"],
                      ascending=[True, False, False, True, False, True])
    C = C.drop_duplicates(["mi", "hr_source", "hr_id"], keep="first")
    n_match = n_amb = 0
    for mi, grp in C.groupby("mi", sort=False):
        top = grp.iloc[0]
        amb = False
        if len(grp) > 1:
            second = grp.iloc[1]
            lf_a, lf_b = top["legal_form"], second["legal_form"]
            same_form = pd.isna(lf_a) or pd.isna(lf_b) or bool(lf_a == lf_b)
            same_company = second["matched_key"] == top["matched_key"] and same_form   # same firm, other source
            exact_top = top["score"] >= 100 > second["score"]          # an exact key beats near misses
            lf_decides = top["lf_match"] and not second["lf_match"]
            if (second["hr_id"] != top["hr_id"] and not same_company and not exact_top and not lf_decides
                    and top["score"] - second["score"] < ambiguity_window
                    and top["mrank"] == second["mrank"] and top["active"] == second["active"]):
                amb = True
        idx = map_df.index[mi]
        rec = comp.loc[(top["hr_source"], top["hr_id"])]
        if amb:
            n_amb += 1
            result.at[idx, "register_match"] = "ambiguous"
            result.at[idx, "hr_candidates"] = json.dumps([
                {"hr_id": r["hr_id"], "source": r["hr_source"], "score": r["score"], "type": r["mtype"],
                 "name": comp.loc[(r["hr_source"], r["hr_id"])]["name"], "status": r["status"]}
                for _, r in grp.head(3).iterrows()], ensure_ascii=False, default=str)
            continue
        n_match += 1
        result.at[idx, "register_match"] = top["mtype"]
        result.at[idx, "register_score"] = float(top["score"])
        result.at[idx, "hr_id"] = top["hr_id"]
        result.at[idx, "hr_source"] = top["hr_source"]
        result.at[idx, "hr_name"] = rec["name"]
        result.at[idx, "hr_matched_name"] = top["matched_name"]
        result.at[idx, "hr_status"] = rec["status"]
        result.at[idx, "hr_dissolved_date"] = rec["dissolved"]
        result.at[idx, "hr_snapshot_date"] = rec["snapshot_date"]
        result.at[idx, "hr_objective"] = rec["objective"]
        result.at[idx, "hr_capital"] = rec["capital_amount"]
        result.at[idx, "_legal_form"] = rec["legal_form"]
        result.at[idx, "_hr_registration"] = rec["hr_registration"]
        result.at[idx, "_hr_court"] = rec["court"]
        if len(grp) > 1:
            result.at[idx, "hr_candidates"] = json.dumps([
                {"hr_id": r["hr_id"], "source": r["hr_source"], "score": r["score"], "type": r["mtype"]}
                for _, r in grp.head(3).iterrows()], ensure_ascii=False, default=str)
    print(f"[register] {int(named.sum())} named rows, {n_pairs} pairs scored, {len(C)} candidates, "
          f"{n_match} matched, {n_amb} ambiguous ({time.time() - t0:.0f} s)")
    return result


def apply_match(gdf: pd.DataFrame, result: pd.DataFrame) -> pd.DataFrame:
    """Write the result columns into the Company frame (additive; contract fields filled
    where empty; hr_registration normalised everywhere)."""
    gdf = gdf.copy()
    for c in ("register_match", "hr_id", "hr_source", "hr_name", "hr_matched_name", "hr_status",
              "hr_dissolved_date", "hr_snapshot_date", "hr_objective", "hr_candidates"):
        gdf[c] = result[c].astype("string")
    gdf["register_score"] = pd.to_numeric(result["register_score"], errors="coerce").astype("Float64")
    gdf["hr_capital"] = pd.to_numeric(result["hr_capital"], errors="coerce").astype("Float64")
    for col, src in (("legal_form", "_legal_form"), ("hr_court", "_hr_court")):
        cur = gdf[col].astype("string")
        new = result[src].astype("string")
        gdf[col] = cur.where(cur.notna() & (cur.str.strip() != ""), new)
    # hr_registration: keep a source value that already names the register (HRB …); a bare
    # number (MaStR "541657") is replaced by the matched register's full reference
    cur = gdf["hr_registration"].astype("string")
    has_prefix = cur.notna() & cur.str.contains(r"\b(?:HRA|HRB|GnR|PR|VR|GsR)\s*\d", regex=True, na=False)
    new = result["_hr_registration"].astype("string")
    gdf["hr_registration"] = cur.where(has_prefix | new.isna(), new)
    gdf["hr_registration"] = gdf["hr_registration"].map(normalize_registration, na_action="ignore").astype("string")
    matched = gdf["register_match"].isin(list(MATCH_RANK) + ["exact_hrb"])
    # confidence: matched active +0.10, matched dissolved −0.10 (spec §5.4 amendment, clipped 0–1)
    conf = pd.to_numeric(gdf["confidence_score"], errors="coerce").astype(float)
    conf = conf + np.where(matched & (gdf["hr_status"] == "active"), 0.10, 0.0) \
                - np.where(matched & (gdf["hr_status"] == "dissolved"), 0.10, 0.0)
    gdf["confidence_score"] = pd.Series(conf, index=gdf.index).clip(0, 1).round(2).astype("Float64")
    return gdf


def run(data_root: Path, scope: str, sources: list[str] | None = None) -> pd.DataFrame:
    """Load merged table + register tables for ``scope``, match, write back (4326/3857/summary)."""
    import geopandas as gpd

    from .. import export, paths
    src = paths.merged_parquet(data_root, scope, "4326")
    gdf = gpd.read_parquet(src)
    names = register_build.load_names(data_root, sources)
    companies = register_build.load_companies(data_root, sources)
    if names.empty:
        raise SystemExit("no register tables — run `geoextract register build` first")
    # restrict the register side to the scope's postcodes / cities (blocking does the rest)
    plz = set(gdf["address_postcode"].dropna().astype(str))
    cities = set(gdf["address_city"].dropna().map(normalize_city)) | set(gdf["district"].dropna().map(normalize_city))
    keep = names["postcode"].astype("string").isin(plz) | names["city"].map(normalize_city).isin(cities)
    names = names[keep.fillna(False)].reset_index(drop=True)
    print(f"[register] scope {scope}: {len(gdf)} map rows, {len(names)} register name variants in scope "
          f"({names['hr_source'].value_counts().to_dict()})")
    result = match_frame(gdf, names, companies)
    gdf = apply_match(gdf, result)
    rep = gdf["register_match"].value_counts(dropna=False).to_dict()
    print(f"[register] register_match: {rep}; industrial matched: "
          f"{int((gdf['is_industrial'].fillna(False) & gdf['hr_id'].notna()).sum())} of {int(gdf['is_industrial'].fillna(False).sum())}")
    for p in [*export.write_merged(gdf, data_root, scope),
              export.write_summary(gdf, data_root, scope, None, {})]:
        print(f"[out] {p}")
    return gdf
