import csv
import requests
from bs4 import BeautifulSoup

# Read CSV
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

                print("\nLinks found:\n")

                # Find all links
                links = soup.find_all('a')

                for link in links[:20]:  # limit to first 20
                    text = link.get_text(strip=True)
                    href = link.get('href')

                    if href:
                        print(f"{text} → {href}")

            else:
                print("Failed to fetch page ❌")

        except Exception as e:
            print("Error:", e)

        break  # only first URL
