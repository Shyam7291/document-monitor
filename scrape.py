import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

output_data = []
raw_links = []

# Words that are NOT actual titles
IGNORE_WORDS = ["download", "view", "open", "read", "click"]

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

                    # ✅ STEP 1 — get link text
                    text = link.get_text(strip=True)
                    text_lower = text.lower()

                    # ✅ STEP 2 — handle generic words like "Download", "View"
                    if text_lower in IGNORE_WORDS or not text:
                        row_elem = link.find_parent("tr")

                        if row_elem:
                            # ✅ scan entire row (ALL columns)
                            candidates = []

                            for element in row_elem.find_all(["td", "th", "div", "span", "p"]):
                                col_text = element.get_text(strip=True)
                                col_text_lower = col_text.lower()

                                if (
                                    col_text
                                    and col_text_lower not in IGNORE_WORDS
                                    and len(col_text) > 10
                                ):
                                    candidates.append(col_text)

                            if candidates:
                                # ✅ choose best meaningful title
                                text = max(candidates, key=len)

                    # ✅ STEP 3 — fallback: parent container (for layouts like Space42)
                    if not text or text.lower() in IGNORE_WORDS:
                        parent = link.parent
                        if parent:
                            parent_text = parent.get_text(strip=True)
                            if len(parent_text) > 10:
                                text = parent_text

                    # ✅ STEP 4 — final fallback (file name)
                    if not text or text.lower() in IGNORE_WORDS:
                        text = href.split("/")[-1]

                    # ✅ SAVE RAW LINKS
                    raw_links.append({
                        "company": url,
                        "text": text,
                        "url": full_url
                    })

                    # ❌ REMOVE NON-DOCUMENT LINKS
                    if (
                        href_lower.endswith("/")
                        or href_lower.endswith(".aspx")
                        or href_lower.endswith(".html")
                    ):
                        print(f"REMOVED → {full_url}")
                        continue

                    # ✅ KEEP DOCUMENT LINKS
                    if (
                        ".pdf" in href_lower
                        or ".ashx" in href_lower
                        or "download" in href_lower
                        or "financial" in href_lower
                        or "statement" in href_lower
                        or "results" in href_lower
                    ):
                        if full_url in seen:
                            continue
                        seen.add(full_url)

                        print(f"KEPT → {text} → {full_url}")

                        output_data.append({
                            "company": url,
                            "document_title": text,
                            "document_url": full_url
                        })

            else:
                print("Failed ❌")

        except Exception as e:
            print(f"Error: {e}")

# ✅ SAVE FILTERED OUTPUT
with open('output.csv', 'w', newline='', encoding='utf-8') as out_file:
    writer = csv.DictWriter(
        out_file,
        fieldnames=["company", "document_title", "document_url"]
    )
    writer.writeheader()
    writer.writerows(output_data)

# ✅ SAVE RAW LINKS
with open('raw_links.csv', 'w', newline='', encoding='utf-8') as raw_file:
    writer = csv.DictWriter(
        raw_file,
        fieldnames=["company", "text", "url"]
    )
    writer.writeheader()
    writer.writerows(raw_links)

print("\n✅ Files saved successfully")



