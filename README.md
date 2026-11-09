# Federal Job Postings by Education Requirement

**Live site: [opm-educ-req.abigailhaddad.com](https://opm-educ-req.abigailhaddad.com)**

What proportion of federal job postings require a college degree? This project answers that by classifying every OPM occupational series by its education requirement and joining that against USAJOBS postings from 2018 on.

## What it shows

Every posting is placed in one of four tiers by its occupational series: licensed professions where a degree is inherently required; series where OPM's standard requires a degree; series where education is one qualifying path but experience alone also works; and series with no published education requirement (mostly Federal Wage System trades, plus attorneys). The live site shows the current shares by tier, year and series.

## How it works

Two scripts, run in order:

### 1. `scrape_opm.py` — Build the education crosswalk

Scrapes [OPM's GS Qualification Standards](https://www.opm.gov/policy-data-oversight/classification-qualifications/general-schedule-qualification-standards/) for every series, extracts the Individual Occupational Requirements, and classifies each series into four tiers:

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
- **[USAJOBS historical data](https://github.com/abigailhaddad/usajobs_historical)** — postings from 2018 on, deduplicated by control number

## Manual overrides

Every GS series was reviewed individually. Some needed manual correction where the auto-classifier missed the education requirement (mostly healthcare series that say "Education" instead of "Degree:" in their basic requirements). See `manual_overrides.json`.

The professional vs. qualification split within mandatory is defined in `mandatory_subcategory.json` — the series where the degree is a prerequisite for professional licensure.

## Deploy

The site is `public/index.html`, served from Cloudflare Pages (project `opm-educ-req`). A push to `main` that changes `public/` deploys it through `.github/workflows/deploy.yml`, which needs the `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` repository secrets. To deploy by hand: `wrangler pages deploy public --project-name opm-educ-req`.

## Requirements

- Python 3.9+
- `pandas`, `pyarrow` (for the USAJOBS analysis)
- `pdftotext` (optional, for extracting the engineering IOR PDF — falls back to pypdf)
- Everything else is stdlib
