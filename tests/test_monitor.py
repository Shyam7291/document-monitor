import csv
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PyPDF2 import PdfWriter
from PyPDF2.generic import DecodedStreamObject, NameObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor_io import (PDFRangeReader, read_modified, pdf_date, recent_modified,
                        url_key, embedded_pdf_links, prefetched, close_browser, shared_browser)
import scrape


def make_pdf(modified=None, created=None, large=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    metadata = {}
    if modified:
        metadata['/ModDate'] = modified
    if created:
        metadata['/CreationDate'] = created
    writer.add_metadata(metadata)
    if large:
        stream = DecodedStreamObject()
        stream.set_data(b' ' * (4 * 1024 * 1024))
        page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    counts = {}
    transferred = {}
    bodies = {}

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass

    def do_GET(self):
        path = self.path.split('?')[0]
        self.counts[path] = self.counts.get(path, 0) + 1
        if path == '/favicon.ico':
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path == '/source':
            body = b'''<a href="/known.pdf">Known annual report</a>
                <a href="/old.pdf">Old annual report</a>
                <a data-url="/recent.pdf">Recent annual report</a>
                <script type="application/json">{"url":"/missing.pdf"}</script>
                <a href="/download.pdf?id=1">Report One</a><a href="/download.pdf?id=2">Report Two</a>'''
            content_type = 'text/html'
        elif path == '/browser':
            body = b'''<html><body><div id="docs"></div>
            <button onclick="load()">Load more</button>
            <script>let n=0; function load(){n++;document.querySelector('#docs').innerHTML+=
            '<a href="/browser-'+n+'.pdf">Report '+n+'</a>';if(n===3)document.querySelector('button').remove();}
            </script></body></html>'''
            content_type = 'text/html'
        elif path == '/pages':
            index = '2' if 'page=2' in self.path else '1'
            body = (f'<a href="/page-{index}.pdf">Report {index}</a>' +
                    ('<a rel="next" href="?page=2">Next</a>' if index == '1' else '')).encode()
            content_type = 'text/html'
        else:
            body = self.bodies.get(path, self.bodies['/recent.pdf'])
            content_type = 'application/pdf'
        start, end = 0, len(body)-1
        status = 200
        requested = self.headers.get('Range')
        if requested and path != '/no-range.pdf':
            start, end = map(int, requested.split('=')[1].split('-'))
            end = min(end, len(body)-1)
            status = 206
        chunk = body[start:end+1]
        self.send_response(status)
        if status == 206:
            offset = 1 if path == '/bad-range.pdf' else 0
            self.send_header('Content-Range', f'bytes {start+offset}-{end}/{len(body)}')
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(chunk)))
        self.send_header('ETag', '"fixture-v1"')
        self.end_headers()
        try:
            self.wfile.write(chunk)
            self.transferred[path] = self.transferred.get(path, 0) + len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.now = datetime.now(timezone.utc)
        cls.recent = cls.now - timedelta(days=2)
        recent = cls.recent.strftime('D:%Y%m%d%H%M%SZ')
        Handler.bodies = {
            '/recent.pdf': make_pdf(recent),
            '/old.pdf': make_pdf('D:20200101000000Z', recent),
            '/missing.pdf': make_pdf(created=recent),
            '/large.pdf': make_pdf(recent, large=True),
            '/bad-range.pdf': make_pdf(recent),
            '/no-range.pdf': make_pdf(recent),
        }
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def read(self, path, limit=10*1024*1024):
        return read_modified(self.base+path, {}, limit)

    def test_modified_not_creation(self):
        value, status = self.read('/old.pdf')
        self.assertEqual(status, 'ok')
        self.assertEqual(value.year, 2020)
        self.assertFalse(recent_modified(value, 60))

    def test_creation_is_not_fallback(self):
        self.assertEqual(self.read('/missing.pdf'), (None, 'missing_modified_date'))

    def test_partial_reads_save_bytes(self):
        before = Handler.transferred.get('/large.pdf', 0)
        value, status = self.read('/large.pdf')
        self.assertEqual(status, 'ok')
        self.assertEqual(value.date(), self.recent.date())
        used = Handler.transferred.get('/large.pdf', 0)-before
        self.assertLess(used, len(Handler.bodies['/large.pdf']) // 4)
        print(f'Range benchmark: {used:,} bytes of {len(Handler.bodies["/large.pdf"]):,}')

    def test_no_range_server(self):
        before = Handler.counts.get('/no-range.pdf', 0)
        self.assertEqual(self.read('/no-range.pdf')[1], 'ok')
        self.assertEqual(Handler.counts['/no-range.pdf']-before, 1)

    def test_bad_range_falls_back(self):
        self.assertEqual(self.read('/bad-range.pdf')[1], 'ok')

    def test_size_limit(self):
        value, status = self.read('/large.pdf', 100)
        self.assertIsNone(value)
        self.assertEqual(status, 'too_large')

    def test_timezone(self):
        self.assertEqual(pdf_date("D:20261027123000+05'30'"),
                         datetime(2026, 10, 27, 7, tzinfo=timezone.utc))

    def test_invalid_dates(self):
        self.assertIsNone(pdf_date('D:20261301000000'))
        self.assertIsNone(pdf_date('not-a-date'))

    def test_window_edges(self):
        self.assertTrue(recent_modified(self.now-timedelta(days=60), 60, self.now))
        self.assertFalse(recent_modified(self.now+timedelta(days=1), 60, self.now))
        self.assertFalse(recent_modified(None, 60, self.now))

    def test_url_identity(self):
        self.assertNotEqual(url_key('https://a/download?id=1'), url_key('https://a/download?id=2'))
        self.assertNotEqual(url_key('https://a/Report.pdf'), url_key('https://a/report.pdf'))
        self.assertEqual(url_key('HTTPS://EXAMPLE.COM/a.pdf#p1'), 'https://example.com/a.pdf')

    def test_embedded_json(self):
        self.assertIn('https://a/reports/annual.pdf?id=2&x=3', embedded_pdf_links(
            r'{"url":"https:\/\/a\/reports\/annual.pdf?id=2\u0026x=3"}'))

    def test_relative_embedded_url(self):
        self.assertEqual(embedded_pdf_links('{"url":"reports/annual.pdf"}'), {'reports/annual.pdf'})
        self.assertEqual(embedded_pdf_links("prefix + '.pdf'"), set())

    def test_extensionless_download(self):
        from bs4 import BeautifulSoup
        from unittest.mock import patch
        soup = BeautifulSoup('<a href="/download?id=42">Annual report</a>', 'html.parser')
        with patch.object(scrape, 'output_data', []), patch.object(scrape, 'global_seen_document_urls', set()):
            scrape.extract_links_from_soup(soup, self.base, self.base, set())
            self.assertEqual(scrape.output_data[0]['document_url'], self.base+'/download?id=42')

    def test_http_pagination(self):
        from bs4 import BeautifulSoup
        from unittest.mock import patch
        source = self.base+'/pages'
        with patch.object(scrape, 'output_data', []), patch.object(scrape, 'global_seen_document_urls', set()):
            response = scrape.fetch_url(source)
            soup = BeautifulSoup(response.text, 'html.parser')
            seen = set()
            scrape.extract_links_from_soup(soup, source, source, seen)
            scrape.scan_http_pagination(soup, source, source, seen)
            self.assertEqual({r['document_url'] for r in scrape.output_data},
                             {self.base+'/page-1.pdf',self.base+'/page-2.pdf'})

    def test_source_cache_invalidates_on_html_change(self):
        from unittest.mock import patch
        source = 'https://example.com/reports'
        with patch.object(scrape, 'ENABLE_BROWSER_FALLBACK', True), \
             patch.object(scrape, 'browser_attempted_sources', set()), \
             patch.object(scrape, 'source_page_fingerprints', {source:'one'}), \
             patch.object(scrape, 'source_strategies', {source:{'browser_required':False,
                    'html_fingerprint':'one','checked_at':time.time()}}):
            self.assertFalse(scrape.should_use_browser_fallback(source))
            scrape.source_page_fingerprints[source] = 'two'
            self.assertTrue(scrape.should_use_browser_fallback(source))
            self.assertTrue(scrape.should_use_browser_fallback(source, True))

    @unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == 'true', 'Chromium test runs in GitHub verification')
    def test_browser_load_more_and_reuse(self):
        from unittest.mock import patch
        try:
            with shared_browser() as first:
                browser_identity = id(first)
            with patch.object(scrape, 'global_seen_document_urls', set()):
                docs = scrape.browser_click_fallback(self.base+'/browser', set())
            self.assertEqual({r['document_url'] for r in docs},
                {self.base+f'/browser-{i}.pdf' for i in (1,2,3)})
            with shared_browser() as second:
                self.assertEqual(id(second), browser_identity)
        finally:
            close_browser()

    def test_prefetch_overlaps_independent_requests(self):
        def slow(url):
            time.sleep(0.08)
            return url
        items = [{'source_url':str(i)} for i in range(8)]
        start = time.monotonic()
        results = list(prefetched(items, slow, workers=8))
        self.assertEqual([value for item, value in results], [str(i) for i in range(8)])
        self.assertLess(time.monotonic()-start, 0.5)

    def test_full_diff_and_retry_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'sources.csv').write_text('source_url\n'+self.base+'/source\n')
            with (path/'known_documents.csv').open('w') as f:
                writer = csv.DictWriter(f, fieldnames=scrape.KNOWN_DOCUMENTS_FIELDNAMES)
                writer.writeheader()
                writer.writerow({'first_seen_date':'2026-01-01', 'company':self.base+'/source',
                    'document_title':'Known report', 'document_url':self.base+'/known.pdf', 'source_run_mode':'seed'})
            env = dict(os.environ, RUN_MODE='full', TARGET_URL_FILE='sources.csv',
                ENABLE_BROWSER_FALLBACK='false', RETRY_FAILED_URLS='false', HTTP_WORKERS='2',
                PDF_METADATA_RECENCY_DAYS='60')
            before = Handler.counts.get('/known.pdf', 0)
            for _ in range(2):
                run = subprocess.run([sys.executable, str(Path(scrape.__file__).resolve())],
                                     cwd=path, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
            self.assertEqual(Handler.counts.get('/known.pdf', 0), before)
            with (path/'diff.csv').open() as f:
                rows = list(csv.DictReader(f))
            urls = {r['document_url'] for r in rows}
            self.assertEqual(urls, {self.base+'/recent.pdf', self.base+'/download.pdf?id=1', self.base+'/download.pdf?id=2'})
            with (path/'known_documents.csv').open() as f:
                known = {r['document_url'] for r in csv.DictReader(f)}
            self.assertNotIn(self.base+'/missing.pdf', known)
            self.assertIn(self.base+'/old.pdf', known)


if __name__ == '__main__':
    unittest.main()
