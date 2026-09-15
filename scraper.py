#!/usr/bin/env python3
"""
scraper.py -- AYUSH Research Portal Bulk Data Collector

Robustly collects all publicly accessible research metadata and PDFs
from https://ayushportal.nic.in/

ARCHITECTURE
------------
Phase 1 (metadata): Enumerate record IDs, fetch each ShowDefault.aspx?IDD=<N>
                    page, parse all fields, write to CSV/JSONL incrementally.
Phase 2 (pdfs):     Read the collected metadata, download PDFs for records
                    that have a valid full_paper_url and pdf_status == pending.
Phase 3 (retry):    Re-attempt previously failed records.

USAGE
-----
  python scraper.py metadata           # collect metadata
  python scraper.py pdfs               # download PDFs
  python scraper.py retry-failed       # retry failed records
  python scraper.py stats              # show statistics
  python scraper.py --dry-run          # inspect 10 records only
  python scraper.py --limit 100        # stop after 100 records
  python scraper.py --system Ayurveda  # filter by medical system
  python scraper.py --start-id 5000    # override start ID
  python scraper.py --end-id 25000     # override end ID
  python scraper.py --workers 5        # override concurrency

DISCOVERY NOTES
---------------
Portal inspection (2026-09-15) revealed:
- URL: https://ayushportal.nic.in/ShowDefault.aspx?IDD=<N>
- robots.txt: Allow: / (no access restrictions)
- ID range scanned: 2000-300000 (sparse; many gaps)
- Valid record detection: title != "Article ID"
- Span IDs are stable (ctl00_ContentPlaceHolder1_<field>)
- Medical system not on record page; inferred from keywords/disease
- Journal field: "Name | Year: YYYY | Volume: V | Issue: I | Page: P"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
import config

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def setup_logging(log_path: str) -> logging.Logger:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ayush_scraper")
    logger.setLevel(logging.DEBUG)

    # Console handler (INFO+)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
    logger.addHandler(ch)

    # File handler (DEBUG+)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
    logger.addHandler(fh)

    return logger


log = setup_logging(config.SCRAPER_LOG)

# ---------------------------------------------------------------------------
# CSV column schema
# ---------------------------------------------------------------------------
CSV_COLUMNS = [
    "record_id",
    "medical_system",
    "research_category",
    "title",
    "authors",
    "journal_raw",
    "journal_name",
    "publication_year",
    "publication_date",
    "volume",
    "issue",
    "pages",
    "institution",
    "corresponding_address",
    "disease",
    "keywords",
    "abstract",
    "full_paper_url",
    "pdf_status",
    "pdf_filename",
    "visits",
    "downloads",
    "source_url",
    "record_url",
    "scraped_at",
    "status",
]

# ---------------------------------------------------------------------------
# HTTP Session builder
# ---------------------------------------------------------------------------
def build_session() -> requests.Session:
    session = requests.Session()
    session.verify = config.VERIFY_SSL
    session.headers.update({"User-Agent": config.USER_AGENT})
    adapter = requests.adapters.HTTPAdapter(
        max_retries=0,  # We handle retries manually
        pool_connections=config.MAX_WORKERS + 2,
        pool_maxsize=config.MAX_WORKERS + 2,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Polite request helper (with exponential backoff)
# ---------------------------------------------------------------------------
def polite_get(
    session: requests.Session,
    url: str,
    delay: bool = True,
    stream: bool = False,
    timeout: Optional[tuple] = None,
) -> Optional[requests.Response]:
    """
    Fetch URL with retries and exponential backoff.
    Returns None if all retries are exhausted.
    """
    if delay:
        jitter = random.uniform(-config.REQUEST_JITTER, config.REQUEST_JITTER)
        time.sleep(max(0.1, config.REQUEST_DELAY + jitter))

    if timeout is None:
        timeout = (config.CONNECT_TIMEOUT, config.READ_TIMEOUT)

    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=timeout, stream=stream)
            if resp.status_code == 429:
                wait = 30 * attempt
                log.warning("Rate limited (429). Sleeping %ds before retry %d.", wait, attempt)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = config.RETRY_BACKOFF ** attempt
                log.warning("Server error %d on %s. Retry %d in %.1fs.", resp.status_code, url, attempt, wait)
                time.sleep(wait)
                continue
            return resp
        except requests.exceptions.ConnectionError as exc:
            wait = config.RETRY_BACKOFF ** attempt
            log.debug("Connection error on %s (attempt %d): %s. Retry in %.1fs.", url, attempt, exc, wait)
            time.sleep(wait)
        except requests.exceptions.Timeout as exc:
            wait = config.RETRY_BACKOFF ** attempt
            log.debug("Timeout on %s (attempt %d): %s. Retry in %.1fs.", url, attempt, exc, wait)
            time.sleep(wait)
        except requests.exceptions.RequestException as exc:
            log.warning("Request error on %s (attempt %d): %s", url, attempt, exc)
            break

    log.error("All %d retries exhausted for %s", config.MAX_RETRIES, url)
    return None


# ---------------------------------------------------------------------------
# HTML parser helpers
# ---------------------------------------------------------------------------
def _get_span_text(soup: BeautifulSoup, span_id: str) -> str:
    """Return stripped text of a span by ID, or empty string."""
    tag = soup.find(id=span_id)
    if tag is None:
        return ""
    return tag.get_text(separator=" ", strip=True)


def _clean(text: str) -> str:
    """Normalise whitespace and strip portal placeholder '--'."""
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"  +", " ", text).strip()
    if text in ("-", "--", "- -", "N/A", "NA"):
        return ""
    return text


def extract_record_id(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_lblID"))


def extract_title(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_ArtTitle"))


def extract_authors(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_Auth"))


def extract_journal_raw(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_Jrnl"))


def extract_institution(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_Desig"))


def extract_corresponding_address(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_Address"))


def extract_disease(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_Disease"))


def extract_keywords(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_Keywords"))


def extract_abstract(soup: BeautifulSoup) -> str:
    return _clean(_get_span_text(soup, "ctl00_ContentPlaceHolder1_Label15"))


def extract_full_paper_url(soup: BeautifulSoup) -> str:
    """
    The full paper URL lives in a span whose id ends with 'lnkF_url'.
    The portal also has a submit button that POSTs to download it.
    We extract the direct URL from the span for link following.
    """
    tag = soup.find(id="ctl00_ContentPlaceHolder1_lnkF_url")
    if tag:
        text = tag.get_text(separator=" ", strip=True)
        if text and text not in ("-", "--"):
            return text.strip()
    return ""


def extract_visits_downloads(soup: BeautifulSoup) -> tuple[str, str]:
    visits_tag = soup.find(id="ctl00_ContentPlaceHolder1_ArtHitLabel")
    downloads_tag = soup.find(id="ctl00_ContentPlaceHolder1_ArtDowldLabel")

    def parse_num(tag, prefix):
        if not tag:
            return ""
        text = tag.get_text(strip=True)
        m = re.search(r"\d+", text)
        return m.group(0) if m else ""

    return parse_num(visits_tag, "Visits"), parse_num(downloads_tag, "Downloads")


def parse_journal_fields(journal_raw: str) -> Dict[str, str]:
    """
    Parse structured journal string such as:
      "Alternative Therapies in Health and Medicine | Year: 2006 | Volume: 12 | Issue: 5 | Page: 24-30"
    or legacy format:
      "Homeopathy | 2006 July | 95 | 3 | 136-143"
    Returns dict with keys: journal_name, publication_year, volume, issue, pages, publication_date
    """
    result = {
        "journal_name": "",
        "publication_year": "",
        "publication_date": "",
        "volume": "",
        "issue": "",
        "pages": "",
    }

    if not journal_raw:
        return result

    # Modern format: pipe-separated with labels
    if "|" in journal_raw and "Year:" in journal_raw:
        parts = [p.strip() for p in journal_raw.split("|")]
        result["journal_name"] = parts[0] if parts else ""
        for part in parts[1:]:
            part = part.strip()
            kv = re.match(r"^(Year|Volume|Issue|Page):\s*(.+)$", part, re.I)
            if kv:
                key, val = kv.group(1).lower(), kv.group(2).strip()
                if key == "year":
                    # Year may be "2006" or "2006 Jun" or "1991 Jun"
                    yr_m = re.search(r"\b(19|20)\d{2}\b", val)
                    result["publication_year"] = yr_m.group(0) if yr_m else ""
                    result["publication_date"] = val
                elif key == "volume":
                    result["volume"] = val
                elif key == "issue":
                    result["issue"] = val
                elif key == "page":
                    result["pages"] = val
        return result

    # Legacy format: "Journal Name | YYYY Month | Vol | Issue | Pages"
    if "|" in journal_raw:
        parts = [p.strip() for p in journal_raw.split("|")]
        result["journal_name"] = parts[0] if len(parts) > 0 else ""
        if len(parts) > 1:
            date_part = parts[1]
            yr_m = re.search(r"\b(19|20)\d{2}\b", date_part)
            result["publication_year"] = yr_m.group(0) if yr_m else ""
            result["publication_date"] = date_part.strip()
        if len(parts) > 2:
            result["volume"] = parts[2].strip()
        if len(parts) > 3:
            result["issue"] = parts[3].strip()
        if len(parts) > 4:
            result["pages"] = parts[4].strip()
        return result

    # Fallback: no pipe separator; entire string is journal name + maybe year
    result["journal_name"] = journal_raw
    yr_m = re.search(r"\b(19|20)\d{2}\b", journal_raw)
    if yr_m:
        result["publication_year"] = yr_m.group(0)
    return result


def infer_medical_system(
    title: str, journal: str, disease: str, keywords: str
) -> str:
    """
    Infer medical system from text fields using keyword matching.
    Returns the best-matching system name or empty string.
    """
    combined = " ".join([title, journal, disease, keywords]).lower()
    scores: Dict[str, int] = {}
    for system, kw_list in config.SYSTEM_KEYWORDS.items():
        score = sum(1 for kw in kw_list if kw in combined)
        if score:
            scores[system] = score
    if scores:
        return max(scores, key=scores.__getitem__)
    return ""


def is_valid_record(soup: BeautifulSoup) -> bool:
    """
    Return True if the page represents a real research record.
    Invalid pages render field labels as their own values (e.g., title = "Article ID").
    """
    title = _get_span_text(soup, "ctl00_ContentPlaceHolder1_ArtTitle")
    if not title:
        return False
    # Strip whitespace variations
    title_clean = " ".join(title.split())
    if title_clean in (config.INVALID_FIELD_SENTINEL, "Title of the Article/ Research work", ""):
        return False
    # Also guard against journal field being "Article ID"
    journal = _get_span_text(soup, "ctl00_ContentPlaceHolder1_Jrnl")
    if " ".join(journal.split()) == config.INVALID_FIELD_SENTINEL:
        return False
    return True


def parse_record(idd: int, soup: BeautifulSoup) -> Dict[str, Any]:
    """
    Extract all fields from a valid record page and return a dict.
    Never raises; returns partial data on parse errors.
    """
    try:
        record_id  = extract_record_id(soup) or str(idd)
        title      = extract_title(soup)
        authors    = extract_authors(soup)
        journal_raw= extract_journal_raw(soup)
        institution= extract_institution(soup)
        address    = extract_corresponding_address(soup)
        disease    = extract_disease(soup)
        keywords   = extract_keywords(soup)
        abstract   = extract_abstract(soup)
        full_url   = extract_full_paper_url(soup)
        visits, downloads = extract_visits_downloads(soup)

        jfields = parse_journal_fields(journal_raw)
        med_sys = infer_medical_system(
            title, jfields["journal_name"], disease, keywords
        )

        record_url = config.RECORD_URL_TEMPLATE.format(idd=idd)
        now_utc    = datetime.now(timezone.utc).isoformat()

        return {
            "record_id":           record_id,
            "medical_system":      med_sys,
            "research_category":   "",     # not present on portal
            "title":               title,
            "authors":             authors,
            "journal_raw":         journal_raw,
            "journal_name":        jfields["journal_name"],
            "publication_year":    jfields["publication_year"],
            "publication_date":    jfields["publication_date"],
            "volume":              jfields["volume"],
            "issue":               jfields["issue"],
            "pages":               jfields["pages"],
            "institution":         institution,
            "corresponding_address": address,
            "disease":             disease,
            "keywords":            keywords,
            "abstract":            abstract,
            "full_paper_url":      full_url,
            "pdf_status":          "pending" if full_url else "unavailable",
            "pdf_filename":        "",
            "visits":              visits,
            "downloads":           downloads,
            "source_url":          "https://ayushportal.nic.in/",
            "record_url":          record_url,
            "scraped_at":          now_utc,
            "status":              "valid",
        }
    except Exception as exc:
        log.warning("Parse error for IDD=%d: %s", idd, exc)
        return {
            "record_id":   str(idd),
            "status":      "parse_error",
            "scraped_at":  datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def validate_record(rec: Dict[str, Any]) -> str:
    """
    Return 'valid', 'empty', or a description of the problem.
    """
    if not rec.get("title"):
        return "missing_title"
    if not rec.get("record_id"):
        return "missing_id"
    year = rec.get("publication_year", "")
    if year:
        try:
            y = int(year)
            if not (1900 <= y <= 2030):
                return "impossible_year"
        except ValueError:
            pass
    return "valid"


def is_suspicious(rec: Dict[str, Any]) -> bool:
    if not rec.get("title"):
        return True
    if not rec.get("record_id"):
        return True
    if not rec.get("authors") and not rec.get("abstract"):
        return True
    year = rec.get("publication_year", "")
    if year:
        try:
            y = int(year)
            if not (1900 <= y <= 2030):
                return True
        except ValueError:
            return True
    return False


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------
class ProgressTracker:
    """
    Manages progress.json for resumable scraping.

    progress.json schema:
      {
        "phase":           "metadata" | "pdfs",
        "last_id_scanned": int,
        "valid_count":     int,
        "duplicate_count": int,
        "failed_count":    int,
        "total_scanned":   int,
        "seen_ids":        [list of record IDs already collected],
        "started_at":      ISO timestamp,
        "updated_at":      ISO timestamp
      }
    """

    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, Any] = {}
        self._seen_ids: Set[str] = set()
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                self._seen_ids = set(self._data.get("seen_ids", []))
                log.info("Resuming from progress.json: %d records already seen.", len(self._seen_ids))
            except Exception as exc:
                log.warning("Could not load progress.json (%s). Starting fresh.", exc)
                self._data = {}
                self._seen_ids = set()
        else:
            self._data = {
                "phase": "metadata",
                "last_id_scanned": 0,
                "valid_count": 0,
                "duplicate_count": 0,
                "failed_count": 0,
                "total_scanned": 0,
                "seen_ids": [],
                "started_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    def save(self):
        self._data["seen_ids"] = list(self._seen_ids)
        self._data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    @property
    def last_id_scanned(self) -> int:
        return self._data.get("last_id_scanned", 0)

    @last_id_scanned.setter
    def last_id_scanned(self, val: int):
        self._data["last_id_scanned"] = val

    @property
    def phase(self) -> str:
        return self._data.get("phase", "metadata")

    @phase.setter
    def phase(self, val: str):
        self._data["phase"] = val

    def is_seen(self, record_id: str) -> bool:
        return record_id in self._seen_ids

    def mark_seen(self, record_id: str):
        self._seen_ids.add(record_id)

    def inc_valid(self):
        self._data["valid_count"] = self._data.get("valid_count", 0) + 1

    def inc_duplicate(self):
        self._data["duplicate_count"] = self._data.get("duplicate_count", 0) + 1

    def inc_failed(self):
        self._data["failed_count"] = self._data.get("failed_count", 0) + 1

    def inc_scanned(self):
        self._data["total_scanned"] = self._data.get("total_scanned", 0) + 1

    def stats(self) -> Dict[str, Any]:
        return {
            "valid":      self._data.get("valid_count", 0),
            "duplicates": self._data.get("duplicate_count", 0),
            "failed":     self._data.get("failed_count", 0),
            "scanned":    self._data.get("total_scanned", 0),
        }


# ---------------------------------------------------------------------------
# Incremental writers
# ---------------------------------------------------------------------------
class RecordWriter:
    """Thread-safe incremental CSV + JSONL writer."""

    def __init__(self, csv_path: str, jsonl_path: str):
        self.csv_path  = csv_path
        self.jsonl_path= jsonl_path
        self._csv_exists = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
        import threading
        self._lock = threading.Lock()

    def write(self, record: Dict[str, Any]):
        with self._lock:
            self._write_csv(record)
            self._write_jsonl(record)

    def _write_csv(self, record: Dict[str, Any]):
        write_header = not self._csv_exists
        with open(self.csv_path, "a", encoding=config.CSV_ENCODING, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
                self._csv_exists = True
            writer.writerow(record)

    def _write_jsonl(self, record: Dict[str, Any]):
        with open(self.jsonl_path, "a", encoding=config.JSONL_ENCODING) as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class FailedWriter:
    """Thread-safe writer for failed_records.csv."""

    COLUMNS = ["record_id", "url", "error_type", "error_message", "timestamp", "retry_count"]

    def __init__(self, path: str):
        self.path = path
        self._exists = os.path.exists(path) and os.path.getsize(path) > 0
        import threading
        self._lock = threading.Lock()

    def write(self, record_id: str, url: str, error_type: str, error_msg: str, retry_count: int = 0):
        with self._lock:
            write_header = not self._exists
            with open(self.path, "a", encoding=config.CSV_ENCODING, newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.COLUMNS)
                if write_header:
                    writer.writeheader()
                    self._exists = True
                writer.writerow({
                    "record_id":     record_id,
                    "url":           url,
                    "error_type":    error_type,
                    "error_message": error_msg[:500],
                    "timestamp":     datetime.now(timezone.utc).isoformat(),
                    "retry_count":   retry_count,
                })


# ---------------------------------------------------------------------------
# Core scraping worker
# ---------------------------------------------------------------------------
def scrape_one(
    session: requests.Session,
    idd: int,
    system_filter: Optional[str],
) -> tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """
    Fetch and parse a single record.

    Returns:
      (status, record_dict, error_message)
      status: 'valid' | 'invalid' | 'error' | 'duplicate' | 'skip_system'
    """
    url = config.RECORD_URL_TEMPLATE.format(idd=idd)
    resp = polite_get(session, url)

    if resp is None:
        return "error", None, "All retries exhausted"

    if resp.status_code != 200:
        return "error", None, f"HTTP {resp.status_code}"

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        return "error", None, f"HTML parse failed: {exc}"

    if not is_valid_record(soup):
        return "invalid", None, None

    record = parse_record(idd, soup)

    # System filter
    if system_filter:
        sys_inferred = record.get("medical_system", "")
        if sys_inferred.lower() != system_filter.lower():
            return "skip_system", None, None

    return "valid", record, None


# ---------------------------------------------------------------------------
# Phase 1 – Metadata collection
# ---------------------------------------------------------------------------
def run_metadata(
    start_id: int,
    end_id: int,
    limit: Optional[int],
    dry_run: bool,
    system_filter: Optional[str],
    workers: int,
):
    """Scan ID range and collect metadata for all valid records."""
    log.info("=" * 70)
    log.info("PHASE 1: Metadata collection")
    log.info("  ID range: %d – %d", start_id, end_id)
    log.info("  Workers:  %d", workers)
    log.info("  System:   %s", system_filter or "all")
    log.info("  Dry-run:  %s", dry_run)
    log.info("=" * 70)

    # Setup output
    for d in [config.DATA_DIR, config.LOG_DIR, config.RAW_DIR]:
        Path(d).mkdir(parents=True, exist_ok=True)

    progress   = ProgressTracker(config.PROGRESS_JSON)
    rec_writer = RecordWriter(config.RECORDS_CSV, config.RECORDS_JSONL)
    fail_writer= FailedWriter(config.FAILED_CSV)

    # Resume: skip already-scanned IDs
    resume_from = progress.last_id_scanned + 1
    if resume_from > start_id:
        log.info("Resuming from IDD=%d (last scanned: %d)", resume_from, resume_from - 1)
        start_id = resume_from

    effective_limit = config.DRY_RUN_LIMIT if dry_run else limit
    id_range = range(start_id, end_id + 1)

    session = build_session()

    # Progress display
    total_ids = len(id_range)
    pbar = tqdm(
        total=total_ids,
        desc="Scanning IDs",
        unit=" IDs",
        dynamic_ncols=True,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
    )

    valid_count   = progress.stats()["valid"]
    dup_count     = progress.stats()["duplicates"]
    failed_count  = progress.stats()["failed"]
    start_time    = time.time()
    processed     = 0

    def process_idd(idd: int) -> tuple[int, str, Optional[Dict], Optional[str]]:
        return (idd,) + scrape_one(session, idd, system_filter)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_idd, idd): idd for idd in id_range}

            for future in as_completed(futures):
                idd = futures[future]
                try:
                    result_idd, status, record, error = future.result()
                except Exception as exc:
                    status, record, error = "error", None, str(exc)
                    result_idd = idd

                progress.last_id_scanned = result_idd
                progress.inc_scanned()
                pbar.update(1)
                processed += 1

                if status == "valid" and record:
                    rid = record["record_id"]
                    if progress.is_seen(rid):
                        progress.inc_duplicate()
                        dup_count += 1
                        log.debug("Duplicate: IDD=%d record_id=%s", result_idd, rid)
                    else:
                        progress.mark_seen(rid)
                        progress.inc_valid()
                        valid_count += 1
                        rec_writer.write(record)
                        log.debug("Valid: IDD=%d title=%s", result_idd, record.get("title", "")[:60])

                elif status == "error":
                    progress.inc_failed()
                    failed_count += 1
                    fail_writer.write(
                        str(result_idd),
                        config.RECORD_URL_TEMPLATE.format(idd=result_idd),
                        "fetch_error",
                        error or "unknown",
                    )
                    log.warning("Failed: IDD=%d – %s", result_idd, error)

                # Save progress periodically
                if processed % 50 == 0:
                    progress.save()

                # Update tqdm description
                elapsed = time.time() - start_time
                speed = processed / elapsed if elapsed > 0 else 0
                pbar.set_postfix(
                    valid=valid_count,
                    dups=dup_count,
                    failed=failed_count,
                    speed=f"{speed:.1f}/s",
                    refresh=False,
                )

                # Limit check
                if effective_limit and valid_count >= effective_limit:
                    log.info("Reached limit of %d valid records. Stopping.", effective_limit)
                    for f in futures:
                        f.cancel()
                    break

    except KeyboardInterrupt:
        log.info("Interrupted by user. Saving progress...")
    finally:
        pbar.close()
        progress.save()

    # Summary
    elapsed_total = time.time() - start_time
    log.info("=" * 70)
    log.info("PHASE 1 COMPLETE")
    log.info("  Valid records:  %d", valid_count)
    log.info("  Duplicates:     %d", dup_count)
    log.info("  Failed:         %d", failed_count)
    log.info("  Total scanned:  %d", processed)
    log.info("  Time elapsed:   %.1f min", elapsed_total / 60)
    log.info("=" * 70)

    generate_summary()


# ---------------------------------------------------------------------------
# Phase 2 – PDF download
# ---------------------------------------------------------------------------
def run_pdfs(workers: int, dry_run: bool, limit: Optional[int]):
    """Download PDFs for records with pending pdf_status."""
    log.info("=" * 70)
    log.info("PHASE 2: PDF download")
    log.info("=" * 70)

    Path(config.PDF_DIR).mkdir(parents=True, exist_ok=True)

    if not os.path.exists(config.RECORDS_JSONL):
        log.error("records.jsonl not found. Run 'metadata' phase first.")
        sys.exit(1)

    # Load records with pending PDFs
    pending = []
    with open(config.RECORDS_JSONL, "r", encoding=config.JSONL_ENCODING) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("pdf_status") == "pending" and rec.get("full_paper_url"):
                pending.append(rec)

    log.info("Records with pending PDFs: %d", len(pending))
    if not pending:
        log.info("Nothing to download.")
        return

    effective_limit = config.DRY_RUN_LIMIT if dry_run else limit
    if effective_limit:
        pending = pending[:effective_limit]

    session = build_session()
    fail_writer = FailedWriter(config.FAILED_CSV)

    downloaded, unavailable, failed_dl = 0, 0, 0
    pdf_updates: Dict[str, Dict[str, str]] = {}

    pbar = tqdm(pending, desc="Downloading PDFs", unit=" PDFs", dynamic_ncols=True)

    for rec in pbar:
        rid = rec["record_id"]
        url = rec["full_paper_url"]

        status, filename, err = download_pdf(session, rid, url)
        pdf_updates[rid] = {"pdf_status": status, "pdf_filename": filename}

        if status == "downloaded":
            downloaded += 1
        elif status in ("unavailable", "invalid", "skipped"):
            unavailable += 1
        else:
            failed_dl += 1
            fail_writer.write(rid, url, "pdf_download_failed", err or "unknown")

        pbar.set_postfix(downloaded=downloaded, unavail=unavailable, failed=failed_dl, refresh=False)

    pbar.close()

    # Update JSONL file with pdf_status
    _update_jsonl_pdf_status(pdf_updates)

    log.info("=" * 70)
    log.info("PHASE 2 COMPLETE")
    log.info("  Downloaded:   %d", downloaded)
    log.info("  Unavailable:  %d", unavailable)
    log.info("  Failed:       %d", failed_dl)
    log.info("=" * 70)
    generate_summary()


def download_pdf(
    session: requests.Session, record_id: str, url: str
) -> tuple[str, str, str]:
    """
    Download a PDF for the given record.
    Returns (status, filename, error_message).
    status: 'downloaded' | 'unavailable' | 'failed' | 'invalid' | 'skipped'
    """
    if not url or url in ("-", "--"):
        return "unavailable", "", ""

    # Check deterministic filename first (already downloaded?)
    safe_id = re.sub(r"[^\w\-]", "_", record_id)
    pdf_path = os.path.join(config.PDF_DIR, f"{safe_id}.pdf")
    if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1024:
        return "downloaded", os.path.basename(pdf_path), ""

    # Validate URL
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "invalid", "", f"Invalid scheme: {parsed.scheme}"
    except Exception:
        return "invalid", "", "URL parse error"

    # Skip known non-PDF domains
    domain = parsed.netloc.lower()
    for skip_domain in config.SKIP_PDF_DOMAINS:
        if skip_domain in domain:
            return "skipped", "", f"Skipped domain: {domain}"

    # Download
    resp = polite_get(
        session, url, delay=True, stream=True,
        timeout=(config.CONNECT_TIMEOUT, config.PDF_TIMEOUT),
    )
    if resp is None:
        return "failed", "", "No response after retries"

    if resp.status_code == 404:
        return "unavailable", "", "HTTP 404"
    if resp.status_code != 200:
        return "failed", "", f"HTTP {resp.status_code}"

    # Check Content-Type
    content_type = resp.headers.get("Content-Type", "").lower()
    is_pdf = (
        "pdf" in content_type
        or url.lower().endswith(".pdf")
        or "application/octet-stream" in content_type
    )

    # Check size
    content_len = resp.headers.get("Content-Length")
    if content_len:
        size_mb = int(content_len) / (1024 * 1024)
        if size_mb > config.PDF_MAX_SIZE_MB:
            return "skipped", "", f"File too large: {size_mb:.1f}MB"

    # Peek at first bytes
    first_chunk = b""
    try:
        chunks = []
        total_bytes = 0
        for chunk in resp.iter_content(chunk_size=config.PDF_CHUNK_SIZE):
            if not chunk:
                continue
            if not first_chunk:
                first_chunk = chunk[:8]
            chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > config.PDF_MAX_SIZE_MB * 1024 * 1024:
                return "skipped", "", "File too large (streaming)"
    except Exception as exc:
        return "failed", "", f"Download error: {exc}"

    # Verify PDF magic bytes or content-type
    is_actually_pdf = first_chunk.startswith(b"%PDF")
    if not is_pdf and not is_actually_pdf:
        return "invalid", "", "Not a PDF (magic bytes check failed)"

    # Write file
    try:
        data = b"".join(chunks)
        with open(pdf_path, "wb") as f:
            f.write(data)
        log.debug("PDF saved: %s (%d bytes)", pdf_path, len(data))
        return "downloaded", os.path.basename(pdf_path), ""
    except IOError as exc:
        return "failed", "", f"Write error: {exc}"


def _update_jsonl_pdf_status(updates: Dict[str, Dict[str, str]]):
    """
    Rewrite records.jsonl updating pdf_status and pdf_filename for changed records.
    Also rebuilds records.csv.
    """
    if not updates:
        return
    if not os.path.exists(config.RECORDS_JSONL):
        return

    tmp_path = config.RECORDS_JSONL + ".tmp"
    rec_writer = RecordWriter(config.RECORDS_CSV + ".new", tmp_path + ".csv")

    with open(config.RECORDS_JSONL, "r", encoding=config.JSONL_ENCODING) as fin, \
         open(tmp_path, "w", encoding=config.JSONL_ENCODING) as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                fout.write(line + "\n")
                continue
            rid = rec.get("record_id", "")
            if rid in updates:
                rec.update(updates[rid])
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")

    os.replace(tmp_path, config.RECORDS_JSONL)
    log.info("records.jsonl updated with PDF statuses.")


# ---------------------------------------------------------------------------
# Phase 3 – Retry failed records
# ---------------------------------------------------------------------------
def run_retry_failed(workers: int, limit: Optional[int]):
    """Re-scrape records listed in failed_records.csv."""
    log.info("=" * 70)
    log.info("PHASE 3: Retry failed records")
    log.info("=" * 70)

    if not os.path.exists(config.FAILED_CSV):
        log.info("No failed_records.csv found.")
        return

    failed_ids = []
    seen_in_failed: Set[str] = set()
    with open(config.FAILED_CSV, "r", encoding=config.CSV_ENCODING, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rid = row.get("record_id", "")
            url = row.get("url", "")
            if rid and rid not in seen_in_failed:
                seen_in_failed.add(rid)
                # Extract IDD from URL
                m = re.search(r"IDD=(\d+)", url)
                if m:
                    failed_ids.append(int(m.group(1)))

    if not failed_ids:
        log.info("No failed IDs to retry.")
        return

    log.info("Retrying %d failed records.", len(failed_ids))
    if limit:
        failed_ids = failed_ids[:limit]

    progress   = ProgressTracker(config.PROGRESS_JSON)
    rec_writer = RecordWriter(config.RECORDS_CSV, config.RECORDS_JSONL)
    fail_writer= FailedWriter(config.FAILED_CSV)
    session    = build_session()

    recovered, still_failed = 0, 0

    pbar = tqdm(failed_ids, desc="Retrying", unit=" records", dynamic_ncols=True)
    for idd in pbar:
        status, record, error = scrape_one(session, idd, None)
        if status == "valid" and record:
            rid = record["record_id"]
            if not progress.is_seen(rid):
                progress.mark_seen(rid)
                rec_writer.write(record)
                recovered += 1
        else:
            still_failed += 1
            fail_writer.write(
                str(idd),
                config.RECORD_URL_TEMPLATE.format(idd=idd),
                "retry_failed",
                error or status,
                retry_count=1,
            )

    pbar.close()
    progress.save()

    log.info("Retry complete: recovered=%d, still_failed=%d", recovered, still_failed)


# ---------------------------------------------------------------------------
# Stats & Summary
# ---------------------------------------------------------------------------
def run_stats():
    """Print statistics about the collected data."""
    print("\n" + "=" * 70)
    print("AYUSH Research Portal Scraper – Statistics")
    print("=" * 70)

    if not os.path.exists(config.RECORDS_JSONL):
        print("No data collected yet. Run 'metadata' phase first.")
        return

    summary = _compute_summary()
    print(f"\nTotal records:            {summary['total_records']:,}")
    print(f"With abstract:            {summary['records_with_abstract']:,}")
    print(f"With keywords:            {summary['records_with_keywords']:,}")
    print(f"With PDF URL:             {summary['records_with_pdf_url']:,}")
    print(f"PDFs downloaded:          {summary['pdf_download_success']:,}")
    print(f"PDFs failed:              {summary['pdf_download_failed']:,}")
    print(f"Duplicate count:          {summary['duplicate_count']:,}")
    print(f"Failed records:           {summary['failed_count']:,}")

    print("\n--- By Medical System ---")
    for sys_name, count in sorted(summary["records_by_medical_system"].items()):
        print(f"  {sys_name:<30} {count:,}")

    print("\n--- By Publication Year (top 20) ---")
    years = sorted(summary["records_by_year"].items(), key=lambda x: -x[1])
    for year, count in years[:20]:
        print(f"  {year:<10} {count:,}")

    if summary.get("suspicious_count"):
        print(f"\nSuspicious records: {summary['suspicious_count']:,}")

    print("=" * 70)


def _compute_summary() -> Dict[str, Any]:
    total, with_abstract, with_keywords, with_pdf_url = 0, 0, 0, 0
    pdf_ok, pdf_fail = 0, 0
    by_system: Dict[str, int] = {}
    by_year: Dict[str, int] = {}
    suspicious = 0

    if os.path.exists(config.RECORDS_JSONL):
        with open(config.RECORDS_JSONL, "r", encoding=config.JSONL_ENCODING) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                if rec.get("abstract"):
                    with_abstract += 1
                if rec.get("keywords"):
                    with_keywords += 1
                if rec.get("full_paper_url"):
                    with_pdf_url += 1
                ps = rec.get("pdf_status", "")
                if ps == "downloaded":
                    pdf_ok += 1
                elif ps == "failed":
                    pdf_fail += 1
                sys_name = rec.get("medical_system", "") or "Unknown"
                by_system[sys_name] = by_system.get(sys_name, 0) + 1
                year = rec.get("publication_year", "") or "Unknown"
                by_year[year] = by_year.get(year, 0) + 1
                if is_suspicious(rec):
                    suspicious += 1

    # Dedup count from progress
    dup_count, fail_count = 0, 0
    if os.path.exists(config.PROGRESS_JSON):
        try:
            with open(config.PROGRESS_JSON) as f:
                prog = json.load(f)
            dup_count  = prog.get("duplicate_count", 0)
            fail_count = prog.get("failed_count", 0)
        except Exception:
            pass

    summary = {
        "total_records":             total,
        "records_with_abstract":     with_abstract,
        "records_with_keywords":     with_keywords,
        "records_with_pdf_url":      with_pdf_url,
        "pdf_download_success":      pdf_ok,
        "pdf_download_failed":       pdf_fail,
        "duplicate_count":           dup_count,
        "failed_count":              fail_count,
        "records_by_medical_system": by_system,
        "records_by_year":           by_year,
        "suspicious_count":          suspicious,
        "generated_at":              datetime.now(timezone.utc).isoformat(),
    }
    return summary


def generate_summary():
    summary = _compute_summary()
    tmp = config.SUMMARY_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config.SUMMARY_JSON)
    log.info("Summary written to %s", config.SUMMARY_JSON)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AYUSH Research Portal Bulk Data Collector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scraper.py metadata                # collect all metadata
  python scraper.py pdfs                    # download PDFs
  python scraper.py retry-failed            # retry failed records
  python scraper.py stats                   # show statistics
  python scraper.py metadata --dry-run      # test with 10 records
  python scraper.py metadata --limit 100    # stop after 100 valid records
  python scraper.py metadata --system Siddha
  python scraper.py metadata --start-id 5000 --end-id 25000
        """,
    )
    p.add_argument(
        "command",
        nargs="?",
        default="metadata",
        choices=["metadata", "pdfs", "retry-failed", "stats"],
        help="Command to run (default: metadata)",
    )
    p.add_argument("--dry-run",    action="store_true", help=f"Process only {config.DRY_RUN_LIMIT} records")
    p.add_argument("--limit",      type=int, metavar="N", help="Stop after N valid records")
    p.add_argument("--system",     choices=config.MEDICAL_SYSTEMS, help="Filter by medical system")
    p.add_argument("--start-id",   type=int, default=config.ID_RANGE_START, metavar="N", help="Start IDD")
    p.add_argument("--end-id",     type=int, default=config.ID_RANGE_END,   metavar="N", help="End IDD")
    p.add_argument("--workers",    type=int, default=config.MAX_WORKERS, metavar="N", help="Concurrent workers")
    p.add_argument("--delay",      type=float, default=config.REQUEST_DELAY, metavar="SEC", help="Request delay (seconds)")
    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()

    # Apply overrides
    if args.delay != config.REQUEST_DELAY:
        config.REQUEST_DELAY = args.delay
        log.info("Request delay overridden: %.2fs", config.REQUEST_DELAY)

    # Print banner
    print()
    print("  +----------------------------------------------------------+")
    print("  |        AYUSH Research Portal - Data Collector            |")
    print("  |   https://ayushportal.nic.in/  |  robots.txt: Allow: /  |")
    print("  +----------------------------------------------------------+")
    print()

    if args.dry_run:
        print(f"  *** DRY RUN MODE – processing at most {config.DRY_RUN_LIMIT} records ***\n")

    cmd = args.command
    if cmd == "metadata":
        run_metadata(
            start_id      = args.start_id,
            end_id        = args.end_id,
            limit         = args.limit,
            dry_run       = args.dry_run,
            system_filter = args.system,
            workers       = args.workers,
        )
    elif cmd == "pdfs":
        run_pdfs(
            workers  = args.workers,
            dry_run  = args.dry_run,
            limit    = args.limit,
        )
    elif cmd == "retry-failed":
        run_retry_failed(
            workers = args.workers,
            limit   = args.limit,
        )
    elif cmd == "stats":
        run_stats()


if __name__ == "__main__":
    main()


