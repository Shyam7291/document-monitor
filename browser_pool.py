"""Persistent browser processes with a parent-enforced per-source deadline.

Each child owns its Playwright connection. The collector alone changes CSV/history
state. Incremental document events survive a hung browser being terminated.
"""
import multiprocessing as mp
import os
import signal
import time
from collections import deque
from multiprocessing.connection import wait


def capture_worker(connection):
    import scrape
    from monitor_io import close_browser

    try:
        while True:
            job = connection.recv()
            if job is None:
                return
            url = job["source_url"]
            scrape.global_seen_document_urls = set(job["existing_keys"])
            scrape.issue_rows = []
            scrape.browser_attempted_sources = set()
            scrape.source_strategies = {}
            scrape.source_page_fingerprints = {url: job.get("fingerprint", "")}
            scrape.BROWSER_PROGRESS_CALLBACK = lambda doc: connection.send(("document", doc))
            os.environ["BROWSER_SOURCE_SECONDS"] = str(job["timeout"])
            try:
                scrape.browser_click_capture(url, set(job["existing_keys"]))
                result = {"issues": scrape.issue_rows,
                          "strategy": scrape.source_strategies.get(url),
                          "incomplete": bool(scrape.issue_rows)}
            except Exception as error:
                result = {"issues": [], "strategy": None, "error": str(error)}
            finally:
                scrape.BROWSER_PROGRESS_CALLBACK = None
            connection.send(("done", result))
    finally:
        close_browser()


def _bootstrap(connection, worker):
    # Chromium descendants belong to this group, so a stuck child cannot leak them.
    if os.name == "posix":
        os.setsid()
    worker(connection)


def _stop(slot):
    process = slot["process"]
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            if process.is_alive():
                process.terminate()
    if process.is_alive():
        if os.name != "posix":
            process.terminate()
        process.join(timeout=0.5)
        if process.is_alive():
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    process.kill()
            else:
                process.kill()
            process.join(timeout=0.5)
    slot["connection"].close()
    process.close()


def browser_events(jobs, workers=2, timeout=30, worker=capture_worker):
    """Yield (event, job, data), reusing healthy workers and replacing hung ones.

    Timeout includes worker startup, browser navigation, JS, cleanup and callbacks.
    Unlike future.result(timeout), expiry actually terminates the worker process.
    """
    pending = deque(jobs)
    if not pending:
        return
    workers, timeout = max(1, int(workers)), max(0.1, float(timeout))
    context = mp.get_context("spawn")
    slots = []

    def start():
        parent, child = context.Pipe()
        process = context.Process(target=_bootstrap, args=(child, worker))
        process.start()
        child.close()
        return {"process": process, "connection": parent, "job": None}

    try:
        slots = [start() for _ in range(min(workers, len(pending)))]
        while pending or any(slot["job"] is not None for slot in slots):
            for slot in slots:
                if slot["job"] is None and pending:
                    if not slot["process"].is_alive():
                        _stop(slot)
                        slot.clear()
                        slot.update(start())
                    job = dict(pending.popleft(), timeout=timeout)
                    slot.update(job=job, started=time.monotonic(), documents=0)
                    slot["connection"].send(job)
            active = [slot for slot in slots if slot["job"] is not None]
            remaining = min(timeout-(time.monotonic()-slot["started"]) for slot in active)
            ready = wait([slot["connection"] for slot in active], timeout=max(0, min(0.05, remaining)))
            for slot in active:
                connection = slot["connection"]
                failed = None
                # Drain incremental discoveries before considering timeout/crash.
                for _ in range(100):
                    if connection not in ready or not connection.poll():
                        break
                    try:
                        event, data = connection.recv()
                    except (EOFError, OSError):
                        failed = "worker_exited"
                        break
                    if event == "document":
                        slot["documents"] += 1
                        yield event, slot["job"], data
                    elif event == "done":
                        yield "done", slot["job"], dict(data,
                            outcome="error" if data.get("error") else "incomplete" if data.get("incomplete") else "complete",
                            seconds=time.monotonic()-slot["started"],
                            documents=slot["documents"])
                        slot["job"] = None
                        break
                if slot["job"] is None:
                    continue
                if time.monotonic()-slot["started"] >= timeout:
                    failed = "timeout"
                elif not slot["process"].is_alive():
                    failed = failed or "worker_exited"
                if failed:
                    job = slot["job"]
                    result = {"outcome": failed, "seconds": time.monotonic()-slot["started"],
                              "documents": slot["documents"], "issues": [], "strategy": None}
                    _stop(slot)
                    replacement = start() if pending else None
                    if replacement is None:
                        slots.remove(slot)
                    else:
                        slot.clear()
                        slot.update(replacement)
                    yield "done", job, result
    finally:
        for slot in slots:
            _stop(slot)
