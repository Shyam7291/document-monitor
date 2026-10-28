"""Small P4/P5 work batches scheduled into six GitHub Actions runner slots.

The immutable plan assigns every source once. Only finished, validated batches
can be published; an exit code of zero does not imply successful page capture.
"""
import argparse
import csv
import hashlib
import html
import json
import os
from pathlib import Path
import shutil

PLAN = Path('.p4_p5_batches/manifest.json')
SOURCE_FILES = [f'p4_p5/documents_p4_p5_{i:02}.csv' for i in range(1, 7)]
PREFIXES = {'output': 'output', 'issues': 'capture_issues', 'diff': 'diff',
            'known': 'known_documents', 'summary': 'run_summary', 'status': 'url_status'}


def read_csv(path):
    with Path(path).open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def write_csv(path, fields, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def source(raw):
    raw = html.unescape((raw or '').strip())
    for prefix in ('fallback/', 'fallback:'):
        if raw.lower().startswith(prefix):
            return raw[len(prefix):].strip(), True
    return raw, False


def work_file(kind, batch):
    return f'{PREFIXES[kind]}_p4_p5_{batch}_work.csv'


def strategy_file(batch):
    return f'source_strategies_documents_p4_p5_batch_{batch}.json'


def plan_batches(count=24):
    if count < 1 or count > 256:
        raise ValueError('Batch count must be between 1 and 256')
    sources = {}
    for filename in SOURCE_FILES:
        fields, rows = read_csv(filename)
        if 'source_url' not in (fields or []):
            raise ValueError(f'Missing source_url header: {filename}')
        for row in rows:
            url, forced = source(row.get('source_url'))
            if not url:
                continue
            if url not in sources:
                sources[url] = {'url': url, 'owner': filename, 'forced': forced}
            else:
                sources[url]['forced'] |= forced
    if not sources:
        raise ValueError('No P4/P5 sources to schedule')
    batches = {f'{i+1:03}': [] for i in range(min(count, len(sources)))}
    ids = list(batches)
    # Spread each of the previous large shards across the runner queue.
    for i, entry in enumerate(sources.values()):
        batches[ids[i % len(ids)]].append(entry)
    manifest = {'run_id': os.environ.get('GITHUB_RUN_ID', 'local'),
                'source_count': len(sources), 'batches': {}}
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    for batch, entries in batches.items():
        filename = PLAN.parent / f'documents_p4_p5_batch_{batch}.csv'
        write_csv(filename, ['source_url'], [
            {'source_url': ('fallback:' if entry['forced'] else '') + entry['url']}
            for entry in entries])
        manifest['batches'][batch] = {'sources': entries,
            'input_sha256': hashlib.sha256(filename.read_bytes()).hexdigest()}
    PLAN.write_text(json.dumps(manifest, indent=2))
    matrix = json.dumps({'batch': ids})
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'matrix={matrix}\n')
    print(f'Planned {len(sources)} unique sources in {len(batches)} batches; max 6 running')
    return manifest


def load_plan(path=PLAN):
    manifest = json.loads(Path(path).read_text())
    expected_run = os.environ.get('GITHUB_RUN_ID', 'local')
    if manifest['run_id'] != expected_run:
        raise ValueError('Batch plan belongs to another workflow run')
    return manifest


def prepare(batch):
    manifest = load_plan()
    entry = manifest['batches'][batch]
    filename = PLAN.parent / f'documents_p4_p5_batch_{batch}.csv'
    if hashlib.sha256(filename.read_bytes()).hexdigest() != entry['input_sha256']:
        raise ValueError('Batch source file differs from plan')
    urls = {row['url'] for row in entry['sources']}
    for kind, original in [('known', 'p4_p5/known_documents_p4_p5.csv'),
                           ('diff', 'diff_p4_p5.csv')]:
        shutil.copyfile(original, work_file(kind, batch))
    # Do not copy unrelated historical output into every batch artifact.
    fields, rows = read_csv('p4_p5/output_p4_p5.csv')
    write_csv(work_file('output', batch), fields,
              [row for row in rows if row['company'] in urls])
    strategies = {}
    for original in SOURCE_FILES:
        path = Path(f'source_strategies_{Path(original).stem}.json')
        if path.exists():
            strategies.update({url: value for url, value in json.loads(path.read_text()).items()
                               if url in urls})
    Path(strategy_file(batch)).write_text(json.dumps(strategies))


def finish(batch):
    manifest = load_plan()
    entry = manifest['batches'][batch]
    _, summaries = read_csv(work_file('summary', batch))
    if len(summaries) != 1 or int(summaries[0]['total_urls_processed']) != len(entry['sources']):
        raise ValueError('Batch summary does not match planned source count')
    owners = {row['url']: row['owner'] for row in entry['sources']}
    for kind, column in [('status', 'url'), ('issues', 'source_url')]:
        fields, rows = read_csv(work_file(kind, batch))
        for row in rows:
            row['url_file'] = owners.get(row[column], row['url_file'])
        write_csv(work_file(kind, batch), fields, rows)
    # The merger already retains production history; upload only new history.
    for kind, original in [('known', 'p4_p5/known_documents_p4_p5.csv'),
                           ('diff', 'diff_p4_p5.csv')]:
        _, baseline = read_csv(original)
        existing = {row['document_url'] for row in baseline}
        fields, rows = read_csv(work_file(kind, batch))
        write_csv(work_file(kind, batch), fields,
                  [row for row in rows if row['document_url'] not in existing])
    for kind in PREFIXES:
        read_csv(work_file(kind, batch))  # incomplete files must not get a marker
    marker = {'batch': batch, 'run_id': manifest['run_id'],
              'input_sha256': entry['input_sha256'], 'processed': len(entry['sources'])}
    Path(f'completion_p4_p5_{batch}.json').write_text(json.dumps(marker))


def validate_results(manifest_path, root='shard_outputs'):
    manifest = load_plan(manifest_path)
    root = Path(root)
    markers = list(root.rglob('completion_p4_p5_*.json'))
    expected = manifest['batches']
    if len(markers) != len(expected):
        raise ValueError(f'Expected {len(expected)} completed batches, found {len(markers)}')
    seen = set()
    for path in markers:
        marker = json.loads(path.read_text())
        batch = marker['batch']
        if batch not in expected or batch in seen:
            raise ValueError('Unexpected or duplicate completed batch')
        seen.add(batch)
        entry = expected[batch]
        if (marker['run_id'] != manifest['run_id'] or
            marker['input_sha256'] != entry['input_sha256'] or
            marker['processed'] != len(entry['sources'])):
            raise ValueError('Completion marker does not match batch plan')
        for kind in PREFIXES:
            fields, rows = read_csv(path.parent / work_file(kind, batch))
            if not fields:
                raise ValueError(f'Empty batch file: {kind} {batch}')
            if kind == 'summary' and (len(rows) != 1 or
                    int(rows[0]['total_urls_processed']) != len(entry['sources'])):
                raise ValueError('Published source count does not match batch plan')
        json.loads((path.parent / strategy_file(batch)).read_text())
    outputs = list(root.rglob('output_p4_p5_*_work.csv'))
    if len(outputs) != len(expected):
        raise ValueError('Missing or unexpected batch output files')
    # Preserve prior learned strategies; update only each batch's own sources.
    strategies = {}
    for path in root.rglob('source_strategies_documents_p4_p5_batch_*.json'):
        strategies.update(json.loads(path.read_text()))
    for original in SOURCE_FILES:
        path = Path(f'source_strategies_{Path(original).stem}.json')
        saved = json.loads(path.read_text()) if path.exists() else {}
        for entry in expected.values():
            for row in entry['sources']:
                if row['owner'] == original and row['url'] in strategies:
                    saved[row['url']] = strategies[row['url']]
        path.write_text(json.dumps(saved, indent=2, sort_keys=True))
    print(f'Validated all {len(expected)} batches / {manifest["source_count"]} sources')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'prepare', 'finish'])
    parser.add_argument('--batch')
    parser.add_argument('--count', type=int, default=24)
    args = parser.parse_args()
    if args.action == 'plan':
        plan_batches(args.count)
    elif not args.batch:
        parser.error('--batch is required')
    elif args.action == 'prepare':
        prepare(args.batch)
    else:
        finish(args.batch)
