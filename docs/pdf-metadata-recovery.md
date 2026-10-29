# Separate PDF metadata recovery

`recover_pdf_metadata.py` retries only `PDF_MODIFIED_DATE_UNKNOWN` records from a
capture-issues CSV. It does not scrape source pages for new documents, alter the
normal scraper or write production known/diff CSVs.

Install `requirements-metadata.txt`, then run:

```bash
python recover_pdf_metadata.py plan --issues p4_p5/capture_issues_p4_p5.csv --shards 6
python recover_pdf_metadata.py run --shard 01 --output metadata_shards/01
# Run the other five queue shards in their own processes or Actions jobs.
python recover_pdf_metadata.py consolidate --input metadata_shards --output metadata_recovery_results
```

The **Recover failed PDF metadata** Actions workflow performs these steps with
six parallel jobs. It is manually triggered and does not start on routine
scraper runs. Each job has four persistent metadata workers and a 45-second
parent watchdog per document. Requests and parsers that exceed that deadline
are terminated. Results are checkpointed after each PDF and uploaded even if a
job fails; consolidation refuses missing or mismatched completed shards.

The immutable queue checks each document URL once across all six jobs and keeps
its source-page associations. Query strings and case are preserved. HTTP ranges
are tried first; a bounded full read is used where necessary, up to 100 MiB. When
available, full response bytes are reused for the second parser rather than
downloaded again. A public source-page session and referrer are tried after a
401/403. No authentication, browser rendering or TLS-verification bypass is used.

Both parsers include support for PDFs encrypted with an empty user password;
password-protected PDFs remain unresolved. The workflow installs the AES crypto
dependency as part of the pinned recovery requirements.

Outputs in the `pdf-metadata-recovery-report` artifact:

- `all_results.csv`: every unique URL, status, raw `/ModDate`, parsed date,
  creation date, XMP date when checked, parser, HTTP status and source pages.
- `recovered_internal_dates.csv`: PDFs with a usable internal `/ModDate`.
- `recovered_recent_pdfs.csv`: recovered `/ModDate` within 60 days of the
  planning timestamp, excluding future dates. This is a recovery report, not an
  automatic addition to production diff history.
- `unresolved.csv`: HTTP, parsing, size, timeout, password and missing-date cases.
- `xmp_only.csv`: unresolved regular dates for which XMP ModifyDate was available.
- `summary.json`: counts and the fixed timestamp used for recency filtering.

XMP ModifyDate, CreationDate, HTTP Last-Modified and website dates never replace
`/ModDate`. A successful metadata read is not proof that the publisher set the
date correctly. Non-PDF responses may be HTML error/challenge pages; they do not
prove the originally linked PDF lacks metadata.

To resume a recovery snapshot locally, rerun a queue with
`--resume path/to/prior/results.csv`. Only successful regular dates are reused;
unresolved URLs are retried once in that invocation. The caller must use this
option for the same recovery snapshot, rather than as a permanent URL cache.
