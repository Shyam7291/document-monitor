"""Bounded network I/O and PDF property reading (never HTTP date guessing)."""
import atexit
import io
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import requests
from PyPDF2 import PdfReader


def url_key(url):
    """Preserve document IDs, path case and query; fragments do not identify files."""
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       parts.query, ""))


def embedded_pdf_links(text):
    """Read URLs embedded in JSON/script attributes without evaluating JavaScript."""
    text = (text or "").replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    text = text.replace("\\u0026", "&")
    quoted = re.findall(r'''["']([^\s<>"'`\\]+?\.pdf(?:[?#][^\s<>"'`\\]*)?)["']''', text, re.I)
    absolute = re.findall(r'''https?://[^\s<>"'`\\]*?\.pdf(?:[?#][^\s<>"'`\\]*)?''', text, re.I)
    return set(quoted + absolute)


def settle(page, milliseconds=2500):
    """Return early when traffic settles; retain a bounded wait on busy pages."""
    try:
        page.wait_for_load_state("networkidle", timeout=milliseconds)
    except Exception:
        pass


def click_once(page, element):
    """Observe popup/download/navigation together, issuing just one click."""
    before = page.url
    downloads, popups = [], []
    on_download = lambda download: downloads.append(download.url)
    on_popup = lambda popup: popups.append(popup)
    page.on("download", on_download)
    page.on("popup", on_popup)
    try:
        element.click(timeout=4000)
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if downloads or popups or page.url != before:
                break
            page.wait_for_timeout(100)
        if downloads:
            return downloads[0]
        if popups:
            popup = popups[0]
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=10000)
                return popup.url
            finally:
                popup.close()
        if page.url != before:
            return page.url
        return ""
    finally:
        page.remove_listener("download", on_download)
        page.remove_listener("popup", on_popup)
        if not page.is_closed() and page.url != before:
            page.goto(before, wait_until="domcontentloaded", timeout=30000)
            settle(page, 1500)


def paginate_browser(page, scan, on_limit, limit=100):
    """Follow explicit next/load-more controls; report caps or stalled controls."""
    seen = set()
    for step in range(limit):
        scan(page)
        fingerprint = (page.url, page.locator("body").inner_text(timeout=3000),
                       tuple(page.locator("a").evaluate_all("els => els.map(el => el.href)")))
        if fingerprint in seen:
            on_limit("Pagination repeated a page before exhaustion")
            return
        seen.add(fingerprint)
        next_control = page.locator(
            "a[rel='next'], button[aria-label='Next'], a[aria-label='Next']"
        ).or_(page.get_by_role("button", name=re.compile(r"^(load more|show more|next(?: page)?|older(?: reports)?)$", re.I))).or_(
            page.get_by_role("link", name=re.compile(r"^(load more|show more|next(?: page)?|older(?: reports)?)$", re.I)))
        target = None
        for i in range(next_control.count()):
            candidate = next_control.nth(i)
            if candidate.is_visible() and candidate.is_enabled() and candidate.get_attribute("aria-disabled") != "true":
                target = candidate
                break
        if target is None:
            return
        before = {"url": fingerprint[0], "text": fingerprint[1], "hrefs": list(fingerprint[2])}
        target.click(timeout=5000)
        try:
            page.wait_for_function("""before => location.href !== before.url ||
                document.body.innerText !== before.text ||
                JSON.stringify(Array.from(document.querySelectorAll('a'), a => a.href)) !== JSON.stringify(before.hrefs)
                """, arg=before, timeout=5000)
        except Exception:
            on_limit("Pagination control did not produce new content")
            return
        settle(page)
    scan(page)
    on_limit(f"Pagination reached {limit} pages; needs source-specific review")


def scroll_collect(page, scan, on_limit, deadline):
    """Trigger lazy loading, stopping after stable document height/link counts."""
    if not page.evaluate("document.documentElement.scrollHeight > innerHeight"):
        return
    previous, stable = None, 0
    for _ in range(30):
        if time.monotonic() > deadline:
            raise BrowserBudgetExceeded("Browser time budget exceeded while scrolling")
        page.evaluate("scrollTo(0, document.documentElement.scrollHeight)")
        settle(page, 1500)
        scan(page)
        current = page.evaluate("[document.documentElement.scrollHeight, document.querySelectorAll('a').length]")
        stable = stable + 1 if current == previous else 0
        if stable >= 2:
            page.evaluate("scrollTo(0, 0)")
            return
        previous = current
    on_limit("Lazy-loading scan reached 30 scrolls; capture needs review")


def pdf_date(value):
    """Parse PDF partial dates and offsets into UTC, without losing timezone data."""
    match = re.fullmatch(
        r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
        r"(?:(Z)|([+-])(\d{2})'?(\d{2})?'?)?", str(value or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second, z, sign, oh, om = match.groups()
    try:
        offset = timedelta(hours=int(oh or 0), minutes=int(om or 0))
        if int(oh or 0) > 23 or int(om or 0) > 59:
            return None
        if sign == "-":
            offset = -offset
        return datetime(int(year), int(month or 1), int(day or 1),
                        int(hour or 0), int(minute or 0), int(second or 0),
                        tzinfo=timezone(offset)).astimezone(timezone.utc)
    except ValueError:
        return None


def recent_modified(value, days, now=None):
    now = now or datetime.now(timezone.utc)
    return value is not None and now - timedelta(days=days) <= value <= now


_local = threading.local()
_domain_lock = threading.Lock()
_domain_next = {}


def session():
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


def paced_get(url, *, interval=None, **kwargs):
    """Space same-host requests, allowing unrelated hosts to overlap."""
    host = urlsplit(url).netloc.lower()
    interval = max(0, float(os.environ.get("SLEEP_SECONDS", "0.5"))) if interval is None else interval
    with _domain_lock:
        now = time.monotonic()
        slot = max(now, _domain_next.get(host, now))
        _domain_next[host] = slot + interval
    delay = slot - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    return session().get(url, **kwargs)


def download_candidate(url):
    return bool(re.search(r"/(?:download|downloadfile)(?:/|$)", urlsplit(url).path, re.I))


@lru_cache(maxsize=4096)
def confirms_pdf(url):
    """Check an extensionless download's headers without consuming its body."""
    try:
        with paced_get(url, stream=True, timeout=(5, 10)) as response:
            return response.ok and ("application/pdf" in response.headers.get("Content-Type", "").lower()
                or ".pdf" in response.headers.get("Content-Disposition", "").lower())
    except requests.RequestException:
        return False


def prefetched(items, fetch, workers=8, window=16):
    """Bounded look-ahead: parsing and CSV mutation remain on the main thread."""
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = []
        for _ in range(window):
            item = next(iterator, None)
            if item is None:
                break
            pending.append((item, pool.submit(fetch, item["source_url"])))
        while pending:
            item, future = pending.pop(0)
            result = future.result()
            following = next(iterator, None)
            if following is not None:
                pending.append((following, pool.submit(fetch, following["source_url"])))
            yield item, result


class RangeUnavailable(IOError):
    pass


class BrowserBudgetExceeded(Exception):
    pass


class PDFRangeReader(io.RawIOBase):
    """Seekable cached HTTP ranges for PdfReader; validate every returned range."""
    block_size = 65536

    def __init__(self, url, headers, max_bytes, max_requests=32, range_budget=8*1024*1024):
        super().__init__()
        self.url, self.headers, self.max_bytes = url, dict(headers), max_bytes
        self.headers["Accept-Encoding"] = "identity"
        self.max_requests, self.range_budget = max_requests, range_budget
        self.position = self.requests = self.transferred = 0
        self.blocks = {}
        self.size = None
        self.validator = None
        self.full = None
        self._fetch(0)

    def _bounded(self, response, limit):
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > limit:
                raise RangeUnavailable("PDF response exceeds byte limit")
        return bytes(data)

    def _fetch(self, start):
        if self.requests >= self.max_requests or self.transferred >= self.range_budget:
            raise RangeUnavailable("PDF range budget exhausted")
        end = start + self.block_size - 1
        if self.size is not None:
            end = min(end, self.size - 1)
        headers = dict(self.headers, Range=f"bytes={start}-{end}")
        if self.validator:
            headers["If-Range"] = self.validator
        self.requests += 1
        with paced_get(self.url, headers=headers, timeout=(10, 25), stream=True) as response:
            response.raise_for_status()
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise RangeUnavailable("Encoded range response")
            if response.status_code == 200:
                if self.size is not None:
                    raise RangeUnavailable("PDF changed or server stopped honoring ranges")
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_bytes:
                    raise RangeUnavailable("PDF exceeds configured size limit")
                self.full = self._bounded(response, self.max_bytes)
                self.size = len(self.full)
                self.transferred += self.size
                return
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                 response.headers.get("Content-Range", ""))
            if response.status_code != 206 or not match:
                raise RangeUnavailable("Invalid partial response")
            actual_start, actual_end, size = map(int, match.groups())
            if actual_start != start or actual_end != min(end, size-1) or size <= 0:
                raise RangeUnavailable("Mismatched partial response")
            if size > self.max_bytes or (self.size is not None and size != self.size):
                raise RangeUnavailable("PDF size changed or exceeds limit")
            validator = response.headers.get("ETag")
            if validator and validator.startswith("W/"):
                validator = None
            validator = validator or response.headers.get("Last-Modified")
            if self.validator and validator != self.validator:
                raise RangeUnavailable("PDF validator changed")
            # Never combine multiple unvalidated representations of a changing file.
            if self.requests > 1 and not self.validator:
                raise RangeUnavailable("No stable validator for partial PDF")
            self.validator, self.size = validator, size
            data = self._bounded(response, actual_end-actual_start+1)
            if len(data) != actual_end-actual_start+1:
                raise RangeUnavailable("Truncated partial response")
            self.transferred += len(data)
            self.blocks[start] = data

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else (self.position if whence == 1 else self.size) + offset
        if target < 0:
            raise ValueError("Negative seek")
        self.position = target
        return target

    def read(self, size=-1):
        end = self.size if size < 0 else min(self.position + size, self.size)
        if self.full is not None:
            result = self.full[self.position:end]
            self.position += len(result)
            return result
        result = bytearray()
        while self.position < end:
            start = (self.position // self.block_size) * self.block_size
            if start not in self.blocks:
                self._fetch(start)
            data = self.blocks[start]
            offset = self.position-start
            piece = data[offset:offset+end-self.position]
            if not piece:
                raise RangeUnavailable("Empty partial PDF read")
            result.extend(piece)
            self.position += len(piece)
        return bytes(result)


def read_modified(url, headers, max_bytes):
    """Return internal /ModDate only; missing/failed dates never imply recency."""
    def parse(stream):
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            return None, "encrypted"
        metadata = reader.metadata
        value = pdf_date(metadata.get("/ModDate")) if metadata else None
        return value, "ok" if value else "missing_modified_date"

    try:
        stream = PDFRangeReader(url, headers, max_bytes)
        try:
            result = parse(stream)
            print(f"PDF PROPERTY I/O → bytes={stream.transferred} total={stream.size} requests={stream.requests}")
            return result
        finally:
            stream.close()
    except Exception as range_error:
        # Malformed ranges, unsupported servers and PDFs requiring too many seeks
        # get one bounded sequential read; never inspect raw bytes using regex.
        try:
            with paced_get(url, headers=headers, timeout=(10, 30), stream=True) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    return None, "too_large"
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        return None, "too_large"
                return parse(io.BytesIO(data))
        except Exception as error:
            return None, f"unreadable:{type(error).__name__}"


_playwright = _browser = None


def close_browser():
    global _playwright, _browser
    if _browser is not None:
        _browser.close()
    if _playwright is not None:
        _playwright.stop()
    _playwright = _browser = None


@contextmanager
def shared_browser():
    """One Chromium per scraper process, isolated contexts per source."""
    global _playwright, _browser
    if _browser is None or not _browser.is_connected():
        from playwright.sync_api import sync_playwright
        close_browser()
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
    yield _browser


atexit.register(close_browser)
