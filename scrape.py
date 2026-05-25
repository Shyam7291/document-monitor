import csv
import requests
import os
import re
import html
import time
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote

# =========================
# RUN MODE CONFIGURATION
# =========================

RUN_MODE = os.environ.get("RUN_MODE", "full").strip().lower()
TARGET_URL_FILE = os.environ.get("TARGET_URL_FILE", "documents.csv").strip()

ENABLE_BROWSER_FALLBACK = os.environ.get("ENABLE_BROWSER_FALLBACK", "false").strip().lower() == "true"

# Sleep between URLs to reduce blocking
SLEEP_SECONDS = float(os.environ.get("SLEEP_SECONDS", "2"))

# Retry failed URLs once after the first pass
RETRY_FAILED_URLS = os.environ.get("RETRY_FAILED_URLS", "true").strip().lower() == "true"
RETRY_SLEEP_SECONDS = float(os.environ.get("RETRY_SLEEP_SECONDS", "10"))

if RUN_MODE in ["full", "seed"]:
    OUTPUT_FILE = "output.csv"
    RAW_FILE = "raw_links.csv"
    ISSUES_FILE = "capture_issues.csv"
elif RUN_MODE == "test":
    OUTPUT_FILE = "output_test.csv"
    RAW_FILE = "raw_links_test.csv"
    ISSUES_FILE = "capture_issues_test.csv"
elif RUN_MODE == "baseline":
    OUTPUT_FILE = "output_baseline.csv"
    RAW_FILE = "raw_links_baseline.csv"
    ISSUES_FILE = "capture_issues_baseline.csv"
else:
    OUTPUT_FILE = "output.csv"
    RAW_FILE = "raw_links.csv"
    ISSUES_FILE = "capture_issues.csv"

DIFF_FILE = "diff.csv"
KNOWN_DOCUMENTS_FILE = "known_documents.csv"
REPORT_KEYWORDS_FILE = "report_keywords.csv"

# Final clean summary file
RUN_SUMMARY_FILE = "run_summary_master.csv"

# =========================
# LOCKED CSV FORMATS
# =========================

RUN_SUMMARY_FIELDNAMES = [
    "run_number",
    "date",
    "run_mode",
    "url_file",
    "total_urls_processed",
    "total_documents_captured",
    "new_diff_records",
    "issue_count",
    "success_zero_docs_count",
    "fetch_failed_status_count",
    "fetch_error_count",
    "browser_fallback_enabled",
    "output_file",
    "raw_file",
    "issues_file"
]

DIFF_FIELDNAMES = [
    "date",
    "company",
    "document_title",
    "document_url"
]

KNOWN_DOCUMENTS_FIELDNAMES = [
    "first_seen_date",
    "company",
    "document_title",
    "document_url",
    "source_run_mode"
]

output_data = []
raw_links = []
issue_rows = []

# Retry queue for failed/zero-doc URLs
retry_queue = []

# Global duplicate prevention across the whole run
global_seen_document_urls = set()

# Known document history loaded from known_documents.csv
known_document_urls = set()
known_source_urls = set()
known_document_urls_before_run = set()
known_source_urls_before_run = set()
known_documents_to_append = []

current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# =========================
# REQUEST HEADERS / FETCH
# =========================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close"
}

IGNORE_WORDS = {
    "download",
    "view",
    "open",
    "read",
    "click",
    "file",
    "document",
    "pdf",
    "link",
    "here",
    "more",
    "details"
}

BAD_SECTION_TITLES = {
    "disclosures",
    "reports",
    "announcements",
    "investor relations",
    "financial reports",
    "company announcements",
    "corporate governance"
}

IMAGE_OR_ASSET_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".gif",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot"
)


def fetch_url(source_url):
    """
    Safe fetch logic.

    Default:
    - Use old simple requests.get() behavior for all normal URLs.

    Special:
    - Use browser-like headers + retry only for EQT ESG site,
      because EQT was timing out with normal request.

    Extra:
    - If a normal site returns 403 / 406 / 429,
      retry once with browser-like headers.
      This helps intermittent blocking without changing behavior for normal 200 pages.
    """

    source_lower = source_url.lower()

    # Special handling only for EQT ESG site
    if "esg.eqt.com" in source_lower:
        try:
            response = requests.get(
                source_url,
                timeout=30,
                headers=HEADERS
            )
            return response

        except requests.exceptions.Timeout as e:
            print(f"EQT timeout while fetching {source_url}: {e}")
            print("Retrying EQT once with longer timeout...")

            try:
                response = requests.get(
                    source_url,
                    timeout=60,
                    headers=HEADERS
                )
                return response

            except Exception as retry_error:
                print(f"EQT retry failed: {retry_error}")
                return None

        except requests.exceptions.ConnectionError as e:
            print(f"EQT connection error while fetching {source_url}: {e}")
            return None

        except Exception as e:
            print(f"EQT request error while fetching {source_url}: {e}")
            return None

    # Default old behavior for all other websites
    try:
        response = requests.get(source_url, timeout=15)

        # Generic retry only when site blocks simple request
        if response.status_code in [403, 406, 429]:
            print(f"Status {response.status_code} detected. Retrying with browser-like headers...")

            try:
                parsed = urlparse(source_url)

                retry_headers = HEADERS.copy()
                retry_headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

                retry_response = requests.get(
                    source_url,
                    timeout=25,
                    headers=retry_headers
                )

                print("Retry status:", retry_response.status_code)

                return retry_response

            except Exception as retry_error:
                print(f"Blocked-status retry failed: {retry_error}")
                return response

        return response

    except requests.exceptions.Timeout as e:
        print(f"Timeout while fetching {source_url}: {e}")
        return None

    except requests.exceptions.ConnectionError as e:
        print(f"Connection error while fetching {source_url}: {e}")
        return None

    except Exception as e:
        print(f"Request error while fetching {source_url}: {e}")
        return None


def add_issue(source_url, issue_type, status_code="", documents_captured=0, error_message=""):
    issue_rows.append({
        "date": current_date,
        "run_mode": RUN_MODE,
        "url_file": TARGET_URL_FILE,
        "source_url": source_url,
        "issue_type": issue_type,
        "status_code": status_code,
        "documents_captured": documents_captured,
        "error_message": error_message
    })


def validate_csv_header(file_path, expected_fieldnames):
    """
    Lock CSV format.

    If the file already exists and its header does not exactly match
    the expected fieldnames, stop the run before corrupting the file.
    """

    if not os.path.exists(file_path):
        return

    try:
        with open(file_path, newline="", encoding="utf-8") as existing_file:
            reader = csv.reader(existing_file)
            existing_header = next(reader, None)

        if existing_header is None:
            return

        if existing_header != expected_fieldnames:
            raise ValueError(
                f"CSV header mismatch in {file_path}. "
                f"Expected: {expected_fieldnames}. "
                f"Found: {existing_header}. "
                f"Please backup/reset this file before running again."
            )

    except StopIteration:
        return


def load_known_documents():
    """
    Load permanent known document history.

    known_document_urls:
    - used to prevent old documents from appearing in diff again.

    known_source_urls:
    - used to identify newly onboarded source URLs.
    """

    loaded_document_urls = set()
    loaded_source_urls = set()

    if not os.path.exists(KNOWN_DOCUMENTS_FILE):
        return loaded_document_urls, loaded_source_urls

    validate_csv_header(KNOWN_DOCUMENTS_FILE, KNOWN_DOCUMENTS_FIELDNAMES)

    try:
        with open(KNOWN_DOCUMENTS_FILE, newline="", encoding="utf-8") as known_file:
            reader = csv.DictReader(known_file)

            for r in reader:
                document_url = r.get("document_url", "")
                company = r.get("company", "")

                if document_url:
                    loaded_document_urls.add(normalize_url_key(document_url))

                if company:
                    loaded_source_urls.add(company)

    except Exception as e:
        print(f"Known documents load error: {e}")

    return loaded_document_urls, loaded_source_urls


def append_known_documents():
    """
    Append new known documents to known_documents.csv.

    This runs only for seed/full modes.
    Test and baseline modes do not update permanent known history.
    """

    if RUN_MODE not in ["full", "seed"]:
        return

    if not known_documents_to_append:
        return

    validate_csv_header(KNOWN_DOCUMENTS_FILE, KNOWN_DOCUMENTS_FIELDNAMES)

    file_exists = os.path.exists(KNOWN_DOCUMENTS_FILE)

    for record in known_documents_to_append:
        extra_keys = set(record.keys()) - set(KNOWN_DOCUMENTS_FIELDNAMES)
        missing_keys = set(KNOWN_DOCUMENTS_FIELDNAMES) - set(record.keys())

        if extra_keys or missing_keys:
            raise ValueError(
                f"Known documents row format mismatch. "
                f"Extra keys: {extra_keys}. "
                f"Missing keys: {missing_keys}. "
                f"Record: {record}"
            )

    with open(KNOWN_DOCUMENTS_FILE, "a", newline="", encoding="utf-8") as known_file:
        writer = csv.DictWriter(known_file, fieldnames=KNOWN_DOCUMENTS_FIELDNAMES)

        if not file_exists:
            writer.writeheader()

        writer.writerows(known_documents_to_append)


def load_previous_output_by_company():
    """
    Load previous output.csv before current run overwrites it.

    Kept for compatibility, but current output preservation is intentionally disabled.
    """

    previous_output_by_company = {}

    if not os.path.exists(OUTPUT_FILE):
        return previous_output_by_company

    try:
        with open(OUTPUT_FILE, newline="", encoding="utf-8") as old_output_file:
            reader = csv.DictReader(old_output_file)

            for r in reader:
                company = r.get("company", "")
                document_url = r.get("document_url", "")

                if not company or not document_url:
                    continue

                previous_output_by_company.setdefault(company, []).append({
                    "company": company,
                    "document_title": r.get("document_title", "Unknown Title"),
                    "document_title_source": r.get("document_title_source", "previous_output"),
                    "document_url": document_url
                })

    except Exception as e:
        print(f"Previous output load error: {e}")

    return previous_output_by_company


def preserve_previous_output_documents(target_source_urls, previous_output_by_company):
    """
    Preserve previous output documents for target URLs.

    Function kept intentionally, but current system does not call it.
    output.csv represents only documents captured in current run.
    known_documents.csv is the permanent history used for diff protection.
    """

    preserved_count = 0

    for source_url in target_source_urls:
        previous_docs = previous_output_by_company.get(source_url, [])

        for doc in previous_docs:
            document_url = doc.get("document_url", "")

            if not document_url:
                continue

            if not mark_document_seen(document_url):
                continue

            preserved_doc = {
                "company": doc.get("company", source_url),
                "document_title": doc.get("document_title", "Unknown Title"),
                "document_title_source": doc.get("document_title_source", "previous_output_reused"),
                "document_url": document_url
            }

            output_data.append(preserved_doc)
            preserved_count += 1

    if preserved_count > 0:
        print(f"PREVIOUS OUTPUT PRESERVED → {preserved_count} documents")

    return preserved_count


def queue_known_document_if_new(doc):
    """
    Add document to known_documents queue if not already known.

    This is used in seed/full only.
    """

    if RUN_MODE not in ["full", "seed"]:
        return

    document_url = doc.get("document_url", "")

    if not document_url:
        return

    document_key = normalize_url_key(document_url)

    if document_key in known_document_urls:
        return

    known_document_urls.add(document_key)
    known_source_urls.add(doc.get("company", ""))

    known_documents_to_append.append({
        "first_seen_date": current_date,
        "company": doc.get("company", ""),
        "document_title": doc.get("document_title", "Unknown Title"),
        "document_url": document_url,
        "source_run_mode": RUN_MODE
    })


def normalize_text(text):
    if not text:
        return ""

    text = html.unescape(text)
    text = unquote(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_report_keywords():
    """
    Load configurable report/card keywords from report_keywords.csv.

    If report_keywords.csv is missing or empty, use safe default keywords.
    This allows adding future card/report phrases without changing Python code.
    """

    default_keywords = [
        "integrated annual report",
        "annual report",
        "sustainability report",
        "esg report",
        "climate report",
        "cdp",
        "databook"
    ]

    keywords = []

    if os.path.exists(REPORT_KEYWORDS_FILE):
        try:
            with open(REPORT_KEYWORDS_FILE, newline="", encoding="utf-8") as keyword_file:
                reader = csv.DictReader(keyword_file)

                for row in reader:
                    keyword = normalize_text(row.get("keyword", ""))

                    if keyword:
                        keywords.append(keyword.lower())

        except Exception as e:
            print(f"Report keywords load error: {e}")

    if not keywords:
        keywords = default_keywords

    unique_keywords = []
    seen_keywords = set()

    for keyword in keywords:
        key = keyword.lower().strip()

        if key and key not in seen_keywords:
            seen_keywords.add(key)
            unique_keywords.append(key)

    return unique_keywords


def build_report_keyword_regex():
    """
    Build regex pattern from report_keywords.csv values.
    Spaces in keywords are converted to flexible whitespace.
    """

    escaped_keywords = []

    for keyword in REPORT_CARD_KEYWORDS:
        keyword = keyword.strip()

        if not keyword:
            continue

        escaped = re.escape(keyword)
        escaped = escaped.replace(r"\ ", r"\s+")
        escaped_keywords.append(escaped)

    if not escaped_keywords:
        escaped_keywords = [
            r"annual\s+report",
            r"sustainability\s+report",
            r"esg\s+report"
        ]

    return r"(" + "|".join(escaped_keywords) + r")"


REPORT_CARD_KEYWORDS = load_report_keywords()
REPORT_CARD_KEYWORD_REGEX = build_report_keyword_regex()


def is_generic_action_title(text):
    """
    Detect weak/generic link text that should not be used as document title.
    """

    text = normalize_text(text).lower()

    if not text:
        return True

    generic_patterns = [
        r"^download\s*(pdf|report|document|file)?",
        r"^view\s*(pdf|report|document|file)?",
        r"^open\s*(pdf|report|document|file)?",
        r"^read\s*(more|report|document)?",
        r"^click\s*here",
        r"^learn\s*more",
        r"opens?\s+in\s+(a\s+)?new\s+(window|tab)",
        r"^pdf$",
        r"^download$",
        r"^view$",
        r"^open$",
        r"^more$",
        r"^details$",
        r"^file$",
        r"^document$"
    ]

    return any(re.search(pattern, text, re.IGNORECASE) for pattern in generic_patterns)


def is_date_only(text):
    text = normalize_text(text)

    date_patterns = [
        r"^\d{1,2}\s+\w+,\s+\d{4}$",
        r"^\d{1,2}\s+\w+\s+\d{4}$",
        r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$",
        r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$"
    ]

    return any(re.match(pattern, text, re.IGNORECASE) for pattern in date_patterns)


def is_number_file(text):
    text = normalize_text(text).lower()

    text = text.replace(".pdf", "")
    text = text.replace(".ashx", "")
    text = text.replace("_", "-")

    return bool(re.match(r"^[0-9\-]+$", text))


def is_uuid_like(text):
    text = normalize_text(text).lower()
    text = text.replace(" ", "-")

    uuid_pattern = r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"

    return bool(re.match(uuid_pattern, text))


def is_bad_title(text):
    text = normalize_text(text)
    lower = text.lower()

    if not text:
        return True

    if is_generic_action_title(text):
        return True

    if lower in IGNORE_WORDS:
        return True

    if lower in BAD_SECTION_TITLES:
        return True

    if len(text) < 6:
        return True

    if is_date_only(text):
        return True

    if is_number_file(text):
        return True

    if is_uuid_like(text):
        return True

    if lower in ["download pdf", "view pdf", "open pdf"]:
        return True

    if not re.search(r"[a-zA-Z]", text):
        return True

    return False


def title_quality_score(title):
    """
    Score title quality.
    Higher score means title is more descriptive.
    """

    title = normalize_text(title)

    if not title:
        return 0

    if is_bad_title(title):
        return 0

    score = 0

    score += min(len(title), 80)

    useful_words = [
        "annual",
        "quarter",
        "quarterly",
        "interim",
        "sustainability",
        "esg",
        "climate",
        "governance",
        "financial",
        "statement",
        "report",
        "results",
        "presentation",
        "proxy",
        "agm",
        "csr",
        "brsr",
        "modern slavery",
        "committee",
        "charter",
        "policy"
    ]

    lower = title.lower()

    for word in useful_words:
        if word in lower:
            score += 12

    if re.search(r"\b20\d{2}\b", title):
        score += 15

    if re.search(r"\b(q[1-4]|h1|h2|fy|year[-\s]?end|annual)\b", lower):
        score += 10

    if len(title) > 160:
        score -= 30

    if is_generic_action_title(title):
        score -= 100

    return score


def is_image_or_asset_url(url):
    lower = url.lower().split("?")[0]
    return lower.endswith(IMAGE_OR_ASSET_EXTENSIONS)


def clean_title_from_url(url):
    parsed = urlparse(url)
    path = parsed.path

    parts = [p for p in path.split("/") if p]

    if not parts:
        return ""

    filename_raw = parts[-1]
    filename_raw = unquote(filename_raw)
    filename_raw = html.unescape(filename_raw)
    filename_raw = filename_raw.split("?")[0]

    filename_without_ext = filename_raw
    filename_without_ext = filename_without_ext.replace(".pdf", "")
    filename_without_ext = filename_without_ext.replace(".ashx", "")
    filename_without_ext = filename_without_ext.replace(".aspx", "")
    filename_without_ext = filename_without_ext.replace(".html", "")

    if is_uuid_like(filename_without_ext):
        return ""

    filename = filename_without_ext.replace("-", " ")
    filename = filename.replace("_", " ")
    filename = normalize_text(filename)

    if is_bad_title(filename):
        return ""

    return filename


def choose_best_title_from_candidates(candidates):
    valid_candidates = []

    for candidate in candidates:
        title = normalize_text(candidate.get("title", ""))
        source = candidate.get("source", "unknown")

        if not title:
            continue

        score = title_quality_score(title)

        if score <= 0:
            continue

        valid_candidates.append({
            "title": title,
            "source": source,
            "score": score
        })

    if not valid_candidates:
        return {
            "title": "Unknown Title",
            "source": "unknown",
            "score": 0
        }

    valid_candidates.sort(key=lambda x: x["score"], reverse=True)

    return valid_candidates[0]


def choose_best_title_from_text_and_url(text_title, url_title):
    candidates = [
        {
            "title": text_title,
            "source": "browser_or_html_text"
        },
        {
            "title": url_title,
            "source": "url"
        }
    ]

    return choose_best_title_from_candidates(candidates)


def collect_text_candidates_from_container(container):
    candidates = []

    if not container:
        return candidates

    for element in container.find_all(
        ["td", "th", "p", "span", "div", "h1", "h2", "h3", "h4", "strong", "a"],
        recursive=True
    ):
        text = normalize_text(element.get_text(" ", strip=True))

        if not is_bad_title(text):
            candidates.append(text)

    for text in container.stripped_strings:
        text = normalize_text(text)

        if not is_bad_title(text):
            candidates.append(text)

    unique_candidates = []
    seen_texts = set()

    for candidate in candidates:
        key = candidate.lower()

        if key not in seen_texts:
            seen_texts.add(key)
            unique_candidates.append(candidate)

    return unique_candidates


def get_title_from_html_context(link):
    row = link.find_parent("tr")
    if row:
        row_candidates = collect_text_candidates_from_container(row)

        if row_candidates:
            return max(row_candidates, key=len)

    li = link.find_parent("li")
    if li:
        li_candidates = collect_text_candidates_from_container(li)

        if li_candidates:
            return max(li_candidates, key=len)

    current = link.parent
    levels_checked = 0

    while current and levels_checked < 5:
        if current.name in ["div", "section", "article", "p"]:
            block_candidates = collect_text_candidates_from_container(current)

            if block_candidates:
                return max(block_candidates, key=len)

        current = current.parent
        levels_checked += 1

    return ""


def get_link_text_title(link):
    text = normalize_text(link.get_text(" ", strip=True))

    if is_bad_title(text):
        return ""

    return text


def normalize_url_key(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".lower()


def is_static_file_link(url):
    return "/static-files/" in url.lower()


def mark_document_seen(document_url):
    """
    Mark document URL globally.
    Returns True if this document URL is new for the current run.
    Returns False if duplicate.
    """

    duplicate_key = normalize_url_key(document_url)

    if duplicate_key in global_seen_document_urls:
        return False

    global_seen_document_urls.add(duplicate_key)
    return True


def get_best_title_with_source(link, full_url, source_url):
    html_title = get_title_from_html_context(link)
    link_text = get_link_text_title(link)
    url_title = clean_title_from_url(full_url)

    candidates = []

    if is_static_file_link(full_url):
        candidates.extend([
            {
                "title": html_title,
                "source": "html_context_static_file"
            },
            {
                "title": link_text,
                "source": "link_text_static_file"
            },
            {
                "title": url_title,
                "source": "url_static_file"
            }
        ])
    else:
        candidates.extend([
            {
                "title": url_title,
                "source": "url"
            },
            {
                "title": html_title,
                "source": "html_context"
            },
            {
                "title": link_text,
                "source": "link_text"
            }
        ])

    if "space42.ai" in source_url.lower() and url_title and not is_bad_title(url_title):
        return {
            "title": url_title,
            "source": "url_space42",
            "score": title_quality_score(url_title)
        }

    best = choose_best_title_from_candidates(candidates)

    if not best["title"] or best["title"] == "Unknown Title":
        parsed = urlparse(full_url)
        filename = parsed.path.split("/")[-1]
        filename = unquote(filename)
        filename = html.unescape(filename)
        filename = filename.split("?")[0]
        filename = filename.replace(".pdf", "")
        filename = filename.replace(".ashx", "")
        filename = filename.replace("-", " ")
        filename = filename.replace("_", " ")
        filename = normalize_text(filename)

        if filename and not is_bad_title(filename):
            return {
                "title": filename,
                "source": "fallback_filename",
                "score": title_quality_score(filename)
            }

    return best


def get_best_text(link, full_url, source_url):
    best = get_best_title_with_source(link, full_url, source_url)
    return best["title"]


def is_document_link(url):
    lower = url.lower()

    if is_image_or_asset_url(lower):
        return False

    if ".pdf" in lower:
        return True

    if ".ashx" in lower:
        return True

    if "/static-files/" in lower:
        return True

    return False


def is_click_document_candidate(url):
    lower = url.lower()

    if is_image_or_asset_url(lower):
        return False

    if is_document_link(lower):
        return True

    if "/download" in lower:
        return True

    if "/downloads/" in lower:
        return True

    if "/media/" in lower:
        return True

    if "/upload" in lower:
        return True

    if "/uploads/" in lower:
        return True

    if "/storage/" in lower:
        return True

    if "/files/" in lower:
        return True

    return False


def is_navigation_link(url):
    lower = url.lower()

    if is_image_or_asset_url(lower):
        return True

    if "#" in lower and not is_document_link(lower):
        return True

    if lower.endswith("/"):
        return True

    if lower.endswith(".html"):
        return True

    if lower.endswith(".aspx"):
        return True

    return False
def parse_source_url(raw_source_url):
    """
    Allow per-URL fallback control from CSV.

    Example:
    Fallback/https://www.ril.com/investors/financial-reporting/online-annual-report

    This means:
    - actual URL = https://www.ril.com/investors/financial-reporting/online-annual-report
    - force browser fallback = True
    """

    raw_source_url = normalize_text(raw_source_url)

    fallback_prefixes = [
        "fallback/",
        "fallback:"
    ]

    lower = raw_source_url.lower()

    for prefix in fallback_prefixes:
        if lower.startswith(prefix):
            clean_url = raw_source_url[len(prefix):].strip()

            return {
                "source_url": clean_url,
                "force_browser_fallback": True
            }

    return {
        "source_url": raw_source_url,
        "force_browser_fallback": False
    }


def should_use_browser_fallback(source_url, force_browser_fallback=False):
    return ENABLE_BROWSER_FALLBACK or force_browser_fallback


def source_url_has_hash(source_url):
    return "#" in source_url


def get_hash_fragments(source_url):
    parsed = urlparse(source_url)
    fragment = parsed.fragment

    if not fragment:
        return []

    parts = [p for p in fragment.split("#") if p.strip()]

    cleaned_parts = []

    for part in parts:
        raw = unquote(part.strip())
        text = raw.replace("-", " ").replace("_", " ")
        text = normalize_text(text)

        if text:
            cleaned_parts.append({
                "raw": raw,
                "text": text
            })

    return cleaned_parts


def get_iframe_soups(source_url, soup):
    iframe_soups = []

    iframe_tags = soup.find_all("iframe")

    for iframe in iframe_tags:
        iframe_src = iframe.get("src")

        if not iframe_src:
            continue

        iframe_url = urljoin(source_url, iframe_src)

        try:
            print(f"Checking iframe: {iframe_url}")

            iframe_response = fetch_url(iframe_url)

            if iframe_response is None:
                print("Iframe failed to fetch")
                continue

            print("Iframe status:", iframe_response.status_code)

            if iframe_response.status_code == 200:
                iframe_soup = BeautifulSoup(iframe_response.text, "html.parser")
                iframe_soups.append({
                    "iframe_url": iframe_url,
                    "soup": iframe_soup
                })

        except Exception as e:
            print(f"Iframe error: {e}")

    return iframe_soups


def extract_links_from_soup(soup, base_url, source_url, seen, label="KEPT"):
    docs_found = []

    links = soup.find_all("a")

    for link in links:
        href = link.get("href")

        if not href:
            continue

        full_url = urljoin(base_url, href)
        href_lower = full_url.lower()

        title_info = get_best_title_with_source(link, full_url, source_url)
        title = title_info["title"]
        title_source = title_info["source"]

        raw_links.append({
            "company": source_url,
            "text": title,
            "title_source": title_source,
            "url": full_url
        })

        if is_document_link(href_lower):
            duplicate_key = normalize_url_key(full_url)

            if duplicate_key in seen:
                continue

            seen.add(duplicate_key)

            if not mark_document_seen(full_url):
                print(f"GLOBAL DUPLICATE SKIPPED → {title}")
                continue

            print(f"{label} → {title} [{title_source}]")

            doc = {
                "company": source_url,
                "document_title": title,
                "document_title_source": title_source,
                "document_url": full_url
            }

            output_data.append(doc)
            docs_found.append(doc)

            continue

        if is_navigation_link(href_lower):
            continue

    return docs_found


def extract_title_from_playwright_element(element):
    try:
        title = element.evaluate(
            """
            el => {
                const bad = new Set(["view", "download", "open", "read", "click", "pdf"]);
                function clean(t) {
                    return (t || "").replace(/\\s+/g, " ").trim();
                }

                function isBad(t) {
                    if (!t) return true;
                    const l = t.toLowerCase();
                    if (bad.has(l)) return true;
                    if (t.length < 6) return true;
                    if (/^\\d{1,2}\\s+\\w+,\\s+\\d{4}$/.test(t)) return true;
                    if (!/[A-Za-z]/.test(t)) return true;
                    if (/^download\\s*(pdf|report|document)?/i.test(t)) return true;
                    if (/^view\\s*(pdf|report|document)?/i.test(t)) return true;
                    if (/opens? in (a )?new (window|tab)/i.test(t)) return true;
                    return false;
                }

                const containers = [
                    el.closest("tr"),
                    el.closest("li"),
                    el.closest("article"),
                    el.closest("section"),
                    el.closest("div")
                ].filter(Boolean);

                for (const c of containers) {
                    const texts = Array.from(c.querySelectorAll("td, th, h1, h2, h3, h4, p, span, div, strong, a"))
                        .map(x => clean(x.innerText))
                        .filter(x => !isBad(x));

                    if (texts.length > 0) {
                        texts.sort((a, b) => b.length - a.length);
                        return texts[0];
                    }
                }

                const own = clean(el.innerText);
                if (!isBad(own)) return own;

                return "";
            }
            """
        )

        title = normalize_text(title)

        if title and not is_bad_title(title):
            return title

    except Exception:
        pass

    return ""


def should_trigger_report_card_fallback(soup, docs_captured_for_url):
    """
    Detect pages where report documents appear as visual cards/tiles
    and normal HTML scraping captured only a few direct links.

    Keywords are loaded from report_keywords.csv.
    """

    if docs_captured_for_url >= 5:
        return False

    try:
        page_text = normalize_text(soup.get_text(" ", strip=True))

        report_matches = re.findall(
            REPORT_CARD_KEYWORD_REGEX + r".{0,80}\b20\d{2}\b",
            page_text,
            flags=re.IGNORECASE
        )

        unique_matches = set()

        for match in report_matches:
            if isinstance(match, tuple):
                match_text = " ".join([str(x) for x in match if x])
            else:
                match_text = str(match)

            match_text = normalize_text(match_text).lower()

            if match_text:
                unique_matches.add(match_text)

        if len(unique_matches) >= 3:
            print(f"Report-card fallback signal detected: {len(unique_matches)} report-like items")
            return True

    except Exception as e:
        print(f"Report-card fallback detection error: {e}")

    return False


def browser_click_fallback(source_url, existing_keys):
    fallback_docs = []

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"Playwright not available: {e}")
        return fallback_docs

    print("Running enhanced browser fallback for failed/zero-doc/hash page...")

    def add_doc_from_url(found_url, title_hint=""):
        full_url = urljoin(source_url, found_url)
        key = normalize_url_key(full_url)

        if key in existing_keys:
            return

        if not is_click_document_candidate(full_url):
            return

        if not mark_document_seen(full_url):
            print(f"GLOBAL DUPLICATE SKIPPED FROM FALLBACK → {full_url}")
            return

        url_title = clean_title_from_url(full_url)

        title_info = choose_best_title_from_text_and_url(title_hint, url_title)
        title = title_info["title"]
        title_source = title_info["source"]

        if not title or is_bad_title(title):
            title = "Unknown Title"
            title_source = "unknown"

        existing_keys.add(key)

        print(f"FALLBACK KEPT → {title} [{title_source}]")

        fallback_docs.append({
            "company": source_url,
            "document_title": title,
            "document_title_source": title_source,
            "document_url": full_url
        })

    def scan_soup_for_links(scan_soup, base_url):
        for tag in scan_soup.find_all(True):
            for attr in [
                "href",
                "src",
                "data-href",
                "data-url",
                "data-link",
                "data-file",
                "data-download",
                "data-src",
                "onclick"
            ]:
                value = tag.get(attr)

                if not value:
                    continue

                possible_values = [value]

                if attr == "onclick":
                    possible_values = re.findall(
                        r"""[^'"]+\.(?:pdf|ashx|aspx[^'"]*)""",
                        value,
                        flags=re.IGNORECASE
                    )

                    possible_values += re.findall(
                        r"""[^'"]*(?:/media/|/download|/downloads/|/files/|/uploads/|/storage/)[^'"]*""",
                        value,
                        flags=re.IGNORECASE
                    )

                for possible_value in possible_values:
                    full_url = urljoin(base_url, possible_value)

                    if not is_click_document_candidate(full_url):
                        continue

                    text_title = normalize_text(tag.get_text(" ", strip=True))
                    url_title = clean_title_from_url(full_url)

                    title_info = choose_best_title_from_text_and_url(text_title, url_title)

                    add_doc_from_url(full_url, title_info["title"])

    def scan_all_rendered_content(page):
        try:
            html_content = page.content()
            main_soup = BeautifulSoup(html_content, "html.parser")
            scan_soup_for_links(main_soup, page.url or source_url)
        except Exception as e:
            print(f"Main page scan error: {e}")

        try:
            frames = page.frames
            print(f"Frames found: {len(frames)}")

            for frame in frames:
                try:
                    frame_url = frame.url or source_url

                    if frame_url == "about:blank":
                        continue

                    print(f"Scanning frame: {frame_url}")

                    frame_html = frame.content()
                    frame_soup = BeautifulSoup(frame_html, "html.parser")

                    scan_soup_for_links(frame_soup, frame_url)

                except Exception as frame_error:
                    print(f"Frame scan error: {frame_error}")

        except Exception as e:
            print(f"Frame collection error: {e}")

    def collect_report_detail_links(page):
        """
        Collect report/detail page links from rendered page.

        Handles pages where report cards/rows open an intermediate viewer/detail page,
        and the PDF is available only after clicking a download icon on that detail page.

        This version avoids same-page anchor links like #main-navigation
        and prioritizes real report detail URLs like:
        - /ar2024-25/index.html
        - /sustainability-report-2024/
        - /annual-report-2024/
        """

        detail_links = []
        seen_detail_urls = set()

        try:
            html_content = page.content()
            page_soup = BeautifulSoup(html_content, "html.parser")

            for link in page_soup.find_all("a"):
                href = link.get("href")

                if not href:
                    continue

                href = href.strip()

                # Skip pure same-page anchors
                if href.startswith("#"):
                    continue

                full_url = urljoin(page.url or source_url, href)
                parsed_full = urlparse(full_url)
                parsed_source = urlparse(source_url)

                # Remove fragment for comparison/storage
                full_url_without_fragment = parsed_full._replace(fragment="").geturl()
                full_url_lower = full_url_without_fragment.lower()

                # Skip same page anchor/navigation URLs
                current_page_without_fragment = urlparse(page.url or source_url)._replace(fragment="").geturl()

                if full_url_without_fragment == current_page_without_fragment:
                    continue

                if is_image_or_asset_url(full_url_lower):
                    continue

                # Direct documents are already handled elsewhere.
                if is_document_link(full_url_lower):
                    continue

                link_text = normalize_text(link.get_text(" ", strip=True))

                parent_text_candidates = []

                for parent_name in ["tr", "li", "article", "section", "div"]:
                    parent = link.find_parent(parent_name)

                    if parent:
                        parent_text = normalize_text(parent.get_text(" ", strip=True))

                        if parent_text:
                            parent_text_candidates.append(parent_text)

                parent_text = " ".join(parent_text_candidates[:3])

                combined_text = normalize_text(f"{link_text} {parent_text} {full_url_without_fragment}")

                keyword_hit = False

                try:
                    keyword_hit = bool(
                        re.search(REPORT_CARD_KEYWORD_REGEX, combined_text, flags=re.IGNORECASE)
                    )
                except Exception:
                    keyword_hit = False

                year_hit = bool(
                    re.search(
                        r"\b20\d{2}([-/]\d{2})?\b",
                        combined_text,
                        flags=re.IGNORECASE
                    )
                )

                path_hit = any(
                    marker in full_url_lower
                    for marker in [
                        "/sustainability-reports/",
                        "/annual-reports/",
                        "/annual-report/",
                        "/reports/",
                        "/report-",
                        "sustainability-report",
                        "annual-report",
                        "online-annual-report"
                    ]
                )

                # RIL-style annual report URL:
                # https://www.ril.com/ar2024-25/index.html
                ril_style_ar_hit = bool(
                    re.search(
                        r"/ar20\d{2}[-/]\d{2}/",
                        full_url_lower,
                        flags=re.IGNORECASE
                    )
                )

                same_domain = parsed_full.netloc == parsed_source.netloc

                if same_domain and (keyword_hit or path_hit or ril_style_ar_hit or (year_hit and keyword_hit)):
                    key = normalize_url_key(full_url_without_fragment)

                    if key not in seen_detail_urls:
                        seen_detail_urls.add(key)

                        title_hint = (
                            link_text
                            or parent_text
                            or clean_title_from_url(full_url_without_fragment)
                            or "Unknown Title"
                        )

                        priority = 0

                        if ril_style_ar_hit:
                            priority += 100

                        if path_hit:
                            priority += 50

                        if keyword_hit:
                            priority += 30

                        if year_hit:
                            priority += 20

                        detail_links.append({
                            "url": full_url_without_fragment,
                            "title": title_hint,
                            "priority": priority
                        })

            detail_links.sort(key=lambda x: x.get("priority", 0), reverse=True)

            print(f"Report detail pages discovered: {len(detail_links)}")

            for detail in detail_links[:10]:
                print(f"Report detail candidate: {detail.get('url')}")

        except Exception as e:
            print(f"Report detail link collection error: {e}")

        return detail_links[:30]

    def click_download_controls_on_detail_page(page, title_hint=""):
        """
        On a report detail/viewer page, click download-like controls/icons
        and capture the final PDF/download URL.
        """

        try:
            scan_all_rendered_content(page)

            download_selector = (
                "a[download], "
                "a[href*='.pdf'], "
                "a[href*='.ashx'], "
                "a[href*='/media/'], "
                "a[href*='/download'], "
                "a[href*='/downloads/'], "
                "a[href*='/files/'], "
                "button[aria-label*='download' i], "
                "a[aria-label*='download' i], "
                "button[title*='download' i], "
                "a[title*='download' i], "
                "[class*='download'], "
                "[class*='Download']"
            )

            download_controls = page.locator(download_selector)

            count = min(download_controls.count(), 15)

            print(f"Download controls found on detail page: {count}")

            for i in range(count):
                try:
                    element = download_controls.nth(i)

                    before_url = page.url
                    captured_url = ""

                    try:
                        element.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass

                    try:
                        href_value = element.get_attribute("href", timeout=1000)

                        if href_value:
                            possible_url = urljoin(page.url or source_url, href_value)

                            if is_click_document_candidate(possible_url):
                                captured_url = possible_url

                    except Exception:
                        pass

                    if not captured_url:
                        try:
                            with page.expect_popup(timeout=2500) as popup_info:
                                element.click(timeout=4000, force=True)

                            popup = popup_info.value
                            popup.wait_for_load_state("domcontentloaded", timeout=10000)
                            captured_url = popup.url
                            popup.close()

                        except Exception:
                            pass

                    if not captured_url:
                        try:
                            with page.expect_download(timeout=2500) as download_info:
                                element.click(timeout=4000, force=True)

                            download = download_info.value
                            captured_url = download.url

                        except Exception:
                            pass

                    if not captured_url:
                        try:
                            element.click(timeout=4000, force=True)
                            page.wait_for_timeout(1500)

                            if page.url != before_url:
                                captured_url = page.url

                                try:
                                    page.goto(before_url, wait_until="domcontentloaded", timeout=60000)
                                    page.wait_for_timeout(1000)
                                except Exception:
                                    pass

                        except Exception:
                            pass

                    if captured_url:
                        add_doc_from_url(captured_url, title_hint)

                    scan_all_rendered_content(page)

                except Exception:
                    continue

        except Exception as e:
            print(f"Download control click error on detail page: {e}")

    def visit_report_detail_pages(page):
        """
        Visit report detail pages discovered from the main page,
        then click download controls/icons on each detail page.
        """

        try:
            detail_links = collect_report_detail_links(page)

            if not detail_links:
                return

            original_url = page.url or source_url

            for detail in detail_links:
                detail_url = detail.get("url", "")
                title_hint = detail.get("title", "")

                if not detail_url:
                    continue

                try:
                    print(f"Visiting report detail page: {detail_url}")

                    page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)

                    click_download_controls_on_detail_page(page, title_hint)

                except Exception as detail_error:
                    print(f"Report detail page visit error: {detail_error}")

            try:
                page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

        except Exception as e:
            print(f"Report detail page phase error: {e}")

    def interact_with_hash_fragments(page):
        hash_fragments = get_hash_fragments(source_url)

        if not hash_fragments:
            return

        print(f"Hash fragments found: {hash_fragments}")

        for fragment in hash_fragments:
            raw_fragment = fragment["raw"]
            text_fragment = fragment["text"]

            try:
                clicked = page.evaluate(
                    """
                    data => {
                        const raw = data.raw;
                        const text = data.text.toLowerCase();

                        function clean(t) {
                            return (t || "").replace(/\\s+/g, " ").trim().toLowerCase();
                        }

                        const candidates = Array.from(document.querySelectorAll(
                            "a, button, [role='button'], [aria-expanded], [data-toggle], [data-bs-toggle], li, div, span"
                        ));

                        for (const el of candidates) {
                            const id = (el.getAttribute("id") || "").trim();
                            const href = (el.getAttribute("href") || "").trim();
                            const dataTarget = (el.getAttribute("data-target") || "").trim();
                            const dataBsTarget = (el.getAttribute("data-bs-target") || "").trim();
                            const label = clean(el.innerText);

                            if (label.length > 120) continue;

                            if (id === raw || href === "#" + raw || dataTarget === "#" + raw || dataBsTarget === "#" + raw || label.includes(text)) {
                                try {
                                    el.scrollIntoView({block: "center", inline: "center"});
                                } catch(e) {}

                                try {
                                    el.click();
                                    return true;
                                } catch(e) {
                                    return false;
                                }
                            }
                        }

                        return false;
                    }
                    """,
                    {
                        "raw": raw_fragment,
                        "text": text_fragment
                    }
                )

                print(f"Hash interaction for '{text_fragment}': {clicked}")

                page.wait_for_timeout(1200)
                scan_all_rendered_content(page)

            except Exception as e:
                print(f"Hash interaction error for {text_fragment}: {e}")

    def click_report_card_controls(page):
        """
        Click report-like cards/tiles that may open PDFs or trigger downloads.

        Keywords are loaded from report_keywords.csv.
        """

        try:
            report_card_selector = (
                "a, button, [role='button'], article, section, .card, div, span"
            )

            report_cards = page.locator(report_card_selector).filter(
                has_text=re.compile(
                    REPORT_CARD_KEYWORD_REGEX + r"|20\d{2}",
                    re.IGNORECASE
                )
            )

            count = min(report_cards.count(), 25)

            print(f"Report-card fallback controls found: {count}")

            for i in range(count):
                try:
                    element = report_cards.nth(i)

                    title = extract_title_from_playwright_element(element)

                    before_url = page.url
                    captured_url = ""

                    try:
                        element.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass

                    try:
                        with page.expect_popup(timeout=2500) as popup_info:
                            element.click(timeout=4000, force=True)

                        popup = popup_info.value
                        popup.wait_for_load_state("domcontentloaded", timeout=10000)
                        captured_url = popup.url
                        popup.close()

                    except Exception:
                        pass

                    if not captured_url:
                        try:
                            with page.expect_download(timeout=2500) as download_info:
                                element.click(timeout=4000, force=True)

                            download = download_info.value
                            captured_url = download.url

                        except Exception:
                            pass

                    if not captured_url:
                        try:
                            element.click(timeout=4000, force=True)
                            page.wait_for_timeout(1500)

                            if page.url != before_url:
                                captured_url = page.url

                                try:
                                    page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
                                    page.wait_for_timeout(1000)
                                except Exception:
                                    pass

                        except Exception:
                            pass

                    if captured_url:
                        add_doc_from_url(captured_url, title)

                    scan_all_rendered_content(page)

                except Exception:
                    continue

        except Exception as e:
            print(f"Report-card click phase error: {e}")

    def is_expandable_element(element):
        try:
            return element.evaluate(
                """
                el => {
                    const text = (el.innerText || "").replace(/\\s+/g, " ").trim();
                    const cls = (el.className || "").toString().toLowerCase();
                    const href = (el.getAttribute("href") || "").trim();

                    if (text.length > 80) return false;

                    if (el.hasAttribute("aria-expanded")) return true;
                    if (el.hasAttribute("data-toggle")) return true;
                    if (el.hasAttribute("data-bs-toggle")) return true;
                    if (el.hasAttribute("onclick")) return true;
                    if (href.startsWith("#")) return true;

                    if (cls.includes("dropdown")) return true;
                    if (cls.includes("accordion")) return true;
                    if (cls.includes("collapse")) return true;
                    if (cls.includes("tab")) return true;
                    if (cls.includes("nav")) return true;
                    if (cls.includes("card")) return true;
                    if (cls.includes("menu")) return true;
                    if (cls.includes("toggle")) return true;

                    return false;
                }
                """
            )
        except Exception:
            return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )

            page = context.new_page()

            def handle_response(response):
                try:
                    response_url = response.url

                    if is_click_document_candidate(response_url):
                        add_doc_from_url(response_url, "")
                except Exception:
                    pass

            page.on("response", handle_response)

            try:
                page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"Browser goto domcontentloaded error: {e}")

                try:
                    page.goto(source_url, wait_until="load", timeout=60000)
                except Exception as e2:
                    print(f"Browser goto load error: {e2}")
                    browser.close()
                    return fallback_docs

            page.wait_for_timeout(5000)

            scan_all_rendered_content(page)

            interact_with_hash_fragments(page)

            visit_report_detail_pages(page)

            click_report_card_controls(page)

            try:
                expander_selector = (
                    "a[href^='#'], "
                    "button, "
                    "[role='button'], "
                    "[aria-expanded], "
                    "[data-toggle], "
                    "[data-bs-toggle], "
                    ".dropdown-toggle, "
                    ".accordion-button, "
                    ".nav-link, "
                    ".tab, "
                    ".card, "
                    "li, "
                    "div, "
                    "span"
                )

                expandable_candidates = page.locator(expander_selector)

                total_expandables = expandable_candidates.count()
                max_expandable_clicks = min(total_expandables, 35)

                print(f"Expandable candidates found: {total_expandables}")
                print(f"Expandable candidates to try: {max_expandable_clicks}")

                clicked_expanders = 0

                for i in range(max_expandable_clicks):
                    try:
                        element = expandable_candidates.nth(i)

                        if not is_expandable_element(element):
                            continue

                        element.scroll_into_view_if_needed(timeout=3000)
                        element.click(timeout=3000, force=True)
                        clicked_expanders += 1

                        page.wait_for_timeout(1000)

                        scan_all_rendered_content(page)

                    except Exception:
                        continue

                print(f"Expandable controls clicked: {clicked_expanders}")

            except Exception as e:
                print(f"Expandable click phase error: {e}")

            try:
                clickable = page.locator(
                    "a, button, [role='button'], .btn, .button"
                ).filter(
                    has_text=re.compile(
                        r"(view|download|pdf|open|report|annual|sustainability|disclosure)",
                        re.IGNORECASE
                    )
                )

                count = min(clickable.count(), 15)

                print(f"Clickable fallback controls found: {count}")

                for i in range(count):
                    try:
                        element = clickable.nth(i)
                        title = extract_title_from_playwright_element(element)

                        before_url = page.url
                        captured_url = ""

                        try:
                            with page.expect_popup(timeout=2500) as popup_info:
                                element.click(timeout=4000)

                            popup = popup_info.value
                            popup.wait_for_load_state("domcontentloaded", timeout=10000)
                            captured_url = popup.url
                            popup.close()

                        except Exception:
                            pass

                        if not captured_url:
                            try:
                                with page.expect_download(timeout=2500) as download_info:
                                    element.click(timeout=4000)

                                download = download_info.value
                                captured_url = download.url

                            except Exception:
                                pass

                        if not captured_url:
                            try:
                                element.click(timeout=4000)
                                page.wait_for_timeout(1200)

                                if page.url != before_url:
                                    captured_url = page.url
                                    page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
                                    page.wait_for_timeout(1000)

                            except Exception:
                                pass

                        if captured_url:
                            add_doc_from_url(captured_url, title)

                        scan_all_rendered_content(page)

                    except Exception:
                        continue

            except Exception as e:
                print(f"Clickable scan error: {e}")

            browser.close()

    except Exception as e:
        print(f"Browser fallback error: {e}")

    return fallback_docs


def process_source_url(source_url, retry_attempt=False, force_browser_fallback=False):
    """
    Process one source URL.

    retry_attempt=False means first pass.
    retry_attempt=True means retry pass.

    Returns number of documents captured for this source URL.
    """

    print(f"\nChecking: {source_url}")

    start_doc_count = len(output_data)

    try:
        response = fetch_url(source_url)

        if response is None:
            print("Failed to fetch page")

            docs_captured_for_url = 0

            if should_use_browser_fallback(source_url, force_browser_fallback):
                print("Request failed. Trying browser fallback...")

                seen = set()
                fallback_docs = browser_click_fallback(source_url, seen)

                for doc in fallback_docs:
                    output_data.append(doc)

                    raw_links.append({
                        "company": doc["company"],
                        "text": doc["document_title"],
                        "title_source": doc.get("document_title_source", "browser_fallback"),
                        "url": doc["document_url"]
                    })

                docs_captured_for_url = len(fallback_docs)

            if docs_captured_for_url == 0:
                if not retry_attempt and RETRY_FAILED_URLS:
                    print("Queued for retry: request failed")
                    retry_queue.append({
                        "source_url": source_url,
                        "force_browser_fallback": force_browser_fallback
                    })
                else:
                    add_issue(
                        source_url=source_url,
                        issue_type="FETCH_ERROR_AFTER_RETRY" if retry_attempt else "FETCH_ERROR",
                        status_code="",
                        documents_captured=0,
                        error_message="Request failed / timeout and browser fallback captured 0 documents"
                    )
            return docs_captured_for_url

        print("Status:", response.status_code)

        if response.status_code != 200:
            print("Failed to fetch page")

            docs_captured_for_url = 0

            if should_use_browser_fallback(source_url, force_browser_fallback):
                print("Non-200 status detected. Trying browser fallback...")

                seen = set()
                fallback_docs = browser_click_fallback(source_url, seen)

                for doc in fallback_docs:
                    output_data.append(doc)

                    raw_links.append({
                        "company": doc["company"],
                        "text": doc["document_title"],
                        "title_source": doc.get("document_title_source", "browser_fallback"),
                        "url": doc["document_url"]
                    })

                docs_captured_for_url = len(fallback_docs)

            if docs_captured_for_url == 0:
                if not retry_attempt and RETRY_FAILED_URLS:
                    print("Queued for retry: non-200 status")
                    retry_queue.append({
                    "source_url": source_url,
                    "force_browser_fallback": force_browser_fallback
                     })
                else:
                    add_issue(
                        source_url=source_url,
                        issue_type="FETCH_FAILED_STATUS_AFTER_RETRY" if retry_attempt else "FETCH_FAILED_STATUS",
                        status_code=response.status_code,
                        documents_captured=0,
                        error_message="Non-200 status code and browser fallback captured 0 documents"
                    )

            return docs_captured_for_url

        soup = BeautifulSoup(response.text, "html.parser")

        seen = set()

        extract_links_from_soup(
            soup=soup,
            base_url=source_url,
            source_url=source_url,
            seen=seen,
            label="RETRY KEPT" if retry_attempt else "KEPT"
        )

        iframe_soups = get_iframe_soups(source_url, soup)

        for iframe_item in iframe_soups:
            iframe_url = iframe_item["iframe_url"]
            iframe_soup = iframe_item["soup"]

            extract_links_from_soup(
                soup=iframe_soup,
                base_url=iframe_url,
                source_url=source_url,
                seen=seen,
                label="RETRY IFRAME KEPT" if retry_attempt else "IFRAME KEPT"
            )

        docs_captured_for_url = len(output_data) - start_doc_count

        needs_hash_fallback = source_url_has_hash(source_url)
        needs_report_card_fallback = should_trigger_report_card_fallback(soup, docs_captured_for_url)

        if needs_hash_fallback:
            print("Hash URL detected. Browser fallback may be needed for section-specific documents.")

        if needs_report_card_fallback:
            print("Report-card page detected. Browser fallback may be needed for card-based documents.")

        if (docs_captured_for_url == 0 or needs_hash_fallback or needs_report_card_fallback or force_browser_fallback) and should_use_browser_fallback(source_url, force_browser_fallback):
            fallback_docs = browser_click_fallback(source_url, seen)

            for doc in fallback_docs:
                output_data.append(doc)

                raw_links.append({
                    "company": doc["company"],
                    "text": doc["document_title"],
                    "title_source": doc.get("document_title_source", "browser_fallback"),
                    "url": doc["document_url"]
                })

            docs_captured_for_url = len(output_data) - start_doc_count

        if docs_captured_for_url == 0:
            if not retry_attempt and RETRY_FAILED_URLS:
                print("Queued for retry: zero documents captured")
                retry_queue.append({
                "source_url": source_url,
                "force_browser_fallback": force_browser_fallback
                 })
            else:
                add_issue(
                    source_url=source_url,
                    issue_type="SUCCESS_ZERO_DOCS_AFTER_RETRY" if retry_attempt else "SUCCESS_ZERO_DOCS",
                    status_code=response.status_code,
                    documents_captured=0,
                    error_message="Page opened successfully but no document links captured"
                )

        return docs_captured_for_url

    except Exception as e:
        print("Error:", e)

        docs_captured_for_url = 0

        if should_use_browser_fallback(source_url, force_browser_fallback):
            print("Request error detected. Trying browser fallback...")

            seen = set()
            fallback_docs = browser_click_fallback(source_url, seen)

            for doc in fallback_docs:
                output_data.append(doc)

                raw_links.append({
                    "company": doc["company"],
                    "text": doc["document_title"],
                    "title_source": doc.get("document_title_source", "browser_fallback"),
                    "url": doc["document_url"]
                })

            docs_captured_for_url = len(fallback_docs)

        if docs_captured_for_url == 0:
            if not retry_attempt and RETRY_FAILED_URLS:
                print("Queued for retry: exception")
                retry_queue.append({
                "source_url": source_url,
                "force_browser_fallback": force_browser_fallback
                 })
            else:
                add_issue(
                    source_url=source_url,
                    issue_type="FETCH_ERROR_AFTER_RETRY" if retry_attempt else "FETCH_ERROR",
                    status_code="",
                    documents_captured=0,
                    error_message=str(e)
                )

        return docs_captured_for_url


# MAIN SCRAPER

known_document_urls, known_source_urls = load_known_documents()
known_document_urls_before_run = set(known_document_urls)
known_source_urls_before_run = set(known_source_urls)

previous_output_by_company = load_previous_output_by_company()
target_source_urls = []

total_urls_processed = 0

if not os.path.exists(TARGET_URL_FILE):
    raise FileNotFoundError(f"URL file not found: {TARGET_URL_FILE}")

with open(TARGET_URL_FILE, newline="", encoding="utf-8") as file:
    reader = csv.DictReader(file)

    for row in reader:
        raw_source_url = row["source_url"]

        if not raw_source_url:
            continue

        parsed_source = parse_source_url(raw_source_url)

        source_url = parsed_source["source_url"]
        force_browser_fallback = parsed_source["force_browser_fallback"]

        if not source_url:
            continue

        target_source_urls.append(source_url)

        if force_browser_fallback:
            print(f"Force browser fallback enabled for URL: {source_url}")

        if total_urls_processed > 0 and SLEEP_SECONDS > 0:
            print(f"Sleeping {SLEEP_SECONDS} seconds before next URL...")
            time.sleep(SLEEP_SECONDS)

        total_urls_processed += 1

        process_source_url(
            source_url,
            retry_attempt=False,
            force_browser_fallback=force_browser_fallback
        )


# RETRY FAILED URLS ONCE AFTER FIRST PASS

if RETRY_FAILED_URLS and retry_queue:
    unique_retry_items = []
    seen_retry_urls = set()

    for retry_item in retry_queue:
        retry_url = retry_item["source_url"]

        if retry_url not in seen_retry_urls:
            seen_retry_urls.add(retry_url)
            unique_retry_items.append(retry_item)

    print(f"\nRetry queue found: {len(unique_retry_items)} URLs")
    print(f"Waiting {RETRY_SLEEP_SECONDS} seconds before retry pass...")

    if RETRY_SLEEP_SECONDS > 0:
        time.sleep(RETRY_SLEEP_SECONDS)

    for retry_item in unique_retry_items:
        retry_url = retry_item["source_url"]
        retry_force_browser_fallback = retry_item.get("force_browser_fallback", False)

        print(f"\nRETRYING: {retry_url}")

        process_source_url(
            retry_url,
            retry_attempt=True,
            force_browser_fallback=retry_force_browser_fallback
        )


# Previous output preservation disabled intentionally.
# output.csv now represents only documents captured in the current run.
# known_documents.csv remains the permanent history used for diff protection.
# preserve_previous_output_documents(target_source_urls, previous_output_by_company)


# DIFF SYSTEM

existing_diff_urls = set()

if RUN_MODE == "full" and os.path.exists(DIFF_FILE):
    validate_csv_header(DIFF_FILE, DIFF_FIELDNAMES)

    with open(DIFF_FILE, newline="", encoding="utf-8") as diff_read_file:
        reader = csv.DictReader(diff_read_file)

        for r in reader:
            if "document_url" in r and r["document_url"]:
                existing_diff_urls.add(normalize_url_key(r["document_url"]))


new_records = []

if RUN_MODE == "full":
    for r in output_data:
        document_url = r.get("document_url", "")
        source_url = r.get("company", "")

        if not document_url or not source_url:
            continue

        current_url_key = normalize_url_key(document_url)

        source_was_known_before_run = source_url in known_source_urls_before_run
        document_was_known_before_run = current_url_key in known_document_urls_before_run
        already_in_diff = current_url_key in existing_diff_urls

        # Add to diff only when:
        # 1. source URL was already known before this run
        # 2. document URL was not known before this run
        # 3. document URL is not already in diff.csv
        if source_was_known_before_run and not document_was_known_before_run and not already_in_diff:
            new_records.append({
                "date": current_date,
                "company": source_url,
                "document_title": r.get("document_title", "Unknown Title"),
                "document_url": document_url
            })

# Update known document queue for seed/full.
# New source URLs are baselined into known_documents.csv but not added to diff.csv.
if RUN_MODE in ["full", "seed"]:
    for r in output_data:
        queue_known_document_if_new(r)


# SAVE output file
with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as out_file:
    writer = csv.DictWriter(
        out_file,
        fieldnames=[
            "company",
            "document_title",
            "document_title_source",
            "document_url"
        ]
    )
    writer.writeheader()
    writer.writerows(output_data)


# SAVE raw links file
with open(RAW_FILE, "w", newline="", encoding="utf-8") as raw_file:
    writer = csv.DictWriter(
        raw_file,
        fieldnames=[
            "company",
            "text",
            "title_source",
            "url"
        ]
    )
    writer.writeheader()
    writer.writerows(raw_links)


# APPEND diff.csv only for full production run
if RUN_MODE == "full":
    validate_csv_header(DIFF_FILE, DIFF_FIELDNAMES)

    file_exists = os.path.exists(DIFF_FILE)

    for record in new_records:
        extra_keys = set(record.keys()) - set(DIFF_FIELDNAMES)
        missing_keys = set(DIFF_FIELDNAMES) - set(record.keys())

        if extra_keys or missing_keys:
            raise ValueError(
                f"Diff row format mismatch. "
                f"Extra keys: {extra_keys}. "
                f"Missing keys: {missing_keys}. "
                f"Record: {record}"
            )

    with open(DIFF_FILE, "a", newline="", encoding="utf-8") as diff_file:
        writer = csv.DictWriter(diff_file, fieldnames=DIFF_FIELDNAMES)

        if not file_exists:
            writer.writeheader()

        writer.writerows(new_records)


# APPEND known_documents.csv for seed/full runs
append_known_documents()


# SAVE capture issues file
with open(ISSUES_FILE, "w", newline="", encoding="utf-8") as issue_file:
    writer = csv.DictWriter(
        issue_file,
        fieldnames=[
            "date",
            "run_mode",
            "url_file",
            "source_url",
            "issue_type",
            "status_code",
            "documents_captured",
            "error_message"
        ]
    )
    writer.writeheader()
    writer.writerows(issue_rows)


# APPEND clean run_summary_master.csv
previous_run_number = 0

if os.path.exists(RUN_SUMMARY_FILE):
    validate_csv_header(RUN_SUMMARY_FILE, RUN_SUMMARY_FIELDNAMES)

    try:
        with open(RUN_SUMMARY_FILE, newline="", encoding="utf-8") as summary_read:
            reader = csv.DictReader(summary_read)

            for r in reader:
                try:
                    previous_run_number = max(previous_run_number, int(r.get("run_number", 0)))
                except Exception:
                    pass
    except Exception:
        previous_run_number = 0

current_run_number = previous_run_number + 1

summary_file_exists = os.path.exists(RUN_SUMMARY_FILE)

summary_row = {
    "run_number": current_run_number,
    "date": current_date,
    "run_mode": RUN_MODE,
    "url_file": TARGET_URL_FILE,
    "total_urls_processed": total_urls_processed,
    "total_documents_captured": len(output_data),
    "new_diff_records": len(new_records),
    "issue_count": len(issue_rows),
    "success_zero_docs_count": sum(1 for x in issue_rows if x["issue_type"] == "SUCCESS_ZERO_DOCS"),
    "fetch_failed_status_count": sum(1 for x in issue_rows if x["issue_type"] == "FETCH_FAILED_STATUS"),
    "fetch_error_count": sum(1 for x in issue_rows if x["issue_type"] == "FETCH_ERROR"),
    "browser_fallback_enabled": ENABLE_BROWSER_FALLBACK,
    "output_file": OUTPUT_FILE,
    "raw_file": RAW_FILE,
    "issues_file": ISSUES_FILE
}

extra_keys = set(summary_row.keys()) - set(RUN_SUMMARY_FIELDNAMES)
missing_keys = set(RUN_SUMMARY_FIELDNAMES) - set(summary_row.keys())

if extra_keys or missing_keys:
    raise ValueError(
        f"Run summary row format mismatch. "
        f"Extra keys: {extra_keys}. "
        f"Missing keys: {missing_keys}. "
        f"Row: {summary_row}"
    )

with open(RUN_SUMMARY_FILE, "a", newline="", encoding="utf-8") as summary_file:
    writer = csv.DictWriter(summary_file, fieldnames=RUN_SUMMARY_FIELDNAMES)

    if not summary_file_exists:
        writer.writeheader()

    writer.writerow(summary_row)


print("\n✅ SCRAPER COMPLETE")
print("✅ Existing logic preserved")
print("✅ Improved document title selection")
print("✅ Generic title/action text rejected")
print("✅ document_title_source added")
print("✅ PDF links are checked before navigation filters")
print("✅ Global duplicate document URL prevention enabled")
print("✅ Retry queue enabled")
print("✅ Fallback/ URL prefix support enabled")
print("✅ Previous output preservation disabled")
print("✅ output.csv represents current run capture only")
print("✅ known_documents.csv history enabled")
print("✅ New source URL onboarding does not pollute diff.csv")
print("✅ Image/icon files excluded from document capture")
print("✅ Iframe scraping enabled")
print("✅ Hash-aware fallback enabled")
print("✅ Report-card fallback enabled")
print(f"✅ Report keywords file: {REPORT_KEYWORDS_FILE}")
print(f"✅ Report-card keywords loaded: {len(REPORT_CARD_KEYWORDS)}")
print("✅ Browser fallback scans frames/iframes")
print("✅ Browser fallback clicks generic expandable UI")
print("✅ Browser fallback captures document network responses")
print("✅ Browser-like headers and retry fetch enabled for EQT only")
print("✅ diff.csv format locked")
print("✅ run_summary_master.csv format locked")
print("✅ known_documents.csv format locked")
print(f"✅ Sleep seconds between URLs: {SLEEP_SECONDS}")
print(f"✅ Retry failed URLs: {RETRY_FAILED_URLS}")
print(f"✅ Retry sleep seconds: {RETRY_SLEEP_SECONDS}")
print(f"✅ Run mode: {RUN_MODE}")
print(f"✅ URL file: {TARGET_URL_FILE}")
print(f"✅ Output file: {OUTPUT_FILE}")
print(f"✅ Raw file: {RAW_FILE}")
print(f"✅ Issues file: {ISSUES_FILE}")
print(f"✅ Summary file: {RUN_SUMMARY_FILE}")
print(f"✅ Known documents file: {KNOWN_DOCUMENTS_FILE}")
print(f"✅ URLs processed: {total_urls_processed}")
print(f"✅ Documents captured: {len(output_data)}")
print(f"✅ New diff records: {len(new_records)}")
print(f"✅ Known documents appended: {len(known_documents_to_append)}")
print(f"✅ Issues: {len(issue_rows)}")
print(f"✅ Browser fallback enabled: {ENABLE_BROWSER_FALLBACK}")
print(f"✅ Run {current_run_number}: {len(output_data)} documents captured")
