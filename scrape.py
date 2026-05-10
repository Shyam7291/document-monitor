import csv
import requests

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
                print("Page fetched successfully ✅")
            else:
                print("Failed to fetch page ❌")

        except Exception as e:
            print("Error:", e)

        # IMPORTANT: test ONLY first URL
        break
