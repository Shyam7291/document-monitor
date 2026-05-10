import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

output_data = []
raw_links = []

# Read your input URLs
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

                    # ✅ SAVE RAW LINKS (no filtering)
                    raw_links.append({
                        "company": url,
                        "text": text,
                        "url": full_url
                    })

                    href_lower = full_url.lower()

                    # ❌ remove unwanted pages
                    if href_lower.endswith("/"):
                        print(f"REMOVED → {full_url}")
                        continue

                    if "announcements" in href_lower and ".pdf" not in href_lower:
                        print(f"REMOVED → {full_url}")
                        continue

                    # ❌ remove only known navigation pages
                    if href_lower.endswith(".aspx") or href_lower.endswith(".html"):
                    print(f"REMOVED → {full_url}")
                    continue


                    # ✅ keep useful document-like links
                    if (
                        ".pdf" in href_lower
                        or ".ashx" in href_lower
                        or "download" in href_lower
                        or "financial" in href_lower
                        or "statement" in href_lower
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
                print("Failed to fetch page ❌")

        except Exception as e:
            print("Error:", e)

# ✅ SAVE FILTERED OUTPUT
with open('output.csv', 'w', newline='', encoding='utf-8') as out_file:
    fieldnames = ["company", "document_title", "document_url"]
    writer = csv.DictWriter(out_file, fieldnames=fieldnames)

    writer.writeheader()
    writer.writerows(output_data)

# ✅ SAVE RAW DATA
with open('raw_links.csv', 'w', newline='', encoding='utf-8') as raw_file:
    fieldnames = ["company", "text", "url"]
    writer = csv.DictWriter(raw_file, fieldnames=fieldnames)

    writer.writeheader()
    writer.writerows(raw_links)

print("\n✅ Filtered data → output.csv")
print("✅ All raw links → raw_links.csv")


