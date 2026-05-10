import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

output_data = []

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

                    href_lower = href.lower()

                    if ".pdf" in href_lower or "report" in href_lower or "announcement" in href_lower:
                        full_url = urljoin(url, href)

                        if full_url in seen:
                            continue
                        seen.add(full_url)

                        print(f"{text} → {full_url}")

                        # ✅ Save data instead of just printing
                        output_data.append({
                            "source_url": url,
                            "document_title": text,
                            "document_url": full_url
                        })

            else:
                print("Failed to fetch page ❌")

        except Exception as e:
            print("Error:", e)

        

# ✅ Save to CSV
with open('output.csv', 'w', newline='', encoding='utf-8') as out_file:
    fieldnames = ["source_url", "document_title", "document_url"]
    writer = csv.DictWriter(out_file, fieldnames=fieldnames)

    writer.writeheader()
    writer.writerows(output_data)

print("\n✅ Data saved to output.csv")


