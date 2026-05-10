import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

output_data = []
raw_links = []

IGNORE_WORDS = ["download", "view", "open", "read", "click"]

def get_best_text(link, href):
    """Smart function to extract meaningful title"""

    # ✅ 1. direct text
    text = link.get_text(strip=True)
    if text and text.lower() not in IGNORE_WORDS:
        return text

    # ✅ 2. check parent row (table or list)
    parent_row = link.find_parent(["tr", "li", "div"])
    if parent_row:
        candidates = []

        for element in parent_row.find_all(["td", "th", "div", "span", "p", "a"]):
            t = element.get_text(strip=True)

            if (
                t
                and t.lower() not in IGNORE_WORDS
                and len(t) > 10
                and not t.lower().endswith(".pdf")
            ):
                candidates.append(t)

        if candidates:
            return max(candidates, key=len)

    # ✅ 3. check previous sibling
    prev = link.find_previous(string=True)
    if prev:
        prev_text = prev.strip()
        if len(prev_text) > 10:
            return prev_text

    # ✅ 4. check next sibling
    nxt = link.find_next(string=True)
    if nxt:
        next_text = nxt.strip()
        if len(next_text) > 10:
            return next_text

    # ✅ 5. check parent container text
    parent = link.parent
    if parent:
        parent_text = parent.get_text(strip=True)
        if len(parent_text) > 10:
            return parent_text

    # ✅ LAST fallback (only if nothing found)
    return href.split("/")[-1]


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

                    # ✅ GET BEST TITLE
                    text = get_best_text(link, href)

                    # ✅ SAVE RAW
                    raw_links.append({
                        "company": url,
                        "text": text,
                        "url": full_url
                    })

                    # ❌ REMOVE NON DOC LINKS
                    if (
                        href_lower.endswith("/")
                        or href_lower.endswith(".aspx")
                        or href_lower.endswith(".html")
                    ):
                        continue

                    # ✅ KEEP DOCUMENT LINKS
                    if (
                        ".pdf" in href_lower
                        or ".ashx" in href_lower
                    ):
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

# ✅ SAVE OUTPUT
with open('output.csv', 'w', newline='', encoding='utf-8') as out_file:
    writer = csv.DictWriter(
        out_file,
        fieldnames=["company", "document_title", "document_url"]
    )
    writer.writeheader()
    writer.writerows(output_data)

# ✅ SAVE RAW
with open('raw_links.csv', 'w', newline='', encoding='utf-8') as raw_file:
    writer = csv.DictWriter(
        raw_file,
        fieldnames=["company", "text", "url"]
    )
    writer.writeheader()
    writer.writerows(raw_links)

print("\n✅ Done — titles improved")



