"""
score.py
────────────────────────────────────────────────────────────────────
Assigns a numeric priority_score (0–100) and a priority_bucket
(High / Medium / Low) to each CleanJob.

All weights and thresholds come from rules.yaml → "scoring" section.
"""

from __future__ import annotations

import re
import logging
from typing import Sequence

from parse_jobs import CleanJob, parse_salary_range

logger = logging.getLogger(__name__)


# ── Score a single job ────────────────────────────────────────────────────────

def score_job(job: CleanJob, rules: dict, company_priority: int = 3) -> CleanJob:
    """
    Return the same CleanJob with priority_score and priority_bucket filled in.
    company_priority is passed separately because CleanJob doesn't store it.
    """
    w = rules.get("scoring", {})
    score = 0

    # ── Company priority ─────────────────────────────────────────
    if company_priority == 1:
        score += w.get("company_priority_1", 30)
    elif company_priority == 2:
        score += w.get("company_priority_2", 15)
    else:
        score += w.get("company_priority_3", 5)

    # ── Title quality ────────────────────────────────────────────
    title_lower = job.title.lower()
    score += _title_score(title_lower, rules, w)

    # ── Location ─────────────────────────────────────────────────
    loc_lower = job.location.lower()
    if any(k in loc_lower for k in
           ["new york", "nyc", "manhattan", "brooklyn", "queens", "bronx"]):
        score += w.get("location_nyc", 20)
    elif any(k in loc_lower for k in ["remote", "hybrid"]):
        score += w.get("location_remote", 8)

    # ── Salary ───────────────────────────────────────────────────
    max_usd = float(rules.get("salary_max_usd", 100_000))
    s_min, _ = parse_salary_range(job.salary_raw)
    if s_min is not None:
        if s_min <= max_usd:
            score += w.get("salary_in_range", 10)
    else:
        score += w.get("salary_missing", 5)

    # ── Experience signals in title / summary ────────────────────
    text = (job.title + " " + job.job_summary).lower()
    junior_patterns = [
        r"\bentry.?level\b", r"\bnew grad\b", r"\brecent grad\b",
        r"\b0.?2\s*year", r"\bjunior\b", r"\bassociate\b", r"\b1.?2\s*year",
    ]
    if any(re.search(p, text) for p in junior_patterns):
        score += w.get("experience_junior", 10)

    # ── Cap + bucket ─────────────────────────────────────────────
    score = min(100, max(0, score))
    job.priority_score = score
    job.priority_bucket = _bucket(score, rules)
    return job


def _title_score(title_lower: str, rules: dict, weights: dict) -> int:
    exact_targets = [
        "financial analyst", "fp&a analyst", "strategic finance analyst",
        "business finance analyst", "corporate finance analyst",
        "revenue finance analyst", "finance operations analyst",
        "client finance analyst", "commercial finance analyst", "finance analyst",
    ]
    for t in exact_targets:
        if t in title_lower:
            return weights.get("title_exact", 30)

    for pat in rules.get("target_title_patterns", []):
        if re.search(pat, title_lower):
            return weights.get("title_partial", 18)

    adjacent = ["business analyst", "operations analyst", "data analyst", "strategy analyst"]
    if any(a in title_lower for a in adjacent):
        return weights.get("title_adjacent", 6)

    return 0


def _bucket(score: int, rules: dict) -> str:
    thresholds = rules.get("priority_buckets", {})
    if score >= thresholds.get("high", 70):
        return "High"
    if score >= thresholds.get("medium", 40):
        return "Medium"
    return "Low"


def score_all(
    jobs: Sequence[CleanJob],
    rules: dict,
    company_priority_map: dict[str, int] | None = None,
) -> list[CleanJob]:
    """
    Score and sort a list of jobs.

    Parameters
    ----------
    company_priority_map : {company_name: priority_int}
        Optional map passed from main.py so score_job can look up priority.
    """
    pri_map = company_priority_map or {}
    scored = [
        score_job(job, rules, pri_map.get(job.company, 3))
        for job in jobs
    ]
    scored.sort(key=lambda j: j.priority_score, reverse=True)
    logger.info(
        "[score] %d jobs — High: %d  Medium: %d  Low: %d",
        len(scored),
        sum(1 for j in scored if j.priority_bucket == "High"),
        sum(1 for j in scored if j.priority_bucket == "Medium"),
        sum(1 for j in scored if j.priority_bucket == "Low"),
    )
    return scored
