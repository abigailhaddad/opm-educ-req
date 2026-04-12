"""
Join the OPM education-tier classifier (opm_series_tiers.json) against the
historical USAJOBS postings published by /Users/abigailhaddad/Documents/repos/
usajobs_historical to Cloudflare R2, and report the share of postings whose
occupational series maps to mandatory / optional / none.

Rules:
- Any series not present in opm_series_tiers.json → 'none' (per user: if OPM
  didn't publish a standard, there's no detected education requirement).
- A posting can list multiple occupational series. We keep the FULL SET of
  tiers the posting spans as its combo label, e.g. a posting listing 1560
  (mandatory) and 2210 (optional) is bucketed as "mandatory & optional" —
  not forced into a single tier. Single-series postings end up in the
  plain "mandatory" / "optional" / "none" buckets.
- We also report a "primary series" view (the first series listed on the
  posting) and an "any mandatory / any optional" view so you can slice
  whichever way is useful.
- Dedup is whatever lives in jobs_5yr.parquet (deduplicated by
  usajobsControlNumber per prep_web_data.py line 277).

Usage:
    python analyze_usajobs.py                 # uses cache if present
    python analyze_usajobs.py --refresh       # re-download the parquet
    python analyze_usajobs.py --since 2022    # restrict to openDate >= year

Requires: pandas, pyarrow, the tiers JSON from scrape_opm.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
TIERS_PATH = HERE / "opm_series_tiers.json"
PARQUET_URL = "https://pub-317c58882ec04f329b63842c1eb65b0c.r2.dev/web/jobs_5yr.parquet"
PARQUET_CACHE = HERE / "cache" / "jobs_5yr.parquet"

TIER_RANK = {"none": 0, "optional": 1, "mandatory": 2}
TIER_NAME = {0: "none", 1: "optional", 2: "mandatory"}
# 4-category system for display
FOUR_CATS = ("mandatory_professional", "mandatory_qualification", "optional", "none")
CAT_SHORT = {
    "mandatory_professional": "mand_prof",
    "mandatory_qualification": "mand_qual",
    "optional": "optional",
    "none": "none",
}

RE_SERIES_CODE = re.compile(r"\b(\d{4})\b")


def combo_label(cats: list[str]) -> str:
    """Return a stable label for the set of 4-cats a posting spans."""
    if not cats:
        return "none"
    present = [c for c in FOUR_CATS if c in cats]
    return " & ".join(present) if present else "none"


def load_tier_map() -> tuple[dict[str, str], dict[str, str]]:
    """Return ({series_num: tier}, {series_num: mandatory_type}).

    tier is one of: mandatory, optional, none.
    mandatory_type is 'professional' or 'qualification' (only for mandatory).
    """
    rows = json.loads(TIERS_PATH.read_text())
    tier_map: dict[str, str] = {}
    mtype_map: dict[str, str] = {}
    for r in rows:
        num = (r.get("series_num") or "").strip()
        if not num:
            continue
        num = num.zfill(4)
        tier_map[num] = r["tier"]
        mtype_map[num] = r.get("mandatory_type", "")
    return tier_map, mtype_map


def download_parquet(refresh: bool) -> Path:
    PARQUET_CACHE.parent.mkdir(exist_ok=True)
    if PARQUET_CACHE.exists() and not refresh:
        print(f"Using cached {PARQUET_CACHE} "
              f"({PARQUET_CACHE.stat().st_size / 1e6:.1f} MB)")
        return PARQUET_CACHE
    print(f"Downloading {PARQUET_URL} …")
    req = urllib.request.Request(
        PARQUET_URL,
        headers={"User-Agent": "opm-educ-req analysis/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        size = int(resp.headers.get("Content-Length", 0))
        size_mb = size / 1e6 if size else 0
        done = 0
        with PARQUET_CACHE.open("wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if size:
                    pct = done / size * 100
                    print(
                        f"\r  {done / 1e6:6.1f} / {size_mb:6.1f} MB ({pct:5.1f}%)",
                        end="",
                        flush=True,
                    )
    print()
    return PARQUET_CACHE


def extract_series_codes(series_field) -> list[str]:
    """Pull 4-digit codes out of the resolved `occupationalSeries` string.

    The web parquet stores it semicolon-joined like:
        "2210 - Information Technology Specialist; 0301 - Miscellaneous Admin"
    so we just grep every 4-digit token. Returns them in original order.
    """
    if series_field is None:
        return []
    if isinstance(series_field, float) and pd.isna(series_field):
        return []
    s = str(series_field)
    seen = set()
    out: list[str] = []
    for m in RE_SERIES_CODE.findall(s):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _code_to_cat(
    code: str,
    tier_map: dict[str, str],
    mtype_map: dict[str, str],
) -> str:
    """Map a single series code to one of the 4 categories."""
    tier = tier_map.get(code, "none")
    if tier == "mandatory":
        mtype = mtype_map.get(code, "qualification")
        return f"mandatory_{mtype}" if mtype else "mandatory_qualification"
    return tier


def classify_row(
    codes: list[str],
    tier_map: dict[str, str],
    mtype_map: dict[str, str],
) -> tuple[str, str]:
    """Return (combo, primary) for a job row."""
    if not codes:
        return "none", "none"
    cats = [_code_to_cat(c, tier_map, mtype_map) for c in codes]
    return combo_label(cats), cats[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true", help="Re-download parquet")
    ap.add_argument(
        "--since",
        type=int,
        default=0,
        help="Restrict to jobs with openDate year >= this (e.g. 2022)",
    )
    args = ap.parse_args()

    tier_map, mtype_map = load_tier_map()
    print(f"Loaded {len(tier_map)} series → tier from {TIERS_PATH.name}")
    print(f"  mandatory: {sum(1 for t in tier_map.values() if t == 'mandatory')}")
    print(f"    professional:  {sum(1 for s,t in tier_map.items() if t == 'mandatory' and mtype_map.get(s) == 'professional')}")
    print(f"    qualification: {sum(1 for s,t in tier_map.items() if t == 'mandatory' and mtype_map.get(s) == 'qualification')}")
    print(f"  optional:  {sum(1 for t in tier_map.values() if t == 'optional')}")
    print(f"  none:      {sum(1 for t in tier_map.values() if t == 'none')}")
    print()

    parquet = download_parquet(args.refresh)

    print("Loading parquet…")
    df = pd.read_parquet(
        parquet,
        columns=["usajobsControlNumber", "occupationalSeries", "openDate",
                 "hiringDepartmentName"],
    )
    print(f"  {len(df):,} rows")

    if args.since:
        df["_year"] = pd.to_datetime(df["openDate"], errors="coerce").dt.year
        before = len(df)
        df = df[df["_year"] >= args.since].copy()
        print(f"  {len(df):,} rows with openDate year >= {args.since} "
              f"(dropped {before - len(df):,})")
    else:
        df["_year"] = pd.to_datetime(df["openDate"], errors="coerce").dt.year

    # Classify each job
    print("Classifying…")
    codes_list = df["occupationalSeries"].map(extract_series_codes)
    df["_codes"] = codes_list
    classifications = [
        classify_row(c, tier_map, mtype_map) for c in codes_list
    ]
    df["_combo"] = [c[0] for c in classifications]
    df["_primary"] = [c[1] for c in classifications]
    df["_n_series"] = codes_list.map(len)

    total = len(df)

    # -------- 4-category counts -----------------------------------------
    combo_counts = Counter(df["_combo"])
    singletons = list(FOUR_CATS)
    mixed_order = sorted(
        (c for c in combo_counts if c not in singletons),
        key=lambda c: (-combo_counts[c], c),
    )
    display_order = singletons + mixed_order

    print()
    print(f"=== 4-category breakdown ({total:,} postings)")
    print(f"  {'category':50s} {'count':>12s}  {'share':>7s}")
    for cat in display_order:
        n = combo_counts.get(cat, 0)
        if not n:
            continue
        pct = n / total * 100
        print(f"  {cat:50s} {n:>12,}  {pct:5.0f}%")

    # -------- by year table (the main deliverable) --------
    print()
    yrs = sorted(int(y) for y in df["_year"].dropna().unique())
    # Only show columns that have counts
    active_cats = [c for c in display_order if combo_counts.get(c, 0) > 0]

    print("=== By year (4-category, primary series)")
    print()
    primary_counts = Counter(df["_primary"])
    active_primary = [c for c in FOUR_CATS if primary_counts.get(c, 0) > 0]
    hdr = f"{'year':>6s}  {'total':>9s}"
    for cat in active_primary:
        hdr += f"  {CAT_SHORT.get(cat, cat):>12s}"
    print(hdr)
    for y in yrs:
        sub = df[df["_year"] == y]
        n = len(sub)
        row = f"{y:>6d}  {n:>9,}"
        for cat in active_primary:
            c = int((sub["_primary"] == cat).sum())
            pct = c / n * 100 if n else 0
            row += f"  {c:>7,} {pct:3.0f}%"
        print(row)

    print()
    print("=== By year (4-category, combo — multi-series postings keep all tiers)")
    print()
    hdr = f"{'year':>6s}  {'total':>9s}"
    for cat in active_cats:
        short = CAT_SHORT.get(cat, cat[:12])
        hdr += f"  {short:>12s}"
    print(hdr)
    for y in yrs:
        sub = df[df["_year"] == y]
        n = len(sub)
        row = f"{y:>6d}  {n:>9,}"
        for cat in active_cats:
            c = int((sub["_combo"] == cat).sum())
            row += f"  {c:>12,}"
        print(row)

    # -------- sanity / diagnostics --------
    print()
    print("=== Diagnostics")
    no_series = int(df["_n_series"].eq(0).sum())
    print(f"  postings with no parseable series code: {no_series:,} "
          f"({no_series / total * 100:.2f}%)")
    multi = int(df["_n_series"].ge(2).sum())
    print(f"  postings listing 2+ series:             {multi:,} "
          f"({multi / total * 100:.2f}%)")

    # Top 15 series by posting volume, with their tier
    print()
    print("=== Top 20 series by posting volume")
    flat = [c for codes in codes_list for c in codes]
    top = Counter(flat).most_common(20)
    print(f"  {'series':>6s}  {'postings':>10s}  {'tier':10s}")
    for code, n in top:
        tier = tier_map.get(code, "none")
        print(f"  {code:>6s}  {n:>10,}  {tier:10s}")

    # Which "mystery" series (not in our tier map) drive the bulk of 'none'?
    print()
    print("=== Top 15 series NOT in opm_series_tiers.json (default → 'none')")
    missing = Counter(c for c in flat if c not in tier_map).most_common(15)
    print(f"  {'series':>6s}  {'postings':>10s}")
    for code, n in missing:
        print(f"  {code:>6s}  {n:>10,}")

    # -------- save JSON summary --------
    summary_path = HERE / "usajobs_tier_share.json"
    payload = {
        "total_postings": total,
        "since_year": args.since or None,
        "combo_counts": {
            c: int(combo_counts.get(c, 0))
            for c in display_order
            if combo_counts.get(c, 0) > 0
        },
        "primary_counts": {
            c: int((df["_primary"] == c).sum())
            for c in FOUR_CATS
        },
        "by_year_primary": [
            {
                "year": int(y),
                "total": int(len(df[df["_year"] == y])),
                **{
                    cat: int(
                        ((df["_year"] == y) & (df["_primary"] == cat)).sum()
                    )
                    for cat in FOUR_CATS
                },
            }
            for y in yrs
        ],
        "by_year_combo": [
            {
                "year": int(y),
                "total": int(len(df[df["_year"] == y])),
                **{
                    combo: int(
                        ((df["_year"] == y) & (df["_combo"] == combo)).sum()
                    )
                    for combo in display_order
                    if combo_counts.get(combo, 0) > 0
                },
            }
            for y in yrs
        ],
        "top_series_by_volume": [
            {"series": c, "postings": n, "tier": tier_map.get(c, "none"), "mandatory_type": mtype_map.get(c, "")}
            for c, n in top
        ],
        "top_missing_series": [
            {"series": c, "postings": n} for c, n in missing
        ],
    }
    summary_path.write_text(json.dumps(payload, indent=2))

    # -------- per-series posting counts for the crosswalk table ----------
    all_codes_flat = [c for codes in codes_list for c in codes]
    series_posting_counts = Counter(all_codes_flat)

    # Per-series-per-year counts (for Tab 2)
    print("Computing per-series-per-year counts…")
    series_year_rows = []
    yrs = sorted(int(y) for y in df["_year"].dropna().unique())
    # Explode codes per row, group by code+year
    exploded = []
    for codes, year in zip(codes_list, df["_year"]):
        if pd.isna(year):
            continue
        y = int(year)
        for c in codes:
            exploded.append((c, y))
    from collections import defaultdict
    sy_counts: dict[tuple[str, int], int] = Counter(exploded)
    # Build rows: one per series, with year columns
    all_series_codes = sorted(set(c for c, _ in sy_counts.keys()))
    for code in all_series_codes:
        cat = _code_to_cat(code, tier_map, mtype_map)
        total_for_series = sum(sy_counts.get((code, y), 0) for y in yrs)
        yr_data = {str(y): sy_counts.get((code, y), 0) for y in yrs}
        # Get title from crosswalk or handbook
        series_year_rows.append({
            "series_num": code,
            "category": cat,
            "total": total_for_series,
            **yr_data,
        })

    # -------- HTML report ------------------------------------------------
    import csv as _csv

    # Build crosswalk rows from the full CSV (has HTML text for modals)
    crosswalk_rows = []
    full_csv = HERE / "opm_education_requirements.csv"
    if full_csv.exists():
        with full_csv.open() as f:
            for row in _csv.DictReader(f):
                sn = row.get("series_num", "")
                if not sn:
                    continue
                crosswalk_rows.append({
                    "series_num": sn,
                    "series_title": row.get("series_title", ""),
                    "category": _code_to_cat(sn, tier_map, mtype_map),
                    "education_summary": row.get("education_summary", ""),
                    "postings": series_posting_counts.get(sn, 0),
                    "url": row.get("url", ""),
                    "effective_html": row.get("effective_html", ""),
                    "associated_group_name": row.get("associated_group_name", ""),
                    "associated_group_code": row.get("associated_group_code", ""),
                })

    # Load FWS PDF URLs
    fws_pdf_path = HERE / "fws_pdf_urls.json"
    fws_pdfs = json.loads(fws_pdf_path.read_text()) if fws_pdf_path.exists() else {}

    # Load handbook for proper titles on series not in our GS tiers
    handbook_path = HERE / "opm_handbook_series.json"
    handbook = {}
    if handbook_path.exists():
        hb = json.loads(handbook_path.read_text())
        handbook = hb.get("all", {})
        print(f"  Loaded {len(handbook)} series titles from handbook")

    # Add ALL missing series that appear in USAJOBS (not just top 30)
    missing_codes = Counter(c for c in all_codes_flat if c not in tier_map)
    for code, n in missing_codes.most_common():
        hb_entry = handbook.get(code, {})
        hb_title = hb_entry.get("title", "")
        hb_system = hb_entry.get("system", "")
        if hb_system == "FWS":
            title = f"{hb_title} (Federal Wage System)" if hb_title else "(FWS — title not in handbook)"
            summary = "No education requirement — Federal Wage System (trade/craft/labor)"
        elif hb_title:
            title = hb_title
            summary = "No OPM GS qualification standard published for this series"
        else:
            title = "(not in OPM handbook)"
            summary = "Series not found in OPM handbook or GS qualification standards"
        crosswalk_rows.append({
            "series_num": code,
            "series_title": title,
            "category": "none",
            "education_summary": summary,
            "postings": n,
            "url": fws_pdfs.get(code, ""),
            "effective_html": "",
            "associated_group_name": "",
            "associated_group_code": "",
            "system": hb_system or "unknown",
        })

    # Also add system tag to existing rows
    for cr in crosswalk_rows:
        if "system" not in cr:
            hb_entry = handbook.get(cr["series_num"], {})
            cr["system"] = hb_entry.get("system", "GS")

    # Add titles to series_year_rows from crosswalk
    title_lookup = {cr["series_num"]: cr["series_title"] for cr in crosswalk_rows}
    for syr in series_year_rows:
        syr["series_title"] = title_lookup.get(syr["series_num"], "")

    html_path = HERE / "analysis.html"
    html_path.write_text(
        render_analysis_html(payload, crosswalk_rows, series_year_rows, yrs),
        encoding="utf-8",
    )

    print()
    print(f"Wrote summary → {summary_path.name}")
    print(f"       website → {html_path.name}")
    return 0


def render_analysis_html(
    payload: dict,
    crosswalk: list[dict],
    series_year_rows: list[dict],
    yrs: list[int],
) -> str:
    import time as _time

    CAT_LABELS = {
        "mandatory_professional": "Mandatory: Professional",
        "mandatory_qualification": "Mandatory: Qualification",
        "optional": "Optional",
        "none": "None",
    }
    STAT_CLS = {
        "mandatory_professional": "mand-prof",
        "mandatory_qualification": "mand-qual",
        "optional": "opt",
        "none": "none-cat",
    }
    STAT_DESC = {
        "mandatory_professional": "Degree required for licensure (doctors, nurses, architects...)",
        "mandatory_qualification": "OPM requires a degree (economists, scientists, engineers...)",
        "optional": "Education is one qualifying path; experience also works",
        "none": "No published education requirement (excepted service, FWS trades)",
    }

    total = payload["total_postings"]
    pc = payload["primary_counts"]
    def pct(n):
        return f"{n/total*100:.0f}%" if total else "0%"

    CSS = r"""
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Source+Sans+3:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
    :root {
      --color-bg: #FFF8F0; --color-surface: #F5EDE0; --color-text: #3D2B1F;
      --color-text-muted: #7A6E62; --color-accent: #2D6A4F; --color-accent-hover: #245A42;
      --color-highlight: #D4A03C; --color-border: #E8DDD0;
      --font-heading: 'DM Sans', sans-serif; --font-body: 'Source Sans 3', sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
      --shadow-sm: 0 1px 3px rgba(61,43,31,0.08); --shadow-md: 0 4px 8px rgba(61,43,31,0.1);
      --card-radius: 0.5rem;
    }
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: var(--font-body); color: var(--color-text); background: var(--color-bg);
           line-height: 1.7; font-size: 1rem; margin: 0; }
    .site-container { max-width: 1200px; margin: 0 auto; padding: 0 1.5rem 3rem; }
    h1 { font-family: var(--font-heading); font-size: 2.2rem; font-weight: 700; margin: 0 0 .25rem; }
    h2 { font-family: var(--font-heading); font-size: 1.5rem; font-weight: 600; margin: 1.5rem 0 1rem; }
    h3 { font-family: var(--font-heading); font-size: 1.1rem; font-weight: 600; }
    a { color: var(--color-accent); text-decoration: underline;
        text-decoration-color: rgba(45,106,79,0.25); text-underline-offset: 2px; }
    a:hover { text-decoration-color: var(--color-accent); }
    code { font-family: var(--font-mono); font-size: 0.85em; background: var(--color-surface);
           padding: 0.1em 0.4em; border-radius: 3px; }
    .site-subtitle { color: var(--color-text-muted); font-size: 0.95rem; margin: 0 0 1.5rem; }
    .card { background: var(--color-bg); border: 1px solid var(--color-border);
            border-radius: var(--card-radius); padding: 1.5rem; margin-bottom: 1.5rem;
            box-shadow: var(--shadow-sm); }

    /* stat cards */
    .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                 gap: 1rem; margin-bottom: 1.5rem; }
    .stat-card { background: var(--color-bg); border: 1px solid var(--color-border);
                 border-radius: var(--card-radius); padding: 1.1rem 1.25rem;
                 transition: border-color 0.2s; }
    .stat-card:hover { border-color: var(--color-highlight); }
    .stat-card .stat-label { font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
                             letter-spacing: 0.03em; margin: 0 0 .2rem; }
    .stat-card .stat-value { font-family: var(--font-heading); font-size: 1.6rem;
                             font-weight: 700; line-height: 1.2; }
    .stat-card .stat-sub { font-size: 0.78rem; color: var(--color-text-muted); }
    .stat-card.mand-prof .stat-label, .stat-card.mand-prof .stat-value { color: #3D2B1F; }
    .stat-card.mand-qual .stat-label, .stat-card.mand-qual .stat-value { color: #6B4C3B; }
    .stat-card.opt .stat-label, .stat-card.opt .stat-value { color: var(--color-accent); }
    .stat-card.none-cat .stat-label, .stat-card.none-cat .stat-value { color: var(--color-text-muted); }

    /* tabs */
    .tabs { display: flex; gap: 0; border-bottom: 2px solid var(--color-border); margin-bottom: 0; }
    .tab-btn { font-family: var(--font-heading); font-size: 0.9rem; font-weight: 600;
               padding: 0.6rem 1.2rem; border: none; background: none; cursor: pointer;
               color: var(--color-text-muted); border-bottom: 2px solid transparent;
               margin-bottom: -2px; transition: all 0.15s; }
    .tab-btn:hover { color: var(--color-text); }
    .tab-btn.active { color: var(--color-accent); border-bottom-color: var(--color-accent); }
    .tab-panel { display: none; padding-top: 1.25rem; }
    .tab-panel.active { display: block; }

    /* badges */
    .cat-badge { padding: 3px 10px; border-radius: 16px; font-size: 11px;
                 font-weight: 600; display: inline-block; white-space: nowrap; }
    .cat-mandatory_professional { background: #3D2B1F; color: #FFF8F0; }
    .cat-mandatory_qualification { background: #6B4C3B; color: #FFF8F0; }
    .cat-optional { background: var(--color-accent); color: #FFF8F0; }
    .cat-none { background: var(--color-surface); color: var(--color-text-muted);
                border: 1px solid var(--color-border); }

    /* tables */
    table.data-table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
    table.data-table th { background: var(--color-surface); color: var(--color-text);
                          font-weight: 600; text-align: right; padding: 0.5rem 0.75rem;
                          border-bottom: 2px solid var(--color-border); font-size: 0.78rem; }
    table.data-table td { padding: 0.5rem 0.75rem; text-align: right;
                          border-bottom: 1px solid var(--color-border); }
    table.data-table tr:nth-child(even) { background: rgba(245,237,224,0.3); }
    table.data-table tr:hover { background: var(--color-border); }
    table.data-table td:first-child { text-align: center; font-weight: 600;
                                       font-family: var(--font-heading); }
    .pct { color: var(--color-text-muted); font-size: 0.7rem; }

    /* DataTable overrides */
    table.dataTable { font-size: 0.8125rem; }
    table.dataTable td { vertical-align: top; }
    table.dataTable thead th { background: var(--color-surface) !important;
                                color: var(--color-text) !important; font-weight: 600;
                                border-bottom: 2px solid var(--color-border) !important; }
    table.dataTable tbody tr:hover { background: var(--color-border) !important; }
    td.cat-cell { white-space: nowrap; }
    .dt-length, .dt-search, .dt-info, .dt-paging { font-size: 0.8125rem; }

    .view-btn { background: var(--color-accent); color: #fff; border: none;
                padding: 3px 12px; border-radius: 0.375rem; cursor: pointer;
                font-size: 0.72rem; font-weight: 600; font-family: var(--font-body);
                white-space: nowrap; transition: background 0.15s; }
    .view-btn:hover { background: var(--color-accent-hover); }

    /* modal */
    .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%;
                     height: 100%; background: rgba(61,43,31,0.3); z-index: 9999;
                     justify-content: center; align-items: flex-start; padding-top: 50px; }
    .modal-overlay.open { display: flex; }
    .modal { background: var(--color-bg); border: 1px solid var(--color-border);
             border-radius: var(--card-radius); max-width: 800px; width: 95%;
             max-height: 82vh; overflow-y: auto; padding: 1.75rem; position: relative;
             box-shadow: var(--shadow-md); }
    .modal-close { position: absolute; top: 12px; right: 16px; font-size: 1.25rem;
                   cursor: pointer; color: var(--color-text-muted); background: none; border: none; }
    .modal-close:hover { color: var(--color-text); }
    .modal h3 { margin-top: 0; padding-right: 2rem; font-family: var(--font-heading); }
    .modal .meta-line { font-size: 0.8125rem; color: var(--color-text-muted); margin-bottom: .75rem; }
    .modal .opm-text { border-top: 1px solid var(--color-border); padding-top: .75rem; margin-top: .5rem; }
    .modal .opm-text h2, .modal .opm-text h3, .modal .opm-text h4 { font-size: 1em; margin: .8em 0 .3em; }
    .modal .opm-text ol, .modal .opm-text ul { margin: .3em 0 .6em 1.4em; }
    .modal .opm-text p { margin: .3em 0; }
    .modal .opm-text table { border-collapse: collapse; margin: .5em 0; font-size: 0.8125rem; }
    .modal .opm-text th, .modal .opm-text td { border: 1px solid var(--color-border); padding: .3em .6em; }
    .modal .no-text { color: var(--color-text-muted); font-style: italic; }
    .modal .dod-note { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 4px;
                       padding: 0.6rem 0.9rem; margin-top: 0.75rem; font-size: 0.8125rem; color: #1e40af; }

    /* methodology */
    .methodology { font-size: 0.9rem; }
    .methodology dt { font-weight: 700; margin-top: .6em; }
    .methodology dd { margin-left: 1.2em; }
    """

    p = []
    p.append("<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>")
    p.append("<meta name='viewport' content='width=device-width, initial-scale=1.0'>")
    p.append("<title>Federal Job Postings by Education Requirement</title>")
    p.append('<link rel="stylesheet" href="https://cdn.datatables.net/1.13.8/css/jquery.dataTables.min.css">')
    p.append(f"<style>{CSS}</style>")
    p.append("</head><body><div class='site-container'>")

    # Header
    p.append(f"<h1>Federal Job Postings by Education Requirement</h1>")
    p.append(
        f"<p class='site-subtitle'>{total:,} USAJOBS postings (2018–2026) classified "
        f"against OPM qualification standards. 2026 is partial (through ~April). "
        f"Click <strong>View OPM Text</strong> on any series to read the source and verify. "
        f"Generated {_time.strftime('%Y-%m-%d', _time.gmtime())}. "
        f"By <a href='https://abigailhaddad.netlify.app/'>Abigail Haddad</a> "
        f"(<a href='https://github.com/abigailhaddad'>GitHub</a> · "
        f"<a href='https://presentofcoding.substack.com'>Blog</a>).</p>"
    )

    # Stat cards
    p.append("<div class='stats-row'>")
    for cat in FOUR_CATS:
        n = pc.get(cat, 0)
        p.append(
            f"<div class='stat-card {STAT_CLS[cat]}'>"
            f"<div class='stat-label'>{CAT_LABELS[cat]}</div>"
            f"<div class='stat-value'>{pct(n)}</div>"
            f"<div class='stat-sub'>{n:,} postings</div>"
            f"<div class='stat-sub'>{STAT_DESC[cat]}</div>"
            f"</div>"
        )
    p.append("</div>")

    # === TABS ===
    p.append("<div class='tabs'>")
    p.append("<button class='tab-btn active' data-tab='by-year'>By Year</button>")
    p.append("<button class='tab-btn' data-tab='by-series-year'>By Series &amp; Year</button>")
    p.append("<button class='tab-btn' data-tab='crosswalk'>Series Crosswalk</button>")
    p.append("</div>")

    # --- TAB 1: By Year ---
    p.append("<div class='tab-panel active' id='tab-by-year'>")
    p.append("<div class='card'>")
    p.append("<h2 style='margin-top:0'>Postings by Year and Education Category</h2>")
    p.append("<p style='font-size:0.85rem;color:var(--color-text-muted)'>Primary series classification. 2026 is partial.</p>")
    p.append("<table class='data-table'><thead><tr><th>Year</th><th>Total</th>")
    for cat in FOUR_CATS:
        p.append(f"<th><span class='cat-badge cat-{cat}'>{CAT_LABELS[cat]}</span></th>")
    p.append("</tr></thead><tbody>")
    for row in payload["by_year_primary"]:
        y = row["year"]; t = row["total"]
        p.append(f"<tr><td>{y}</td><td>{t:,}</td>")
        for cat in FOUR_CATS:
            n = row.get(cat, 0)
            pv = f"{n/t*100:.0f}" if t else "0"
            p.append(f"<td>{n:,} <span class='pct'>({pv}%)</span></td>")
        p.append("</tr>")
    p.append("</tbody></table></div>")

    # Methodology (inside tab 1)
    p.append("<div class='card methodology'>")
    p.append("<h3>Methodology</h3>")
    p.append("<dl>")
    p.append(
        "<dt><span class='cat-badge cat-mandatory_professional'>Mandatory: Professional</span></dt>"
        "<dd>Degree is a prerequisite for professional licensure. Same requirement exists "
        "in the private sector. (Physicians, nurses, pharmacists, dentists, vets, architects.)</dd>"
    )
    p.append(
        "<dt><span class='cat-badge cat-mandatory_qualification'>Mandatory: Qualification</span></dt>"
        "<dd>OPM requires a degree as a federal qualification standard; no external licensing "
        "body mandates it. (Economists, biologists, engineers, statisticians, accountants, "
        "student trainees.) Note: 1102 Contracting and 0081 Fire Protection have DoD-specific "
        "frameworks (DAWIA / DoD Fire Cert) but with similar education requirements.</dd>"
    )
    p.append(
        "<dt><span class='cat-badge cat-optional'>Optional</span></dt>"
        "<dd>Education is one qualifying path; experience alone also qualifies. "
        "(HR, IT management, police, management analysts, clerical.)</dd>"
    )
    p.append(
        "<dt><span class='cat-badge cat-none'>None</span></dt>"
        "<dd>No published education requirement. Excepted service (attorneys, chaplains) "
        "or Federal Wage System trades (cooks, mechanics, warehouse workers).</dd>"
    )
    p.append("</dl>")
    p.append(
        "<p style='font-size:0.85rem'>Sources: "
        "<a href='https://www.opm.gov/policy-data-oversight/classification-qualifications/"
        "general-schedule-qualification-standards/' target='_blank'>OPM GS Qualification Standards</a>, "
        "<a href='https://www.opm.gov/policy-data-oversight/classification-qualifications/"
        "classifying-general-schedule-positions/occupationalhandbook.pdf' target='_blank'>"
        "OPM Handbook of Occupational Groups</a>, "
        "<a href='https://www.usajobs.gov' target='_blank'>USAJOBS</a> historical data. "
        "All 422 GS series reviewed individually; 9 manual overrides applied.</p>"
    )
    p.append("</div></div>")

    # --- TAB 2: By Series & Year ---
    p.append("<div class='tab-panel' id='tab-by-series-year'>")
    p.append("<p style='font-size:0.85rem;color:var(--color-text-muted)'>"
             "Posting counts per series per year. Search or sort to find specific series.</p>")
    p.append("<table id='series-year-table' class='display compact' style='width:100%'>")
    p.append("<thead><tr><th>Category</th><th>Series</th><th>Title</th><th>Total</th>")
    for y in yrs:
        p.append(f"<th>{y}</th>")
    p.append("</tr></thead><tbody></tbody></table>")
    p.append("</div>")

    # --- TAB 3: Crosswalk ---
    p.append("<div class='tab-panel' id='tab-crosswalk'>")
    p.append("<p style='font-size:0.85rem;color:var(--color-text-muted)'>"
             "Every occupational series with its education classification. "
             "Click <strong>View OPM Text</strong> to read the actual requirement and verify.</p>")
    p.append("<table id='crosswalk' class='display compact' style='width:100%'>")
    p.append("<thead><tr><th>Category</th><th>Series</th><th>Title</th>"
             "<th>Postings</th><th>OPM Text</th><th>Source</th></tr></thead>"
             "<tbody></tbody></table>")
    p.append("</div>")

    # Modal
    p.append(
        "<div class='modal-overlay' id='modalOverlay'>"
        "<div class='modal' id='modal'>"
        "<button class='modal-close' id='modalClose'>&times;</button>"
        "<h3 id='modalTitle'></h3>"
        "<div class='meta-line' id='modalMeta'></div>"
        "<div class='opm-text' id='modalBody'></div>"
        "</div></div>"
    )

    # Embed data
    sy_json = json.dumps(series_year_rows, ensure_ascii=False).replace("</", "<\\/")
    cw_json = json.dumps(crosswalk, ensure_ascii=False).replace("</", "<\\/")
    p.append(f"<script id='sy-data' type='application/json'>{sy_json}</script>")
    p.append(f"<script id='cw-data' type='application/json'>{cw_json}</script>")

    yr_cols_js = json.dumps([str(y) for y in yrs])
    cat_labels_js = json.dumps(CAT_LABELS)

    p.append('<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>')
    p.append('<script src="https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js"></script>')

    p.append(f"""<script>
(function() {{
  const catLabels = {cat_labels_js};
  const yrCols = {yr_cols_js};
  const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const badgeHtml = cat => '<span class="cat-badge cat-'+esc(cat)+'">'
    +esc(catLabels[cat]||cat)+'</span>';
  const numFmt = (d,t) => t==='display' ? Number(d).toLocaleString() : d;

  // ---- Tabs ----
  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
      this.classList.add('active');
      document.getElementById('tab-'+this.dataset.tab).classList.add('active');
      $.fn.dataTable.tables({{visible:true, api:true}}).columns.adjust();
    }});
  }});

  // ---- Tab 2: Series × Year ----
  const syData = JSON.parse(document.getElementById('sy-data').textContent);
  const syCols = [
    {{ data:'category', render:d=>badgeHtml(d), className:'cat-cell' }},
    {{ data:'series_num', render:d=>'<code>'+esc(d)+'</code>' }},
    {{ data:'series_title' }},
    {{ data:'total', render:numFmt }}
  ];
  yrCols.forEach(y => syCols.push({{ data:y, render:numFmt }}));
  $('#series-year-table').DataTable({{
    data: syData, pageLength: 25, order:[[3,'desc']], columns: syCols
  }});

  // ---- Tab 3: Crosswalk ----
  const cwData = JSON.parse(document.getElementById('cw-data').textContent);
  const DOD_NOTE_SERIES = ['1102','0081'];
  $('#crosswalk').DataTable({{
    data: cwData, pageLength: 25, order:[[3,'desc']],
    columns: [
      {{ data:'category', render:d=>badgeHtml(d), className:'cat-cell' }},
      {{ data:'series_num', render:d=>'<code>'+esc(d)+'</code>' }},
      {{ data:'series_title' }},
      {{ data:'postings', render:numFmt }},
      {{ data:null, orderable:false,
        render:(d,t,row) => '<button class="view-btn" data-sn="'+esc(row.series_num)+'">View OPM Text</button>' }},
      {{ data:'url', orderable:false,
        render:d => d ? '<a href="'+esc(d)+'" target="_blank">Source</a>' : '' }}
    ]
  }});

  // ---- Modal ----
  const overlay = document.getElementById('modalOverlay');
  const modalTitle = document.getElementById('modalTitle');
  const modalMeta = document.getElementById('modalMeta');
  const modalBody = document.getElementById('modalBody');
  const cwLookup = {{}};
  cwData.forEach(r => {{ cwLookup[r.series_num] = r; }});

  $(document).on('click', '.view-btn', function() {{
    const sn = this.dataset.sn;
    const row = cwLookup[sn];
    if (!row) return;
    modalTitle.textContent = sn + ' — ' + (row.series_title||'');
    let meta = badgeHtml(row.category);
    if (row.education_summary) meta += ' ' + esc(row.education_summary);
    if (row.associated_group_name) meta += '<br>Group: ' + esc(row.associated_group_name);
    if (row.url) meta += '<br><a href="'+esc(row.url)+'" target="_blank">View on OPM.gov ↗</a>';
    modalMeta.innerHTML = meta;
    let body = '';
    if (row.effective_html) {{
      body = row.effective_html;
    }} else {{
      body = '<p class="no-text">No OPM requirement text available. This may be an excepted-service '
        + 'or Federal Wage System series.</p>';
    }}
    if (DOD_NOTE_SERIES.includes(sn)) {{
      body += '<div class="dod-note"><strong>DoD note:</strong> OPM\\'s standard states it '
        + '"does not apply to Department of Defense positions." DoD uses '
        + (sn==='1102' ? 'DAWIA (Defense Acquisition Workforce Improvement Act)'
                        : 'its own Fire and Emergency Services Certification Program')
        + ', which has similar education requirements.</div>';
    }}
    modalBody.innerHTML = body;
    overlay.classList.add('open');
  }});
  overlay.addEventListener('click', e => {{ if(e.target===overlay) overlay.classList.remove('open'); }});
  document.getElementById('modalClose').addEventListener('click', () => overlay.classList.remove('open'));
  document.addEventListener('keydown', e => {{ if(e.key==='Escape') overlay.classList.remove('open'); }});
}})();
</script>""")

    p.append("</div></body></html>")
    return "".join(p)



if __name__ == "__main__":
    sys.exit(main())
