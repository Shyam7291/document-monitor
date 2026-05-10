import csv
import requests
from bs4 import BeautifulSoup

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

                print("\nFiltered document links:\n")

                for link in links:
                    text = link.get_text(strip=True)
                    href = link.get('href')

                    if not href:
                        continue

                    href_lower = href.lower()

                    if ".pdf" in href_lower or "report" in href_lower or "announcement" in href_lower:
                        print(f"{text} → {href}")

            else:
                print("Failed to fetch page ❌")

        except Exception as e:
            print("Error:", e)

        break

