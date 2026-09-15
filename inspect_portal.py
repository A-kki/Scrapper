#!/usr/bin/env python3
"""
inspect_portal.py -- AYUSH Research Portal Inspector

Run this before the main scraper to verify the portal's current structure
and confirm that the scraper will work correctly.

Usage:
    python inspect_portal.py
    python inspect_portal.py --id 2104
    python inspect_portal.py --id 279174
    python inspect_portal.py --probe-range  (test ID validity in range)
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

BASE_URL = "https://ayushportal.nic.in"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AYUSHPortalInspector/1.0; "
        "Research/DataCollection)"
    )
}


def get(url: str, session: requests.Session) -> requests.Response:
    return session.get(url, timeout=30, verify=False)


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    return s


def check_robots(session: requests.Session):
    print("\n╔══════════ ROBOTS.TXT ══════════╗")
    r = get(f"{BASE_URL}/robots.txt", session)
    print(f"Status: {r.status_code}")
    print(r.text[:500])
    print("╚════════════════════════════════╝")


def inspect_homepage(session: requests.Session):
    print("\n╔══════════ HOMEPAGE ══════════╗")
    r = get(f"{BASE_URL}/", session)
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.find("title")
    print(f"Title: {title.text.strip() if title else 'N/A'}")
    print("\nNavigation links:")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.text.strip()
        if text and len(text) < 60:
            print(f"  {href:<60} | {text}")
    print("╚═══════════════════════════════╝")


def inspect_search(session: requests.Session):
    print("\n╔══════════ SEARCH PAGE (srch1.aspx) ══════════╗")
    r = get(f"{BASE_URL}/srch1.aspx", session)
    soup = BeautifulSoup(r.text, "html.parser")
    print(f"Status: {r.status_code}")
    print("\nForm elements:")
    for inp in soup.find_all("input"):
        itype = inp.get("type", "")
        if itype not in ("hidden",):
            print(f"  INPUT name={inp.get('name')} type={itype} value={str(inp.get('value',''))[:60]}")
    for inp in soup.find_all("input", type="checkbox"):
        print(f"  CHECKBOX name={inp.get('name')} id={inp.get('id')} value={inp.get('value')}")
    for sel in soup.find_all("select"):
        print(f"  SELECT name={sel.get('name')}")
        for opt in sel.find_all("option")[:5]:
            print(f"    OPTION value={opt.get('value')} text={opt.text.strip()}")
    print("╚═══════════════════════════════╝")


def inspect_record(idd: int, session: requests.Session):
    url = f"{BASE_URL}/ShowDefault.aspx?IDD={idd}"
    print(f"\n╔══════════ RECORD IDD={idd} ══════════╗")
    print(f"URL: {url}")
    r = get(url, session)
    print(f"Status: {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")

    SPAN_FIELDS = {
        "ctl00_ContentPlaceHolder1_lblID":         "Article ID",
        "ctl00_ContentPlaceHolder1_ArtTitle":      "Title",
        "ctl00_ContentPlaceHolder1_Jrnl":          "Journal (raw)",
        "ctl00_ContentPlaceHolder1_Auth":          "Authors",
        "ctl00_ContentPlaceHolder1_Desig":         "Institution",
        "ctl00_ContentPlaceHolder1_Address":       "Corresponding Address",
        "ctl00_ContentPlaceHolder1_Disease":       "Disease",
        "ctl00_ContentPlaceHolder1_Keywords":      "Keywords",
        "ctl00_ContentPlaceHolder1_lnkF_url":     "Full Paper URL",
        "ctl00_ContentPlaceHolder1_Label15":       "Abstract",
        "ctl00_ContentPlaceHolder1_ArtHitLabel":   "Visits",
        "ctl00_ContentPlaceHolder1_ArtDowldLabel": "Downloads",
        "ctl00_ContentPlaceHolder1_RegularExpressionValidator1": "Validity",
    }

    print("\nParsed fields:")
    for span_id, label in SPAN_FIELDS.items():
        tag = soup.find(id=span_id)
        val = tag.get_text(separator=" ", strip=True) if tag else "NOT FOUND"
        # Truncate for display
        display = val[:120] + "..." if len(val) > 120 else val
        print(f"  {label:<30} : {display}")

    # Validity check
    title_tag = soup.find(id="ctl00_ContentPlaceHolder1_ArtTitle")
    title_text = title_tag.get_text(strip=True) if title_tag else ""
    is_valid = bool(title_text) and "Article ID" not in title_text and title_text != "Title of the Article/ Research work"
    print(f"\n  ✓ VALID RECORD: {is_valid}")

    # Full paper button
    btn = soup.find("input", {"name": "ctl00$ContentPlaceHolder1$btnFullPaper"})
    if btn:
        print(f"  Full paper button: {btn.get('value','')}")

    print("╚═══════════════════════════════╝")


def probe_id_range(session: requests.Session, sample_size: int = 30):
    """Sample IDs across the range to find valid clusters."""
    print("\n╔══════════ ID RANGE PROBE ══════════╗")
    import random
    ranges = [
        range(2000, 2100),     # low range
        range(5000, 5050),
        range(10000, 10050),
        range(20000, 20020),
        range(24000, 24050),   # edge
        range(50000, 50010),
        range(97050, 97060),
        range(160000, 160010),
        range(279170, 279180), # recent
    ]

    print(f"{'IDD':<10} {'Valid':<8} {'Title':<50}")
    print("-" * 70)

    for rng in ranges:
        for idd in list(rng)[:3]:
            r = get(f"{BASE_URL}/ShowDefault.aspx?IDD={idd}", session)
            soup = BeautifulSoup(r.text, "html.parser")
            t_tag = soup.find(id="ctl00_ContentPlaceHolder1_ArtTitle")
            title = t_tag.get_text(strip=True) if t_tag else ""
            valid = bool(title) and "Article ID" not in title
            print(f"{idd:<10} {'YES' if valid else 'no':<8} {title[:50]}")
            time.sleep(0.5)
        print()

    print("╚════════════════════════════════╝")


def main():
    parser = argparse.ArgumentParser(description="AYUSH Portal Inspector")
    parser.add_argument("--id",          type=int, default=23335, help="Record ID to inspect")
    parser.add_argument("--probe-range", action="store_true",     help="Probe ID range for valid records")
    parser.add_argument("--no-record",   action="store_true",     help="Skip record inspection")
    args = parser.parse_args()

    session = build_session()

    print("\n" + "=" * 70)
    print("  AYUSH Research Portal Inspector")
    print(f"  Target: {BASE_URL}")
    print("=" * 70)

    check_robots(session)
    inspect_homepage(session)
    inspect_search(session)

    if not args.no_record:
        inspect_record(args.id, session)
        # Also show a record we know is valid
        if args.id != 2104:
            inspect_record(2104, session)

    if args.probe_range:
        probe_id_range(session)

    print("\n✓ Inspection complete.\n")


if __name__ == "__main__":
    main()
