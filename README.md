# Document monitor

Captures PDF links from configured company pages and appends **unseen links with
recent internal PDF modified dates** to `diff.csv` (or `diff_p4_p5.csv`). Existing
CSV column formats are unchanged.

## Detection rules

1. The first successful capture of a new source establishes its baseline in
   `known_documents.csv`; it does not generate diff records.
2. Subsequent captures skip URLs already in known history or diff before making
   PDF requests. A replacement PDF at the same URL is intentionally skipped.
3. An unseen URL qualifies only when the PDF's internal **`/ModDate` property** is
   within the last `PDF_METADATA_RECENCY_DAYS` days (default **60**), and is not in
   the future. HTTP `Last-Modified`, `/CreationDate`, URL years and filenames are
   never substitutes. The recent-date check is always enforced for full runs.
4. Missing, invalid, encrypted, oversized or unreadable metadata does not qualify.
   These candidates remain in captured output, are logged as
   `PDF_MODIFIED_DATE_UNKNOWN` in capture issues, and are not marked permanently
   known, so the next run can retry them. Confirmed old PDFs are remembered to
   avoid downloading them again.
5. Document identity preserves path case and the query string, including download
   IDs. Scheme/host case and fragments are normalized. Legacy canonical filename
   tables no longer participate in diff decisions, so report years cannot collide.

## Speed and capture coverage

- Eight bounded HTTP workers prefetch source pages while a single collector parses
  pages and writes CSVs. This HTTP pass finishes before browser jobs begin, so a
  dynamic page cannot block all remaining source captures. Connections are pooled
  per thread; requests to the same
  host are spaced by `SLEEP_SECONDS` (default 0.5 in production workflows).
- Only unseen candidates have their PDF properties checked, using four workers.
  A seekable HTTP range reader caches 64 KB blocks and lets the PDF parser locate
  the metadata through the PDF object structure. It validates byte offsets, total
  size and validators; unsupported or unreliable ranges fall back to a bounded
  full read. It does not guess metadata by searching the first/last bytes.
- Range reads are limited to 32 requests / approximately 8 MB. Full reads respect
  `PDF_METADATA_MAX_BYTES` (100 MB by default), including responses without a
  Content-Length header. A server validator is used solely to keep byte ranges
  consistent, never to determine PDF recency.
- Static extraction reads anchors, lazy/data attributes, embeds, inline handlers,
  PDF URLs serialized in scripts/JSON, iframes and explicit same-host next links.
  Navigation links are not subjected to expensive document-title analysis when
  raw-link diagnostics are disabled (the default).
- Browser fallback remains enabled. HTTP **404/410** skips browser processing and
  redundant HTTP retries, including forced entries. HTTP 403 is access denied,
  rather than proof of a removed URL, so it can receive a bounded browser attempt.
  Browser navigation that itself returns 404/410 also stops.
- Each shard runs **two persistent browser worker processes**, for up to twelve
  across the six P4/P5 shards. Each reuses Chromium with an isolated context per
  source. Images,
  fonts and media are blocked while scripts/XHR remain enabled. Download controls
  are clicked once while observing popup, download and navigation events together.
  Explicit load-more/next controls are followed, and JSON/PDF network responses
  are scanned alongside the rendered content.
- The parent enforces `BROWSER_SOURCE_SECONDS` (**30 seconds** in production).
  On expiry it terminates the worker and its Chromium process group, retains
  incremental PDF discoveries, reports incomplete capture, and replaces the worker.
  This covers blocked browser calls and cleanup, unlike checks between operations.
  A timed-out page may need a source-specific extractor or a longer configured budget.
- Country/language selectors and navigation/footer controls do not trigger browser
  processing by themselves. Nor do shared PDFs already captured on another source.
  Document year filters, tabs/cards, load-more controls, forced entries, hashes,
  known browser-dependent sources and zero-document captures still qualify.
  PDF iframe links already captured are not downloaded again as HTML.
- Browser strategy JSON files are maintained per input group. If a browser found
  no additional links on an otherwise populated source, an identical HTML response
  can use the HTTP path for seven days. Changed HTML, forced `Fallback/` entries,
  and the weekly recheck invalidate that shortcut. Sources requiring a browser
  keep using it. A source has at most one browser attempt per run.
- P4/P5 now enables this selective browser fallback. Its first run may take longer
  than HTTP-only scraping while strategies are learned. Network/anti-bot failures
  still require source-specific investigation; there is no guaranteed overall
  runtime or universal completeness claim.

Pagination limits (100 linked/browser pages), control limits (100 per stage), and
the parent-enforced browser source budget produce
explicit capture issues. These limits prevent one site from consuming an entire
run; an issue means capture needs review. Sites with custom APIs, unusual controls,
or documents appearing only in remote data without a changed HTML shell can need
a source-specific extractor or a forced browser entry. The weekly audit reduces
but does not eliminate delayed discovery on those sites.

P1/P2/P3 retain their shared production lock. P4/P5 uses an independent lock and
writes `p4_p5/run_summary_p4_p5.csv`, so it cannot queue P1 behind its long run.
On first use the P4/P5 summary imports only its historical rows from the master,
without rewriting that master. Workflows refuse merges with missing shards,
preserve query IDs and fail if result publishing fails. Per-source browser timings
are written to `browser_timings_*.jsonl`; P4/P5 uploads these diagnostics even on
failure. Diagnostics never enter production merges as completed shard outputs.
Logs separately time HTTP, browser and metadata phases. Learned browser strategies
are saved after every result rather than only at the end of a long run.
Dependencies are pinned in `requirements.txt`. No historical CSV data is rewritten
by this code change. `scrape_dev.py` is retained as legacy code; the manual test
workflow now exercises `scrape.py` in test mode.

## Run and verify

```sh
pip install -r requirements.txt
playwright install chromium
RUN_MODE=test TARGET_URL_FILE=test_urls.csv ENABLE_BROWSER_FALLBACK=true python scrape.py
python -m unittest discover -s tests -v
RUN_BROWSER_TESTS=true python -m unittest discover -s tests -v
```

The verification workflow runs the browser-enabled suite. Tests use a local HTTP
server and generated PDFs: known-link skipping, old versus recent modified dates,
missing dates, timezone offsets, malformed/no-range servers, transfer limits,
query IDs, JSON links, pagination, source-cache invalidation and browser reuse.
Additional regressions cover overlapping workers, actual timeout/termination with
partial discoveries retained, worker crashes, 404/410 handling, irrelevant menus,
shared PDFs and the complete deferred-browser-to-modified-date pipeline.
Logs include PDF bytes transferred and total elapsed run time for measuring real
improvements. The local 4 MB fixture required about 132 KB of PDF transfer; this is
a controlled transfer-size result, not a prediction for total production runtime.
