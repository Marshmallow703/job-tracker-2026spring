"""
fetch_ashby.py
────────────────────────────────────────────────────────────────────
Fetches job listings from the Ashby public job board API.

Ashby API (no auth required):
  GET https://api.ashbyhq.com/posting-api/job-board/{identifier}

Returns a JSON object with a jobPostings array — all fields including
salary, description, and apply URL are in one response per company.
No separate detail call needed (unlike Greenhouse).
"""

from __future__ import annotations

import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from parse_jobs import RawJob

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"


# ── HTTP session ─────────────────────────────────────────────────

def _build_session(rules: dict) -> requests.Session:
    http = rules.get("http", {})
    retry = Retry(
        total=http.get("retry_attempts", 3),
        backoff_factor=http.get("retry_backoff_factor", 1.5),
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = http.get("user_agent", "JobBot/1.0")
    return s


# ── Quick pre-filters ────────────────────────────────────────────

def _title_ok(title: str, rules: dict) -> bool:
    import re
    t = title.lower()
    for pat in rules.get("title_exclude_patterns", []):
        if re.search(pat, t):
            return False
    for pat in rules.get("target_title_patterns", []):
        if re.search(pat, t):
            return True
    return False


def _location_ok(location: str, rules: dict) -> bool:
    if not location:
        return True
    loc = location.lower()
    for ex in rules.get("location_exclude_strict", []):
        if ex.lower() in loc:
            for inc in rules.get("location_include", []):
                if inc.lower() in loc:
                    return True
            return False
    for inc in rules.get("location_include", []):
        if inc.lower() in loc:
            return True
    return True


# ── Salary extraction ────────────────────────────────────────────

def _extract_salary(posting: dict) -> str:
    """
    Ashby exposes compensation as a structured object:
    {
      "minValue": 70000,
      "maxValue": 90000,
      "currencyCode": "USD",
      "interval": "YEARLY"  | "HOURLY" | "MONTHLY"
    }
    """
    comp = posting.get("compensation") or posting.get("compensationTierSummary") or {}
    if not comp:
        return ""

    min_v = comp.get("minValue") or comp.get("min")
    max_v = comp.get("maxValue") or comp.get("max")
    currency = comp.get("currencyCode", "USD")
    interval = comp.get("interval", "YEARLY")

    if min_v or max_v:
        interval_label = {
            "YEARLY": "/ year",
            "HOURLY": "/ hour",
            "MONTHLY": "/ month",
        }.get(interval, "")
        return f"{currency} {min_v or '?'}–{max_v or '?'} {interval_label}".strip()

    # Some Ashby boards expose a free-text summary instead
    summary = comp.get("summary") or comp.get("label") or ""
    return str(summary) if summary else ""


# ── Location extraction ──────────────────────────────────────────

def _extract_location(posting: dict) -> str:
    """
    Ashby location can be:
      - posting["location"]                    string
      - posting["locationStr"]                 string
      - posting["office"]["location"]["city"]  nested object
      - posting["isRemote"] == True            → "Remote"
    """
    loc = (
        posting.get("locationStr")
        or posting.get("location")
        or ""
    )
    if loc:
        return str(loc)

    office = posting.get("office") or {}
    office_loc = office.get("location") or {}
    city = office_loc.get("city") or ""
    state = office_loc.get("state") or ""
    if city:
        return f"{city}, {state}".strip(", ")

    if posting.get("isRemote"):
        return "Remote"

    return ""


# ── Main fetcher ─────────────────────────────────────────────────

def fetch_ashby(company: dict, rules: dict) -> list[RawJob]:
    """
    Fetch RawJob list for one Ashby company entry from companies.yaml.
    """
    token = company["board_token"]
    name = company["name"]
    timeout = rules.get("http", {}).get("timeout_seconds", 15)
    session = _build_session(rules)

    url = _BASE_URL.format(token=token)
    try:
        r = session.get(url, timeout=timeout, params={"includeCompensation": "true"})
        r.raise_for_status()
        data = r.json()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        logger.warning("[AB] %s: HTTP %s — skipping", name, code)
        return []
    except Exception as e:
        logger.error("[AB] %s: fetch error: %s", name, e)
        return []

    postings: list[dict] = data.get("jobPostings", [])
    logger.info("[AB] %s: %d total postings", name, len(postings))
    results: list[RawJob] = []

    for posting in postings:
        title: str = posting.get("title", "")
        location: str = _extract_location(posting)
        job_id: str = posting.get("id", "")
        posting_url: str = posting.get("jobUrl", posting.get("externalLink", ""))
        apply_url: str = posting.get("applyLink", posting_url)

        if not _title_ok(title, rules):
            continue
        if not _location_ok(location, rules):
            continue

        salary_raw = _extract_salary(posting)
        description = (
            posting.get("descriptionPlain")
            or posting.get("descriptionHtml")
            or posting.get("description")
            or ""
        )

        time.sleep(0.1)

        results.append(RawJob(
            ats="ashby",
            ats_job_id=job_id,
            company=name,
            company_priority=company.get("priority", 3),
            title=title,
            location=location,
            salary_raw=salary_raw,
            description=description,
            posting_url=posting_url,
            apply_url=apply_url,
        ))

    logger.info("[AB] %s: %d passed pre-filter", name, len(results))
    return results
