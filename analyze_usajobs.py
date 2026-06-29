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

    # ---- dept-split datasets for client-side filtering ----
    dept_series = df["hiringDepartmentName"].fillna("Unknown")
    depts_sorted = sorted(str(d) for d in dept_series.unique())
    dept_idx = {d: i for i, d in enumerate(depts_sorted)}

    # primary-category x dept x year (drives stat cards + by-year table)
    prim_counter: Counter = Counter()
    for dep, yr, cat in zip(dept_series, df["_year"], df["_primary"]):
        if pd.isna(yr):
            continue
        prim_counter[(dept_idx[str(dep)], cat, int(yr))] += 1
    dsy_primary = [[d, c, y, n] for (d, c, y), n in prim_counter.items()]

    # series (all listed codes) x dept x year (drives series-year table + crosswalk)
    ser_counter: Counter = Counter()
    for dep, yr, codes in zip(dept_series, df["_year"], codes_list):
        if pd.isna(yr):
            continue
        yi = int(yr)
        di = dept_idx[str(dep)]
        for c in codes:
            ser_counter[(di, c, yi)] += 1
    dsy_series = [[d, s, y, n] for (d, s, y), n in ser_counter.items()]

    series_meta = {
        cr["series_num"]: {"category": cr["category"], "title": cr["series_title"]}
        for cr in crosswalk_rows
    }

    html_path = HERE / "analysis.html"
    html_path.write_text(
        render_analysis_html(
            payload, crosswalk_rows, series_year_rows, yrs,
            depts_sorted, dsy_primary, dsy_series, series_meta,
        ),
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
    depts_sorted: list[str],
    dsy_primary: list[list],
    dsy_series: list[list],
    series_meta: dict,
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

    /* ── chip filter bar (PD.ChipBar pattern) ───────────────────────── */
    .filters-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
                   min-height: 46px; margin: 0 0 1.5rem; padding: 0.6rem 0.85rem;
                   background: var(--color-surface); border: 1px solid var(--color-border);
                   border-radius: var(--card-radius); }
    .filters-bar-empty { color: var(--color-text-muted); font-size: 0.82rem; }
    .add-filter-btn { background: var(--color-accent); color: #fff; border: none;
                      padding: 6px 14px; border-radius: 999px; font-family: var(--font-body);
                      font-size: 0.82rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }
    .add-filter-btn:hover { background: var(--color-accent-hover); transform: translateY(-1px); }
    .filter-chip { display: inline-flex; align-items: center; gap: 7px; background: var(--color-bg);
                   border: 1px solid var(--color-accent); border-radius: 999px;
                   padding: 4px 6px 4px 12px; font-size: 0.8rem; color: var(--color-text); }
    .filter-chip-label { font-weight: 700; }
    .filter-chip-value { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .filter-chip-remove { cursor: pointer; font-weight: 700; width: 18px; height: 18px;
                          display: flex; align-items: center; justify-content: center;
                          border-radius: 50%; color: var(--color-text-muted); }
    .filter-chip-remove:hover { background: var(--color-accent); color: #fff; }
    .copy-link-btn { margin-left: auto; background: var(--color-bg); color: var(--color-text);
                     border: 1px solid var(--color-border); padding: 5px 13px; border-radius: 999px;
                     font-family: var(--font-body); font-size: 0.8rem; font-weight: 600; cursor: pointer; }
    .copy-link-btn:hover { border-color: var(--color-highlight); }
    .gf-copied { font-size: 0.78rem; color: var(--color-accent); font-weight: 600; }

    /* add-filter popover (PD.overlay backdrop = .filter-modal) */
    .filter-modal { position: fixed; inset: 0; background: rgba(61,43,31,0.4);
                    display: flex; align-items: center; justify-content: center; z-index: 10000;
                    backdrop-filter: blur(2px); }
    .filter-popover { background: var(--color-bg); border: 1px solid var(--color-border);
                      border-radius: 14px; box-shadow: var(--shadow-md); padding: 18px;
                      min-width: 300px; max-width: 460px; max-height: 80vh; overflow-y: auto; }
    .filter-title { font-size: 0.98rem; font-weight: 700; color: var(--color-text);
                    margin-bottom: 12px; padding-bottom: 10px; border-bottom: 2px solid var(--color-border);
                    font-family: var(--font-heading); }
    .filter-search { width: 100%; padding: 9px 11px; border: 1px solid var(--color-border);
                     border-radius: 9px; font-size: 0.88rem; margin-bottom: 10px;
                     font-family: var(--font-body); }
    .filter-search:focus { outline: none; border-color: var(--color-accent); }
    .filter-options { display: flex; flex-direction: column; gap: 2px; max-height: 320px; overflow-y: auto; }
    .filter-option { display: flex; align-items: center; gap: 9px; padding: 7px 6px;
                     border-radius: 7px; cursor: pointer; font-size: 0.9rem; }
    .filter-option:hover { background: var(--color-surface); }
    .filter-option input { width: 16px; height: 16px; accent-color: var(--color-accent); cursor: pointer; }
    .filter-option .opt-count { color: var(--color-text-muted); font-size: 0.78rem; margin-left: auto; }
    .filter-selectall { border-bottom: 1px solid var(--color-border); margin-bottom: 4px;
                        border-radius: 0; }
    .filter-buttons { display: flex; gap: 9px; justify-content: flex-end; margin-top: 14px; }
    .filter-buttons button { font-family: var(--font-body); font-size: 0.82rem; font-weight: 600;
                             padding: 6px 14px; border-radius: 0.375rem; cursor: pointer; }
    .btn-clear { background: var(--color-bg); color: var(--color-text); border: 1px solid var(--color-border); }
    .btn-clear:hover { border-color: var(--color-highlight); }
    .btn-apply { background: var(--color-accent); color: #fff; border: 1px solid var(--color-accent); }
    .btn-apply:hover { background: var(--color-accent-hover); }
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

    # Global chip filter bar (PD.ChipBar) — wired up by JS from embedded data + URL
    p.append(
        "<div class='filters-bar' id='filtersBar'>"
        "<button class='add-filter-btn' id='addFilterBtn' type='button'>+ Add filter</button>"
        "<span class='filters-bar-empty' id='filtersBarEmpty'>No filters applied</span>"
        "<button class='copy-link-btn' id='gfCopy' type='button'>Copy link</button>"
        "<span class='gf-copied' id='gfCopied' style='display:none'>Copied!</span>"
        "</div>"
    )

    # Stat cards
    p.append("<div class='stats-row' id='statsRow'>")
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
    p.append("</tr></thead><tbody id='byYearBody'>")
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
             "Posting counts per series per year. Use the filters above or search.</p>")
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
    def _embed(s: str) -> str:
        return s.replace("</", "<\\/")

    cw_json = _embed(json.dumps(crosswalk, ensure_ascii=False))
    depts_json = _embed(json.dumps(depts_sorted, ensure_ascii=False))
    dsyp_json = _embed(json.dumps(dsy_primary, ensure_ascii=False))
    dsys_json = _embed(json.dumps(dsy_series, ensure_ascii=False))
    smeta_json = _embed(json.dumps(series_meta, ensure_ascii=False))
    p.append(f"<script id='cw-data' type='application/json'>{cw_json}</script>")
    p.append(f"<script id='depts-data' type='application/json'>{depts_json}</script>")
    p.append(f"<script id='dsyp-data' type='application/json'>{dsyp_json}</script>")
    p.append(f"<script id='dsys-data' type='application/json'>{dsys_json}</script>")
    p.append(f"<script id='smeta-data' type='application/json'>{smeta_json}</script>")

    yr_cols_js = json.dumps([str(y) for y in yrs])
    cat_labels_js = json.dumps(CAT_LABELS)
    cat_desc_js = json.dumps(STAT_DESC)
    cat_cls_js = json.dumps(STAT_CLS)

    p.append('<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>')
    p.append('<script src="https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js"></script>')

    # PD.ChipBar + PD.overlay (vendored verbatim from the urgency-tracker shared lib)
    p.append(r"""<script>
/* PD.ChipBar — removable filter chips (vanilla DOM). */
(function (root) {
  var PD = root.PD = root.PD || {};
  function _resolve(s) { if (!s) return null; return typeof s === 'string' ? document.querySelector(s) : s; }
  function ChipBar(opts) {
    opts = opts || {};
    this._barEl = _resolve(opts.barEl);
    this._emptyEl = _resolve(opts.emptyEl);
    if (!this._barEl) throw new Error('PD.ChipBar: barEl is required');
  }
  ChipBar.prototype.render = function (chips) {
    chips = chips || [];
    this._barEl.querySelectorAll('.filter-chip.column-filter-chip').forEach(function (c) { c.remove(); });
    if (this._emptyEl) this._emptyEl.style.display = chips.length ? 'none' : '';
    var bar = this._barEl, anchor = this._emptyEl;
    chips.forEach(function (chip) {
      var el = document.createElement('div');
      el.className = 'filter-chip column-filter-chip';
      var labelSpan = document.createElement('span');
      labelSpan.className = 'filter-chip-label'; labelSpan.textContent = chip.label;
      var valueSpan = document.createElement('span');
      valueSpan.className = 'filter-chip-value'; valueSpan.textContent = chip.value;
      var removeSpan = document.createElement('span');
      removeSpan.className = 'filter-chip-remove'; removeSpan.textContent = '×';
      if (chip.onRemove) removeSpan.addEventListener('click', chip.onRemove);
      el.appendChild(labelSpan); el.appendChild(valueSpan); el.appendChild(removeSpan);
      if (anchor && anchor.parentNode === bar) bar.insertBefore(el, anchor.nextSibling);
      else bar.appendChild(el);
    });
  };
  ChipBar.prototype.clear = function () { this.render([]); };
  PD.ChipBar = ChipBar;
})(window);
/* PD.overlay — minimal popover backdrop. */
(function (root) {
  var PD = root.PD = root.PD || {};
  function open(opts) {
    opts = opts || {};
    var overlay = document.createElement('div');
    overlay.className = 'filter-modal' + (opts.className ? ' ' + opts.className : '');
    overlay.innerHTML = opts.content || '';
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(overlay); });
    var escHandler = function (e) { if (e.key === 'Escape') { close(overlay); document.removeEventListener('keydown', escHandler); } };
    overlay._pdEscHandler = escHandler;
    document.addEventListener('keydown', escHandler);
    document.body.appendChild(overlay);
    return overlay;
  }
  function close(overlay) {
    if (!overlay) return;
    if (overlay._pdEscHandler) { document.removeEventListener('keydown', overlay._pdEscHandler); overlay._pdEscHandler = null; }
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
  }
  PD.overlay = { open: open, close: close };
})(window);
</script>""")

    p.append(f"""<script>
(function() {{
  const catLabels = {cat_labels_js};
  const catDesc   = {cat_desc_js};
  const catCls    = {cat_cls_js};
  const YEARS = {yr_cols_js};
  const CATS = ['mandatory_professional','mandatory_qualification','optional','none'];
  const DEPTS     = JSON.parse(document.getElementById('depts-data').textContent);
  const DSY_PRIM  = JSON.parse(document.getElementById('dsyp-data').textContent);
  const DSY_SER   = JSON.parse(document.getElementById('dsys-data').textContent);
  const seriesMeta= JSON.parse(document.getElementById('smeta-data').textContent);
  const cwData    = JSON.parse(document.getElementById('cw-data').textContent);

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

  // ---- Filter state ----
  const sel = {{ dept:new Set(), year:new Set(), cat:new Set() }};

  function readURL() {{
    const q = new URLSearchParams(location.search);
    const load = (key, set, valid) => {{
      const raw = q.get(key);
      if (!raw) return;
      raw.split(',').map(s=>s.trim()).filter(Boolean).forEach(v => {{
        if (valid(v)) set.add(v);
      }});
    }};
    load('dept', sel.dept, v=>DEPTS.includes(v));
    load('year', sel.year, v=>YEARS.includes(v));
    load('cat',  sel.cat,  v=>CATS.includes(v));
  }}

  function writeURL() {{
    const q = new URLSearchParams();
    if (sel.dept.size) q.set('dept', DEPTS.filter(d=>sel.dept.has(d)).join(','));
    if (sel.year.size) q.set('year', YEARS.filter(y=>sel.year.has(y)).join(','));
    if (sel.cat.size)  q.set('cat',  CATS.filter(c=>sel.cat.has(c)).join(','));
    const qs = q.toString();
    history.replaceState(null, '', qs ? location.pathname + '?' + qs : location.pathname);
  }}

  // ---- Filter definitions + per-option counts ----
  const FILTER_DEFS = [
    {{ key:'dept', name:'Department', items:DEPTS,
       label:d=>d, validate:v=>DEPTS.includes(v) }},
    {{ key:'year', name:'Year', items:YEARS,
       label:y=>y, validate:v=>YEARS.includes(v) }},
    {{ key:'cat',  name:'Category', items:CATS,
       label:c=>catLabels[c]||c, validate:v=>CATS.includes(v) }},
  ];
  const DEF_BY_KEY = {{}}; FILTER_DEFS.forEach(d => DEF_BY_KEY[d.key] = d);

  // Total postings per option value (ignores current filters), for the popover counts.
  const OPT_COUNTS = {{ dept:{{}}, year:{{}}, cat:{{}} }};
  for (const [d,c,y,n] of DSY_PRIM) {{
    OPT_COUNTS.dept[DEPTS[d]] = (OPT_COUNTS.dept[DEPTS[d]]||0) + n;
    OPT_COUNTS.year[String(y)] = (OPT_COUNTS.year[String(y)]||0) + n;
    OPT_COUNTS.cat[c] = (OPT_COUNTS.cat[c]||0) + n;
  }}

  let chipBar = null;

  function renderChips() {{
    const chips = [];
    FILTER_DEFS.forEach(def => {{
      const set = sel[def.key];
      if (!set.size) return;
      const vals = def.items.filter(v => set.has(v)).map(def.label);
      chips.push({{
        label: def.name + ':',
        value: vals.join(', '),
        onRemove: () => {{ set.clear(); writeURL(); renderChips(); applyAll(); }},
      }});
    }});
    chipBar.render(chips);
  }}

  // ---- Add-filter popover flow (PD.overlay) ----
  function openFilterSelection() {{
    const opts = FILTER_DEFS.map(d =>
      '<label class="filter-option"><input type="checkbox" value="'+d.key+'"> '+esc(d.name)+'</label>'
    ).join('');
    const m = PD.overlay.open({{content:
      '<div class="filter-popover" style="min-width:280px"><div class="filter-title">Add filter</div>'+opts+'</div>'}});
    m.querySelectorAll('input[type=checkbox]').forEach(cb => cb.addEventListener('change', () => {{
      PD.overlay.close(m);
      openMultiselect(DEF_BY_KEY[cb.value]);
    }}));
  }}

  function openMultiselect(def) {{
    const set = sel[def.key];
    const counts = OPT_COUNTS[def.key] || {{}};
    const opts = def.items.map(v =>
      '<label class="filter-option"><input type="checkbox" value="'+esc(v)+'"'
      + (set.has(v) ? ' checked' : '') + '> <span>' + esc(def.label(v)) + '</span>'
      + '<span class="opt-count">' + (counts[v]||0).toLocaleString() + '</span></label>'
    ).join('');
    const m = PD.overlay.open({{content:
      '<div class="filter-popover"><div class="filter-title">Filter: '+esc(def.name)+'</div>'
      + '<input type="text" class="filter-search" placeholder="Search options…">'
      + '<label class="filter-option filter-selectall"><input type="checkbox" class="selectall-cb">'
      + ' <strong>Select all</strong></label>'
      + '<div class="filter-options">'+opts+'</div>'
      + '<div class="filter-buttons"><button class="btn-clear">Clear</button>'
      + '<button class="btn-apply">Apply</button></div></div>'}});
    const pop = m.querySelector('.filter-popover');
    const search = pop.querySelector('.filter-search');
    const optsBox = pop.querySelector('.filter-options');
    const selectAll = pop.querySelector('.selectall-cb');
    // visible (i.e. not hidden by search) option checkboxes
    const visibleBoxes = () => [...optsBox.querySelectorAll('.filter-option')]
      .filter(o => o.style.display !== 'none').map(o => o.querySelector('input'));
    const syncSelectAll = () => {{
      const vis = visibleBoxes();
      const checked = vis.filter(cb => cb.checked).length;
      selectAll.checked = vis.length > 0 && checked === vis.length;
      selectAll.indeterminate = checked > 0 && checked < vis.length;
    }};
    search.addEventListener('input', function() {{
      const q = this.value.toLowerCase();
      optsBox.querySelectorAll('.filter-option').forEach(o => {{
        o.style.display = o.textContent.toLowerCase().includes(q) ? '' : 'none';
      }});
      syncSelectAll();
    }});
    optsBox.addEventListener('change', syncSelectAll);
    selectAll.addEventListener('change', function() {{
      visibleBoxes().forEach(cb => {{ cb.checked = this.checked; }});
    }});
    search.focus();
    syncSelectAll();
    pop.querySelector('.btn-clear').addEventListener('click', () => {{
      set.clear(); writeURL(); renderChips(); applyAll(); PD.overlay.close(m);
    }});
    pop.querySelector('.btn-apply').addEventListener('click', () => {{
      set.clear();
      optsBox.querySelectorAll('input:checked').forEach(cb => set.add(cb.value));
      writeURL(); renderChips(); applyAll(); PD.overlay.close(m);
    }});
  }}

  function buildFilters() {{
    chipBar = new PD.ChipBar({{ barEl:'#filtersBar', emptyEl:'#filtersBarEmpty' }});
    document.getElementById('addFilterBtn').addEventListener('click', openFilterSelection);
    document.getElementById('gfCopy').addEventListener('click', () => {{
      navigator.clipboard.writeText(location.href).then(() => {{
        const c = document.getElementById('gfCopied');
        c.style.display = 'inline';
        setTimeout(() => c.style.display = 'none', 1500);
      }});
    }});
    renderChips();
  }}

  // ---- Filter predicates ----
  const deptOk = d => !sel.dept.size || sel.dept.has(DEPTS[d]);
  const yearOk = y => !sel.year.size || sel.year.has(String(y));
  const catOk  = c => !sel.cat.size  || sel.cat.has(c);

  // ---- Aggregations ----
  function cardCounts() {{
    const out = {{}}; CATS.forEach(c=>out[c]=0); let total = 0;
    for (const [d,c,y,n] of DSY_PRIM) {{
      if (!deptOk(d) || !yearOk(y)) continue;
      out[c] = (out[c]||0) + n; total += n;
    }}
    return {{out, total}};
  }}
  function byYear() {{
    const m = {{}};
    for (const [d,c,y,n] of DSY_PRIM) {{
      if (!deptOk(d) || !yearOk(y)) continue;
      const e = m[y] || (m[y] = {{total:0}});
      e[c] = (e[c]||0) + n; e.total += n;
    }}
    return m;
  }}
  function seriesAgg() {{
    const m = {{}};
    for (const [d,s,y,n] of DSY_SER) {{
      if (!deptOk(d) || !yearOk(y)) continue;
      const e = m[s] || (m[s] = {{total:0, yr:{{}}}});
      e.total += n; e.yr[y] = (e.yr[y]||0) + n;
    }}
    return m;
  }}

  // ---- Renderers ----
  function renderCards() {{
    const {{out, total}} = cardCounts();
    const pct = n => total ? Math.round(n/total*100)+'%' : '0%';
    let h = '';
    CATS.forEach(c => {{
      const n = out[c]||0;
      h += '<div class="stat-card '+catCls[c]+'">'
         + '<div class="stat-label">'+catLabels[c]+'</div>'
         + '<div class="stat-value">'+pct(n)+'</div>'
         + '<div class="stat-sub">'+n.toLocaleString()+' postings</div>'
         + '<div class="stat-sub">'+catDesc[c]+'</div></div>';
    }});
    document.getElementById('statsRow').innerHTML = h;
  }}
  function renderByYear() {{
    const m = byYear();
    const years = YEARS.filter(y => yearOk(y));
    let h = '';
    years.forEach(ys => {{
      const y = Number(ys); const e = m[y] || {{total:0}}; const t = e.total||0;
      h += '<tr><td>'+ys+'</td><td>'+t.toLocaleString()+'</td>';
      CATS.forEach(c => {{
        const n = e[c]||0; const pv = t ? Math.round(n/t*100) : 0;
        h += '<td>'+n.toLocaleString()+' <span class="pct">('+pv+'%)</span></td>';
      }});
      h += '</tr>';
    }});
    if (!h) h = '<tr><td colspan="6" style="text-align:center;color:var(--color-text-muted)">'
              + 'No data for this selection</td></tr>';
    document.getElementById('byYearBody').innerHTML = h;
  }}
  function setTable(tbl, rows) {{ if (!tbl) return; tbl.clear(); tbl.rows.add(rows); tbl.draw(); }}
  function renderSeriesYear() {{
    const agg = seriesAgg(); const rows = [];
    for (const s in agg) {{
      const meta = seriesMeta[s] || {{}}; const cat = meta.category || 'none';
      if (!catOk(cat)) continue;
      const a = agg[s];
      const row = {{category:cat, series_num:s, series_title:meta.title||'', total:a.total}};
      YEARS.forEach(y => {{ row[y] = a.yr[y] || 0; }});
      rows.push(row);
    }}
    setTable(tables['series-year-table'], rows);
  }}
  function renderCrosswalk() {{
    const agg = seriesAgg(); const filtered = sel.dept.size || sel.year.size;
    const rows = [];
    cwData.forEach(cw => {{
      if (!catOk(cw.category)) return;
      const a = agg[cw.series_num];
      const postings = a ? a.total : 0;
      if (filtered && postings === 0) return;
      rows.push(Object.assign({{}}, cw, {{postings:postings}}));
    }});
    setTable(tables['crosswalk'], rows);
  }}
  function applyAll() {{
    renderCards(); renderByYear(); renderSeriesYear(); renderCrosswalk();
  }}

  // ---- DataTables ----
  const tables = {{}};
  const syCols = [
    {{ data:'category', render:d=>badgeHtml(d), className:'cat-cell' }},
    {{ data:'series_num', render:d=>'<code>'+esc(d)+'</code>' }},
    {{ data:'series_title' }},
    {{ data:'total', render:numFmt }}
  ];
  YEARS.forEach(y => syCols.push({{ data:y, render:numFmt }}));
  tables['series-year-table'] = $('#series-year-table').DataTable({{
    data: [], pageLength: 25, order:[[3,'desc']], columns: syCols
  }});

  const DOD_NOTE_SERIES = ['1102','0081'];
  tables['crosswalk'] = $('#crosswalk').DataTable({{
    data: [], pageLength: 25, order:[[3,'desc']],
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

  // ---- Init ----
  readURL();
  buildFilters();
  applyAll();
}})();
</script>""")

    p.append("</div></body></html>")
    return "".join(p)



if __name__ == "__main__":
    sys.exit(main())
