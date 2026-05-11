import csv
import requests
import os
import re
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin

output_data = []
raw_links = []

IGNORE_WORDS = ["download", "view", "open", "read", "click"]

def get_best_text(link, href):

    text_raw = link.get_text(strip=True)
    text = text_raw.lower()

    # ✅ STEP 0 — SPACE42 STRUCTURE (text left, icon right)
    container = link.find_parent(["div", "li"])
    if container:
        for child in container.find_all(["div", "span", "p"], recursive=False):
            t = child.get_text(strip=True)

            if (
                t
                and t.lower() not in IGNORE_WORDS
                and len(t) > 10
                and not t.endswith(".pdf")
                and not re.search(r"\d{1,2}\s+\w+,\s+\d{4}", t)
            ):
                return t

    # ✅ STEP 1 — TABLE (Albuhaira)
    row = link.find_parent("tr")
    if row:
        cols = row.find_all("td")
        if len(cols) >= 2:
            title = cols[1].get_text(strip=True)
            if title:
                return title

    # ✅ STEP 2 — fallback search
    if text in IGNORE_WORDS or not text:
        parent = link.find_parent(["div", "li", "section"])

        if parent:
            for t in parent.stripped_strings:
                if (
                    len(t) > 15
                    and not t.endswith(".pdf")
                    and not re.search(r"\d{1,2}\s+\w+,\s+\d{4}", t)
                ):
                    return t

    # ✅ STEP 3 — normal text
    if (
        text_raw
        and text not in IGNORE_WORDS
        and len(text_raw) > 5
        and not re.search(r"\d{1,2}\s+\w+,\s+\d{4}", text_raw)
    ):
        return text_raw

    # ✅ FINAL fallback
    return href.split("/")[-1]


# ✅ MAIN SCRAPER
with open("documents.csv", newline="", encoding="utf-8") as file:
    reader = csv.DictReader(file)

    for row in reader:
        url = row["source_url"]
        print(f"\nChecking: {url}")

        try:
            response = requests.get(url, timeout=10)
            print("Status:", response.status_code)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                links = soup.find_all("a")

                seen = set()

                for link in links:
                    href = link.get("href")

                    if not href:
                        continue

                    full_url = urljoin(url, href)
                    href_lower = full_url.lower()

                    text = get_best_text(link, href)

                    raw_links.append({
                        "company": url,
                        "text": text,
                        "url": full_url
                    })

                    # ❌ ignore navigation links
                    if (
                        href_lower.endswith("/")
                        or href_lower.endswith(".aspx")
                        or href_lower.endswith(".html")
                    ):
                        continue

                    # ✅ keep only docs
                    if ".pdf" in href_lower or ".ashx" in href_lower:
                        if full_url in seen:
                            continue
                        seen.add(full_url)

                        print(f"KEPT → {text}")

                        output_data.append({
                            "company": url,
                            "document_title": text,
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
            old_data.add((r["company"], r["document_title"], r["document_url"]))

new_records = []

for r in output_data:
    rec = (r["company"], r["document_title"], r["document_url"])

    if not os.path.exists("diff.csv") or os.stat("diff.csv").st_size == 0:
        new_records.append({
            "date": current_date,
            "company": r["company"],
            "document_title": r["document_title"],
            "document_url": r["document_url"]
        })

    elif rec not in old_data:
        new_records.append({
            "date": current_date,
            "company": r["company"],
            "document_title": r["document_title"],
            "document_url": r["document_url"]
        })


# ✅ SAVE OUTPUT
with open("output.csv", "w", newline="", encoding="utf-8") as out_file:
    writer = csv.DictWriter(out_file, fieldnames=["company", "document_title", "document_url"])
    writer.writeheader()
    writer.writerows(output_data)

# ✅ SAVE RAW
with open("raw_links.csv", "w", newline="", encoding="utf-8") as raw_file:
    writer = csv.DictWriter(raw_file, fieldnames=["company", "text", "url"])
    writer.writeheader()
    writer.writerows(raw_links)

# ✅ SAVE DIFF
file_exists = os.path.exists("diff.csv")

with open("diff.csv", "a", newline="", encoding="utf-8") as diff_file:
    fieldnames = ["date", "company", "document_title", "document_url"]
    writer = csv.DictWriter(diff_file, fieldnames=fieldnames)

    if not file_exists:
        writer.writeheader()

    writer.writerows(new_records)

print("\n✅ DONE — Scraper working")
