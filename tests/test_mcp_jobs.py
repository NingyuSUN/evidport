"""Serialized local job lifecycle is independent of SDK and scientific validation."""
import json
import threading
import time

import pytest

from evidence_database.mcp_jobs import LocalJobs
from evidence_database.store import EvidenceError


def wait(jobs, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = jobs.get(job_id)
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(.01)
    pytest.fail("Local job did not finish")


def test_job_deduplication_queue_bounds_cancellation_and_result(tmp_path):
    jobs = LocalJobs(tmp_path, threading.RLock(), max_active=2)
    started, finish = threading.Event(), threading.Event()

    def operation():
        started.set()
        assert finish.wait(5)
        return {"done": True}

    try:
        first = jobs.submit("first", {}, operation)
        assert started.wait(5)
        duplicate = jobs.submit("first", {}, operation)
        assert duplicate["job_id"] == first["job_id"] and duplicate["deduplicated"]
        queued = jobs.submit("second", {}, lambda: {"never": "called"})
        with pytest.raises(EvidenceError, match="full"):
            jobs.submit("third", {}, dict)
        assert jobs.cancel(queued["job_id"])["status"] == "cancelled"
        assert jobs.cancel(first["job_id"])["status"] == "running"
        finish.set()
        assert wait(jobs, first["job_id"])["result"] == {"done": True}
    finally:
        finish.set()
        jobs.close()
    restored = LocalJobs(tmp_path, threading.RLock())
    try:
        assert restored.get(first["job_id"])["result"] == {"done": True}
        with pytest.raises(EvidenceError, match="Invalid"):
            restored.get("../outside")
    finally:
        restored.close()


def test_job_error_does_not_expose_exception_path_or_approve(tmp_path):
    jobs = LocalJobs(tmp_path, threading.RLock())
    try:
        def fail():
            raise OSError("private-path-or-secret")
        result = wait(jobs, jobs.submit("failure", {}, fail)["job_id"])
        assert result["status"] == "failed" and result["human_approvals_created"] == 0
        assert "private-path-or-secret" not in json.dumps(result)
    finally:
        jobs.close()


def test_restart_marks_interrupted_without_running_again(tmp_path):
    job_id = "job_" + "a" * 32
    (tmp_path / (job_id + ".json")).write_text(json.dumps({"job_id": job_id, "status": "running"}))
    jobs = LocalJobs(tmp_path, threading.RLock())
    try:
        assert jobs.get(job_id)["status"] == "interrupted"
        assert "retry" in jobs.get(job_id)["error"]
    finally:
        jobs.close()

# These tests launch a real second Python process. In-memory/thread-only locks
# cannot satisfy the assertions and cannot protect independently launched hosts.
def _start_process(code, *arguments):
    import os
    import subprocess
    import sys
    from pathlib import Path
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.Popen([sys.executable, "-u", "-c", code, *map(str, arguments)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf8", env=environment)


def _ready(process, path):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf8"))
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail("Child process exited before readiness: " + stdout + stderr)
        time.sleep(.02)
    pytest.fail("Child process did not become ready")


def _stop_process(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=15)


@pytest.mark.parametrize("crash", [False, True])
def test_second_process_does_not_interrupt_live_or_queued_owner_jobs(tmp_path, crash):
    root, ready = tmp_path / "jobs", tmp_path / "owner-ready.json"
    code = r'''
import json, sys, threading, time
from pathlib import Path
from evidence_database.mcp_jobs import LocalJobs
root, ready = map(Path, sys.argv[1:])
started, finish = threading.Event(), threading.Event()
jobs = LocalJobs(root, threading.RLock(), max_active=2)
def held():
    started.set()
    if not finish.wait(30):
        raise RuntimeError("test owner was not released")
    return {"done": True}
first = jobs.submit("held", {}, held)
assert started.wait(10)
second = jobs.submit("queued", {}, lambda: {"second": True})
ready.write_text(json.dumps({"first": first["job_id"], "second": second["job_id"]}), encoding="utf8")
sys.stdin.readline()
finish.set()
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    if jobs.get(second["job_id"])["status"] == "completed":
        break
    time.sleep(.01)
else:
    raise RuntimeError("second test job did not finish")
jobs.close()
'''
    process = _start_process(code, root, ready)
    observer = None
    try:
        ids = _ready(process, ready)
        observer = LocalJobs(root, threading.RLock(), max_active=2)
        assert observer.get(ids["first"])["status"] == "running"
        assert observer.get(ids["second"])["status"] == "queued"
        assert observer.submit("held", {}, lambda: pytest.fail("must not run twice"))["job_id"] == ids["first"]
        with pytest.raises(EvidenceError, match="full"):
            observer.submit("third", {}, dict)
        denied = observer.cancel(ids["second"])
        assert denied["status"] == "queued" and "another active server" in denied["cancellation"]
        assert all((root / (job_id + ".lease")).is_file() for job_id in ids.values())
        if crash:
            process.kill()
            process.communicate(timeout=15)
            assert observer.get(ids["first"])["status"] == "interrupted"
            assert observer.get(ids["second"])["status"] == "interrupted"
            with observer.operation_lock:
                pass  # The crashed process's project lock was also released by the OS.
        else:
            stdout, stderr = process.communicate("release\n", timeout=15)
            assert process.returncode == 0, stdout + stderr
            # Cached foreign running receipts must refresh from disk at completion.
            assert observer.get(ids["first"])["result"] == {"done": True}
            assert observer.get(ids["second"])["result"] == {"second": True}
        assert all((root / (job_id + ".lease")).is_file() for job_id in ids.values())
    finally:
        _stop_process(process)
        if observer is not None:
            observer.close()


def test_project_operation_lock_serializes_processes_times_out_and_is_reentrant(tmp_path):
    root, ready = tmp_path / "jobs", tmp_path / "lock-ready.json"
    code = r'''
import json, sys, threading
from pathlib import Path
from evidence_database.mcp_jobs import LocalJobs
root, ready = map(Path, sys.argv[1:])
jobs = LocalJobs(root, threading.RLock())
with jobs.operation_lock:
    ready.write_text(json.dumps({"locked": True}), encoding="utf8")
    sys.stdin.readline()
jobs.close()
'''
    process = _start_process(code, root, ready)
    observer = None
    try:
        assert _ready(process, ready)["locked"]
        observer = LocalJobs(root, threading.RLock(), operation_timeout=.1)
        with pytest.raises(EvidenceError, match="busy in another process"), observer.operation_lock:
            pytest.fail("different processes entered the project operation concurrently")
        stdout, stderr = process.communicate("release\n", timeout=15)
        assert process.returncode == 0, stdout + stderr
        with observer.operation_lock, observer.operation_lock:
            assert (root / "project-operation.lock").is_file()
        # Persistent files survive release/reconnect; replacing them breaks OS locking.
        assert (root / "project-operation.lock").is_file()
    finally:
        _stop_process(process)
        if observer is not None:
            observer.close()


def test_cache_is_bounded_and_older_receipts_remain_readable(tmp_path):
    jobs = LocalJobs(tmp_path, threading.RLock(), max_active=2, max_cached=3)
    identifiers = []
    try:
        for number in range(9):
            result = jobs.submit("number", {"number": number}, lambda value=number: {"number": value})
            identifiers.append(result["job_id"])
            assert wait(jobs, result["job_id"])["result"] == {"number": number}
            assert len(jobs._jobs) <= 3
        assert identifiers[0] not in jobs._jobs
        assert jobs.get(identifiers[0])["result"] == {"number": 0}
        assert len(jobs._jobs) <= 3
    finally:
        jobs.close()
    assert jobs._futures == {} and jobs._leases == {}
    restored = LocalJobs(tmp_path, threading.RLock(), max_active=2, max_cached=3)
    try:
        assert len(restored._jobs) <= 3
        assert restored.get(identifiers[0])["result"] == {"number": 0}
        assert len(restored._jobs) <= 3
    finally:
        restored.close()


def test_second_manager_in_same_process_does_not_recover_live_job(tmp_path):
    owner = LocalJobs(tmp_path, threading.RLock())
    started, finish = threading.Event(), threading.Event()
    observer = None
    try:
        def operation():
            started.set()
            assert finish.wait(5)
            return {"done": True}
        submitted = owner.submit("held", {}, operation)
        assert started.wait(5)
        observer = LocalJobs(tmp_path, threading.RLock())
        assert observer.get(submitted["job_id"])["status"] == "running"
        finish.set()
        assert wait(observer, submitted["job_id"])["result"] == {"done": True}
    finally:
        finish.set()
        owner.close()
        if observer:
            observer.close()
