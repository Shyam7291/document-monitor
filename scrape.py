import csv
import requests
import os
import re
import html
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote

# =========================
# RUN MODE CONFIGURATION
# =========================

RUN_MODE = os.environ.get("RUN_MODE", "full").strip().lower()
TARGET_URL_FILE = os.environ.get("TARGET_URL_FILE", "documents.csv").strip()

ENABLE_BROWSER_FALLBACK = os.environ.get("ENABLE_BROWSER_FALLBACK", "false").strip().lower() == "true"

if RUN_MODE == "full":
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

# New clean summary file because old run_summary.csv has mixed columns
RUN_SUMMARY_FILE = "run_summary_v2.csv"

output_data = []
raw_links = []
issue_rows = []

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

    # Default old behavior for all other websites
    try:
        response = requests.get(source_url, timeout=15)
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


def normalize_text(text):
    if not text:
        return ""

    text = html.unescape(text)
    text = unquote(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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


def get_best_text(link, full_url, source_url):
    if is_static_file_link(full_url):
        html_title = get_title_from_html_context(link)

        if html_title:
            return html_title

        link_text = get_link_text_title(link)

        if link_text:
            return link_text

        url_title = clean_title_from_url(full_url)

        if url_title:
            return url_title

    url_title = clean_title_from_url(full_url)

    if "space42.ai" in source_url.lower() and url_title:
        return url_title

    if url_title:
        return url_title

    html_title = get_title_from_html_context(link)

    if html_title:
        return html_title

    link_text = get_link_text_title(link)

    if link_text:
        return link_text

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

    return filename if filename else "Unknown Title"


def is_document_link(url):
    """
    Keep actual document-like URLs.
    IMPORTANT: This function must be checked before navigation filtering.
    """

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


def should_use_browser_fallback(source_url):
    return ENABLE_BROWSER_FALLBACK


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
    """
    Extract document links from a BeautifulSoup object.

    Important fix:
    Document links are checked BEFORE navigation links.
    This ensures valid .pdf/.ashx/static-file links never get skipped by navigation filters.
    """

    docs_found = []

    links = soup.find_all("a")

    for link in links:
        href = link.get("href")

        if not href:
            continue

        full_url = urljoin(base_url, href)
        href_lower = full_url.lower()

        title = get_best_text(link, full_url, source_url)

        raw_links.append({
            "company": source_url,
            "text": title,
            "url": full_url
        })

        # FIRST: keep document links
        if is_document_link(href_lower):
            duplicate_key = normalize_url_key(full_url)

            if duplicate_key in seen:
                continue

            seen.add(duplicate_key)

            print(f"{label} → {title}")

            doc = {
                "company": source_url,
                "document_title": title,
                "document_url": full_url
            }

            output_data.append(doc)
            docs_found.append(doc)

            continue

        # SECOND: skip navigation only after document check
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


def browser_click_fallback(source_url, existing_keys):
    fallback_docs = []

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"Playwright not available: {e}")
        return fallback_docs

    print("Running enhanced browser fallback for failed/zero-doc page...")

    def add_doc_from_url(found_url, title_hint=""):
        full_url = urljoin(source_url, found_url)
        key = normalize_url_key(full_url)

        if key in existing_keys:
            return

        if not is_click_document_candidate(full_url):
            return

        title = title_hint

        if not title or is_bad_title(title):
            title = clean_title_from_url(full_url)

        if not title or is_bad_title(title):
            title = "Unknown Title"

        existing_keys.add(key)

        print(f"FALLBACK KEPT → {title}")

        fallback_docs.append({
            "company": source_url,
            "document_title": title,
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

                    title = normalize_text(tag.get_text(" ", strip=True))

                    if is_bad_title(title):
                        title = clean_title_from_url(full_url)

                    add_doc_from_url(full_url, title)

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

            # Capture document-like network responses too
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

            # Initial scan
            scan_all_rendered_content(page)

            # Generic expandable UI click phase
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

                        # After every expand/click, scan again
                        scan_all_rendered_content(page)

                    except Exception:
                        continue

                print(f"Expandable controls clicked: {clicked_expanders}")

            except Exception as e:
                print(f"Expandable click phase error: {e}")

            # Obvious document action controls
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


# MAIN SCRAPER

total_urls_processed = 0

if not os.path.exists(TARGET_URL_FILE):
    raise FileNotFoundError(f"URL file not found: {TARGET_URL_FILE}")

with open(TARGET_URL_FILE, newline="", encoding="utf-8") as file:
    reader = csv.DictReader(file)

    for row in reader:
        source_url = row["source_url"]

        if not source_url:
            continue

        total_urls_processed += 1

        print(f"\nChecking: {source_url}")

        start_doc_count = len(output_data)

        try:
            response = fetch_url(source_url)

            if response is None:
                print("Failed to fetch page")

                docs_captured_for_url = 0

                if should_use_browser_fallback(source_url):
                    print("Request failed. Trying browser fallback...")

                    seen = set()
                    fallback_docs = browser_click_fallback(source_url, seen)

                    for doc in fallback_docs:
                        output_data.append(doc)

                        raw_links.append({
                            "company": doc["company"],
                            "text": doc["document_title"],
                            "url": doc["document_url"]
                        })

                    docs_captured_for_url = len(fallback_docs)

                if docs_captured_for_url == 0:
                    add_issue(
                        source_url=source_url,
                        issue_type="FETCH_ERROR",
                        status_code="",
                        documents_captured=0,
                        error_message="Request failed / timeout and browser fallback captured 0 documents"
                    )

                continue

            print("Status:", response.status_code)

            if response.status_code != 200:
                print("Failed to fetch page")

                docs_captured_for_url = 0

                if should_use_browser_fallback(source_url):
                    print("Non-200 status detected. Trying browser fallback...")

                    seen = set()
                    fallback_docs = browser_click_fallback(source_url, seen)

                    for doc in fallback_docs:
                        output_data.append(doc)

                        raw_links.append({
                            "company": doc["company"],
                            "text": doc["document_title"],
                            "url": doc["document_url"]
                        })

                    docs_captured_for_url = len(fallback_docs)

                if docs_captured_for_url == 0:
                    add_issue(
                        source_url=source_url,
                        issue_type="FETCH_FAILED_STATUS",
                        status_code=response.status_code,
                        documents_captured=0,
                        error_message="Non-200 status code and browser fallback captured 0 documents"
                    )

                continue

            soup = BeautifulSoup(response.text, "html.parser")

            seen = set()

            extract_links_from_soup(
                soup=soup,
                base_url=source_url,
                source_url=source_url,
                seen=seen,
                label="KEPT"
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
                    label="IFRAME KEPT"
                )

            docs_captured_for_url = len(output_data) - start_doc_count

            if docs_captured_for_url == 0 and should_use_browser_fallback(source_url):
                fallback_docs = browser_click_fallback(source_url, seen)

                for doc in fallback_docs:
                    output_data.append(doc)

                    raw_links.append({
                        "company": doc["company"],
                        "text": doc["document_title"],
                        "url": doc["document_url"]
                    })

                docs_captured_for_url = len(output_data) - start_doc_count

            if docs_captured_for_url == 0:
                add_issue(
                    source_url=source_url,
                    issue_type="SUCCESS_ZERO_DOCS",
                    status_code=response.status_code,
                    documents_captured=0,
                    error_message="Page opened successfully but no document links captured"
                )

        except Exception as e:
            print("Error:", e)

            docs_captured_for_url = 0

            if should_use_browser_fallback(source_url):
                print("Request error detected. Trying browser fallback...")

                seen = set()
                fallback_docs = browser_click_fallback(source_url, seen)

                for doc in fallback_docs:
                    output_data.append(doc)

                    raw_links.append({
                        "company": doc["company"],
                        "text": doc["document_title"],
                        "url": doc["document_url"]
                    })

                docs_captured_for_url = len(fallback_docs)

            if docs_captured_for_url == 0:
                add_issue(
                    source_url=source_url,
                    issue_type="FETCH_ERROR",
                    status_code="",
                    documents_captured=0,
                    error_message=str(e)
                )


# DIFF SYSTEM

old_output_urls = set()
existing_diff_urls = set()

if RUN_MODE == "full" and os.path.exists(OUTPUT_FILE):
    with open(OUTPUT_FILE, newline="", encoding="utf-8") as old_file:
        reader = csv.DictReader(old_file)

        for r in reader:
            if "document_url" in r and r["document_url"]:
                old_output_urls.add(normalize_url_key(r["document_url"]))


if RUN_MODE == "full" and os.path.exists(DIFF_FILE):
    with open(DIFF_FILE, newline="", encoding="utf-8") as diff_read_file:
        reader = csv.DictReader(diff_read_file)

        for r in reader:
            if "document_url" in r and r["document_url"]:
                existing_diff_urls.add(normalize_url_key(r["document_url"]))


new_records = []

if RUN_MODE == "full":
    for r in output_data:
        current_url_key = normalize_url_key(r["document_url"])

        if current_url_key not in old_output_urls and current_url_key not in existing_diff_urls:
            new_records.append({
                "date": current_date,
                "company": r["company"],
                "document_title": r["document_title"],
                "document_url": r["document_url"]
            })


# SAVE output file
with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as out_file:
    writer = csv.DictWriter(
        out_file,
        fieldnames=["company", "document_title", "document_url"]
    )
    writer.writeheader()
    writer.writerows(output_data)


# SAVE raw links file
with open(RAW_FILE, "w", newline="", encoding="utf-8") as raw_file:
    writer = csv.DictWriter(
        raw_file,
        fieldnames=["company", "text", "url"]
    )
    writer.writeheader()
    writer.writerows(raw_links)


# APPEND diff.csv only for full production run
if RUN_MODE == "full":
    file_exists = os.path.exists(DIFF_FILE)

    with open(DIFF_FILE, "a", newline="", encoding="utf-8") as diff_file:
        fieldnames = ["date", "company", "document_title", "document_url"]
        writer = csv.DictWriter(diff_file, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerows(new_records)


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


# APPEND clean run_summary_v2.csv
previous_run_number = 0

if os.path.exists(RUN_SUMMARY_FILE):
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

with open(RUN_SUMMARY_FILE, "a", newline="", encoding="utf-8") as summary_file:
    fieldnames = [
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

    writer = csv.DictWriter(summary_file, fieldnames=fieldnames)

    if not summary_file_exists:
        writer.writeheader()

    writer.writerow({
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
    })


print("\n✅ SCRAPER COMPLETE")
print("✅ Existing logic preserved")
print("✅ PDF links are checked before navigation filters")
print("✅ Image/icon files excluded from document capture")
print("✅ Iframe scraping enabled")
print("✅ Browser fallback scans frames/iframes")
print("✅ Browser fallback clicks generic expandable UI")
print("✅ Browser fallback captures document network responses")
print("✅ Browser-like headers and retry fetch enabled for EQT only")
print(f"✅ Run mode: {RUN_MODE}")
print(f"✅ URL file: {TARGET_URL_FILE}")
print(f"✅ Output file: {OUTPUT_FILE}")
print(f"✅ Raw file: {RAW_FILE}")
print(f"✅ Issues file: {ISSUES_FILE}")
print(f"✅ Summary file: {RUN_SUMMARY_FILE}")
print(f"✅ URLs processed: {total_urls_processed}")
print(f"✅ Documents captured: {len(output_data)}")
print(f"✅ New diff records: {len(new_records)}")
print(f"✅ Issues: {len(issue_rows)}")
print(f"✅ Browser fallback enabled: {ENABLE_BROWSER_FALLBACK}")
print(f"✅ Run {current_run_number}: {len(output_data)} documents captured")
