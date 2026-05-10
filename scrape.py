import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

output_data = []
raw_links = []

IGNORE_WORDS = ["download", "view", "open", "read", "click"]

def get_best_text(link, href):

    # ✅ 1. direct text
    text = link.get_text(strip=True)
    if text and text.lower() not in IGNORE_WORDS:
        return text

    # ✅ 2. find nearest block container
    container = link.find_parent(["div", "li", "section"])

    if container:
        # ✅ PRIORITY: headings (most accurate titles)
        headings = container.find_all(["h1", "h2", "h3", "h4", "strong"])

        for h in headings:
            t = h.get_text(strip=True)
            if t and len(t) > 10:
                return t

        # ✅ fallback: scan all text elements
        candidates = []

        for el in container.find_all(["p", "span", "div"]):
            t = el.get_text(strip=True)

            if (
                t
                and t.lower() not in IGNORE_WORDS
                and len(t) > 15
                and not t.lower().endswith(".pdf")
            ):
                candidates.append(t)

        if candidates:
            return max(candidates, key=len)

    # ✅ 3. previous sibling text
    prev = link.find_previous(string=True)
    if prev:
        t = prev.strip()
        if len(t) > 15:
            return t

    # ✅ 4. next sibling text
    nxt = link.find_next(string=True)
    if nxt:
        t = nxt.strip()
        if len(t) > 15:
            return t

    # ✅ FINAL fallback (only if nothing found)
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

                    text = get_best_text(link, href)

                    # ✅ save raw
                    raw_links.append({
                        "company": url,
                        "text": text,
                        "url": full_url
                    })

                    # ❌ remove non-docs
                    if (
                        href_lower.endswith("/")
                        or href_lower.endswith(".aspx")
                        or href_lower.endswith(".html")
                    ):
                        continue

                    # ✅ keep docs
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

# ✅ SAVE FILES
with open('output.csv', 'w', newline='', encoding='utf-8') as out_file:
    writer = csv.DictWriter(out_file, fieldnames=["company", "document_title", "document_url"])
    writer.writeheader()
    writer.writerows(output_data)

with open('raw_links.csv', 'w', newline='', encoding='utf-8') as raw_file:
    writer = csv.DictWriter(raw_file, fieldnames=["company", "text", "url"])
    writer.writeheader()
    writer.writerows(raw_links)

print("\n✅ Titles fixed for complex layouts")

