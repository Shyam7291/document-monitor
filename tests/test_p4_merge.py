"""Exercise the real publication script against miniature shard artifacts."""
import csv
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape


class MergeIsolationTests(unittest.TestCase):
    def test_p4_publication_preserves_master_and_download_ids(self):
        workflow = Path(scrape.__file__).parent/'.github/workflows/run_p4_p5_parallel.yml'
        code = textwrap.dedent(re.search(r"python - <<'PY'\n(.*?)\n          PY", workflow.read_text(), re.S)[1])

        def write(path, fields, rows):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = {key:'1' for key in scrape.RUN_SUMMARY_FIELDNAMES}
            summary.update(date='2026-10-28 10:00:00', run_mode='full', url_file='p4_p5_total',
                           browser_fallback_enabled='True')
            write(root/'run_summary_master.csv', scrape.RUN_SUMMARY_FIELDNAMES,
                  [dict(summary, url_file='documents_p1.csv'), summary])
            original = (root/'run_summary_master.csv').read_bytes()
            for i in range(1, 7):
                source = f'https://fixture.test/source/{i}'
                write(root/f'p4_p5/documents_p4_p5_{i:02}.csv', ['source_url'], [{'source_url':source}])
                output = {'company':source, 'document_title':'Annual report',
                    'document_title_source':'link_text', 'document_url':f'https://fixture.test/download?id={i}'}
                write(root/f'shard_outputs/shard-{i}/output_p4_p5_{i:02}_work.csv', list(output), [output])
                write(root/f'shard_outputs/shard-{i}/run_summary_p4_p5_{i:02}_work.csv',
                      scrape.RUN_SUMMARY_FIELDNAMES, [summary])
            run = subprocess.run([sys.executable, '-c', code], cwd=root, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
            self.assertEqual((root/'run_summary_master.csv').read_bytes(), original)
            with (root/'p4_p5/run_summary_p4_p5.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)  # one P4 historical row and one new aggregate
            self.assertEqual(rows[-1]['total_urls_processed'], '6')
            with (root/'p4_p5/output_p4_p5.csv').open() as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 6)


if __name__ == '__main__':
    unittest.main()
