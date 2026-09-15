"""
config.py -- Central configuration for the AYUSH Research Portal Scraper.

All tuneable parameters live here. Override via command-line arguments.

DISCOVERY NOTES (from portal inspection on 2026-09-15):
  - URL pattern: https://ayushportal.nic.in/ShowDefault.aspx?IDD=<number>
  - robots.txt: Allow: / (no restrictions)
  - Invalid records render title/authors/journal as literal "Article ID"
  - Valid IDs observed: ~2024 to ~24000, plus sparse: 97053, 164648, 279174
  - No medical system field on record page; inferred from disease/keywords
  - Journal field contains: "Name | Year: YYYY | Volume: V | Issue: I | Page: P"
  - Abstract in span: ctl00_ContentPlaceHolder1_Label15
  - Portal uses SSL with NIC cert chain (non-standard); use verify=False
"""

# Portal
BASE_URL = "https://ayushportal.nic.in"
RECORD_URL_TEMPLATE = BASE_URL + "/ShowDefault.aspx?IDD={idd}"

# ID scan range (IDs are SPARSE - we check each one cheaply)
ID_RANGE_START: int = 2000
ID_RANGE_END:   int = 300000

# Rate limiting
REQUEST_DELAY:   float = 1.0   # base delay between requests (seconds)
REQUEST_JITTER:  float = 0.3   # random jitter +/- (seconds)
MAX_RETRIES:     int   = 3     # max retry attempts before logging as failed
RETRY_BACKOFF:   float = 2.0   # exponential backoff multiplier
CONNECT_TIMEOUT: int   = 15    # connection timeout (seconds)
READ_TIMEOUT:    int   = 30    # read timeout (seconds)

# Concurrency (keep low to be polite)
MAX_WORKERS: int = 3

# SSL
VERIFY_SSL: bool = False  # NIC portal uses a non-standard CA chain

# Output
DATA_DIR     = "data"
PDF_DIR      = "pdfs"
LOG_DIR      = "logs"
RAW_DIR      = "data/raw"
RECORDS_CSV  = "data/records.csv"
RECORDS_JSONL= "data/records.jsonl"
FAILED_CSV   = "data/failed_records.csv"
PROGRESS_JSON= "data/progress.json"
SUMMARY_JSON = "data/summary.json"
SCRAPER_LOG  = "logs/scraper.log"

# Encoding
CSV_ENCODING  = "utf-8-sig"  # BOM for Excel compatibility
JSONL_ENCODING= "utf-8"

# HTTP
USER_AGENT = (
    "Mozilla/5.0 (compatible; AYUSHResearchDataBot/1.0; "
    "Academic Research/Data-Collection; "
    "Contact: research-data-bot@example.org)"
)

# Validity sentinels (fields show these literals when IDD is invalid)
INVALID_FIELD_SENTINEL   = "Article ID"
INVALID_ABSTRACT_SENTINEL= "Label"
PORTAL_EMPTY_PLACEHOLDER = "--"

# Medical systems
MEDICAL_SYSTEMS = [
    "Ayurveda",
    "Yoga and Naturopathy",
    "Unani",
    "Siddha",
    "Homoeopathy",
    "Sowa Rigpa",
]

# Keyword-based medical system inference
SYSTEM_KEYWORDS: dict = {
    "Ayurveda": [
        "ayurved", "ayurvedic", "vata", "pitta", "kapha", "dosha",
        "rasayana", "panchakarma", "kashaya", "kwatha", "bhasma",
        "triphala", "ashwagandha", "chyawanprash", "churna", "taila",
    ],
    "Yoga and Naturopathy": [
        "yoga", "naturopathy", "pranayama", "asana", "meditation",
        "hydrotherapy", "fasting", "naturopathic",
    ],
    "Unani": [
        "unani", "tibb", "mizaj", "hamdard", "rogan",
    ],
    "Siddha": [
        "siddha", "siddhar", "chooranam", "kudineer", "mezhugu",
        "thambira", "varma",
    ],
    "Homoeopathy": [
        "homeopath", "homoeopath", "homeopathy", "homoeopathy",
        "materia medica", "potency", "trituration", "repertory",
    ],
    "Sowa Rigpa": [
        "sowa rigpa", "tibetan medicine",
    ],
}

# PDF settings
PDF_MAX_SIZE_MB = 50
PDF_TIMEOUT     = 60
PDF_CHUNK_SIZE  = 65536

# Domains that serve landing pages instead of direct PDFs
SKIP_PDF_DOMAINS = [
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov/pubmed",
    "doi.org",
    "dx.doi.org",
]

# Dry-run / test
DRY_RUN_LIMIT = 10
