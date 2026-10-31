"""Independent, resumable recovery of failed internal PDF metadata checks.

Never writes production known/diff CSVs. XMP, creation dates and HTTP headers
are not substitutes for the PDF Info dictionary's /ModDate.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from browser_pool import browser_events
from monitor_io import PDFRangeReader, pdf_date, paced_get

FIELDS = ['document_url', 'status', 'modified_date', 'mod_date_raw', 'creation_date',
          'xmp_modified_date', 'parser', 'http_status', 'error', 'bytes_read',
          'elapsed_seconds', 'original_reasons', 'source_urls']
QUEUE = Path('.metadata_recovery_queue')
DEFAULT_LIMIT = 100 * 1024 * 1024
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; DocumentMonitor/1.0)',
           'Accept': 'application/pdf,*/*;q=0.8'}


def read_rows(path):
    with Path(path).open(newline='', encoding='utf-8-sig') as stream:
        return list(csv.DictReader(stream))


def write_rows(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({key: row.get(key, '') for key in FIELDS} for row in rows)


def key(url):
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ''))
    except ValueError:
        return url


def make_plan(issue_file, shards=6, days=60, as_of=None):
    if not 1 <= shards <= 24 or days < 1:
        raise ValueError('Use 1–24 shards and a positive recency window')
    grouped = {}
    records = 0
    for row in read_rows(issue_file):
        if row.get('issue_type') != 'PDF_MODIFIED_DATE_UNKNOWN':
            continue
        reason, separator, url = row.get('error_message', '').partition(' | ')
        if not separator or not url.strip():
            raise ValueError('Metadata issue lacks a document URL; refusing an incomplete recovery queue')
        records += 1
        document_key = key(url.strip())
        entry = grouped.setdefault(document_key, {'document_url': url.strip(), 'source_urls': set(),
                                                  'original_reasons': set()})
        entry['source_urls'].add(row.get('source_url', ''))
        entry['original_reasons'].add(reason)
    if not grouped:
        raise ValueError('No failed PDF metadata records found')
    # Keep one host in one shard, spreading large hosts first to balance the six jobs.
    hosts = defaultdict(list)
    for entry in grouped.values():
        try:
            host = urlsplit(entry['document_url']).netloc.lower()
        except ValueError:
            host = ''
        hosts[host].append(entry)
    queues = [[] for _ in range(shards)]
    for host, entries in sorted(hosts.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        destination = min(range(shards), key=lambda i: (len(queues[i]), i))
        queues[destination].extend(entries)
    QUEUE.mkdir(parents=True, exist_ok=True)
    plan = {'source_issue_file': str(issue_file), 'issue_records': records,
            'unique_pdfs': len(grouped), 'shards': {}, 'recency_days': days,
            'as_of': (as_of or datetime.now(timezone.utc)).isoformat()}
    for i, entries in enumerate(queues, 1):
        # Interleave hosts to avoid sending a burst of consecutive PDFs to one site.
        by_host = defaultdict(list)
        for entry in entries:
            try:
                host = urlsplit(entry['document_url']).netloc.lower()
            except ValueError:
                host = ''
            by_host[host].append(entry)
        ordered = []
        while by_host:
            for host in list(by_host):
                entry = by_host[host].pop()
                ordered.append({**entry, 'source_urls': sorted(entry['source_urls']),
                                'original_reasons': sorted(entry['original_reasons'])})
                if not by_host[host]:
                    del by_host[host]
        filename = QUEUE / f'queue_{i:02}.jsonl'
        filename.write_text(''.join(json.dumps(row) + '\n' for row in ordered))
        plan['shards'][f'{i:02}'] = {'count': len(ordered),
            'sha256': hashlib.sha256(filename.read_bytes()).hexdigest()}
    (QUEUE / 'plan.json').write_text(json.dumps(plan, indent=2))
    print('METADATA PLAN', json.dumps(plan), flush=True)
    return plan


def iso_pdf(raw):
    value = pdf_date(raw)
    return value.isoformat() if value else ''


def parse_pypdf(stream):
    from pypdf import PdfReader
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    reader = PdfReader(stream, strict=False)
    if reader.is_encrypted and not reader.decrypt(''):
        return {'status': 'password_required', 'parser': 'pypdf'}
    meta = reader.metadata or {}
    raw = str(meta.get('/ModDate') or '')
    result = {'status': 'resolved' if iso_pdf(raw) else 'invalid_mod_date' if raw else 'missing_mod_date',
              'modified_date': iso_pdf(raw), 'mod_date_raw': raw,
              'creation_date': iso_pdf(meta.get('/CreationDate')),
              'xmp_modified_date': '', 'parser': 'pypdf'}
    # Avoid extra remote reads for XMP when the requested Info date is already known.
    if not result['modified_date']:
        try:
            xmp = reader.xmp_metadata
            value = xmp.xmp_modify_date if xmp else None
            if value:
                result['xmp_modified_date'] = value.isoformat()
        except Exception as error:
            result['error'] = f'xmp:{type(error).__name__}'
    return result


def parse_full(data):
    """Use a different parser on valid bytes that pypdf cannot interpret."""
    try:
        primary = parse_pypdf(io.BytesIO(data))
        if primary.get('modified_date') or primary['status'] == 'password_required':
            return primary
    except Exception as error:
        primary = {'status': 'parse_error', 'error': f'pypdf:{type(error).__name__}'}
    try:
        import pymupdf
        with pymupdf.open(stream=data, filetype='pdf') as doc:
            if doc.needs_pass and not doc.authenticate(''):
                return {'status': 'password_required', 'parser': 'pymupdf'}
            meta = doc.metadata or {}
            raw = meta.get('modDate') or ''
            modified = iso_pdf(raw)
            if modified:
                return {**primary, 'status': 'resolved', 'parser': 'pymupdf',
                        'mod_date_raw': raw, 'modified_date': modified,
                        'creation_date': iso_pdf(meta.get('creationDate')), 'error': ''}
            primary['parser'] = primary.get('parser', '') + '+pymupdf'
    except Exception as error:
        primary['error'] = primary.get('error', '') + f';pymupdf:{type(error).__name__}'
    return primary


def probe(entry, max_bytes=DEFAULT_LIMIT):
    url = entry['document_url']
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ('http', 'https') or not parts.netloc:
            return {'status': 'invalid_url', 'error': 'Expected an absolute HTTP(S) document URL'}
    except ValueError:
        return {'status': 'invalid_url'}
    headers = dict(HEADERS)
    sources = entry.get('source_urls') or []
    if sources:
        headers['Referer'] = sources[0]
    transferred = 0
    stream = None
    full_data = None
    range_error = ''
    status = ''
    try:
        stream = PDFRangeReader(url, headers, max_bytes, max_requests=10, range_budget=2*1024*1024)
        # A blocked HTML page returned with HTTP 200 is not a PDF parse failure.
        prefix = stream.read(1024)
        if b'%PDF-' not in prefix:
            return {'status': 'not_pdf', 'http_status': '200' if stream.full is not None else '206',
                    'bytes_read': stream.transferred}
        stream.seek(0)
        result = parse_pypdf(stream)
        return {**result, 'bytes_read': stream.transferred,
                'http_status': '200' if stream.full is not None else '206'}
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else ''
        if status in (404, 410):
            return {'status': 'not_found', 'http_status': status}
        range_error = f'range:HTTP_{status}'
    except Exception as error:
        range_error = f'range:{type(error).__name__}'
    finally:
        if stream is not None:
            transferred = stream.transferred
            full_data = stream.full
            stream.close()
    # Reuse bytes from a server that ignored Range; do not download twice.
    if full_data is not None:
        return {**parse_full(full_data), 'bytes_read': transferred,
                'http_status': '200', 'error_range': range_error}
    # One public source-page session warm-up for blocked requests, never log in.
    if status in (401, 403) and sources:
        try:
            with paced_get(sources[0], headers=HEADERS, timeout=(5, 8), stream=True):
                pass
        except requests.RequestException:
            pass
    try:
        with paced_get(url, headers=headers, stream=True, timeout=(7, 15)) as response:
            status = response.status_code
            response.raise_for_status()
            length = response.headers.get('Content-Length')
            if length and int(length) > max_bytes:
                return {'status': 'too_large', 'http_status': status, 'bytes_read': transferred}
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > max_bytes:
                    return {'status': 'too_large', 'http_status': status,
                            'bytes_read': transferred + len(data)}
            if b'%PDF-' not in data[:1024]:
                return {'status': 'not_pdf' if data else 'empty_response', 'http_status': status,
                        'bytes_read': transferred + len(data)}
            return {**parse_full(bytes(data)), 'http_status': status,
                    'bytes_read': transferred + len(data)}
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else status
        return {'status': 'not_found' if status in (404, 410) else 'blocked' if status in (401, 403)
                else 'http_error', 'http_status': status, 'error': range_error}
    except Exception as error:
        return {'status': 'request_error', 'http_status': status, 'bytes_read': transferred,
                'error': f'{range_error};full:{type(error).__name__}'}


def metadata_worker(connection):
    os.environ['SLEEP_SECONDS'] = '0.5'
    while True:
        job = connection.recv()
        if job is None:
            return
        try:
            result = probe(job, job.get('max_bytes', DEFAULT_LIMIT))
        except Exception as error:
            result = {'status': 'worker_error', 'error': type(error).__name__}
        connection.send(('done', result))


def run_queue(shard, output, workers=4, timeout=45, max_bytes=DEFAULT_LIMIT, resume=None,
              worker=metadata_worker):
    plan = json.loads((QUEUE / 'plan.json').read_text())
    path = QUEUE / f'queue_{shard}.jsonl'
    expected = plan['shards'][shard]
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256']:
        raise ValueError('Recovery queue differs from the immutable plan')
    entries = [json.loads(line) for line in path.read_text().splitlines() if line]
    cached = {key(row['document_url']): row for row in read_rows(resume)
              if row['status'] == 'resolved'} if resume else {}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    results = []
    jobs = []
    for entry in entries:
        cached_row = cached.get(key(entry['document_url']))
        if cached_row:
            results.append({**cached_row, 'source_urls': json.dumps(entry['source_urls']),
                            'original_reasons': json.dumps(entry['original_reasons'])})
        else:
            jobs.append({**entry, 'max_bytes': max_bytes})
    # The shared process supervisor kills a stuck request/parser at the deadline.
    checkpoint = output / 'results.csv'
    write_rows(checkpoint, results)
    with checkpoint.open('a', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        for event, job, result in browser_events(jobs, workers=workers, timeout=timeout, worker=worker):
            if event != 'done':
                continue
            row = {field: result.get(field, '') for field in FIELDS}
            row.update(document_url=job['document_url'], elapsed_seconds=round(result['seconds'], 3),
                       source_urls=json.dumps(job['source_urls']),
                       original_reasons=json.dumps(job['original_reasons']))
            if result['outcome'] in ('timeout', 'worker_exited'):
                row['status'] = result['outcome']
                row['error'] = 'Parent watchdog stopped an unfinished metadata check'
            results.append(row)
            writer.writerow(row)
            stream.flush()
            if len(results) % 25 == 0:
                print(f'METADATA PROGRESS shard={shard} processed={len(results)}/{len(entries)} '
                      f'resolved={sum(row["status"] == "resolved" for row in results)}', flush=True)
    if len(results) != expected['count']:
        raise ValueError('Metadata task did not account for every planned PDF')
    manifest = {'shard': shard, 'queue_sha256': expected['sha256'], 'count': len(results),
                'statuses': dict(Counter(row['status'] for row in results))}
    (output / 'completion.json').write_text(json.dumps(manifest, indent=2))
    print('METADATA SHARD RESULT', json.dumps(manifest), flush=True)
    return results


def consolidate(root, output):
    plan = json.loads((QUEUE / 'plan.json').read_text())
    results = []
    markers = list(Path(root).rglob('completion.json'))
    if len(markers) != len(plan['shards']):
        raise ValueError('Missing completed metadata shard; partial artifacts remain available')
    seen = set()
    for path in markers:
        marker = json.loads(path.read_text())
        shard = marker['shard']
        if shard in seen or shard not in plan['shards']:
            raise ValueError('Unexpected or duplicate metadata shard')
        seen.add(shard)
        expected = plan['shards'][shard]
        rows = read_rows(path.parent / 'results.csv')
        if marker['queue_sha256'] != expected['sha256'] or len(rows) != expected['count']:
            raise ValueError('Metadata shard output does not match plan')
        actual_urls = {key(row['document_url']) for row in rows}
        expected_urls = {key(json.loads(line)['document_url']) for line in
                         (QUEUE / f'queue_{shard}.jsonl').read_text().splitlines() if line}
        if actual_urls != expected_urls or len(actual_urls) != len(rows):
            raise ValueError('Missing or duplicated PDF results')
        results.extend(rows)
    results.sort(key=lambda row: row['document_url'])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    as_of = datetime.fromisoformat(plan['as_of'])
    cutoff = as_of - timedelta(days=plan['recency_days'])
    recovered = [row for row in results if row['status'] == 'resolved' and row['modified_date']]
    recent = [row for row in recovered if cutoff <= datetime.fromisoformat(row['modified_date']) <= as_of]
    unresolved = [row for row in results if row['status'] != 'resolved']
    xmp_only = [row for row in unresolved if row['xmp_modified_date']]
    for filename, rows in [('all_results.csv', results), ('recovered_internal_dates.csv', recovered),
                           ('recovered_recent_pdfs.csv', recent), ('unresolved.csv', unresolved),
                           ('xmp_only.csv', xmp_only)]:
        write_rows(output / filename, rows)
    summary = {'issue_records': plan['issue_records'], 'unique_pdfs': plan['unique_pdfs'],
               'processed': len(results), 'recovered_internal_mod_dates': len(recovered),
               'recent_internal_mod_dates': len(recent), 'unresolved': len(unresolved),
               'xmp_only': len(xmp_only), 'statuses': dict(Counter(row['status'] for row in results)),
               'as_of': plan['as_of'], 'recency_days': plan['recency_days'],
               'production_csvs_modified': False}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2))
    print('METADATA RECOVERY REPORT', json.dumps(summary, indent=2), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'run', 'consolidate'])
    parser.add_argument('--issues', default='p4_p5/capture_issues_p4_p5.csv')
    parser.add_argument('--shards', type=int, default=6)
    parser.add_argument('--shard', default='01')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=45)
    parser.add_argument('--max-mb', type=int, default=100)
    parser.add_argument('--days', type=int, default=60)
    parser.add_argument('--output', default='metadata_recovery_results')
    parser.add_argument('--input', default='metadata_shards')
    parser.add_argument('--resume', help='Optional prior recovery CSV; reuses successful dates for this recovery snapshot')
    args = parser.parse_args()
    if args.action == 'plan':
        make_plan(args.issues, args.shards, args.days)
    elif args.action == 'run':
        run_queue(args.shard, args.output, args.workers, args.timeout,
                  args.max_mb * 1024 * 1024, args.resume)
    else:
        consolidate(args.input, args.output)
