import csv
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recover_pdf_metadata as recovery


def pdf(modified=None, encrypted=False, large=False):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    metadata = {'/CreationDate': 'D:20261001000000Z'}
    if modified:
        metadata['/ModDate'] = modified
    writer.add_metadata(metadata)
    if large:
        unused = DecodedStreamObject()
        unused.set_data(b'x' * (3 * 1024 * 1024))
        writer._root_object[NameObject('/Unused')] = writer._add_object(unused)
    if encrypted:
        writer.encrypt('', owner_password='fixture-owner', algorithm='AES-256')
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class Handler(BaseHTTPRequestHandler):
    calls = []
    data = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).calls.append(self.path)
        if self.path == '/404.pdf':
            self.send_response(404)
            self.end_headers()
            return
        if self.path == '/source':
            self.send_response(200)
            self.send_header('Set-Cookie', 'fixture=ok; Path=/')
            self.end_headers()
            return
        if self.path == '/cookie.pdf' and ('fixture=ok' not in self.headers.get('Cookie', '') or
                                         not self.headers.get('Referer', '').endswith('/source')):
            self.send_response(403)
            self.end_headers()
            return
        data = self.data.get(self.path, b'<html>Not a PDF</html>')
        start, end = 0, len(data)-1
        status = 200
        if self.path == '/range.pdf' and self.headers.get('Range'):
            start, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers['Range']).groups())
            end = min(end, len(data)-1)
            status = 206
        self.send_response(status)
        self.send_header('Content-Length', str(end-start+1))
        self.send_header('ETag', '"fixture-v1"')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
        self.end_headers()
        self.wfile.write(data[start:end+1])


def fixture_worker(connection):
    while True:
        job = connection.recv()
        if job is None:
            return
        url = job['document_url']
        if url.endswith('/hung'):
            time.sleep(5)
        result = {'status': 'resolved', 'modified_date': '2026-10-01T00:00:00+00:00',
                  'parser': 'fixture'}
        if url.endswith('/old'):
            result['modified_date'] = '2020-01-01T00:00:00+00:00'
        if url.endswith('/xmp'):
            result.update(status='missing_mod_date', modified_date='',
                          xmp_modified_date='2026-10-01T00:00:00+00:00')
        connection.send(('done', result))


class MetadataRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.data = {'/good.pdf': pdf('D:20261001000000Z'),
                        '/missing.pdf': pdf(), '/encrypted.pdf': pdf('D:20261001000000Z', encrypted=True),
                        '/range.pdf': pdf('D:20261001000000Z', large=True),
                        '/cookie.pdf': pdf('D:20261001000000Z')}
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, cwd)
        self.environment = patch.dict(os.environ, {'SLEEP_SECONDS': '0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def entry(self, path):
        return {'document_url': self.base+path, 'source_urls': [self.base+'/source']}

    def issues(self, urls):
        with open('issues.csv', 'w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['issue_type', 'error_message', 'source_url'])
            writer.writeheader()
            for i, url in enumerate(urls):
                writer.writerow({'issue_type': 'PDF_MODIFIED_DATE_UNKNOWN',
                                 'error_message': 'unreadable:PdfReadError | '+url,
                                 'source_url': f'https://source.test/{i}'})

    def test_moddate_range_reads_and_aes_without_full_download(self):
        result = recovery.probe(self.entry('/range.pdf'))
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['http_status'], '206')
        self.assertLess(result['bytes_read'], len(Handler.data['/range.pdf']) // 2)
        self.assertEqual(recovery.probe(self.entry('/encrypted.pdf'))['status'], 'resolved')

    def test_missing_modified_date_does_not_use_creation_or_http_date(self):
        result = recovery.probe(self.entry('/missing.pdf'))
        self.assertEqual(result['status'], 'missing_mod_date')
        self.assertFalse(result['modified_date'])
        self.assertTrue(result['creation_date'])

    def test_xmp_is_recorded_separately(self):
        from pypdf import PdfReader, PdfWriter
        from pypdf.xmp import XmpInformation
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf())))
        xmp = XmpInformation.create()
        xmp.xmp_modify_date = datetime(2026, 10, 1, tzinfo=timezone.utc)
        writer.xmp_metadata = xmp
        data = io.BytesIO()
        writer.write(data)
        result = recovery.parse_pypdf(io.BytesIO(data.getvalue()))
        self.assertEqual(result['status'], 'missing_mod_date')
        self.assertFalse(result['modified_date'])
        self.assertTrue(result['xmp_modified_date'])

    def test_second_parser_recovers_when_primary_cannot_parse(self):
        with patch.object(recovery, 'parse_pypdf', side_effect=ValueError('fixture parser failure')):
            result = recovery.parse_full(Handler.data['/good.pdf'])
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['parser'], 'pymupdf')

    def test_http_errors_html_size_limits_and_public_session(self):
        before = Handler.calls.count('/404.pdf')
        self.assertEqual(recovery.probe(self.entry('/404.pdf'))['status'], 'not_found')
        self.assertEqual(Handler.calls.count('/404.pdf')-before, 1)
        self.assertEqual(recovery.probe(self.entry('/html.pdf'))['status'], 'not_pdf')
        self.assertEqual(recovery.probe(self.entry('/good.pdf'), max_bytes=100)['status'], 'too_large')
        self.assertEqual(recovery.probe(self.entry('/cookie.pdf'))['status'], 'resolved')
        self.assertEqual(recovery.probe({'document_url': 'javascript:bad()'})['status'], 'invalid_url')

    def test_plan_deduplicates_ids_and_preserves_source_associations(self):
        self.issues(['https://docs.test/report?id=1', 'https://docs.test/report?id=1',
                     'https://docs.test/report?id=2'])
        plan = recovery.make_plan('issues.csv', shards=2)
        self.assertEqual(plan['issue_records'], 3)
        self.assertEqual(plan['unique_pdfs'], 2)
        entries = [json.loads(line) for path in recovery.QUEUE.glob('queue_*.jsonl')
                   for line in path.read_text().splitlines()]
        self.assertEqual(len(next(row for row in entries if row['document_url'].endswith('=1'))['source_urls']), 2)

    def test_pipeline_retains_timeout_and_separates_recent_xmp_and_unresolved(self):
        self.issues(['https://fixture.test/good', 'https://fixture.test/old',
                     'https://fixture.test/xmp', 'https://fixture.test/hung'])
        recovery.make_plan('issues.csv', shards=1, as_of=datetime(2026, 10, 29, tzinfo=timezone.utc))
        results = recovery.run_queue('01', 'shards/01', workers=2, timeout=1.5, worker=fixture_worker)
        self.assertEqual(len(results), 4)
        self.assertEqual(next(r for r in results if r['document_url'].endswith('/hung'))['status'], 'timeout')
        summary = recovery.consolidate('shards', 'report')
        self.assertEqual(summary['recovered_internal_mod_dates'], 2)
        self.assertEqual(summary['recent_internal_mod_dates'], 1)
        self.assertEqual(summary['xmp_only'], 1)
        self.assertEqual(summary['unresolved'], 2)
        self.assertFalse(summary['production_csvs_modified'])
        Path('shards/01/completion.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing completed'):
            recovery.consolidate('shards', 'report')


if __name__ == '__main__':
    unittest.main()
