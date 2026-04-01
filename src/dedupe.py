"""
dedupe.py
────────────────────────────────────────────────────────────────────
Provides deduplication for the append-only job tracker.

Design:
  - The "existing set" is built from whatever is already in the Sheet
    (loaded by sheets.py before the run starts).
  - A job is a duplicate if its stable job_id matches any existing id.
  - As a belt-and-suspenders check we also test against a composite
    key (company_norm + title_norm + url) in case job_id diverged
    between runs due to URL changes.
  - New jobs are returned in insertion order.
  - Nothing is ever modified or deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from parse_jobs import CleanJob, _normalize, make_job_id

logger = logging.getLogger(__name__)


@dataclass
class DedupeResult:
    new_jobs: list[CleanJob]
    duplicate_count: int
    total_candidates: int


def build_existing_set(sheet_rows: list[dict]) -> tuple[set[str], set[str]]:
    """
    Build two lookup sets from existing sheet rows.

    Parameters
    ----------
    sheet_rows : list[dict]
        Each dict is a row from the Sheet, keyed by column header.

    Returns
    -------
    (id_set, composite_set)
      id_set        : set of existing job_id values
      composite_set : set of "company_norm|title_norm|url" strings
    """
    id_set: set[str] = set()
    composite_set: set[str] = set()

    for row in sheet_rows:
        jid = (row.get("job_id") or "").strip()
        if jid:
            id_set.add(jid)

        company = _normalize(row.get("company") or "")
        title = _normalize(row.get("title") or "")
        url = (row.get("posting_url") or "").strip().lower()
        composite_key = f"{company}|{title}|{url}"
        if company and title:
            composite_set.add(composite_key)

    logger.info(
        "[dedupe] Loaded %d existing ids, %d composite keys",
        len(id_set), len(composite_set),
    )
    return id_set, composite_set


def deduplicate(
    candidates: Sequence[CleanJob],
    id_set: set[str],
    composite_set: set[str],
) -> DedupeResult:
    """
    Filter candidates to only those that are genuinely new.

    Parameters
    ----------
    candidates    : jobs from this run (already scored)
    id_set        : existing job_id values from the sheet
    composite_set : existing composite keys from the sheet

    Returns
    -------
    DedupeResult with new_jobs list + stats
    """
    new_jobs: list[CleanJob] = []
    seen_this_run_ids: set[str] = set()
    seen_this_run_composite: set[str] = set()
    dup_count = 0

    for job in candidates:
        jid = job.job_id
        composite = (
            f"{job.company_normalized}|{job.title_normalized}"
            f"|{job.posting_url.strip().lower()}"
        )

        # Check against sheet
        if jid in id_set or composite in composite_set:
            dup_count += 1
            logger.debug("[dedupe] SKIP (already in sheet): %s — %s", job.company, job.title)
            continue

        # Check within this run (guard against same job from 2 fetchers)
        if jid in seen_this_run_ids or composite in seen_this_run_composite:
            dup_count += 1
            logger.debug("[dedupe] SKIP (dup in run): %s — %s", job.company, job.title)
            continue

        seen_this_run_ids.add(jid)
        seen_this_run_composite.add(composite)
        new_jobs.append(job)

    logger.info(
        "[dedupe] %d new | %d duplicates | %d total candidates",
        len(new_jobs), dup_count, len(candidates),
    )
    return DedupeResult(
        new_jobs=new_jobs,
        duplicate_count=dup_count,
        total_candidates=len(candidates),
    )
