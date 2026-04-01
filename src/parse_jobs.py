"""
parse_jobs.py
────────────────────────────────────────────────────────────────────
Two responsibilities:
  1. Define RawJob (the intermediate typed dict from fetchers)
  2. parse_job() — normalise + apply all hard filters (salary,
     experience, sponsorship) and return a final CleanJob or None.

CleanJob fields map 1-to-1 to Google Sheet columns.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class RawJob:
    """Output from fetchers — minimal fields, no normalisation yet."""
    ats: str                    # "greenhouse" | "lever"
    ats_job_id: str
    company: str
    company_priority: int       # 1 = highest
    title: str
    location: str
    salary_raw: str             # raw string, may be empty
    description: str            # full JD text / HTML
    posting_url: str
    apply_url: str


@dataclass
class CleanJob:
    """Normalised job ready to write to Google Sheets."""
    job_id: str                 # stable composite hash
    company: str
    title: str
    location: str
    salary_raw: str
    source_type: str            # "greenhouse" | "lever"
    posting_url: str
    apply_url: str
    priority_score: int         # 0–100
    priority_bucket: str        # High / Medium / Low
    job_summary: str            # 2-sentence auto-summary
    date_found: str             # YYYY-MM-DD
    run_timestamp: str          # ISO 8601
    applied_status: str         # always blank on insert
    notes: str                  # always blank on insert

    # ── internal normalised keys (used for dedup, not written to sheet) ──
    company_normalized: str = field(repr=False)
    title_normalized: str = field(repr=False)


# ── Normalisation helpers ────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """
    Lowercase, strip accents, collapse whitespace, remove punctuation.
    Used for dedup key generation.
    """
    text = text.lower().strip()
    # Strip accents
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Collapse punctuation and whitespace
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_job_id(company: str, title: str, url: str, ats_id: str = "") -> str:
    """
    Stable composite key:
      SHA-1 of normalised(company) + normalised(title) + url + ats_id
    Truncated to 12 hex chars for readability.
    """
    key = "|".join([
        _normalize(company),
        _normalize(title),
        url.strip().lower(),
        ats_id.strip(),
    ])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


# ── Salary parsing ───────────────────────────────────────────────────────────

_SALARY_CLEAN = re.compile(r"[\$,\s]")
_NUM = re.compile(r"\d[\d,]*")

def parse_salary_range(salary_raw: str) -> tuple[Optional[float], Optional[float]]:
    """
    Return (min_usd, max_usd) as floats where detectable.
    Returns (None, None) if salary is missing or unparseable.

    Handles:
      "$70,000 – $90,000"
      "70000-90000"
      "USD 65000–85000 / year"
      "up to $95,000"
      "$80k"
      "80,000"
    """
    if not salary_raw:
        return None, None

    raw = salary_raw.lower()

    # Detect hourly — convert to annual estimate
    is_hourly = bool(re.search(r"\bhour\b|\bhr\b|\b/h\b", raw))

    # Extract all numeric tokens
    nums = [float(n.replace(",", "")) for n in _NUM.findall(raw)]

    if not nums:
        return None, None

    # Scale up "k" notation (e.g. "80k" → 80000)
    # Find positions of "k" after numbers
    scaled = []
    for m in re.finditer(r"(\d[\d,]*)k\b", raw):
        scaled.append(float(m.group(1).replace(",", "")) * 1000)

    if scaled:
        # Use scaled values preferentially
        nums_final = scaled
    else:
        nums_final = nums

    if is_hourly:
        nums_final = [n * 2080 for n in nums_final]  # 52 weeks × 40 hrs

    if len(nums_final) == 1:
        v = nums_final[0]
        return v, v
    else:
        return min(nums_final[:2]), max(nums_final[:2])


# ── Hard filters ─────────────────────────────────────────────────────────────

def _check_salary(salary_raw: str, rules: dict) -> bool:
    """Return False (drop) only if salary_min is CLEARLY above the cap."""
    max_usd: float = float(rules.get("salary_max_usd", 100_000))
    s_min, _s_max = parse_salary_range(salary_raw)
    if s_min is None:
        return True  # no salary info → keep
    return s_min <= max_usd  # drop only if min is above cap


def _check_experience(description: str, rules: dict) -> bool:
    """Return False (drop) if JD implies 3+ YOE as a hard minimum."""
    desc_lower = description.lower()
    for pat in rules.get("experience_exclude_patterns", []):
        if re.search(pat, desc_lower):
            return False
    return True


def _check_sponsorship(description: str, title: str, rules: dict) -> bool:
    """Return False (drop) if JD explicitly denies visa sponsorship."""
    combined = (description + " " + title).lower()
    for pat in rules.get("no_sponsorship_patterns", []):
        if re.search(pat, combined):
            return False
    return True


# ── Summary generator ────────────────────────────────────────────────────────

def _make_summary(title: str, company: str, location: str, description: str) -> str:
    """
    Generate a short 1–2 sentence human-readable summary.
    We strip HTML, grab the first substantive sentence(s).
    """
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", " ", description)
    clean = re.sub(r"&[a-z]+;", " ", clean)  # HTML entities
    clean = re.sub(r"\s+", " ", clean).strip()

    # First sentence (up to 200 chars)
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    snippet = ""
    for s in sentences:
        s = s.strip()
        if len(s) > 20:  # skip very short fragments
            snippet = s[:250]
            break

    if snippet:
        return f"{title} at {company} ({location}). {snippet}"
    return f"{title} at {company} ({location})."


# ── Main parser ──────────────────────────────────────────────────────────────

def parse_job(raw: RawJob, rules: dict) -> Optional[CleanJob]:
    """
    Apply all hard filters to a RawJob.
    Returns CleanJob if it passes, None if it should be dropped.

    Note: title + location pre-filtering is already done in fetchers.
    This function handles salary, experience, and sponsorship checks.
    """
    # ── Hard filter: salary ──────────────────────────────────────
    if not _check_salary(raw.salary_raw, rules):
        return None

    # ── Hard filter: experience ──────────────────────────────────
    if not _check_experience(raw.description, rules):
        return None

    # ── Hard filter: sponsorship ─────────────────────────────────
    if not _check_sponsorship(raw.description, raw.title, rules):
        return None

    # ── Normalise fields ─────────────────────────────────────────
    co_norm = _normalize(raw.company)
    title_norm = _normalize(raw.title)

    job_id = make_job_id(
        raw.company, raw.title, raw.posting_url, raw.ats_job_id
    )

    now = datetime.now(timezone.utc)
    date_found = date.today().isoformat()
    run_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = _make_summary(
        raw.title, raw.company, raw.location, raw.description
    )

    return CleanJob(
        job_id=job_id,
        company=raw.company,
        title=raw.title,
        location=raw.location,
        salary_raw=raw.salary_raw or "",
        source_type=raw.ats,
        posting_url=raw.posting_url,
        apply_url=raw.apply_url,
        priority_score=0,           # filled in by score.py
        priority_bucket="",         # filled in by score.py
        job_summary=summary,
        date_found=date_found,
        run_timestamp=run_ts,
        applied_status="",
        notes="",
        company_normalized=co_norm,
        title_normalized=title_norm,
    )
