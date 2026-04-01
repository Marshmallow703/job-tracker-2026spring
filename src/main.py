"""
main.py
────────────────────────────────────────────────────────────────────
Orchestrator for the daily NYC Finance Job Tracker run.

Pipeline:
  1. Load config (companies.yaml, rules.yaml)
  2. Read existing sheet rows (for dedup)
  3. Fetch jobs from Greenhouse companies
  4. Fetch jobs from Lever companies
  5. Parse + hard-filter each raw job
  6. Score all clean jobs
  7. Deduplicate against existing rows (+ within run)
  8. Append new jobs to Google Sheet
  9. Print run summary

Usage:
  python main.py

Environment variables (required):
  GOOGLE_CREDENTIALS_PATH   path to service account JSON key
  GOOGLE_SPREADSHEET_ID     spreadsheet ID from the Sheets URL

Optional:
  DRY_RUN=1                 fetch and parse but do NOT write to sheet
  LOG_LEVEL=DEBUG            set log verbosity (default: INFO)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import yaml

# ── Resolve project root (one level up from src/) ───────────────────────────
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# Add src/ to path so relative imports work when invoked directly
sys.path.insert(0, str(Path(__file__).parent))

from fetch_greenhouse import fetch_greenhouse
from fetch_lever import fetch_lever
from parse_jobs import RawJob, parse_job, CleanJob
from dedupe import build_existing_set, deduplicate
from score import score_all
import sheets as sh


# ── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Config loaders ────────────────────────────────────────────────────────────

def load_companies() -> dict:
    path = CONFIG_DIR / "companies.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def load_rules() -> dict:
    path = CONFIG_DIR / "rules.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def build_priority_map(companies: dict) -> dict[str, int]:
    """Return {company_name: priority_int} for all companies."""
    m: dict[str, int] = {}
    for entry in companies.get("greenhouse", []):
        m[entry["name"]] = entry.get("priority", 3)
    for entry in companies.get("lever", []):
        m[entry["name"]] = entry.get("priority", 3)
    return m


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def fetch_all_greenhouse(companies: dict, rules: dict) -> list[RawJob]:
    all_raw: list[RawJob] = []
    gh_list = companies.get("greenhouse", [])
    logger = logging.getLogger("main")
    logger.info("Fetching %d Greenhouse companies …", len(gh_list))
    for co in gh_list:
        try:
            raw = fetch_greenhouse(co, rules)
            all_raw.extend(raw)
        except Exception as e:
            logger.error("Greenhouse %s: unhandled error: %s", co["name"], e)
        time.sleep(0.5)
    return all_raw


def fetch_all_lever(companies: dict, rules: dict) -> list[RawJob]:
    all_raw: list[RawJob] = []
    lv_list = companies.get("lever", [])
    logger = logging.getLogger("main")
    logger.info("Fetching %d Lever companies …", len(lv_list))
    for co in lv_list:
        try:
            raw = fetch_lever(co, rules)
            all_raw.extend(raw)
        except Exception as e:
            logger.error("Lever %s: unhandled error: %s", co["name"], e)
        time.sleep(0.5)
    return all_raw


# ── Main pipeline ─────────────────────────────────────────────────────────────

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

    # 2 — Read existing sheet rows
    existing_rows: list[dict] = []
    if not dry_run:
        logger.info("Reading existing sheet rows …")
        try:
            sh.ensure_header()
            existing_rows = sh.read_existing_rows()
        except Exception as e:
            logger.error("Failed to read sheet: %s", e)
            logger.error("Aborting — cannot deduplicate without sheet data.")
            sys.exit(1)

    id_set, composite_set = build_existing_set(existing_rows)

    # 3+4 — Fetch
    raw_jobs: list[RawJob] = []
    raw_jobs.extend(fetch_all_greenhouse(companies, rules))
    raw_jobs.extend(fetch_all_lever(companies, rules))
    logger.info("Total raw candidates after pre-filter: %d", len(raw_jobs))

    # 5 — Parse + hard-filter
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

    # 6 — Score
    scored_jobs = score_all(clean_jobs, rules, priority_map)

    # 7 — Deduplicate
    dedup = deduplicate(scored_jobs, id_set, composite_set)
    new_jobs = dedup.new_jobs

    # 8 — Write
    if dry_run:
        logger.info("[DRY RUN] Would append %d new rows:", len(new_jobs))
        for j in new_jobs[:20]:
            logger.info(
                "  [%s] %s @ %s | %s | score=%d",
                j.priority_bucket, j.title, j.company, j.location, j.priority_score,
            )
        if len(new_jobs) > 20:
            logger.info("  … and %d more", len(new_jobs) - 20)
    else:
        if new_jobs:
            appended = sh.append_jobs(new_jobs)
            logger.info("Appended %d new rows to sheet.", appended)
        else:
            logger.info("No new jobs to append.")

    # 9 — Summary
    n_high   = sum(1 for j in new_jobs if j.priority_bucket == "High")
    n_medium = sum(1 for j in new_jobs if j.priority_bucket == "Medium")
    n_low    = sum(1 for j in new_jobs if j.priority_bucket == "Low")

    logger.info(
        "\n"
        "═══════════════════════════════════════\n"
        "  Run complete\n"
        "  Raw fetched   : %d\n"
        "  Hard-filtered : %d\n"
        "  Duplicates    : %d\n"
        "  NEW rows added: %d  (High: %d  Medium: %d  Low: %d)\n"
        "═══════════════════════════════════════",
        len(raw_jobs),
        dropped,
        dedup.duplicate_count,
        len(new_jobs),
        n_high,
        n_medium,
        n_low,
    )

    if new_jobs:
        _print_new_jobs_banner(len(new_jobs), n_high, n_medium)


def _print_new_jobs_banner(total: int, n_high: int, n_medium: int) -> None:
    """
    Print a visually prominent summary to stdout when new jobs are found.
    Uses a GitHub Actions ::notice:: annotation so it also appears in the
    workflow annotations panel (visible at the top of the Actions run page).
    """
    border = "=" * 50
    msg_lines = [
        "",
        border,
        f"  NEW JOBS FOUND: {total}",
        f"  High priority : {n_high}",
        f"  Medium priority: {n_medium}",
        border,
        "",
    ]
    print("\n".join(msg_lines), flush=True)

    # GitHub Actions annotation — shows up in the summary / annotations tab
    annotation = (
        f"New jobs found: {total} total "
        f"({n_high} High, {n_medium} Medium)"
    )
    print(f"::notice title=New Jobs Found::{annotation}", flush=True)


if __name__ == "__main__":
    run()
