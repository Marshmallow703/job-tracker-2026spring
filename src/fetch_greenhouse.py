"""
fetch_greenhouse.py
────────────────────────────────────────────────────────────────────
Fetches job listings from the Greenhouse public JSON API.

Greenhouse board API (no auth required):
  List:   GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs
  Detail: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}

Strategy: pull the full list (one request per company), then fetch
detail only for jobs that survive the quick title + location pre-
filter, to keep HTTP load minimal.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from parse_jobs import RawJob

logger = logging.getLogger(__name__)

_BASE_LIST = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
_BASE_DETAIL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"


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


# ── Quick pre-filters ────────────────────────────────────────────────────────

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


# ── Main fetcher ─────────────────────────────────────────────────────────────

def fetch_greenhouse(company: dict, rules: dict) -> list[RawJob]:
    """
    Fetch RawJob list for one Greenhouse company entry from companies.yaml.
    """
    token = company["board_token"]
    name = company["name"]
    timeout = rules.get("http", {}).get("timeout_seconds", 15)
    session = _build_session(rules)

    # 1 — job list
    try:
        r = session.get(_BASE_LIST.format(token=token), timeout=timeout)
        r.raise_for_status()
        jobs: list[dict[str, Any]] = r.json().get("jobs", [])
    except requests.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        logger.warning("[GH] %s: HTTP %s on list — skipping", name, code)
        return []
    except Exception as e:
        logger.error("[GH] %s: list fetch error: %s", name, e)
        return []

    logger.info("[GH] %s: %d total postings", name, len(jobs))
    results: list[RawJob] = []

    for job in jobs:
        title: str = job.get("title", "")
        location: str = (job.get("location") or {}).get("name", "")
        job_id = str(job.get("id", ""))
        posting_url: str = job.get("absolute_url", "")

        if not _title_ok(title, rules):
            continue
        if not _location_ok(location, rules):
            continue

        # 2 — full detail (salary, description)
        description = salary_raw = ""
        apply_url = posting_url

        try:
            dr = session.get(
                _BASE_DETAIL.format(token=token, job_id=job_id), timeout=timeout
            )
            dr.raise_for_status()
            detail = dr.json()
            description = detail.get("content", "")
            apply_url = detail.get("absolute_url", posting_url)
            salary_raw = (
                _salary_from_metadata(detail.get("metadata") or [])
                or _salary_from_fields(detail.get("custom_fields") or [])
            )
        except Exception as e:
            logger.warning("[GH] %s/%s: detail error: %s", name, job_id, e)

        time.sleep(0.3)

        results.append(RawJob(
            ats="greenhouse",
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

    logger.info("[GH] %s: %d passed pre-filter", name, len(results))
    return results


# ── Salary helpers ───────────────────────────────────────────────────────────

def _salary_from_metadata(metadata: list[dict]) -> str:
    for item in metadata:
        if any(k in (item.get("name") or "").lower()
               for k in ("salary", "compensation", "pay", "wage")):
            v = item.get("value")
            if v:
                return str(v)
    return ""


def _salary_from_fields(fields: list[dict]) -> str:
    for f in fields:
        if any(k in (f.get("name") or "").lower()
               for k in ("salary", "compensation", "pay")):
            v = f.get("value")
            if v:
                return str(v)
    return ""
