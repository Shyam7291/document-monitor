import csv
import requests
import os
import re
import html
import subprocess
from types import SimpleNamespace
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote

output_data = []
raw_links = []

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
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


def fetch_page(source_url):
    """
    Normal fetch for all sites.
    If ACWA has SSL certificate issue, use curl fallback only for ACWA.
    """

    try:
        response = requests.get(
            source_url,
            timeout=20,
            headers=HEADERS
        )
        return response

    except requests.exceptions.SSLError:
        print(f"SSL issue detected for: {source_url}")

        if "acwapower.com" in source_url.lower():
            print("Using ACWA fallback with curl...")

            try:
                result = subprocess.run(
                    [
                        "curl",
                        "-L",
                        "-k",
                        "-A",
                        HEADERS["User-Agent"],
                        source_url
                    ],
                    capture_output=True,
                    text=True,
                    timeout=40
                )

                if result.returncode == 0 and result.stdout:
                    return SimpleNamespace(
                        status_code=200,
                        text=result.stdout
                    )

                print("ACWA curl fallback failed")
                return None

            except Exception as curl_error:
                print(f"ACWA curl fallback error: {curl_error}")
                return None

        return None

    except Exception as e:
        print(f"Request error for {source_url}: {e}")
        return None


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
    """
    Detect UUID-like static file names.

    Example:
    b24f08e2-755f-495f-a972-3ed11903e135
    b24f08e2 755f 495f a972 3ed11903e135
    """

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


def clean_title_from_url(url):
    """
    Extract meaningful title from URL only if URL contains real words.
    UUID/static-file IDs are rejected.
    """

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
    """
    Collect meaningful text from same HTML row/block.
    """

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
    """
    If URL title is weak, scan same row/block and choose longest meaningful text.
    """

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
    """
    Use visible link text if meaningful.
    """

    text = normalize_text(link.get_text(" ", strip=True))

    if is_bad_title(text):
        return ""

    return text


def normalize_url_key(url):
    """
    Used for duplicate detection and diff comparison.
    Removes query string.
    """

    parsed = urlparse(url)

    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".lower()


def is_static_file_link(url):
    return "/static-files/" in url.lower()


def get_best_text(link, full_url, source_url):
    """
    Title priority:
    1. For /static-files/ links, use HTML title first because URL is usually UUID.
    2. For other links, use URL title first.
    3. Then HTML context.
    4. Then link text.
    5. Then fallback.
    """

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
    """

    lower = url.lower()

    if ".pdf" in lower:
        return True

    if ".ashx" in lower:
        return True

    if "/static-files/" in lower:
        return True

    return False


def is_navigation_link(url):
    """
    Remove page/navigation links.
    """

    lower = url.lower()

    if "#" in lower and not is_document_link(lower):
        return True

    if lower.endswith("/"):
        return True

    if lower.endswith(".html"):
        return True

    if lower.endswith(".aspx"):
        return True

    return False


# MAIN SCRAPER
with open("documents.csv", newline="", encoding="utf-8") as file:
    reader = csv.DictReader(file)

    for row in reader:
        source_url = row["source_url"]

        print(f"\nChecking: {source_url}")

        try:
            response = fetch_page(source_url)

            if response is None:
                print("Failed to fetch page")
                continue

            print("Status:", response.status_code)

            if response.status_code != 200:
                print("Failed to fetch page")
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            links = soup.find_all("a")

            seen = set()

            for link in links:
                href = link.get("href")

                if not href:
                    continue

                full_url = urljoin(source_url, href)
                href_lower = full_url.lower()

                title = get_best_text(link, full_url, source_url)

                raw_links.append({
                    "company": source_url,
                    "text": title,
                    "url": full_url
                })

                if is_navigation_link(href_lower):
                    continue

                if is_document_link(href_lower):

                    duplicate_key = normalize_url_key(full_url)

                    if duplicate_key in seen:
                        continue

                    seen.add(duplicate_key)

                    print(f"KEPT → {title}")

                    output_data.append({
                        "company": source_url,
                        "document_title": title,
                        "document_url": full_url
                    })

        except Exception as e:
            print("Error:", e)


# DIFF SYSTEM
# diff.csv compares ONLY document_url, not title + URL.

current_date = datetime.now().strftime("%Y-%m-%d")

old_output_urls = set()
existing_diff_urls = set()

if os.path.exists("output.csv"):
    with open("output.csv", newline="", encoding="utf-8") as old_file:
        reader = csv.DictReader(old_file)

        for r in reader:
            if "document_url" in r and r["document_url"]:
                old_output_urls.add(normalize_url_key(r["document_url"]))


if os.path.exists("diff.csv"):
    with open("diff.csv", newline="", encoding="utf-8") as diff_read_file:
        reader = csv.DictReader(diff_read_file)

        for r in reader:
            if "document_url" in r and r["document_url"]:
                existing_diff_urls.add(normalize_url_key(r["document_url"]))


new_records = []

for r in output_data:
    current_url_key = normalize_url_key(r["document_url"])

    if current_url_key not in old_output_urls and current_url_key not in existing_diff_urls:
        new_records.append({
            "date": current_date,
            "company": r["company"],
            "document_title": r["document_title"],
            "document_url": r["document_url"]
        })


with open("output.csv", "w", newline="", encoding="utf-8") as out_file:
    writer = csv.DictWriter(
        out_file,
        fieldnames=["company", "document_title", "document_url"]
    )
    writer.writeheader()
    writer.writerows(output_data)


with open("raw_links.csv", "w", newline="", encoding="utf-8") as raw_file:
    writer = csv.DictWriter(
        raw_file,
        fieldnames=["company", "text", "url"]
    )
    writer.writeheader()
    writer.writerows(raw_links)


file_exists = os.path.exists("diff.csv")

with open("diff.csv", "a", newline="", encoding="utf-8") as diff_file:
    fieldnames = ["date", "company", "document_title", "document_url"]
    writer = csv.DictWriter(diff_file, fieldnames=fieldnames)

    if not file_exists:
        writer.writeheader()

    writer.writerows(new_records)


print("\n✅ SCRAPER COMPLETE")
print("✅ ACWA curl fallback added")
print("✅ Existing title logic kept")
print("✅ Diff compares only document_url")
print("✅ output.csv updated")
print("✅ raw_links.csv updated")
print("✅ diff.csv updated")
