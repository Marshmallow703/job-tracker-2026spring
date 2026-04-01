"""
sheets.py
────────────────────────────────────────────────────────────────────
Google Sheets integration using the official Sheets API v4 via the
google-api-python-client library.

Design:
  - All reads and writes go to a single sheet tab called "Jobs".
  - Reads existing rows once per run (used for deduplication).
  - Appends new rows in a single batchUpdate call.
  - Never modifies existing rows.
  - Automatically creates the header row if the sheet is empty.

Auth:
  - Service account JSON key, path set via env var GOOGLE_CREDENTIALS_PATH.
  - Share the spreadsheet with the service account email (editor).

Environment variables required:
  GOOGLE_CREDENTIALS_PATH   path to service account JSON key file
  GOOGLE_SPREADSHEET_ID     the 44-char ID from the Sheets URL
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from parse_jobs import CleanJob

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_TAB = "Jobs"

# Ordered column headers — must match CleanJob fields exactly
COLUMNS = [
    "job_id",
    "company",
    "title",
    "location",
    "salary_raw",
    "source_type",
    "posting_url",
    "apply_url",
    "priority_score",
    "priority_bucket",
    "job_summary",
    "date_found",
    "run_timestamp",
    "applied_status",
    "notes",
]


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_credentials() -> Credentials:
    creds_path = os.environ.get("GOOGLE_CREDENTIALS_PATH")
    if not creds_path:
        raise EnvironmentError(
            "GOOGLE_CREDENTIALS_PATH env var not set. "
            "Point it at your service account JSON key file."
        )
    if not os.path.isfile(creds_path):
        raise FileNotFoundError(f"Credentials file not found: {creds_path}")
    return Credentials.from_service_account_file(creds_path, scopes=SCOPES)


def _get_spreadsheet_id() -> str:
    sid = os.environ.get("GOOGLE_SPREADSHEET_ID")
    if not sid:
        raise EnvironmentError(
            "GOOGLE_SPREADSHEET_ID env var not set. "
            "Copy the ID from your Sheets URL: "
            "https://docs.google.com/spreadsheets/d/<ID>/edit"
        )
    return sid


# ── Client ────────────────────────────────────────────────────────────────────

def _build_client():
    creds = _get_credentials()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# ── Read existing rows ────────────────────────────────────────────────────────

def read_existing_rows(spreadsheet_id: str | None = None) -> list[dict[str, str]]:
    """
    Read all current rows from the Jobs tab.
    Returns a list of dicts keyed by column header.
    Returns [] if the sheet is empty or has only a header row.
    """
    sid = spreadsheet_id or _get_spreadsheet_id()
    service = _build_client()
    range_name = f"{SHEET_TAB}!A1:ZZ"

    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=range_name)
            .execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            logger.warning("[sheets] Spreadsheet not found: %s", sid)
        raise

    values: list[list[str]] = result.get("values", [])
    if not values:
        return []

    headers = values[0]
    rows = []
    for row in values[1:]:
        # Pad short rows
        padded = row + [""] * (len(headers) - len(row))
        rows.append(dict(zip(headers, padded)))

    logger.info("[sheets] Read %d existing rows", len(rows))
    return rows


# ── Ensure header row ─────────────────────────────────────────────────────────

def ensure_header(spreadsheet_id: str | None = None) -> None:
    """
    Write the header row if the sheet is completely empty.
    Safe to call on every run.
    """
    sid = spreadsheet_id or _get_spreadsheet_id()
    service = _build_client()
    range_name = f"{SHEET_TAB}!A1:A1"

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sid, range=range_name)
        .execute()
    )
    existing = result.get("values", [])
    if existing and existing[0]:
        return  # header already present

    logger.info("[sheets] Writing header row")
    service.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [COLUMNS]},
    ).execute()


# ── Append new rows ───────────────────────────────────────────────────────────

def append_jobs(
    jobs: list[CleanJob],
    spreadsheet_id: str | None = None,
) -> int:
    """
    Append new jobs as rows to the Jobs tab.
    Uses APPEND mode — never overwrites existing data.

    Returns
    -------
    int : number of rows successfully appended.
    """
    if not jobs:
        logger.info("[sheets] Nothing to append.")
        return 0

    sid = spreadsheet_id or _get_spreadsheet_id()
    service = _build_client()

    rows = [_job_to_row(j) for j in jobs]

    try:
        result = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=sid,
                range=f"{SHEET_TAB}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            )
            .execute()
        )
        updates = result.get("updates", {})
        n = updates.get("updatedRows", len(rows))
        logger.info("[sheets] Appended %d rows", n)
        return n
    except HttpError as e:
        logger.error("[sheets] Append failed: %s", e)
        raise


# ── Row serialiser ────────────────────────────────────────────────────────────

def _job_to_row(job: CleanJob) -> list[Any]:
    """Convert a CleanJob to an ordered list of cell values."""
    return [
        job.job_id,
        job.company,
        job.title,
        job.location,
        job.salary_raw,
        job.source_type,
        job.posting_url,
        job.apply_url,
        job.priority_score,
        job.priority_bucket,
        job.job_summary,
        job.date_found,
        job.run_timestamp,
        job.applied_status,
        job.notes,
    ]
