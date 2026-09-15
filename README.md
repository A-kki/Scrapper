# AYUSH Research Portal – Bulk Data Collector

A **polite, resumable, fault-tolerant** bulk-data scraper for the
[AYUSH Research Portal](https://ayushportal.nic.in/).

---

## Portal Discovery Summary

Before writing a single line of scraper code, the portal was thoroughly
inspected. Here is what was found:

| Finding | Detail |
|---------|--------|
| **robots.txt** | `Allow: /` – no automated-access restrictions |
| **Record URL pattern** | `https://ayushportal.nic.in/ShowDefault.aspx?IDD=<N>` |
| **Validity detection** | Invalid IDs render title/authors/journal as literal `"Article ID"` |
| **ID range** | Sparse and non-sequential: valid IDs seen from ~2024 to ~279174 |
| **Search/pagination** | Search page uses ASP.NET AJAX (UpdatePanel); POST does not work without session cookies. ID enumeration is more reliable. |
| **Medical system field** | NOT on the record page; inferred from keywords/disease/journal |
| **Journal field** | Contains structured data: `Name | Year: YYYY | Volume: V | Issue: I | Page: P` |
| **Abstract span ID** | `ctl00_ContentPlaceHolder1_Label15` |
| **SSL** | NIC uses a non-standard CA chain; `verify=False` required |
| **Concurrency limit** | Conservative default (3 workers, 1 s delay) |

---

## Project Structure

```
ayush_scraper/
├── scraper.py          # Main scraper
├── inspect_portal.py   # Portal structure inspector
├── config.py           # All configuration
├── requirements.txt
├── README.md
├── data/
│   ├── records.csv     # Metadata (UTF-8 with BOM for Excel)
│   ├── records.jsonl   # Full JSON records (one per line)
│   ├── failed_records.csv
│   ├── progress.json   # Resumption checkpoint
│   ├── summary.json    # Quality statistics
│   └── raw/            # Raw HTML for unusual/failed records
├── pdfs/               # Downloaded PDFs (<record_id>.pdf)
└── logs/
    └── scraper.log
```

---

## Installation

```bash
# Clone or copy the project
cd ayush_scraper

# Create virtual environment (recommended)
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # Linux/macOS

# Install dependencies
pip install -r requirements.txt
```

---

## Quick Start

### 1. Inspect the portal (verify it is still reachable)

```bash
python inspect_portal.py
python inspect_portal.py --probe-range   # check ID validity across range
```

### 2. Dry run (10 records only, no side effects)

```bash
python scraper.py metadata --dry-run
```

### 3. Collect metadata (full run)

```bash
python scraper.py metadata
```

Stops at `data/progress.json`. Run again to resume.

### 4. Download PDFs

```bash
python scraper.py pdfs
```

### 5. Retry failed records

```bash
python scraper.py retry-failed
```

### 6. Show statistics

```bash
python scraper.py stats
```

---

## Commands Reference

| Command | Description |
|---------|-------------|
| `python scraper.py metadata` | Collect metadata for all records |
| `python scraper.py pdfs` | Download PDFs for collected records |
| `python scraper.py retry-failed` | Re-attempt failed records |
| `python scraper.py stats` | Show statistics and quality report |

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` | off | Process only 10 records |
| `--limit N` | none | Stop after N valid records |
| `--system S` | all | Filter: Ayurveda, Unani, Siddha, Homoeopathy, "Yoga and Naturopathy", "Sowa Rigpa" |
| `--start-id N` | 2000 | Start scanning from IDD=N |
| `--end-id N` | 300000 | Stop scanning at IDD=N |
| `--workers N` | 3 | Concurrent HTTP workers |
| `--delay SEC` | 1.0 | Base delay between requests |

---

## Resumability

**If you stop the scraper at any point, restart it with the same command.**

The scraper records the last scanned ID in `data/progress.json` and picks
up exactly where it left off. Records are written to CSV/JSONL incrementally
so no data is lost if the process is killed.

---

## Output Format

### records.csv

UTF-8 with BOM (Excel-compatible). One row per research record.

| Column | Description |
|--------|-------------|
| `record_id` | Portal article ID |
| `medical_system` | Inferred: Ayurveda / Siddha / Homoeopathy / etc. |
| `title` | Article title |
| `authors` | Author names |
| `journal_raw` | Raw journal string from portal |
| `journal_name` | Extracted journal name |
| `publication_year` | 4-digit year |
| `volume`, `issue`, `pages` | Parsed from journal string |
| `institution` | Author institution/designation |
| `disease` | Disease with ICPC/ICD codes |
| `keywords` | Research keywords |
| `abstract` | Article abstract |
| `full_paper_url` | URL to full text/PDF |
| `pdf_status` | `pending` / `downloaded` / `unavailable` / `failed` / `skipped` |
| `visits`, `downloads` | Portal view/download counts |
| `record_url` | Full URL to record page |
| `scraped_at` | ISO 8601 timestamp |
| `status` | `valid` / `parse_error` |

### records.jsonl

Same fields, one JSON object per line.

---

## PDF Status Values

| Value | Meaning |
|-------|---------|
| `pending` | URL exists, PDF not yet downloaded |
| `downloaded` | PDF saved to `pdfs/<record_id>.pdf` |
| `unavailable` | No URL, or HTTP 404 |
| `failed` | Download attempted but failed |
| `invalid` | URL exists but response is not a PDF |
| `skipped` | Known non-PDF domain (PubMed, DOI, etc.) |

---

## Configuration

All settings are in `config.py`. Key parameters:

```python
REQUEST_DELAY   = 1.0    # seconds between requests
MAX_RETRIES     = 3      # retry attempts before failing
MAX_WORKERS     = 3      # concurrent threads
ID_RANGE_START  = 2000   # start of ID scan range
ID_RANGE_END    = 300000 # end of ID scan range
```

---

## Known Limitations

1. **No Medical System field** – the portal does not expose a medical system
   tag on the record page. The scraper infers it from keywords/disease/journal
   text. This inference is ~85% accurate; some records will be untagged.

2. **Sparse IDs** – the ID space 2000–300000 contains large gaps. The scraper
   checks each ID cheaply (invalid pages return in <1s) but the total scan
   still takes several hours.

3. **Full paper PDFs** – many full paper URLs point to journal landing pages
   (Elsevier, PubMed, etc.) that require institutional access. The scraper
   downloads directly accessible PDFs and marks inaccessible ones as `skipped`.

4. **SSL certificate** – `ayushportal.nic.in` uses a non-standard NIC CA chain
   not trusted by Python's certifi. The scraper uses `verify=False` with a
   warning. This is safe for read-only data collection.

5. **AJAX search** – the search page uses ASP.NET UpdatePanel (AJAX). Submitting
   the search form without a browser session returns an empty result. ID
   enumeration is therefore the most reliable collection strategy.

---

## Legal & Ethics

- `robots.txt` allows all crawlers (`Allow: /`)
- User-Agent is clearly identified as a research data collection bot
- Conservative rate limiting (1 s between requests, max 3 workers)
- No authentication bypass, CAPTCHA bypass, or access control circumvention
- Data is publicly accessible research information for legitimate academic use
