"""
fetch_lever.py
────────────────────────────────────────────────────────────────────
Fetches job listings from the Lever public JSON API.

Lever posting API (no auth required):
  GET https://api.lever.co/v0/postings/{company}?mode=json

Returns an array of posting objects — no separate detail call needed.
All fields (title, location, description, salary, apply URL) are in
the same response object.
"""

from __future__ import annotations

import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from parse_jobs import RawJob

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.lever.co/v0/postings/{token}?mode=json"


# ── HTTP session ─────────────────────────────────────────────────────────────

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


# ── Quick pre-filters (same logic as Greenhouse, inlined for independence) ───

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
        return True  # missing → keep for manual review
    loc = location.lower()
    for ex in rules.get("location_exclude_strict", []):
        if ex.lower() in loc:
            # Drop unless there is also a US/include signal in the same string
            for inc in rules.get("location_include", []):
                if inc.lower() in loc:
                    return True
            return False
    for inc in rules.get("location_include", []):
        if inc.lower() in loc:
            return True
    return False  # location present but no US signal → drop


# ── Salary extraction ────────────────────────────────────────────────────────

def _extract_salary(posting: dict) -> str:
    """
    Lever may expose salary in:
      - posting["salaryRange"]  (some boards)
      - posting["compensation"] (rare)
      - the plain-text description
    We return raw text; parse_jobs.py handles the structured parse.
    """
    sr = posting.get("salaryRange") or posting.get("salary_range") or {}
    if sr:
        min_s = sr.get("min", "")
        max_s = sr.get("max", "")
        currency = sr.get("currency", "USD")
        interval = sr.get("interval", "year")
        if min_s or max_s:
            return f"{currency} {min_s}–{max_s} / {interval}"

    comp = posting.get("compensation") or posting.get("pay") or ""
    if comp:
        return str(comp)

    # Try to find salary mention in lists (Lever structures content as lists)
    for lst in posting.get("lists", []):
        content = (lst.get("content") or "").lower()
        if any(k in content for k in ("salary", "compensation", "$", "usd")):
            return lst.get("content", "")[:300]

    return ""


# ── Description assembly ─────────────────────────────────────────────────────

def _build_description(posting: dict) -> str:
    """Concatenate Lever's structured lists into a plain-text description."""
    parts = []
    description = posting.get("descriptionPlain") or posting.get("description", "")
    if description:
        parts.append(description)
    for lst in posting.get("lists", []):
        text = lst.get("text", "")
        content = lst.get("content", "")
        if text:
            parts.append(f"\n{text}:\n{content}")
    return "\n".join(parts)


# ── Main fetcher ─────────────────────────────────────────────────────────────

def fetch_lever(company: dict, rules: dict) -> list[RawJob]:
    """
    Fetch RawJob list for one Lever company entry from companies.yaml.
    All data is in a single API call per company.
    """
    token = company["board_token"]
    name = company["name"]
    timeout = rules.get("http", {}).get("timeout_seconds", 15)
    session = _build_session(rules)

    url = _BASE_URL.format(token=token)
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        postings: list[dict] = r.json()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        logger.warning("[LV] %s: HTTP %s — skipping", name, code)
        return []
    except Exception as e:
        logger.error("[LV] %s: fetch error: %s", name, e)
        return []

    logger.info("[LV] %s: %d total postings", name, len(postings))
    results: list[RawJob] = []

    for posting in postings:
        title: str = posting.get("text", "")
        # Lever location can be a string or inside "workplaceType" / "categories"
        categories = posting.get("categories") or {}
        location: str = (
            posting.get("location", "")
            or categories.get("location", "")
            or categories.get("city", "")
        )
        job_id: str = posting.get("id", "")
        posting_url: str = posting.get("hostedUrl", "")
        apply_url: str = posting.get("applyUrl", posting_url)

        if not _title_ok(title, rules):
            continue
        if not _location_ok(location, rules):
            continue

        salary_raw = _extract_salary(posting)
        description = _build_description(posting)

        time.sleep(0.1)  # polite pacing; Lever is one request per company

        results.append(RawJob(
            ats="lever",
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

    logger.info("[LV] %s: %d passed pre-filter", name, len(results))
    return results
