import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

output_data = []
raw_links = []

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
                    text = link.get_text(strip=True)

if not text:
    parent = link.parent
    if parent:
        text = parent.get_text(strip=True)

    if not text:
        text = href.split("/")[-1]
                    href = link.get('href')

                    if not href:
                        continue

                    full_url = urljoin(url, href)
                    href_lower = full_url.lower()

                    # ✅ store raw
                    raw_links.append({
                        "company": url,
                        "text": text,
                        "url": full_url
                    })

                    # ❌ remove pages
                    if href_lower.endswith("/") or href_lower.endswith(".aspx") or href_lower.endswith(".html"):
                        print(f"REMOVED → {full_url}")
                        continue

                    # ✅ keep documents
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

# ✅ save output.csv
with open('output.csv', 'w', newline='', encoding='utf-8') as out_file:
    writer = csv.DictWriter(out_file, fieldnames=["company","document_title","document_url"])
    writer.writeheader()
    writer.writerows(output_data)

# ✅ save raw_links.csv
with open('raw_links.csv', 'w', newline='', encoding='utf-8') as raw_file:
    writer = csv.DictWriter(raw_file, fieldnames=["company","text","url"])
    writer.writeheader()
    writer.writerows(raw_links)

print("\n✅ Files saved successfully")


