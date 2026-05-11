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

# ✅ FINAL SMART TITLE FUNCTION (incremental logic)
def get_best_text(link, href):

    text_raw = link.get_text(strip=True)
    text = text_raw.lower()

    # ✅ STEP 1 — handle Download/View cases
    if text in IGNORE_WORDS or not text:

        # ✅ CASE 1 — table (Albuhaira ✅)
        row = link.find_parent("tr")
        if row:
            cols = row.find_all("td")

            if len(cols) >= 2:
                title = cols[1].get_text(strip=True)

                if title and len(title) > 5:
                    return title

        # ✅ CASE 2 — container extraction (Space42 fallback)
        container = link.find_parent(["div", "li", "section"])

        if container:
            candidates = []

            for el in container.find_all(["h1","h2","h3","h4","p","span","a"]):
                t = el.get_text(strip=True)

                if (
                    t
                    and t.lower() not in IGNORE_WORDS
                    and t.lower() not in ["disclosures"]
                    and len(t) > 15
                    # ✅ NEW RULE — remove dates like "15 May, 2025"
                    and not re.search(r"\b\d{1,2}\s+\w+,\s+\d{4}\b", t)
                ):
                    candidates.append(t)

            if candidates:
                return max(candidates, key=len)

    # ✅ STEP 2 — normal link text
    if (
        text_raw
        and text not in IGNORE_WORDS
        and len(text_raw) > 5
        and not re.search(r"\b\d{1,2}\s+\w+,\s+\d{4}\b", text_raw)  # avoid date-only text
    ):
        return text_raw

    # ✅ STEP 3 — fallback filename
    return href.split("/")[-1]


# ✅ SCRAPING MAIN
with open('documents.csv', newline='', encoding='utf-8') as file:
    reader = csv.DictReader(file)

    for row in reader:
        url = row['source_url']
        print(f"\nChecking: {url}")

        try:
            response = requests.get(url, timeout=10)
            print("Status:", response.status_code)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                links = soup.find_all('a')

                seen = set()

                for link in links:
                    href = link.get('href')
                    if not href:
                        continue

                    full_url = urljoin(url, href)
                    href_lower = full_url.lower()

                    text = get_best_text(link, href)

                    # ✅ save raw
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

                    # ✅ keep only documents
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
            print(f"Error: {e}")


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

    # ✅ first run condition
    if not os.path.exists("diff.csv") or os.stat("diff.csv").st_size == 0:
        new_records.append({
            "date": current_date,
            "company": r["company"],
            "document_title": r["document_title"],
            "document_url": r["document_url"]
        })

    # ✅ normal diff logic
    elif rec not in old_data:
        new_records.append({
            "date": current_date,
            "company": r["company"],
            "document_title": r["document_title"],
            "document_url": r["document_url"]
        })


# ✅ SAVE output.csv
with open("output.csv", "w", newline="", encoding="utf-8") as out_file:
    writer = csv.DictWriter(out_file, fieldnames=["company","document_title","document_url"])
    writer.writeheader()
    writer.writerows(output_data)

# ✅ SAVE raw_links.csv
with open("raw_links.csv", "w", newline="", encoding="utf-8") as raw_file:
    writer = csv.DictWriter(raw_file, fieldnames=["company","text","url"])
    writer.writeheader()
    writer.writerows(raw_links)

# ✅ APPEND diff.csv
file_exists = os.path.exists("diff.csv")

with open("diff.csv", "a", newline="", encoding="utf-8") as diff_file:
    fieldnames = ["date","company","document_title","document_url"]
    writer = csv.DictWriter(diff_file, fieldnames=fieldnames)

    if not file_exists:
        writer.writeheader()

    writer.writerows(new_records)

print("\n✅ SCRAPER COMPLETE")
print("✅ Titles fixed (incremental logic applied)")
print("✅ Diff tracking working")
