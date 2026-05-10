import csv

# Read the CSV file
with open('documents.csv', newline='', encoding='utf-8') as file:
    reader = csv.DictReader(file)

    print("URLs to check:\n")

    for row in reader:
        print(row['source_url'])
