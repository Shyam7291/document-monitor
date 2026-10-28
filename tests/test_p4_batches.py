"""Plan, worker handoff and real publication checks without external scraping."""
import csv
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import p4_batches as batches
import scrape

REPO = Path(batches.__file__).parent


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path.cwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)
        self.env = patch.dict(os.environ, {'GITHUB_RUN_ID': 'test-run'})
        self.env.start()
        self.addCleanup(self.env.stop)
        for i, filename in enumerate(batches.SOURCE_FILES):
            rows = [{'source_url': f'https://fixture.test/source/{i}/{j}'} for j in range(3)]
            if i == 1:
                rows += [{'source_url': 'fallback:https://fixture.test/source/0/0'},
                         {'source_url': ''}]
            batches.write_csv(filename, ['source_url'], rows)
        self.doc = {'company': 'https://fixture.test/source/0/0',
                    'document_title': 'Prior report', 'document_title_source': 'link_text',
                    'document_url': 'https://fixture.test/old.pdf'}
        self.known = {'first_seen_date': '2026-10-01', 'company': self.doc['company'],
                      'document_title': 'Prior report', 'document_url': self.doc['document_url'],
                      'source_run_mode': 'full'}
        self.diff = {'date': '2026-10-01', 'company': self.doc['company'],
                     'document_title': 'Prior report', 'document_url': self.doc['document_url']}
        batches.write_csv('p4_p5/output_p4_p5.csv', list(self.doc),
                          [self.doc, dict(self.doc, company='https://other.test/',
                                          document_url='https://other.test/old.pdf')])
        batches.write_csv('p4_p5/known_documents_p4_p5.csv', list(self.known), [self.known])
        batches.write_csv('diff_p4_p5.csv', list(self.diff), [self.diff])

    def simulate_completed_batches(self):
        manifest = batches.plan_batches(4)
        for batch, entry in manifest['batches'].items():
            batches.prepare(batch)
            docs = [dict(self.doc, company=row['url'],
                         document_url=f'https://fixture.test/download?id={batch}-{i}')
                    for i, row in enumerate(entry['sources'])]
            batches.write_csv(batches.work_file('output', batch), list(self.doc), docs)
            batches.write_csv(batches.work_file('known', batch), list(self.known), [self.known] + [
                dict(self.known, company=doc['company'], document_url=doc['document_url']) for doc in docs])
            batches.write_csv(batches.work_file('diff', batch), list(self.diff), [self.diff] + [
                dict(self.diff, company=doc['company'], document_url=doc['document_url']) for doc in docs])
            batches.write_csv(batches.work_file('status', batch), scrape.URL_STATUS_FIELDNAMES, [
                {'url': row['url'], 'latest_status_code': '200',
                 'url_file': 'temporary-batch.csv'} for row in entry['sources']])
            batches.write_csv(batches.work_file('issues', batch),
                              ['date', 'run_mode', 'url_file', 'source_url', 'issue_type',
                               'status_code', 'documents_captured', 'error_message'], [])
            summary = {key: '' for key in scrape.RUN_SUMMARY_FIELDNAMES}
            summary.update(total_urls_processed=str(len(docs)), total_documents_captured=str(len(docs)),
                           new_diff_records=str(len(docs)), run_mode='full', date='2026-10-28')
            batches.write_csv(batches.work_file('summary', batch), list(summary), [summary])
            batches.finish(batch)
            dest = Path('shard_outputs') / batch
            dest.mkdir(parents=True)
            for kind in batches.PREFIXES:
                shutil.copy(batches.work_file(kind, batch), dest)
            shutil.copy(f'completion_p4_p5_{batch}.json', dest)
            Path(batches.strategy_file(batch)).write_text(json.dumps({
                row['url']: {'browser_required': True} for row in entry['sources']}))
            shutil.copy(batches.strategy_file(batch), dest)
        return manifest

    def test_plan_assigns_all_sources_once_and_preserves_forced_browser(self):
        manifest = batches.plan_batches(24)
        self.assertEqual(manifest['source_count'], 18)
        assigned = [row for entry in manifest['batches'].values() for row in entry['sources']]
        self.assertEqual(len({row['url'] for row in assigned}), 18)
        self.assertTrue(next(row for row in assigned if row['url'].endswith('/0/0'))['forced'])
        self.assertLessEqual(max(len(x['sources']) for x in manifest['batches'].values()) -
                             min(len(x['sources']) for x in manifest['batches'].values()), 1)
        self.assertEqual(manifest, batches.plan_batches(24))

    def test_prepare_rejects_changed_batch_and_keeps_only_own_previous_output(self):
        manifest = batches.plan_batches(4)
        batches.prepare('001')
        _, rows = batches.read_csv(batches.work_file('output', '001'))
        self.assertEqual(rows, [self.doc])
        path = batches.PLAN.parent / 'documents_p4_p5_batch_001.csv'
        path.write_text('source_url\nhttps://wrong.test/\n')
        with self.assertRaisesRegex(ValueError, 'differs from plan'):
            batches.prepare('001')

    def test_real_merge_retains_history_and_restores_source_ownership(self):
        manifest = self.simulate_completed_batches()
        master = Path('run_summary_master.csv')
        master.write_text('unchanged P1 P2 P3 master\n')
        workflow = (REPO / '.github/workflows/run_p4_p5_parallel.yml').read_text()
        code = textwrap.dedent(re.search(r"python - <<'PY'\n(.*?)\n          PY", workflow, re.S)[1])
        env = dict(os.environ, PYTHONPATH=str(REPO), P4_P5_BATCH_MANIFEST=str(batches.PLAN))
        run = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(master.read_text(), 'unchanged P1 P2 P3 master\n')
        _, rows = batches.read_csv('p4_p5/output_p4_p5.csv')
        self.assertEqual(len(rows), 19)
        self.assertEqual(len({row['document_url'] for row in rows}), 19)
        for path in ['diff_p4_p5.csv', 'p4_p5/known_documents_p4_p5.csv']:
            _, rows = batches.read_csv(path)
            self.assertEqual(len(rows), 19)
            self.assertIn('https://fixture.test/old.pdf', {row['document_url'] for row in rows})
        _, rows = batches.read_csv('p4_p5/run_summary_p4_p5.csv')
        self.assertEqual(rows[-1]['total_urls_processed'], '18')
        _, rows = batches.read_csv('p4_p5/url_status_p4_p5.csv')
        self.assertEqual(len(rows), 18)
        self.assertTrue(all(row['url_file'] in batches.SOURCE_FILES for row in rows))
        for filename in batches.SOURCE_FILES:
            path = Path(f'source_strategies_{Path(filename).stem}.json')
            self.assertEqual(len(json.loads(path.read_text())), 3)

    def test_missing_failed_or_stale_batch_cannot_publish(self):
        self.simulate_completed_batches()
        marker = Path('shard_outputs/001/completion_p4_p5_001.json')
        saved = marker.read_text()
        marker.unlink()
        with self.assertRaisesRegex(ValueError, 'Expected 4'):
            batches.validate_results(batches.PLAN)
        marker.write_text(saved)
        data = json.loads(saved)
        data['run_id'] = 'another-run'
        marker.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            batches.validate_results(batches.PLAN)
        marker.write_text(saved)
        summary_path = Path('shard_outputs/001/run_summary_p4_p5_001_work.csv')
        fields, rows = batches.read_csv(summary_path)
        rows[0]['total_urls_processed'] = '0'
        batches.write_csv(summary_path, fields, rows)
        with self.assertRaisesRegex(ValueError, 'source count'):
            batches.validate_results(batches.PLAN)


if __name__ == '__main__':
    unittest.main()
