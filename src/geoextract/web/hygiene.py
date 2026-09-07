"""Stage 0d — website hygiene on the merged Company table (no network).

The contract column ``website`` is the classifier seam (spec §3): after this stage it holds
only URLs that are the company's OWN site or the site of the chain it belongs to. Everything
else — business directories (gelbeseiten.de: 104 903 DE rows), social profiles, parcel-network
listings — moves to the debug column ``website_listing`` so it is never scraped for NACE
evidence but stays available. Every row gets:

``website``          normalised own/chain URL or NA
``website_kind``     own | chain | directory | social | parcel | NA (no URL at all)
``website_host``     registered host without "www."
``website_listing``  the original URL when it was moved out of ``website``
``website_source``   extract (came with the source data) | propagated (copied from a row with
                     the same name key + postcode) — later stages add guess / cc / search

Host classes: seeded lists in config plus two learned rules on the table itself:
a host shared by ≥ WEBSITE_CHAIN_MIN_ROWS rows is a chain; it is a directory instead when it
has ≥ WEBSITE_DIRECTORY_MIN_ROWS rows, almost every row a different name, and the host's
brand label appears in few of those names (CBS rule, van Delden 2019 §4.2: a domain returned
for many distinct units is a directory). Idempotent: re-running on its own output is a no-op.
"""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

from .. import config
from ..resolve import normalize_name

_SPLIT_RE = re.compile(r"[;,\s]+")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HOST_OK_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")

KINDS_KEPT = ("own", "chain")


def normalize_url(raw: object) -> str | None:
    """First URL of a possibly multi-valued field, lower-cased host, scheme added,
    fragment and tracking parameters dropped, trailing slash removed. None if unusable."""
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = _SPLIT_RE.split(text)[0]
    if not _SCHEME_RE.match(text):
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = parts.hostname
    if not host or not _HOST_OK_RE.match(host.lower()):
        return None
    scheme = parts.scheme.lower() if parts.scheme.lower() in ("http", "https") else "https"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(config.WEBSITE_TRACKING_PARAMS)]
    path = parts.path.rstrip("/") or ""
    netloc = host.lower() + (f":{parts.port}" if parts.port and parts.port not in (80, 443) else "")
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def host_of(url: object) -> str | None:
    """Registered host without a leading "www."; None if the URL is unusable."""
    if url is None or (isinstance(url, float) and math.isnan(url)):
        return None
    try:
        host = urlsplit(str(url)).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    return host.removeprefix("www.")


def _matches(host: str, hosts: set[str]) -> bool:
    """host or any parent domain is in the set (maps.google.com → google.com), unless the
    host itself is a known own-site exception (sites.google.com)."""
    if host in config.WEBSITE_OWN_HOST_EXCEPTIONS:
        return False
    parts = host.split(".")
    return any(".".join(parts[i:]) in hosts for i in range(len(parts) - 1))


def _brand_tokens(host: str) -> list[str]:
    """Brand tokens of a host: the labels below the public suffix, split at hyphens, minus
    generic words (huk-vor-ort.de → ["huk"]; filialen.aldi-sued.de → ["aldi", "sued"];
    hotel-bb.com → ["bb"])."""
    parts = host.split(".")
    labels = parts[:-1] if len(parts) >= 2 else parts
    if len(labels) >= 2 and labels[-1] in ("co", "com", "org", "net"):   # example.co.uk
        labels = labels[:-1]
    toks = []
    for label in labels:
        for tok in re.split(r"[-_0-9]+", label):
            if len(tok) >= 2 and tok not in config.WEBSITE_BRAND_STOP_TOKENS:
                toks.append(tok)
    return toks


def _brand_share(names: pd.Series, host: str) -> float:
    """Share of rows whose (raw, lower-cased, umlaut-folded) name contains a brand token."""
    toks = _brand_tokens(host)
    if not toks:
        return 0.0
    folded = (names.fillna("").astype(str).str.lower()
              .str.translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
              .str.replace(r"[^a-z0-9]", "", regex=True))
    hit = pd.Series(False, index=names.index)
    for tok in toks:
        hit |= folded.str.contains(tok, regex=False)
    return float(hit.mean()) if len(hit) else 0.0


def classify_hosts(hosts: pd.Series, names: pd.Series) -> pd.Series:
    """website_kind per row from seeded lists + the learned chain/directory rules."""
    hosts = hosts.astype("string")
    kind = pd.Series(pd.NA, index=hosts.index, dtype="string")
    valid = hosts.notna()
    kind[valid] = "own"
    for label, seeded in (("directory", config.WEBSITE_DIRECTORY_HOSTS),
                          ("social", config.WEBSITE_SOCIAL_HOSTS),
                          ("parcel", config.WEBSITE_PARCEL_HOSTS),
                          ("chain", config.WEBSITE_CHAIN_HOSTS)):
        hit = valid & hosts.map(lambda h, s=seeded: bool(h) and _matches(h, s), na_action="ignore").fillna(False).astype(bool)
        kind[hit] = label

    # learned: shared hosts (own-site exceptions such as sites.google.com never count)
    undecided = valid & (kind == "own") & ~hosts.isin(config.WEBSITE_OWN_HOST_EXCEPTIONS).fillna(False)
    counts = hosts[undecided].value_counts()
    shared = counts[counts >= config.WEBSITE_CHAIN_MIN_ROWS]
    if len(shared):
        keys = names.map(lambda n: normalize_name(n, strip_noise=True)).astype("string")
        sub = pd.DataFrame({"host": hosts[undecided], "key": keys[undecided],
                            "raw": names[undecided]})
        sub = sub[sub["host"].isin(shared.index)]
        stats = sub.groupby("host").agg(rows=("key", "size"), distinct=("key", "nunique"))
        stats["brand_share"] = pd.Series(
            {host: _brand_share(grp["raw"], host) for host, grp in sub.groupby("host")})
        is_dir = ((stats["rows"] >= config.WEBSITE_DIRECTORY_MIN_ROWS)
                  & (stats["distinct"] / stats["rows"] >= config.WEBSITE_DIRECTORY_NAME_RATIO)
                  & (stats["brand_share"] <= config.WEBSITE_DIRECTORY_MAX_BRAND_SHARE))
        dir_hosts = set(stats.index[is_dir])
        chain_hosts = set(stats.index[~is_dir])
        kind[undecided & hosts.isin(dir_hosts)] = "directory"
        kind[undecided & hosts.isin(chain_hosts)] = "chain"
    return kind


def apply_hygiene(df: pd.DataFrame, propagate: bool = True) -> pd.DataFrame:
    """Stage 0d on a Company frame (merged table or a single source). Returns a copy."""
    df = df.copy()
    already = "website_listing" in df.columns
    original = df["website"].astype("string")
    if already:   # re-run: consider the listing column too, so the pass stays idempotent
        original = original.where(original.notna(), df["website_listing"].astype("string"))
    norm = original.map(normalize_url, na_action="ignore").astype("string")
    hosts = norm.map(host_of, na_action="ignore").astype("string")
    kind = classify_hosts(hosts, df["name"] if "name" in df.columns else pd.Series("", index=df.index))

    keep = kind.isin(KINDS_KEPT).fillna(False)
    df["website"] = norm.where(keep, pd.NA).astype("string")
    df["website_kind"] = kind
    df["website_host"] = hosts
    df["website_listing"] = original.where(~keep & norm.notna(), pd.NA).astype("string")
    src = pd.Series(pd.NA, index=df.index, dtype="string")
    src[keep] = "extract"
    if "website_source" in df.columns:   # keep richer provenance from later stages
        prev = df["website_source"].astype("string")
        src = src.where(~(keep & prev.notna() & (prev != "extract")), prev)
    df["website_source"] = src

    if propagate and "name" in df.columns and "address_postcode" in df.columns:
        key = df["name"].map(lambda n: normalize_name(n, strip_noise=True)).astype("string")
        plz = df["address_postcode"].astype("string")
        ok = keep & key.notna() & (key != "") & plz.notna()
        donors = (pd.DataFrame({"key": key[ok], "plz": plz[ok], "url": df.loc[ok, "website"],
                                "host": hosts[ok], "kind": kind[ok]})
                  .drop_duplicates(["key", "plz"]))
        need = df["website"].isna() & key.notna() & (key != "") & plz.notna()
        cand = (pd.DataFrame({"key": key[need], "plz": plz[need]})
                .reset_index().merge(donors, on=["key", "plz"], how="inner").set_index("index"))
        if len(cand):
            df.loc[cand.index, "website"] = cand["url"].astype("string")
            df.loc[cand.index, "website_host"] = cand["host"].astype("string")
            df.loc[cand.index, "website_kind"] = cand["kind"].astype("string")
            df.loc[cand.index, "website_source"] = "propagated"
    return df


def hygiene_report(df: pd.DataFrame, top: int = 25) -> dict:
    """Counts for the summary JSON / console: kinds, learned hosts, propagation."""
    kinds = df["website_kind"].value_counts(dropna=False).to_dict()
    learned = df[df["website_kind"].isin(["directory", "chain"])]
    hosts = (learned.groupby(["website_kind", "website_host"]).size()
             .sort_values(ascending=False).groupby(level=0).head(top))
    return {
        "website_kind": {str(k): int(v) for k, v in kinds.items()},
        "website_kept": int(df["website"].notna().sum()),
        "website_listing": int(df["website_listing"].notna().sum()),
        "website_propagated": int((df["website_source"] == "propagated").sum()),
        "top_hosts": {f"{k}:{h}": int(n) for (k, h), n in hosts.items()},
    }
