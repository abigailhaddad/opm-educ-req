"""
Scrape OPM General Schedule Qualification Standards and build a CSV of
every job series with its educational/basic requirement text.

Three sources are used — one script fetches and joins them:

1. Individual series pages  (e.g. /1500/data-science-series-1560/)
   → per-series Individual Occupational Requirements (IOR) block
2. /tabs/group-standards/
   → umbrella requirements for the four general groups (CLER/TECH/ADMIN/PROF)
     plus the two trainee standards (CSST, PIP). A series with no IOR of its
     own inherits the relevant group standard.
3. /0800/files/all-professional-engineering-positions-0800.pdf
   → the engineering group IOR; every GS-0800 series page just says
     "Use the GS-800 individual occupational requirements", so without this
     PDF those rows have no actual requirement text. Text is extracted with
     `pdftotext` if available, else pypdf (pure Python, optional dep).

Output files (written next to this script):
  opm_education_requirements.csv   — one row per series with plain text + raw HTML
  opm_education_requirements.json  — same data, nested / machine-friendly
  opm_education_requirements.html  — browsable DataTables report
  opm_series_education_summary.csv — filtered list with the classification
  opm_series_tiers.json            — minimal: {series_num, title, tier} per row
  cache/                           — raw HTML + PDF cache for re-runs

Usage:
    python scrape_opm.py
    python scrape_opm.py --refresh   # ignore cache and refetch everything

Requires: Python 3.9+, standard library only (pypdf is optional; the script
falls back to the `pdftotext` binary if present, and still runs if neither is
available — it will just leave the engineering PDF text blank and log a note).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = "https://www.opm.gov"
INDEX_URL = (
    f"{BASE}/policy-data-oversight/classification-qualifications/"
    "general-schedule-qualification-standards/"
)
GROUP_STANDARDS_URL = (
    f"{BASE}/policy-data-oversight/classification-qualifications/"
    "general-schedule-qualification-standards/tabs/group-standards/"
)
ENG_PDF_URL = (
    f"{BASE}/policy-data-oversight/classification-qualifications/"
    "general-schedule-qualification-standards/0800/files/"
    "all-professional-engineering-positions-0800.pdf"
)

HERE = Path(__file__).parent
CACHE = HERE / "cache"
CACHE.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (opm-educ-req scraper; educational use)"
REQUEST_DELAY_S = 0.4  # polite pause between live fetches

# The four general group-coverage standards. Codes match how the series-page
# sidebar phrases them.
GROUP_STANDARDS: list[tuple[str, str]] = [
    ("PROF",  "Professional and Scientific Positions"),
    ("ADMIN", "Administrative and Management Positions"),
    ("TECH",  "Technical and Medical Support Positions"),
    ("CLER",  "Clerical and Administrative Support Positions"),
    ("CSST",  "Competitive Service Student Trainee Positions"),
    ("PIP",   "Pathways Internship Positions"),
]

# ---------------------------------------------------------------------------
# Fetching with on-disk cache
# ---------------------------------------------------------------------------


def fetch(url: str, cache_name: str, *, refresh: bool = False, binary: bool = False):
    """Fetch URL with on-disk cache. Returns str (text) or bytes (binary)."""
    path = CACHE / cache_name
    if not refresh and path.exists() and path.stat().st_size > 0:
        return path.read_bytes() if binary else path.read_text(encoding="utf-8", errors="replace")

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    path.write_bytes(data)
    time.sleep(REQUEST_DELAY_S)
    return data if binary else data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# HTML text-cleaning helpers
# ---------------------------------------------------------------------------

RE_TAGS = re.compile(r"<[^>]+>")
RE_WS = re.compile(r"\s+")
RE_NBSP = re.compile(r"&nbsp;|\xa0")
RE_ENT = re.compile(r"&(amp|lt|gt|quot|#39);")
_ENT_MAP = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "#39": "'"}

# Relative href/src → absolute OPM URL (so the report works offline)
RE_REL_ATTR = re.compile(r'(href|src)="(/[^"]*)"', re.I)
# Scrub script/style tags defensively when we preserve HTML
RE_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
RE_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)


def strip_html(html: str) -> str:
    t = RE_TAGS.sub(" ", html)
    t = RE_NBSP.sub(" ", t)
    t = RE_ENT.sub(lambda m: _ENT_MAP[m.group(1)], t)
    return RE_WS.sub(" ", t).strip()


def clean_html_fragment(html: str) -> str:
    """Return an HTML fragment safe to inline in the report: no script/style,
    relative links rewritten to absolute OPM URLs, whitespace collapsed."""
    t = RE_SCRIPT.sub("", html)
    t = RE_STYLE.sub("", t)
    t = RE_REL_ATTR.sub(lambda m: f'{m.group(1)}="{BASE}{m.group(2)}"', t)
    # Collapse runs of blank whitespace between tags without touching text.
    t = re.sub(r">\s+<", "><", t)
    return t.strip()


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# 1) Series index → list of series URLs
# ---------------------------------------------------------------------------

RE_INDEX_LINK = re.compile(
    r'href="(/policy-data-oversight/classification-qualifications/'
    r'general-schedule-qualification-standards/(\d{4})/([a-z0-9\-]+)/)"'
    r'[^>]*>([^<]+)</a>',
    re.I,
)


def _series_num_from_slug(slug: str, title: str = "") -> str:
    """Extract the 4-digit OPM series number from a URL slug / link title.

    OPM's slugs come in several flavours — normal ones end in the 4-digit
    number, but some variants embed it mid-slug or use only 3 digits:

      data-science-series-1560                              → 1560
      fingerprint-identification-series-0072a               → 0072
      general-mathematics-and-statistics-series-1501star    → 1501
      plant-protection-and-quarantine-series-436            → 0436
      information-technology-it-management-series-2210-alternative-a → 2210
      gs-2210-information-technology-management-series      → 2210
      education-and-training-technician-series-1702-one-grade-interval-positions → 1702
    """
    # Trailing 4-digit + optional letter / "star"
    m = re.search(r"(\d{4})(?:star)?[a-z]?$", slug)
    if m:
        return m.group(1)
    # series-NNNN anywhere in the slug
    m = re.search(r"series[-_](\d{4})(?:[-_]|$)", slug)
    if m:
        return m.group(1)
    # gs-NNNN-
    m = re.search(r"gs[-_](\d{4})[-_]", slug)
    if m:
        return m.group(1)
    # 3-digit at end (e.g. "series-436")
    m = re.search(r"[-_](\d{3})$", slug)
    if m:
        return m.group(1).zfill(4)
    # Last resort: look in the link title text for a "GS-NNNN" or "NNNN" token
    if title:
        m = re.search(r"\bGS[-\s]?(\d{3,4})\b", title)
        if m:
            return m.group(1).zfill(4)
    return ""


def parse_index(html: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for href, group, slug, title in RE_INDEX_LINK.findall(html):
        if href in seen:
            continue
        seen.add(href)
        clean_title = strip_html(title)
        series_num = _series_num_from_slug(slug, clean_title)
        out.append(
            dict(
                group=group,
                slug=slug,
                series_num=series_num,
                url=urljoin(BASE, href),
                index_title=clean_title,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 2) Individual series page → IOR text + associated group standard
# ---------------------------------------------------------------------------

RE_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
# Main content block: from first <h2> inside the grid column down to the
# "generic-content-page__blocks" marker (present on every OPM page).
RE_BODY = re.compile(
    r'<h2[^>]*>(.*?)<div class="generic-content-page__blocks">',
    re.S | re.I,
)
# Associated group standard phrasing in the sidebar / prose
RE_ASSOC_GROUP = re.compile(
    r"Group Coverage Qualification Standard for ([A-Z][A-Za-z /]+? Positions)",
)
RE_REFERS_GS = re.compile(r"GS-(\d{3,4})", re.I)


def parse_series_page(html: str) -> dict:
    h1 = RE_H1.search(html)
    page_title = strip_html(h1.group(1)) if h1 else ""
    if "Page Not Found" in page_title or "Page Not Found" in html[:5000]:
        return dict(
            page_title=page_title,
            status="not_found",
            requirement_text="",
            requirement_html="",
            associated_group="",
            refers_to_gs="",
        )

    m = RE_BODY.search(html)
    if not m:
        body_text = ""
        body_html = ""
        status = "no_ior_block"
    else:
        raw_body = "<h2>" + m.group(1)
        body_html = clean_html_fragment(raw_body)
        body_text = strip_html(raw_body)
        status = "has_text"
        lower = body_text.lower()
        if "there are no individual occupational requirements" in lower:
            status = "no_ior"
        elif "individual occupational requirements" in lower and re.search(
            r"use the gs-\d", lower
        ):
            status = "refers_to_group"

    assoc = ""
    am = RE_ASSOC_GROUP.search(html)
    if am:
        assoc = am.group(1).strip()

    refers = ""
    if status == "refers_to_group":
        rm = RE_REFERS_GS.search(body_text)
        if rm:
            refers = f"GS-{rm.group(1)}"

    return dict(
        page_title=page_title,
        status=status,
        requirement_text=body_text,
        requirement_html=body_html,
        associated_group=assoc,
        refers_to_gs=refers,
    )


# ---------------------------------------------------------------------------
# 3) Group-standards page → per-group full text
# ---------------------------------------------------------------------------

RE_H2_ANY = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)


def parse_group_standards(html: str) -> dict[str, dict]:
    """Slice the group-standards page into sections keyed by group code."""
    # Collect (start_of_h2, stripped_title) for every h2 in the page.
    h2_hits: list[tuple[int, int, str]] = []
    for m in RE_H2_ANY.finditer(html):
        title = strip_html(m.group(1))
        h2_hits.append((m.start(), m.end(), title))

    # Map stripped title → group code
    title_to_code = {name: code for code, name in GROUP_STANDARDS}

    # Find the h2 start offset for each group in order of appearance
    group_offsets: list[tuple[int, int, str, str]] = []
    for start, end, title in h2_hits:
        if title in title_to_code:
            group_offsets.append((start, end, title, title_to_code[title]))

    # Also mark the "Table of Contents" h2 so we know where group content ends.
    toc_start = next((s for s, _, t in h2_hits if t.lower() == "table of contents"), None)

    results: dict[str, dict] = {}
    for i, (start, end, title, code) in enumerate(group_offsets):
        if i + 1 < len(group_offsets):
            end_slice = group_offsets[i + 1][0]
        elif toc_start is not None:
            end_slice = toc_start
        else:
            end_slice = len(html)
        body = html[end:end_slice]
        results[code] = dict(
            code=code,
            title=title,
            text=strip_html(body),
            html=clean_html_fragment(body),
        )
    return results


# ---------------------------------------------------------------------------
# 4) Engineering PDF → text
# ---------------------------------------------------------------------------


def pdf_to_text(pdf_bytes: bytes, cache_name: str) -> tuple[str, str]:
    """Return (text, extractor_used). Tries pdftotext → pypdf → empty."""
    pdf_path = CACHE / cache_name
    pdf_path.write_bytes(pdf_bytes)

    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(
                ["pdftotext", "-layout", str(pdf_path), "-"],
                capture_output=True,
                check=True,
                timeout=60,
            )
            return out.stdout.decode("utf-8", errors="replace"), "pdftotext"
        except Exception as e:  # fall through to pypdf
            print(f"  pdftotext failed on {cache_name}: {e}")

    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            return "", "none"
    try:
        reader = PdfReader(str(pdf_path))
        chunks = []
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks), "pypdf"
    except Exception as e:
        print(f"  pypdf failed on {cache_name}: {e}")
        return "", "none"


def extract_pdf_ior(text: str) -> str:
    """Pull the 'Basic Requirements' / IOR section out of a group PDF."""
    if not text:
        return ""
    # Normalise whitespace but preserve paragraph breaks
    normalised = re.sub(r"[ \t]+", " ", text)
    normalised = re.sub(r"\n{3,}", "\n\n", normalised)
    lower = normalised.lower()
    # Prefer the "Basic Requirements" anchor; fall back to the whole doc.
    i = lower.find("basic requirements")
    if i == -1:
        i = lower.find("individual occupational requirements")
    if i == -1:
        return normalised.strip()
    # End at "Evaluation of Experience" or a double-blank after a reasonable
    # amount of content, whichever comes first.
    tail = normalised[i:]
    end_markers = ["Evaluation of Experience", "Quality Ranking Factors", "Special Provisions"]
    end = len(tail)
    for marker in end_markers:
        j = tail.find(marker, 500)
        if j != -1 and j < end:
            end = j
    return tail[:end].strip()


# ---------------------------------------------------------------------------
# Education-requirement classifier
# ---------------------------------------------------------------------------

# A series' "effective" education requirement lives in one of three places,
# depending on its status: (a) its own IOR text, (b) the associated group
# coverage standard, or (c) the GS-800 engineering PDF. classify_education()
# looks at that effective text and tags what it finds.
#
# Three buckets, per user request:
#   mandatory — you MUST have a degree (or equivalent academic credential);
#               experience alone does not qualify. PROF-group series, series
#               whose IOR opens with "Basic Requirements: Degree:", all
#               engineering, and the student-trainee tracks (CSST/PIP —
#               enrollment is definitionally required).
#   optional  — education is ONE way to qualify but not the only way. You
#               can substitute experience (or vice versa). Covers ADMIN/
#               CLER/TECH group standards and series IORs that offer
#               "combination of education and experience" or semester-hour
#               tradeoffs.
#   none      — no education path detected in the effective text.
#
# The classification is best-effort — OPM's language varies page to page —
# so we also return the cues that fired and a one-line human summary.

RE_DEGREE_BASIC = re.compile(
    r"basic requirements?:?\s*(?:[^a-z]{0,30})?(?:[a-z]\.?\s*)?degree\s*[:\-]",
    re.I | re.S,
)
RE_DEGREE_WORD = re.compile(
    r"\b(?:bachelor(?:'s)?|master(?:'s)?|doctor(?:ate|al)|ph\.?\s*d\.?|degree)\b",
    re.I,
)
RE_SEMESTER_HOURS = re.compile(r"\bsemester\s+hours?\b", re.I)
RE_COMBO_ED_EXP = re.compile(
    r"combinations?\s+of\s+education\s+and\s+experience"
    r"|education\s+and/or\s+experience"
    r"|education[\s/]+training",
    re.I,
)
RE_NO_IOR_PHRASE = re.compile(
    r"there are no individual occupational requirements", re.I
)
RE_DEGREE_FIELD = re.compile(
    r"Degree\s*[:\-]\s*([A-Za-z][A-Za-z ,;/&'\-]{3,200}?)(?:\.|\s+or\b|<|\n)",
)


def classify_education(effective_text: str, group_code: str) -> dict:
    """Return {tier: mandatory|optional|none} + cues + human summary."""
    out = dict(
        education_tier="none",
        required_field="",
        education_summary="",
        education_cues="",
    )
    if not effective_text or not effective_text.strip():
        out["education_summary"] = "No effective requirement text captured"
        return out

    text = effective_text
    lower = text.lower()

    degree_word = bool(RE_DEGREE_WORD.search(text))
    semester_hours = bool(RE_SEMESTER_HOURS.search(text))
    combo_phrase = bool(RE_COMBO_ED_EXP.search(text))
    basic_degree = bool(RE_DEGREE_BASIC.search(text))
    high_school = "high school" in lower
    education_word = "education" in lower or "educational" in lower

    cues = []
    if basic_degree: cues.append("basic-req/degree")
    if degree_word: cues.append("degree-word")
    if semester_hours: cues.append("semester-hours")
    if combo_phrase: cues.append("combo-ed+exp")
    if high_school: cues.append("high-school")
    if education_word: cues.append("education-word")
    out["education_cues"] = ",".join(cues)

    # --- Tier decision ---------------------------------------------------
    # MANDATORY: degree (or enrollment) is required; experience alone won't
    # satisfy the standard.
    if group_code == "PROF":
        tier = "mandatory"
    elif group_code in ("CSST", "PIP"):
        # Student-trainee / internship standards require current enrollment.
        tier = "mandatory"
    elif basic_degree:
        tier = "mandatory"
    # OPTIONAL: degree is one qualifying path; experience (or a combo) is
    # another. This covers the ADMIN/CLER/TECH group standards (all of which
    # allow education↔experience substitution) and series IORs that discuss
    # education as a qualifying route — explicit combo language, semester-hour
    # tradeoffs, high-school minima, or a named degree that's not flagged as
    # the sole basic requirement.
    elif group_code in ("ADMIN", "CLER", "TECH"):
        tier = "optional"
    elif combo_phrase or semester_hours or degree_word or high_school:
        tier = "optional"
    # NONE: nothing education-shaped detected in the effective text.
    else:
        tier = "none"

    out["education_tier"] = tier

    # --- Extract the degree field if the text names one -----------------
    m = RE_DEGREE_FIELD.search(text)
    if m:
        field = re.sub(r"\s+", " ", m.group(1)).strip(" ,;-")
        field = re.split(r"\s{2,}|\s*\(at least\)|\s+that included", field)[0]
        if len(field) > 180:
            field = field[:177] + "…"
        out["required_field"] = field

    # --- One-line summary for humans ------------------------------------
    if tier == "mandatory":
        if group_code == "PROF":
            summary = "Mandatory — degree required (PROF group: professional & scientific)"
        elif group_code in ("CSST", "PIP"):
            summary = f"Mandatory — current enrollment required ({group_code} student standard)"
        elif out["required_field"]:
            summary = f"Mandatory — degree in {out['required_field']}"
        else:
            summary = "Mandatory — degree required (per series IOR)"
    elif tier == "optional":
        if group_code == "ADMIN":
            summary = "Optional — degree OR qualifying experience (ADMIN group)"
        elif group_code in ("CLER", "TECH"):
            summary = f"Optional — experience-primary with education substitution ({group_code} group)"
        elif out["required_field"]:
            summary = f"Optional — degree in {out['required_field']} OR equivalent experience"
        else:
            summary = "Optional — education is one qualifying path"
    else:
        summary = "None — no education path detected in effective requirement"
    out["education_summary"] = summary
    return out


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

REPORT_CSS = """
  :root { --border: #d0d7de; --muted: #57606a; --accent: #0969da; --bg: #f6f8fa; }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, system-ui, Segoe UI, sans-serif;
         max-width: 1400px; margin: 2em auto; padding: 0 1em; color: #1f2328; }
  h1 { border-bottom: 2px solid var(--border); padding-bottom: .3em; }
  h2 { margin-top: 2em; border-bottom: 1px solid var(--border); padding-bottom: .2em; }
  header.meta { color: var(--muted); font-size: 13px; margin-bottom: 1em; }
  .num { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         background: var(--bg); padding: .05em .4em; border-radius: 3px; }
  .tier { font-size: 10px; padding: .15em .55em; border-radius: 10px;
          text-transform: uppercase; letter-spacing: .04em; font-weight: 700;
          display: inline-block; white-space: nowrap; }
  .tier.mandatory { background: #cf222e; color: #fff; }
  .tier.optional  { background: #bf8700; color: #fff; }
  .tier.none      { background: #6e7781; color: #fff; }
  .tier-buttons { margin: .8em 0 1.2em; display: flex; gap: .5em; flex-wrap: wrap;
                  align-items: center; }
  .tier-buttons button { border: 1px solid var(--border); background: #fff;
                         padding: .4em .9em; border-radius: 16px; cursor: pointer;
                         font: inherit; }
  .tier-buttons button.active { border-color: #1f2328; box-shadow: 0 0 0 2px #1f232822; }
  .tier-buttons .label { color: var(--muted); font-size: 13px; margin-right: .3em; }
  .child-row { padding: .8em 1em 1em; background: #fcfcfd;
               border-left: 3px solid var(--accent); }
  .child-row h2, .child-row h3, .child-row h4 { margin: .8em 0 .3em; font-size: 1em; }
  .child-row ol, .child-row ul { margin: .3em 0 .6em 1.4em; }
  .child-row p { margin: .3em 0; }
  .child-row table { border-collapse: collapse; margin: .5em 0; }
  .child-row th, .child-row td {
      border: 1px solid var(--border); padding: .3em .6em; vertical-align: top; }
  .child-row .group-callout { margin-top: 1em; padding: .6em .9em;
                              background: #fff8e1; border-left: 3px solid #d4a72c;
                              border-radius: 3px; font-size: 13px; }
  pre.pdf-text { white-space: pre-wrap; font-size: 13px; background: var(--bg);
                 padding: .8em; border-radius: 4px; overflow-x: auto; }
  section.group-standard { border: 1px solid var(--border); border-radius: 6px;
                           padding: 1em 1.2em; margin: 1em 0; background: #fff; }
  section.group-standard h3 { margin-top: 0; }
  .count { color: var(--muted); font-weight: normal; font-size: .85em; }
  table.dataTable td { vertical-align: top; }
  table.dataTable td.dt-control { cursor: pointer; width: 20px; }
  table.dataTable td.dt-control::before {
      content: "▸"; color: var(--muted); font-size: 12px; }
  table.dataTable tr.dt-hasChild td.dt-control::before { content: "▾"; }
  .dt-length, .dt-search, .dt-info, .dt-paging { font-size: 13px; }
  .summary-cell { max-width: 420px; }
"""


def _status_label(status: str) -> str:
    return {
        "has_text": "own requirements",
        "no_ior": "no IOR (uses group standard)",
        "refers_to_group": "refers to GS-800 PDF",
        "not_found": "page not found",
        "no_ior_block": "could not parse",
    }.get(status, status)


def _child_row_html(r: dict) -> str:
    """HTML shown when a DataTables row is expanded."""
    chunks: list[str] = ["<div class='child-row'>"]
    chunks.append(
        f"<p><strong>{html_escape(r['series_title'] or r['page_title'])}</strong> — "
        f"<a href='{html_escape(r['url'])}' target='_blank' rel='noopener'>"
        "view on OPM.gov ↗</a></p>"
    )
    if r["requirement_html"]:
        chunks.append(r["requirement_html"])
    elif r["status"] == "no_ior":
        chunks.append(
            "<p><em>OPM states there are no Individual Occupational Requirements "
            "for this series — the associated group coverage standard applies "
            "(full text below).</em></p>"
        )
    else:
        chunks.append("<p><em>No requirement text extracted from OPM page.</em></p>")
    if r["associated_group_name"]:
        code = r["associated_group_code"]
        chunks.append(
            "<div class='group-callout'>Associated group standard: "
            f"<strong>{html_escape(r['associated_group_name'])}</strong>"
            + (f" — see <a href='#gs-{code}'>GS-{code} full text below</a>" if code else "")
            + "</div>"
        )
    chunks.append("</div>")
    return "".join(chunks)


def render_html_report(
    rows: list[dict],
    group_texts: dict[str, dict],
    eng_ior_html: str,
    pdf_extractor: str,
) -> str:
    """Single-page DataTables view over every series, plus the group
    standards and engineering PDF reference sections underneath."""

    # Data array for DataTables — one JS object per series
    data_rows = []
    for r in rows:
        data_rows.append(
            {
                "tier": r["education_tier"],
                "series_num": r["series_num"],
                "series_title": r["series_title"] or r["page_title"],
                "summary": r["education_summary"],
                "field": r["required_field"],
                "group": r["associated_group_code"] or "",
                "group_name": r["associated_group_name"] or "",
                "source": r["requirement_source"],
                "status": r["status"],
                "url": r["url"],
                "detail": _child_row_html(r),
            }
        )

    from collections import Counter
    tier_counts = Counter(r["education_tier"] for r in rows)

    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append('<html lang="en"><head><meta charset="utf-8">')
    parts.append("<title>OPM Education Requirements by Job Series</title>")
    parts.append(
        '<link rel="stylesheet" '
        'href="https://cdn.datatables.net/2.1.8/css/dataTables.dataTables.min.css">'
    )
    parts.append(f"<style>{REPORT_CSS}</style>")
    parts.append("</head><body>")

    parts.append("<h1>OPM Education Requirements by Job Series</h1>")
    parts.append(
        f"<header class='meta'>Generated "
        f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} · "
        f"{len(rows)} series · source: "
        f"<a href='{INDEX_URL}' target='_blank' rel='noopener'>OPM GS "
        "Qualification Standards</a> · engineering PDF text via "
        f"<code>{pdf_extractor}</code></header>"
    )

    tier_line = " · ".join(
        f"<span class='tier {t}'>{t.upper()}</span> {tier_counts.get(t, 0)}"
        for t in ("mandatory", "optional", "none")
    )
    parts.append(f"<p><strong>Education requirement:</strong> {tier_line}</p>")
    parts.append(
        "<p style='color:var(--muted);font-size:13px;margin-top:-.4em'>"
        "<strong>MANDATORY</strong> = degree (or enrollment) required; "
        "<strong>OPTIONAL</strong> = education is one qualifying path; "
        "<strong>NONE</strong> = no education path detected in the effective "
        "requirement. Click any row to expand the full original OPM requirement "
        "text; use the tier buttons, column headers, or the search box to filter.</p>"
    )

    parts.append(
        "<div class='tier-buttons'>"
        "<span class='label'>Filter by tier:</span>"
        "<button data-tier='all' class='active'>All</button>"
        "<button data-tier='mandatory'>Mandatory</button>"
        "<button data-tier='optional'>Optional</button>"
        "<button data-tier='none'>None</button>"
        "</div>"
    )

    parts.append(
        "<table id='series-table' class='display compact' style='width:100%'>"
        "<thead><tr>"
        "<th></th>"
        "<th>Tier</th>"
        "<th>Series #</th>"
        "<th>Title</th>"
        "<th>Summary</th>"
        "<th>Required field</th>"
        "<th>Group</th>"
        "<th>Source</th>"
        "<th>Status</th>"
        "<th>Link</th>"
        "</tr></thead><tbody></tbody></table>"
    )

    # JSON data, escaped to be safe inside a <script> block
    json_blob = json.dumps(data_rows, ensure_ascii=False).replace("</", "<\\/")
    parts.append(
        f"<script id='rows' type='application/json'>{json_blob}</script>"
    )

    parts.append("<h2 id='group-standards'>Group-coverage qualification standards</h2>")
    parts.append(
        f"<p>Source: <a href='{GROUP_STANDARDS_URL}' target='_blank' "
        "rel='noopener'>OPM /tabs/group-standards/</a>. These umbrella "
        "standards are inherited by every <code>no_ior</code> series.</p>"
    )
    for code, name in GROUP_STANDARDS:
        if code not in group_texts:
            continue
        gs = group_texts[code]
        parts.append(
            f"<section class='group-standard' id='gs-{code}'>"
            f"<h3>{html_escape(name)} <span class='count'>(GS-{code})</span></h3>"
        )
        parts.append(gs["html"])
        parts.append("</section>")

    if eng_ior_html:
        parts.append("<h2 id='eng-pdf'>GS-800 Engineering IOR (from PDF)</h2>")
        parts.append(
            f"<p>Source: <a href='{ENG_PDF_URL}' target='_blank' rel='noopener'>"
            "all-professional-engineering-positions-0800.pdf</a>. This is the "
            "effective requirement for every engineering sub-series whose page "
            "just says &ldquo;Use the GS-800 IOR.&rdquo;</p>"
        )
        parts.append(eng_ior_html)

    # DataTables scripts + init
    parts.append(
        '<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>'
        '<script src="https://cdn.datatables.net/2.1.8/js/dataTables.min.js"></script>'
    )
    parts.append(
        "<script>\n"
        "(function() {\n"
        "  const rows = JSON.parse(document.getElementById('rows').textContent);\n"
        "  const esc = (s) => String(s == null ? '' : s)\n"
        "    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')\n"
        "    .replace(/\"/g,'&quot;');\n"
        "  const table = new DataTable('#series-table', {\n"
        "    data: rows,\n"
        "    pageLength: 50,\n"
        "    lengthMenu: [25, 50, 100, 200, [-1, 'All']],\n"
        "    order: [[1, 'asc'], [2, 'asc']],\n"
        "    columns: [\n"
        "      { data: null, defaultContent: '', orderable: false,\n"
        "        className: 'dt-control', width: '18px' },\n"
        "      { data: 'tier',\n"
        "        render: (d) => `<span class=\"tier ${esc(d)}\">${esc(d)}</span>` },\n"
        "      { data: 'series_num',\n"
        "        render: (d) => `<span class=\"num\">${esc(d) || '—'}</span>` },\n"
        "      { data: 'series_title' },\n"
        "      { data: 'summary', className: 'summary-cell' },\n"
        "      { data: 'field' },\n"
        "      { data: 'group',\n"
        "        render: (d, t, row) => d\n"
        "          ? `<span title=\"${esc(row.group_name)}\">${esc(d)}</span>` : '' },\n"
        "      { data: 'source' },\n"
        "      { data: 'status' },\n"
        "      { data: 'url', orderable: false,\n"
        "        render: (d) => d\n"
        "          ? `<a href=\"${esc(d)}\" target=\"_blank\" rel=\"noopener\">OPM ↗</a>` : '' }\n"
        "    ]\n"
        "  });\n"
        "\n"
        "  $('#series-table tbody').on('click', 'td.dt-control', function() {\n"
        "    const tr = $(this).closest('tr');\n"
        "    const row = table.row(tr);\n"
        "    if (row.child.isShown()) { row.child.hide(); tr.removeClass('dt-hasChild'); }\n"
        "    else { row.child(row.data().detail).show(); tr.addClass('dt-hasChild'); }\n"
        "  });\n"
        "\n"
        "  document.querySelectorAll('.tier-buttons button').forEach((btn) => {\n"
        "    btn.addEventListener('click', () => {\n"
        "      document.querySelectorAll('.tier-buttons button')\n"
        "        .forEach(b => b.classList.remove('active'));\n"
        "      btn.classList.add('active');\n"
        "      const t = btn.getAttribute('data-tier');\n"
        "      table.column(1).search(t === 'all' ? '' : '^' + t + '$', true, false).draw();\n"
        "    });\n"
        "  });\n"
        "})();\n"
        "</script>"
    )

    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(refresh: bool = False) -> None:
    print("1/4 Fetching series index…")
    index_html = fetch(INDEX_URL, "_index.html", refresh=refresh)
    series = parse_index(index_html)
    print(f"    {len(series)} unique series URLs")

    print("2/4 Fetching each series page…")
    for i, link in enumerate(series, 1):
        cache_name = f"{link['group']}_{link['slug']}.html"
        try:
            html = fetch(link["url"], cache_name, refresh=refresh)
        except Exception as e:
            link["_parse"] = dict(
                page_title="",
                status=f"fetch_error: {e}",
                requirement_text="",
                associated_group="",
                refers_to_gs="",
            )
            continue
        link["_parse"] = parse_series_page(html)
        if i % 50 == 0 or i == len(series):
            print(f"    [{i}/{len(series)}]")

    print("3/4 Fetching group-standards page…")
    gs_html = fetch(GROUP_STANDARDS_URL, "_group_standards.html", refresh=refresh)
    group_texts = parse_group_standards(gs_html)
    for code, name in GROUP_STANDARDS:
        present = code in group_texts
        size = len(group_texts[code]["text"]) if present else 0
        print(f"    {code:6s} {name[:46]:46s} {'ok' if present else 'MISS'}  {size} chars")

    print("4/4 Fetching engineering IOR PDF…")
    try:
        pdf_bytes = fetch(ENG_PDF_URL, "_eng_800.pdf", refresh=refresh, binary=True)
        eng_full, extractor = pdf_to_text(pdf_bytes, "_eng_800.pdf")
        eng_ior = extract_pdf_ior(eng_full)
        print(
            f"    pdf={len(pdf_bytes)} bytes, text via {extractor}, "
            f"IOR slice={len(eng_ior)} chars"
        )
    except Exception as e:
        print(f"    PDF fetch/parse failed: {e}")
        eng_full, eng_ior, extractor = "", "", "none"

    # --- Build final rows -------------------------------------------------
    # Pre-render engineering PDF text as an HTML <pre> block for the report
    eng_ior_html = (
        f'<pre class="pdf-text">{html_escape(eng_ior)}</pre>' if eng_ior else ""
    )

    rows: list[dict] = []
    for link in series:
        p = link["_parse"]
        status = p["status"]
        req_text = p["requirement_text"]
        req_html = p["requirement_html"]
        source = "series_page"

        # For refers_to_group engineering rows, substitute the engineering PDF IOR.
        if status == "refers_to_group" and eng_ior:
            req_text = eng_ior
            req_html = eng_ior_html
            source = "eng_pdf"
        elif status == "no_ior":
            source = "no_ior"  # content lives only in the group standard

        # Attach the associated group standard text as a separate column.
        assoc_name = p["associated_group"]
        group_code = ""
        group_text = ""
        group_html = ""
        for code, name in GROUP_STANDARDS:
            if name == assoc_name:
                group_code = code
                if code in group_texts:
                    group_text = group_texts[code]["text"]
                    group_html = group_texts[code]["html"]
                break

        # The "effective" requirement = what actually governs this series.
        # For own-IOR rows it's the series text; for no_ior rows it's the
        # group standard; for engineering refers_to_group rows it's the PDF.
        if status == "no_ior":
            effective_text = group_text
            effective_html = group_html
        else:
            effective_text = req_text
            effective_html = req_html

        edu = classify_education(effective_text, group_code)

        rows.append(
            dict(
                series_num=link["series_num"],
                group=link["group"],
                series_title=link["index_title"],
                page_title=p["page_title"],
                status=status,
                requirement_text=req_text,
                requirement_html=req_html,
                requirement_source=source,
                refers_to_gs=p["refers_to_gs"],
                associated_group_name=assoc_name,
                associated_group_code=group_code,
                associated_group_text=group_text,
                associated_group_html=group_html,
                effective_text=effective_text,
                effective_html=effective_html,
                education_tier=edu["education_tier"],
                education_summary=edu["education_summary"],
                required_field=edu["required_field"],
                education_cues=edu["education_cues"],
                url=link["url"],
            )
        )

    # Dedupe by series_num. When a series has multiple variant pages
    # (e.g. 2210 Alternative A + Alternative B, or 1001 one-grade + two-grade),
    # we MERGE them before classifying: concatenate their effective-text
    # blobs so the classifier sees every qualification path. This matters
    # because 2210 Alt A alone classifies as "mandatory" (degree required)
    # while Alt B alone provides an experience path — together they mean
    # 2210 is "optional" (education is one way to qualify).
    from collections import defaultdict as _dd

    groups_by_key: dict[str, list[dict]] = _dd(list)
    for r in rows:
        key = r["series_num"] or r["url"]
        groups_by_key[key].append(r)

    final: list[dict] = []
    for key, variants in groups_by_key.items():
        if len(variants) == 1:
            final.append(variants[0])
            continue
        # Pick a primary row (prefer one with requirement_text) to carry
        # metadata like url / titles, then merge text/html from all variants
        primary = next(
            (v for v in variants if v["requirement_text"]),
            variants[0],
        )
        merged = dict(primary)  # shallow copy

        # Concatenate requirement/effective text across every variant.
        def _join(field_text: str) -> str:
            chunks = [v[field_text] for v in variants if v.get(field_text)]
            return "\n\n".join(chunks)

        def _join_html(field_html: str) -> str:
            chunks = [v[field_html] for v in variants if v.get(field_html)]
            if not chunks:
                return ""
            labels = []
            for i, v in enumerate(variants):
                if not v.get(field_html):
                    continue
                slug_label = v.get("slug") or v.get("page_title") or f"variant {i+1}"
                labels.append(
                    f'<section class="variant"><h4 style="margin:.4em 0">'
                    f'Variant: {html_escape(slug_label)}</h4>{v[field_html]}'
                    "</section>"
                )
            return "".join(labels)

        merged["requirement_text"] = _join("requirement_text")
        merged["requirement_html"] = _join_html("requirement_html")

        # Re-derive status across variants: "has_text" wins if any variant
        # has real text, otherwise keep the primary's status.
        if any(v["status"] == "has_text" for v in variants):
            merged["status"] = "has_text"

        # Note which slugs were merged, for transparency in downstream files
        merged["_merged_from"] = [v.get("slug", "") for v in variants]

        # Re-classify using the merged text — this is the whole point.
        effective_text = merged["requirement_text"]
        effective_html = merged["requirement_html"]
        if not effective_text and merged.get("associated_group_text"):
            effective_text = merged["associated_group_text"]
            effective_html = merged.get("associated_group_html", "")
        merged["effective_text"] = effective_text
        merged["effective_html"] = effective_html
        edu = classify_education(
            effective_text, merged.get("associated_group_code", "")
        )
        merged["education_tier"] = edu["education_tier"]
        merged["education_summary"] = edu["education_summary"]
        merged["required_field"] = edu["required_field"]
        merged["education_cues"] = edu["education_cues"]
        final.append(merged)

    final.sort(key=lambda r: (r["group"], r["series_num"] or "zzzz"))

    # --- Apply manual overrides from audit --------------------------------
    overrides_path = HERE / "manual_overrides.json"
    if overrides_path.exists():
        overrides = json.loads(overrides_path.read_text())
        applied = 0
        for r in final:
            sn = r.get("series_num", "")
            if sn in overrides and not overrides[sn].get("_comment"):
                ov = overrides[sn]
                old_tier = r["education_tier"]
                new_tier = ov["tier"]
                if old_tier != new_tier:
                    r["education_tier"] = new_tier
                    reason = ov.get("reason", "")
                    r["education_summary"] = (
                        f"{new_tier.title()} — {reason}"
                        if reason
                        else f"{new_tier.title()} (manual override)"
                    )
                    applied += 1
        print(f"  Applied {applied} manual overrides from {overrides_path.name}")

    # --- Split mandatory into professional vs qualification ---------------
    subcat_path = HERE / "mandatory_subcategory.json"
    if subcat_path.exists():
        subcat = json.loads(subcat_path.read_text())
        prof_set = set(subcat.get("professional", {}).get("series", []))
        for r in final:
            if r["education_tier"] == "mandatory":
                r["mandatory_type"] = (
                    "professional" if r["series_num"] in prof_set else "qualification"
                )
            else:
                r["mandatory_type"] = ""
        n_prof = sum(1 for r in final if r.get("mandatory_type") == "professional")
        n_qual = sum(1 for r in final if r.get("mandatory_type") == "qualification")
        print(f"  Mandatory split: {n_prof} professional, {n_qual} qualification")

    # --- Write outputs ----------------------------------------------------
    csv_path = HERE / "opm_education_requirements.csv"
    fields = [
        "series_num",
        "group",
        "series_title",
        "education_tier",
        "mandatory_type",
        "education_summary",
        "required_field",
        "education_cues",
        "page_title",
        "status",
        "requirement_source",
        "requirement_text",
        "requirement_html",
        "effective_text",
        "effective_html",
        "refers_to_gs",
        "associated_group_code",
        "associated_group_name",
        "associated_group_text",
        "associated_group_html",
        "url",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(final)

    json_path = HERE / "opm_education_requirements.json"
    payload = {
        "meta": {
            "source_index": INDEX_URL,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "row_count": len(final),
            "pdf_extractor": extractor,
        },
        "group_standards": group_texts,
        "engineering_ior": eng_ior,
        "series": final,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    html_path = HERE / "opm_education_requirements.html"
    html_path.write_text(
        render_html_report(final, group_texts, eng_ior_html, extractor),
        encoding="utf-8",
    )

    # A small companion CSV: just the classification, no giant HTML blobs,
    # sorted by tier then series number. This is the "give me the list"
    # output.
    summary_path = HERE / "opm_series_education_summary.csv"
    tier_order = {"mandatory": 0, "optional": 1, "none": 2}
    summary_rows = sorted(
        final,
        key=lambda r: (tier_order.get(r["education_tier"], 9), r["series_num"]),
    )
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "education_tier",
                "series_num",
                "series_title",
                "education_summary",
                "required_field",
                "associated_group_code",
                "requirement_source",
                "url",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        w.writerows(summary_rows)

    # Minimal JSON: just {series_num, series_title, tier}. The "I just want
    # the list in one line of jq" output. Sorted by series number.
    tiers_path = HERE / "opm_series_tiers.json"
    tiers_payload = [
        {
            "series_num": r["series_num"],
            "series_title": r["series_title"],
            "tier": r["education_tier"],
            "mandatory_type": r.get("mandatory_type", ""),
        }
        for r in sorted(final, key=lambda r: r["series_num"])
    ]
    tiers_path.write_text(
        json.dumps(tiers_payload, indent=2) + "\n", encoding="utf-8"
    )

    # --- Summary ----------------------------------------------------------
    from collections import Counter

    status_counts = Counter(r["status"] for r in final)
    source_counts = Counter(r["requirement_source"] for r in final)
    assoc_counts = Counter(r["associated_group_code"] or "(none)" for r in final)
    tier_counts = Counter(r["education_tier"] for r in final)

    print()
    print(f"Wrote {len(final)} rows → {csv_path.name}")
    print(f"       {len(final)} rows → {json_path.name}")
    print(f"       report      → {html_path.name}")
    print(f"       summary     → {summary_path.name}")
    print(f"       tiers json  → {tiers_path.name}")
    print("Education tier (the answer to 'who needs education'):")
    for s in ("mandatory", "optional", "none"):
        print(f"  {tier_counts.get(s, 0):4d}  {s}")
    print("Status breakdown:")
    for s, n in status_counts.most_common():
        print(f"  {n:4d}  {s}")
    print("Requirement-text source:")
    for s, n in source_counts.most_common():
        print(f"  {n:4d}  {s}")
    print("Associated group:")
    for s, n in assoc_counts.most_common():
        print(f"  {n:4d}  {s}")


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cache and refetch every URL.",
    )
    args = ap.parse_args(argv)
    run(refresh=args.refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
