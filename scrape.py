import csv
import requests
import os
import re
import html
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote

output_data = []
raw_links = []

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
    "company announcements"
}


def normalize_text(text):
    """Clean spacing and HTML entities."""
    if not text:
        return ""

    text = html.unescape(text)
    text = unquote(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_date_only(text):
    """Detect date-only strings like 31 December, 2025 or 04 Apr 2026."""
    text = normalize_text(text)

    date_patterns = [
        r"^\d{1,2}\s+\w+,\s+\d{4}$",
        r"^\d{1,2}\s+\w+\s+\d{4}$",
        r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$",
        r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$"
    ]

    return any(re.match(pattern, text, re.IGNORECASE) for pattern in date_patterns)


def is_number_file(text):
    """Detect weak file names like 1.pdf, 4-3.pdf, 123.pdf."""
    text = normalize_text(text).lower()

    text = text.replace(".pdf", "").replace(".ashx", "")
    text = text.replace("_", "-")

    return bool(re.match(r"^[0-9\-]+$", text))


def is_bad_title(text):
    """Reject weak or useless titles."""
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

    if lower.endswith(".pdf") and is_number_file(lower):
        return True

    if lower in ["download pdf", "view pdf", "open pdf"]:
        return True

    # Must contain at least some letters
    if not re.search(r"[a-zA-Z]", text):
        return True

    return False


def clean_title_from_url(href):
    """
    Priority 1:
    Extract meaningful title from document URL.
    Example:
    annual-general-meeting-on-8-april-2026.ashx
    -> annual general meeting on 8 april 2026
    """
    parsed = urlparse(href)
    path = parsed.path

    parts = [p for p in path.split("/") if p]

    if not parts:
        return ""

    filename = parts[-1]

    filename = unquote(filename)
    filename = html.unescape(filename)

    filename = filename.split("?")[0]
    filename = filename.replace(".pdf", "")
    filename = filename.replace(".ashx", "")
    filename = filename.replace(".aspx", "")
    filename = filename.replace(".html", "")

    filename = filename.replace("-", " ")
    filename = filename.replace("_", " ")

    filename = normalize_text(filename)

    if is_bad_title(filename):
        return ""

    return filename


def collect_text_candidates_from_container(container):
    """
    Priority 2:
    Collect meaningful text from same row/block and select longest.
    """
    candidates = []

    if not container:
        return candidates

    # Get text from common content tags
    for element in container.find_all(["td", "th", "p", "span", "div", "h1", "h2", "h3", "h4", "strong"], recursive=True):
        text = normalize_text(element.get_text(" ", strip=True))

        if not is_bad_title(text):
            candidates.append(text)

    # Also check stripped strings directly
    for text in container.stripped_strings:
        text = normalize_text(text)

        if not is_bad_title(text):
            candidates.append(text)

    # Remove duplicates while keeping order
    unique_candidates = []
    seen_texts = set()

    for c in candidates:
        c_key = c.lower()

        if c_key not in seen_texts:
            seen_texts.add(c_key)
            unique_candidates.append(c)

    return unique_candidates


def get_title_from_html_context(link):
    """
    Priority 2:
    If URL title is weak, scan same row/block and choose longest meaningful text.
    """

    # 1. Table row first — works for Albuhaira style
    row = link.find_parent("tr")
    if row:
        row_candidates = collect_text_candidates_from_container(row)

        if row_candidates:
            return max(row_candidates, key=len)

    # 2. List item — common for document lists
    li = link.find_parent("li")
    if li:
        li_candidates = collect_text_candidates_from_container(li)

        if li_candidates:
            return max(li_candidates, key=len)

    # 3. Closest div blocks — but only a few levels to avoid section headings like DISCLOSURES
    current = link.parent
    levels_checked = 0

    while current and levels_checked < 4:
        if current.name in ["div", "section", "article"]:
            block_candidates = collect_text_candidates_from_container(current)

            if block_candidates:
                return max(block_candidates, key=len)

        current = current.parent
        levels_checked += 1

    return ""


def get_link_text_title(link):
    """
    Priority 3:
    Use visible link text if meaningful.
    """
    text = normalize_text(link.get_text(" ", strip=True))

    if is_bad_title(text):
        return ""

    return text


def get_best_text(link, href):
    """
    Final title priority:
    1. URL title
    2. Same HTML row/block longest meaningful text
    3. Link text
    4. Final URL fallback even if weak
    """

    # ✅ Priority 1: URL title
    url_title = clean_title_from_url(href)
    if url_title:
        return url_title

    # ✅ Priority 2: HTML same row/block title
    html_title = get_title_from_html_context(link)
    if html_title:
        return html_title

    # ✅ Priority 3: direct link text
    link_text = get_link_text_title(link)
    if link_text:
        return link_text

    # ✅ Final fallback: cleaned filename, even if weak
    parsed = urlparse(href)
    filename = parsed.path.split("/")[-1]
    filename = unquote(filename)
    filename = html.unescape(filename)
    filename = filename.split("?")[0]
    filename = filename.replace(".pdf", "").replace(".ashx", "")
    filename = filename.replace("-", " ").replace("_", " ")
    filename = normalize_text(filename)

    return filename if filename else "Unknown Title"


def is_document_link(url):
    """Keep actual document-like URLs."""
    lower = url.lower()

    if ".pdf" in lower:
        return True

    if ".ashx" in lower:
        return True

    return False


def is_navigation_link(url):
    """Remove page/navigation links."""
    lower = url.lower()

    if lower.endswith("/"):
        return True

    if lower.endswith(".html"):
        return True

    if lower.endswith(".aspx"):
        return True

    return False


# ✅ MAIN SCRAPER
with open("documents.csv", newline="", encoding="utf-8") as file:
    reader = csv.DictReader(file)

    for row in reader:
        url = row["source_url"]
        print(f"\nChecking: {url}")

        try:
            response = requests.get(url, timeout=15)
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

                full_url = urljoin(url, href)
                href_lower = full_url.lower()

                title = get_best_text(link, href)

                # Save all links for debugging
                raw_links.append({
                    "company": url,
                    "text": title,
                    "url": full_url
                })

                # Skip navigation pages
                if is_navigation_link(href_lower):
                    continue

                # Keep documents only
                if is_document_link(href_lower):

                    if full_url in seen:
                        continue

                    seen.add(full_url)

                    print(f"KEPT → {title}")

                    output_data.append({
                        "company": url,
                        "document_title": title,
                        "document_url": full_url
                    })

        except Exception as e:
            print("Error:", e)


# ✅ ===== DIFF SYSTEM =====

current_date = datetime.now().strftime("%Y-%m-%d")

old_data = set()

if os.path.exists("output.csv"):
    with open("output.csv", newline="", encoding="utf-8") as old_file:
        reader = csv.DictReader(old_file)

        for r in reader:
            old_data.add((
                r["company"],
                r["document_title"],
                r["document_url"]
            ))

new_records = []

for r in output_data:
    rec = (
        r["company"],
        r["document_title"],
        r["document_url"]
    )

    # First run: if diff.csv does not exist or is empty, add all records
    if not os.path.exists("diff.csv") or os.stat("diff.csv").st_size == 0:
        new_records.append({
            "date": current_date,
            "company": r["company"],
            "document_title": r["document_title"],
            "document_url": r["document_url"]
        })

    # Normal runs: add only new records
    elif rec not in old_data:
        new_records.append({
            "date": current_date,
            "company": r["company"],
            "document_title": r["document_title"],
            "document_url": r["document_url"]
        })


# ✅ SAVE output.csv
with open("output.csv", "w", newline="", encoding="utf-8") as out_file:
    writer = csv.DictWriter(
        out_file,
        fieldnames=["company", "document_title", "document_url"]
    )
    writer.writeheader()
    writer.writerows(output_data)


# ✅ SAVE raw_links.csv
with open("raw_links.csv", "w", newline="", encoding="utf-8") as raw_file:
    writer = csv.DictWriter(
        raw_file,
        fieldnames=["company", "text", "url"]
    )
    writer.writeheader()
    writer.writerows(raw_links)


# ✅ APPEND diff.csv
file_exists = os.path.exists("diff.csv")

with open("diff.csv", "a", newline="", encoding="utf-8") as diff_file:
    fieldnames = ["date", "company", "document_title", "document_url"]
    writer = csv.DictWriter(diff_file, fieldnames=fieldnames)

    if not file_exists:
        writer.writeheader()

    writer.writerows(new_records)


print("\n✅ SCRAPER COMPLETE")
print("✅ Priority 1: URL title")
print("✅ Priority 2: HTML row/block longest meaningful title")
print("✅ output.csv updated")
print("✅ raw_links.csv updated")
print("✅ diff.csv updated")
