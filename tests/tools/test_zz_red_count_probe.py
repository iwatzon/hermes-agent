"""RED-LEG DIAGNOSTIC ONLY (never merged): report ESRCH counts for single-file search_files on macOS."""
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.platforms("macos")


def _run(target, task_id, threads, budget):
    from tools.file_tools import _handle_search_files
    deadline = time.monotonic() + budget
    lock = threading.Lock()
    counter = iter(range(10**6))
    done, errs = [0], {}

    def worker():
        while time.monotonic() < deadline:
            with lock:
                i = next(counter)
            raw = _handle_search_files({"pattern": f"needle_{i % 20000:05d}", "path": str(target),
                                        "output_mode": "content", "limit": 3}, task_id=task_id)
            r = json.loads(raw)
            with lock:
                done[0] += 1
                if r.get("error") or not r.get("matches"):
                    k = (r.get("error") or raw[:80])[:80]
                    errs[k] = errs.get(k, 0) + 1
    with ThreadPoolExecutor(threads) as pool:
        for f in [pool.submit(worker) for _ in range(threads)]:
            f.result()
    return done[0], errs


def test_count_probe(tmp_path):
    from tools.terminal_tool_lifecycle import cleanup_vm
    target = tmp_path / "haystack.py"
    target.write_text("".join(f"needle_{i:05d} = {i}\n" for i in range(20000)))
    report = []
    try:
        report.append(("seq", *_run(target, "t-probe", 1, 50)))
        report.append(("conc8", *_run(target, "t-probe", 8, 50)))
        hogs = [subprocess.Popen(["/usr/bin/yes"], stdout=subprocess.DEVNULL) for _ in range((os.cpu_count() or 4) * 2)]
        try:
            report.append(("conc8+hogs", *_run(target, "t-probe", 8, 60)))
        finally:
            for h in hogs:
                h.kill()
                h.wait()
    finally:
        cleanup_vm("t-probe")
    pytest.fail(f"COUNTS {report}")
