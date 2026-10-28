import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_pool import browser_events


def fixture_worker(connection):
    while True:
        job = connection.recv()
        if job is None:
            return
        if job.get("crash"):
            os._exit(3)
        connection.send(("document", {"document_url": job["source_url"] + "/one.pdf"}))
        start = time.monotonic()
        time.sleep(job.get("delay", 0))
        connection.send(("done", {"issues": [], "strategy": None,
            "started": start, "finished": time.monotonic()}))


class BrowserPoolTests(unittest.TestCase):
    def test_parallel_tasks_overlap(self):
        jobs = [{"source_url":f"https://fixture/{i}", "delay":0.4} for i in range(4)]
        events = list(browser_events(jobs, workers=2, timeout=5, worker=fixture_worker))
        completed = [data for event, job, data in events if event == "done"]
        self.assertEqual(len(completed), 4)
        self.assertTrue(all(row["outcome"] == "complete" for row in completed))
        self.assertTrue(any(a["started"] < b["finished"] and b["started"] < a["finished"]
            for i, a in enumerate(completed) for b in completed[i+1:]))

    def test_hung_source_is_killed_partial_docs_survive_and_next_runs(self):
        jobs = [{"source_url":"https://fixture/hung", "delay":30},
                {"source_url":"https://fixture/next", "delay":0}]
        start = time.monotonic()
        events = list(browser_events(jobs, workers=1, timeout=1.5, worker=fixture_worker))
        self.assertLess(time.monotonic()-start, 6)
        outcomes = [data["outcome"] for event, job, data in events if event == "done"]
        self.assertEqual(outcomes, ["timeout", "complete"])
        discovered = [data["document_url"] for event, job, data in events if event == "document"]
        self.assertIn("https://fixture/hung/one.pdf", discovered)
        self.assertIn("https://fixture/next/one.pdf", discovered)

    def test_crashed_worker_is_replaced(self):
        jobs = [{"source_url":"https://fixture/crash", "crash":True},
                {"source_url":"https://fixture/next"}]
        events = list(browser_events(jobs, workers=1, timeout=3, worker=fixture_worker))
        self.assertEqual([data["outcome"] for event, job, data in events if event == "done"],
                         ["worker_exited", "complete"])


if __name__ == "__main__":
    unittest.main()
