"""
main.py
────────────────────────────────────────────────────────────────────
Orchestrator for the daily NYC Finance Job Tracker run.

Pipeline:
  1. Load config (companies.yaml, rules.yaml)
  2. Read existing sheet rows (for dedup)
  3. Fetch jobs from Greenhouse + Lever + Ashby companies
  4. Parse + hard-filter each raw job
  5. Score all clean jobs
  6. Deduplicate against existing rows (+ within run)
  7. Append new jobs to Google Sheet
  8. Print run summary

Environment variables (required):
  GOOGLE_CREDENTIALS_PATH   path to service account JSON key
  GOOGLE_SPREADSHEET_ID     spreadsheet ID from the Sheets URL

Optional:
  DRY_RUN=1                 fetch and parse but do NOT write to sheet
  LOG_LEVEL=DEBUG           set log verbosity (default: INFO)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
sys.path.insert(0, str(Path(__file__).parent))

from fetch_greenhouse import fetch_greenhouse
from fetch_lever import fetch_lever
from fetch_ashby import fetch_ashby
from parse_jobs import RawJob, parse_job, CleanJob
from dedupe import build_existing_set, deduplicate
from score import score_all
import sheets as sh


# ── Logging ──────────────────────────────────────────────────────

def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Config ────────────────────────────────────────────────────────

def load_companies() -> dict:
    with open(CONFIG_DIR / "companies.yaml") as f:
        return yaml.safe_load(f)


def load_rules() -> dict:
    with open(CONFIG_DIR / "rules.yaml") as f:
        return yaml.safe_load(f)


def build_priority_map(companies: dict) -> dict[str, int]:
    m: dict[str, int] = {}
    for ats in ("greenhouse", "lever", "ashby"):
        for entry in companies.get(ats, []):
            m[entry["name"]] = entry.get("priority", 3)
    return m


# ── Fetchers ──────────────────────────────────────────────────────

def _fetch_ats(ats_name: str, fetcher, company_list: list, rules: dict) -> list[RawJob]:
    logger = logging.getLogger("main")
    logger.info("Fetching %d %s companies …", len(company_list), ats_name.upper())
    results: list[RawJob] = []
    for co in company_list:
        try:
            results.extend(fetcher(co, rules))
        except Exception as e:
            logger.error("[%s] %s: unhandled error: %s", ats_name, co["name"], e)
        time.sleep(0.5)
    return results


# ── Main pipeline ─────────────────────────────────────────────────

def run() -> None:
    _setup_logging()
    logger = logging.getLogger("main")
    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"

    if dry_run:
        logger.info("=== DRY RUN MODE — sheet will NOT be updated ===")

    # 1 — Config
    logger.info("Loading config …")
    companies = load_companies()
    rules = load_rules()
    priority_map = build_priority_map(companies)
    logger.info(
        "Companies loaded: %d Greenhouse | %d Lever | %d Ashby = %d total",
        len(companies.get("greenhouse", [])),
        len(companies.get("lever", [])),
        len(companies.get("ashby", [])),
        sum(len(companies.get(k, [])) for k in ("greenhouse", "lever", "ashby")),
    )

    # 2 — Read existing sheet rows
    existing_rows: list[dict] = []
    if not dry_run:
        logger.info("Reading existing sheet rows …")
        try:
            sh.ensure_header()
            existing_rows = sh.read_existing_rows()
        except Exception as e:
            logger.error("Failed to read sheet: %s", e)
            sys.exit(1)

    id_set, composite_set = build_existing_set(existing_rows)

    # 3 — Fetch from all three ATS sources
    raw_jobs: list[RawJob] = []
    raw_jobs += _fetch_ats("greenhouse", fetch_greenhouse, companies.get("greenhouse", []), rules)
    raw_jobs += _fetch_ats("lever",      fetch_lever,      companies.get("lever", []),      rules)
    raw_jobs += _fetch_ats("ashby",      fetch_ashby,      companies.get("ashby", []),       rules)
    logger.info("Total raw candidates after pre-filter: %d", len(raw_jobs))

    # 4 — Parse + hard-filter
    clean_jobs: list[CleanJob] = []
    dropped = 0
    for raw in raw_jobs:
        result = parse_job(raw, rules)
        if result is None:
            dropped += 1
        else:
            clean_jobs.append(result)
    logger.info(
        "After hard-filter: %d kept, %d dropped (salary/exp/sponsorship)",
        len(clean_jobs), dropped,
    )

    # 5 — Score
    scored_jobs = score_all(clean_jobs, rules, priority_map)

    # 6 — Deduplicate
    dedup = deduplicate(scored_jobs, id_set, composite_set)
    new_jobs = dedup.new_jobs

    # 7 — Write
    if dry_run:
        logger.info("[DRY RUN] Would append %d new rows:", len(new_jobs))
        for j in new_jobs[:25]:
            logger.info(
                "  [%s] %s @ %s | %s | score=%d",
                j.priority_bucket, j.title, j.company, j.location, j.priority_score,
            )
        if len(new_jobs) > 25:
            logger.info("  … and %d more", len(new_jobs) - 25)
    else:
        if new_jobs:
            sh.append_jobs(new_jobs)
        else:
            logger.info("No new jobs to append.")

    # 8 — Summary
    logger.info(
        "\n"
        "═══════════════════════════════════════════════\n"
        "  Run complete\n"
        "  Raw fetched (pre-filter) : %d\n"
        "  Hard-filtered (dropped)  : %d\n"
        "  Duplicates skipped       : %d\n"
        "  NEW rows added           : %d\n"
        "    ├─ High                : %d\n"
        "    ├─ Medium              : %d\n"
        "    └─ Low                 : %d\n"
        "═══════════════════════════════════════════════",
        len(raw_jobs),
        dropped,
        dedup.duplicate_count,
        len(new_jobs),
        sum(1 for j in new_jobs if j.priority_bucket == "High"),
        sum(1 for j in new_jobs if j.priority_bucket == "Medium"),
        sum(1 for j in new_jobs if j.priority_bucket == "Low"),
    )


if __name__ == "__main__":
    run()
