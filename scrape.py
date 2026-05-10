import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# Open CSV file
with open('documents.csv', newline='', encoding='utf-8') as file:
    reader = csv.DictReader(file)

    # Loop through each URL
    for row in reader:
        url = row['source_url']

        print(f"\nChecking: {url}")

        try:
            # Fetch page
            response = requests.get(url, timeout=10)
            print("Status:", response.status_code)

            if response.status_code == 200:
                # Parse HTML
                soup = BeautifulSoup(response.text, 'html.parser')

                # Find all links
                links = soup.find_all('a')

                print("\nFiltered document links:\n")

                seen = set()  # to remove duplicates

                for link in links:
                    text = link.get_text(strip=True)
                    href = link.get('href')

                    if not href:
                        continue

                    href_lower = href.lower()

                    # Filter only useful links
                    if ".pdf" in href_lower or "report" in href_lower or "announcement" in href_lower:
                        full_url = urljoin(url, href)

                        # Remove duplicates
                        if full_url in seen:
                            continue
                        seen.add(full_url)

                        print(f"{text} → {full_url}")

            else:
                print("Failed to fetch page ❌")

        except Exception as e:
            print("Error:", e)

        # Only test first URL (for now)
        break


