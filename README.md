# NYC Finance Job Tracker

A lightweight, fully automated daily job collector for NYC finance analyst roles.  
Fetches from Greenhouse + Lever ATS APIs → filters → scores → appends to Google Sheets.  
Runs on GitHub Actions every morning at 4 AM ET, zero manual effort.

---

## How it works

```
GitHub Actions (cron: 4am ET)
        │
        ▼
  fetch_greenhouse.py  ──┐
  fetch_lever.py  ────── ┤──► parse_jobs.py ──► score.py ──► dedupe.py ──► sheets.py
                          │         │
                     ~90 companies  └── hard filters:
                                        salary > $100k  → drop
                                        3+ YOE required → drop
                                        no sponsorship  → drop
```

Each run:
1. Reads all existing rows from the Sheet (for deduplication)
2. Fetches fresh job listings from every company in `config/companies.yaml`
3. Applies hard filters (salary, experience, sponsorship)
4. Scores each job 0–100 and assigns a bucket (High / Medium / Low)
5. Appends only **new** rows — never touches existing ones
6. You review the sheet and apply manually

---

## Project structure

```
job-tracker/
├── README.md
├── requirements.txt
├── config/
│   ├── companies.yaml       # ~90 companies, ATS type, priority tier
│   └── rules.yaml           # all filter + scoring logic (edit here first)
├── src/
│   ├── main.py              # orchestrator
│   ├── fetch_greenhouse.py  # Greenhouse public JSON API
│   ├── fetch_lever.py       # Lever public JSON API
│   ├── parse_jobs.py        # normalise + hard-filter each job
│   ├── dedupe.py            # append-only deduplication
│   ├── score.py             # 0–100 priority scoring
│   └── sheets.py            # Google Sheets read + append
└── .github/
    └── workflows/
        └── daily.yml        # GitHub Actions cron job
```

---

## One-time setup

### 1 — Fork / clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/job-tracker.git
cd job-tracker
```

### 2 — Create the Google Sheet

1. Go to [Google Sheets](https://sheets.google.com) and create a new blank spreadsheet.
2. Rename the first tab to exactly **`Jobs`** (case-sensitive).
3. Copy the spreadsheet ID from the URL:  
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

### 3 — Create a Google Cloud service account

1. Go to [Google Cloud Console](https://console.cloud.google.com).
2. Create a new project (or use an existing one).
3. Enable the **Google Sheets API**:  
   APIs & Services → Library → search "Google Sheets API" → Enable.
4. Create a service account:  
   APIs & Services → Credentials → Create Credentials → Service Account.
5. Give it any name (e.g. `job-tracker-bot`). No roles needed at project level.
6. Open the service account → Keys tab → Add Key → JSON → Download.
7. Note the service account **email address** (looks like `name@project.iam.gserviceaccount.com`).

### 4 — Share the sheet with the service account

In your Google Sheet:  
Share → paste the service account email → set role to **Editor** → Send.

### 5 — Add GitHub Secrets

In your GitHub repo: Settings → Secrets and variables → Actions → New repository secret.

| Secret name | Value |
|---|---|
| `GOOGLE_CREDENTIALS_JSON` | Full contents of the downloaded JSON key file |
| `GOOGLE_SPREADSHEET_ID` | The 44-character spreadsheet ID from step 2 |

### 6 — Install dependencies locally (optional)

```bash
pip install -r requirements.txt
```

---

## Local usage

### Dry run (fetch + parse, no sheet writes)

```bash
export GOOGLE_CREDENTIALS_PATH=/path/to/your-service-account.json
export GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here

DRY_RUN=1 python src/main.py
```

Output will show what *would* be appended without touching the sheet.  
Good for testing filters and verifying the pipeline end-to-end.

### Live run

```bash
export GOOGLE_CREDENTIALS_PATH=/path/to/your-service-account.json
export GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here

python src/main.py
```

### Debug logging

```bash
LOG_LEVEL=DEBUG python src/main.py
```

---

## Configuration

All tunable logic lives in `config/` — no Python changes needed for common adjustments.

### Add or remove companies — `config/companies.yaml`

```yaml
greenhouse:
  - name: Acme Corp
    board_token: acmecorp      # slug from: https://boards.greenhouse.io/acmecorp
    priority: 2

lever:
  - name: Beta Inc
    board_token: betainc       # slug from: https://jobs.lever.co/betainc
    priority: 1
```

**Finding the board token:**
- Greenhouse: visit `https://boards.greenhouse.io/{token}` — try the company name lowercase, no spaces
- Lever: visit `https://jobs.lever.co/{token}` — same pattern

Priority tiers affect scoring:
- `1` = top target (+30 pts) — PE firms, top-tier finance companies  
- `2` = good target (+15 pts) — solid tech/fintech with FP&A culture  
- `3` = worth watching (+5 pts) — lower priority but relevant

### Adjust filters + scoring — `config/rules.yaml`

Key sections:

```yaml
# Target role titles (regex patterns, case-insensitive)
target_title_patterns:
  - "fp&a"
  - "financial analyst"
  - ...

# Hard exclusions — matching any of these drops the job entirely
title_exclude_patterns:
  - "\\bsenior\\b"
  - "\\bmanager\\b"
  - ...

# Salary cap — drop only if clearly above this
salary_max_usd: 100000

# Score thresholds for buckets
priority_buckets:
  high:   70    # score ≥ 70 → High
  medium: 40    # 40 ≤ score < 70 → Medium
```

---

## Google Sheet columns

| Column | Description |
|---|---|
| `job_id` | Stable 12-char hash — used for deduplication |
| `company` | Company name |
| `title` | Job title as posted |
| `location` | Location string from the ATS |
| `salary_raw` | Salary text if provided by the ATS (many won't have this) |
| `source_type` | `greenhouse` or `lever` |
| `posting_url` | Link to the job posting page |
| `apply_url` | Direct application link |
| `priority_score` | 0–100 numeric score |
| `priority_bucket` | `High` / `Medium` / `Low` |
| `job_summary` | Auto-generated 1–2 sentence description |
| `date_found` | Date this job first appeared (YYYY-MM-DD) |
| `run_timestamp` | UTC timestamp of the run that found it |
| `applied_status` | **Fill in manually** — e.g. `Applied`, `Phone Screen`, `Pass` |
| `notes` | **Fill in manually** — recruiter name, referral, deadline, etc. |

### Recommended Sheet formatting tips

- Freeze row 1 (header)
- Filter on `priority_bucket` = "High" to start your daily review
- Conditional formatting: color `applied_status` column by value
- Sort by `priority_score` descending, then `date_found` descending

---

## GitHub Actions schedule

The workflow runs daily at **08:00 UTC (4:00 AM ET / 3:00 AM EDT)** so your sheet is  
populated before you start your morning.

**Manual trigger:** Go to Actions tab → "Daily Job Fetch" → Run workflow.  
You can also trigger a dry run from there without writing to the sheet.

**View run logs:** Actions tab → click any run → "Fetch & Append Jobs" → expand steps.

---

## Deduplication logic

The system uses a two-layer dedup check so the sheet stays clean across runs:

1. **job_id match** — SHA-1 hash of (company + title + URL + ATS job ID)
2. **Composite key match** — normalised company + title + URL as a separate check

A job is skipped if **either** check matches an existing row.  
Existing rows are **never modified** — Helen reviews whether old postings are still open.

---

## Sponsorship filtering

Sponsorship logic is intentionally invisible in the output:

- If a JD contains explicit "will not sponsor" language → job is **silently dropped**
- The `notes` / any sponsorship column is **never written to the sheet**
- Jobs with no mention of sponsorship → kept for manual review
- You decide based on your own H-1B timeline

Sponsorship phrases checked are in `rules.yaml → no_sponsorship_patterns`.

---

## Troubleshooting

**"GOOGLE_CREDENTIALS_PATH env var not set"**  
→ Export the env var before running, or check your GitHub Secret name matches exactly.

**HTTP 404 on a company**  
→ The board token is wrong or the company doesn't use that ATS anymore.  
   Check `https://boards.greenhouse.io/{token}` manually, then update `companies.yaml`.

**Zero jobs found**  
→ Run with `LOG_LEVEL=DEBUG` to see per-company fetch results.  
   Most common cause: all jobs filtered by title. Check `target_title_patterns` in rules.yaml.

**Sheet not updating**  
→ Confirm the service account email has Editor access on the sheet.  
   Check the Actions run log for sheet errors.

**Rate limiting (HTTP 429)**  
→ The fetchers include polite delays (0.3s between Greenhouse detail calls).  
   If you hit 429s, increase `http.retry_backoff_factor` in rules.yaml.

---

## Extending to V2

Ideas for future iterations:

- **Email digest** — send a daily summary of High-priority new jobs via SendGrid / Gmail API
- **Custom company careers pages** — add a `fetch_custom.py` for companies without Greenhouse/Lever
- **Salary extraction from JD text** — improve salary parsing with regex over the description body  
- **LinkedIn / Indeed** — add as secondary sources once V1 is stable
- **Slack notification** — post new High jobs to a private channel
- **Seen/applied webhook** — update `applied_status` from a mobile shortcut

---

*Built for Helen Zhao | NYC Finance Job Search 2025–2026*
