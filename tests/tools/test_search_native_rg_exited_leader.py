"""search_files on a single file survives rg exiting before its group is looked up (#123930).

macOS answers ``getpgid`` with ESRCH for a child that is already tearing down, even
while ``poll()`` still reports it running. A content search of one small file is the
fastest rg run there is, so rg can be exiting before the native lane looks up its
process group. When the lane read the group back with ``getpgid`` instead of recording
rg's own pid (``start_new_session`` makes them equal), the cleanup had no group to fall
back on and ``[Errno 3] No such process`` replaced the matches rg had already produced.

This drives the real handler on the real native rg lane, no mocks, for a fixed time
budget: one search at a time, and several at once from a busy process (the desktop
case, where a search thread waits on the GIL after ``Popen`` while rg finishes). The
race only exists on a real macOS kernel, so the test only runs there.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.platforms("macos")

_BUDGET_S = 45.0


def _hold_the_gil(stop: threading.Event) -> None:
    while not stop.is_set():
        pass


@pytest.mark.parametrize("threads, spinners", [(1, 0), (8, 0), (8, 1)],
                         ids=["sequential", "concurrent", "concurrent-busy"])
def test_single_file_content_search_never_loses_its_matches_to_esrch(tmp_path, threads, spinners):
    from tools.file_tools import _get_file_ops, _handle_search_files
    from tools.terminal_tool_lifecycle import cleanup_vm

    target = tmp_path / "haystack.py"
    target.write_text("".join(f"needle_{i:05d} = {i}\n" for i in range(20000)))
    task_id = f"t-native-rg-exited-leader-{threads}-{spinners}"
    ops = _get_file_ops(task_id)
    stop = threading.Event()
    deadline = time.monotonic() + _BUDGET_S
    counter = iter(range(20000))
    lock = threading.Lock()
    failures, done = [], [0]

    def worker() -> None:
        while time.monotonic() < deadline:
            with lock:
                i = next(counter)
            # A distinct pattern per call: the repeated-search guard never engages.
            raw = _handle_search_files({"pattern": f"needle_{i:05d}", "path": str(target),
                                        "output_mode": "content", "limit": 3}, task_id=task_id)
            result = json.loads(raw)
            with lock:
                done[0] += 1
                if result.get("error") or not result.get("matches"):
                    failures.append((i, result.get("error") or raw[:200]))

    spin = [threading.Thread(target=_hold_the_gil, args=(stop,), daemon=True) for _ in range(spinners)]
    try:
        assert ops._native_read_enabled() and ops._has_command("rg"), "native rg lane not active"
        for t in spin:
            t.start()
        with ThreadPoolExecutor(threads) as pool:
            for f in [pool.submit(worker) for _ in range(threads)]:
                f.result()
        assert done[0] > 0
        assert not failures, f"{len(failures)}/{done[0]} single-file searches failed: {failures[:5]}"
        print(f"{done[0]} single-file searches, 0 failed")
    finally:
        stop.set()
        for t in spin:
            t.join()
        cleanup_vm(task_id)
