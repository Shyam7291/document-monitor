import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

output_data = []
raw_links = []

# Read input URLs
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
                    href = link.get('href')

                    if not href:
                        continue

                    full_url = urljoin(url, href)
                    href_lower = full_url.lower()

                    # ✅ ALWAYS STORE RAW LINKS
                    raw_links.append({
                        "company": url,
                        "text": text,
                        "url": full_url
                    })

                    # ❌ REMOVE NON-DOCUMENT LINKS
                    if href_lower.endswith("/"):
                        print(f"REMOVED → {full_url}")
                        continue

                    if href_lower.endswith(".aspx") or href_lower.endswith(".html"):
                        print(f"REMOVED → {full_url}")
                        continue

                    # ✅ KEEP DOCUMENT-LIKE LINKS
                    if (
                        ".pdf" in href_lower
                        or ".ashx" in href_lower


