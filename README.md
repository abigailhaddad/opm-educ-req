# Federal Job Postings by Education Requirement

**Live site: [opm-educ-req.netlify.app](https://opm-educ-req.netlify.app)**

What proportion of federal job postings require a college degree? This project answers that by classifying every OPM occupational series by its education requirement and joining that against ~2.9 million USAJOBS postings (2018-2026).

## Key findings

- **11%** of postings are for licensed professions where education is inherently required (doctors, nurses, pharmacists, etc.)
- **13%** require a degree because OPM's qualification standard says so (economists, engineers, scientists, etc.)
- **63%** accept education as one qualifying path, but experience alone also works (HR, IT, management, clerical)
- **12%** have no published education requirement (Federal Wage System trades, excepted service positions)

These shares are remarkably stable year-over-year (2018-2024). 2025 showed a shift: healthcare hiring held steady while other categories were cut.

## How it works

Two scripts, run in order:

### 1. `scrape_opm.py` — Build the education crosswalk

Scrapes [OPM's GS Qualification Standards](https://www.opm.gov/policy-data-oversight/classification-qualifications/general-schedule-qualification-standards/) for all 422 series, extracts the Individual Occupational Requirements, and classifies each series into four tiers:

| Tier | Meaning | Example |
|---|---|---|
| **Mandatory: Professional** | Degree is a prerequisite for professional licensure | Physicians (MD), Nurses (RN), Pharmacists (PharmD) |
| **Mandatory: Qualification** | OPM requires a degree; no licensing body mandates it | Economists, Biologists, Engineers, Accountants |
| **Optional** | Education is one path; experience alone also qualifies | HR Specialists, IT Management (2210), Police |
| **None** | No published education requirement | Attorneys (excepted), FWS trades (cooks, mechanics) |

```bash
python scrape_opm.py            # uses cached HTML if present
python scrape_opm.py --refresh  # re-fetch everything from OPM
```

Outputs: `opm_series_tiers.json` (the crosswalk), plus full data in CSV/JSON.

### 2. `analyze_usajobs.py` — Join against USAJOBS postings

Downloads the [USAJOBS historical dataset](https://github.com/abigailhaddad/usajobs_historical) (public Cloudflare R2 bucket), maps each posting's occupational series to its education tier, and produces the analysis website.

```bash
python analyze_usajobs.py            # uses cached parquet if present
python analyze_usajobs.py --refresh  # re-download the USAJOBS data
python analyze_usajobs.py --since 2022  # restrict to recent years
```

Outputs: `analysis.html` (the website), `usajobs_tier_share.json` (results as JSON).

## Data sources

- **[OPM GS Qualification Standards](https://www.opm.gov/policy-data-oversight/classification-qualifications/general-schedule-qualification-standards/)** — per-series education requirements (scraped)
- **[OPM Handbook of Occupational Groups and Families](https://www.opm.gov/policy-data-oversight/classification-qualifications/classifying-general-schedule-positions/occupationalhandbook.pdf)** — series titles for both GS and FWS (PDF, extracted)
- **[USAJOBS historical data](https://github.com/abigailhaddad/usajobs_historical)** — ~2.9M postings, 2018-2026, deduplicated by control number

## Manual overrides

All 422 GS series were reviewed individually. 9 series needed manual correction where the auto-classifier missed the education requirement (mostly healthcare series that say "Education" instead of "Degree:" in their basic requirements). See `manual_overrides.json`.

The professional vs. qualification split within mandatory is defined in `mandatory_subcategory.json` — 21 series where the degree is a prerequisite for professional licensure.

## Requirements

- Python 3.9+
- `pandas`, `pyarrow` (for the USAJOBS analysis)
- `pdftotext` (optional, for extracting the engineering IOR PDF — falls back to pypdf)
- Everything else is stdlib
