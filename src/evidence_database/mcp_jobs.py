"""Serialized local jobs with process leases; evidence receipts stay authoritative."""
from __future__ import annotations

import copy
import hashlib
import heapq
import json
import logging
import re
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from .mcp_locks import FileLease, OperationLock
from .store import EvidenceError

_JOB_ID = re.compile(r"job_[0-9a-f]{32}")
_ACTIVE = {"queued", "running"}
_STATUSES = _ACTIVE | {"completed", "failed", "cancelled", "interrupted"}
_MAX_RECEIPT_BYTES = 300_000


def _now():
    return datetime.now(UTC).isoformat()


class LocalJobs:
    """One worker per process, with project-wide serialization and durable leases."""

    def __init__(self, root: Path, operation_lock: threading.RLock, *, max_active=8,
                 max_cached=100, operation_timeout=30.0):
        if (isinstance(max_active, bool) or not isinstance(max_active, int) or max_active < 1
                or isinstance(max_cached, bool) or not isinstance(max_cached, int)
                or not max_active <= max_cached <= 100):
            raise EvidenceError("Job limits require 1 <= max_active <= max_cached <= 100")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.operation_lock = OperationLock(self.root / "project-operation.lock", operation_lock,
                                            timeout=operation_timeout)
        self._queue_lock = OperationLock(self.root / "job-queue.lock", timeout=operation_timeout)
        self.max_active, self.max_cached = max_active, max_cached
        self._lock = threading.RLock()
        self._jobs = OrderedDict()
        self._futures = {}
        self._leases = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="evidport")
        self._closed = False
        # Read only this service's receipts. Active foreign owners keep their leases;
        # abandoned older jobs are recovered lazily by get() or the next submission.
        paths = heapq.nlargest(max_cached, self.root.glob("job_*.json"), key=lambda p: p.stat().st_mtime_ns)
        try:
            for path in reversed(paths):
                self._remember(self._load_recover(path.stem))
        except BaseException:
            self._executor.shutdown(wait=True, cancel_futures=True)
            raise

    def _remember(self, record):
        self._jobs[record["job_id"]] = record
        self._jobs.move_to_end(record["job_id"])
        while len(self._jobs) > self.max_cached:
            candidate = next((key for key in self._jobs if key not in self._leases), None)
            if candidate is None:
                raise EvidenceError("Local active-job cache exceeded its configured bound")
            self._jobs.pop(candidate)

    def _receipt(self, job_id):
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise EvidenceError("Invalid job identifier")
        return self.root / (job_id + ".json")

    def _read(self, job_id):
        with self._queue_lock:
            path = self._receipt(job_id)
            try:
                if (path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECEIPT_BYTES
                        or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                    raise EvidenceError("Unknown or unsafe local job receipt")
                content = path.read_bytes()
                if len(content) > _MAX_RECEIPT_BYTES:
                    raise EvidenceError("Local job receipt exceeds its size limit")
                record = json.loads(content)
                if (not isinstance(record, dict) or record.get("job_id") != job_id
                        or record.get("status") not in _STATUSES):
                    raise EvidenceError("Invalid local job receipt")
                return record
            except (OSError, ValueError, TypeError) as exc:
                if isinstance(exc, EvidenceError):
                    raise
                raise EvidenceError("Cannot read local job receipt; inspect local diagnostics") from exc


    def _load_recover(self, job_id):
        record = self._read(job_id)
        if record["status"] not in _ACTIVE or job_id in self._leases:
            return record
        lease = FileLease(self.root / (job_id + ".lease"))
        if lease.acquire(blocking=False):
            try:
                # The former owner may have completed between the first read and
                # lease acquisition. Never replace that terminal receipt.
                record = self._read(job_id)
                if record["status"] in _ACTIVE:
                    record.update(status="interrupted", finished_at=_now(),
                        error="Owner process ended. Inspect paper status before retrying; receipts may exist.")
                    self._save(record)
            finally:
                lease.release()
        return record

    def _save(self, record):
        with self._queue_lock:
            target = self._receipt(record["job_id"])
            body = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
            if len(body) > _MAX_RECEIPT_BYTES:
                raise EvidenceError("Local job receipt exceeds its size limit")
            if target.is_symlink() or (target.exists() and getattr(target.lstat(), "st_file_attributes", 0) & 0x400):
                raise EvidenceError("Unsafe local job receipt")
            temporary = self.root / (record["job_id"] + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(body)
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()


    def _release(self, job_id):
        lease = self._leases.pop(job_id, None)
        if lease is not None:
            lease.release()

    def _finished(self, job_id):
        with self._lock:
            self._futures.pop(job_id, None)

    def submit(self, action: str, arguments: dict, operation):
        if not isinstance(action, str) or not action or len(action) > 100 or not isinstance(arguments, dict):
            raise EvidenceError("Job action and arguments are invalid")
        request = json.dumps([action, arguments], sort_keys=True, allow_nan=False).encode()
        if len(request) > 100_000:
            raise EvidenceError("Job arguments exceed the request limit")
        key = hashlib.sha256(request).hexdigest()
        with self._lock, self._queue_lock:
            if self._closed:
                raise EvidenceError("Service is shutting down")
            active = []
            for path in self.root.glob("job_*.json"):
                record = self._load_recover(path.stem)
                if record["status"] in _ACTIVE:
                    active.append(record)
            for job in active:
                if job.get("request_key") == key:
                    self._remember(job)
                    return {**self._public(job), "deduplicated": True}
            if len(active) >= self.max_active:
                raise EvidenceError("Local job queue is full; poll or cancel an existing queued job")
            record = {"job_id": "job_" + uuid.uuid4().hex, "action": action, "request_key": key,
                      "status": "queued", "created_at": _now(), "scientifically_verified": False,
                      "human_approvals_created": 0}
            job_id = record["job_id"]
            lease = FileLease(self.root / (job_id + ".lease"))
            if not lease.acquire(blocking=False):
                raise EvidenceError("Unexpected job identifier collision; retry submission")
            self._leases[job_id] = lease
            try:
                self._save(record)
                self._remember(record)
                future = self._executor.submit(self._run, job_id, operation)
                self._futures[job_id] = future
                future.add_done_callback(lambda _future: self._finished(job_id))
            except BaseException:
                record.update(status="failed", finished_at=_now(), error="Job could not be scheduled")
                try:
                    self._save(record)
                finally:
                    self._release(job_id)
                raise
            return self._public(record)

    def _run(self, job_id, operation):
        try:
            with self._lock:
                record = self._read(job_id)
                record.update(status="running", started_at=_now())
                self._save(record)
                self._remember(record)
            with self.operation_lock:
                result = operation()
            if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 250_000:
                raise EvidenceError("Result exceeded response limit; inspect paper status using narrower queries")
            with self._lock:
                record.update(status="completed", result=result, finished_at=_now())
                self._save(record)
                self._remember(record)
        except Exception as exc:
            with self._lock:
                record = self._read(job_id)
                record.pop("result", None)
                # Public receipts never contain arbitrary paths, contents or credentials.
                record.update(status="failed", error_type=type(exc).__name__,
                    error="Operation failed. Inspect paper status and local server diagnostics before retrying.",
                    finished_at=_now())
                self._save(record)
                self._remember(record)
            logging.getLogger(__name__).exception("Local job %s failed", job_id)
        finally:
            with self._lock:
                self._release(job_id)

    @staticmethod
    def _public(record):
        result = copy.deepcopy(record)
        result.pop("request_key", None)
        return result

    def get(self, job_id: str):
        with self._lock:
            record = self._load_recover(job_id)
            self._remember(record)
            return self._public(record)

    def cancel(self, job_id: str):
        with self._lock:
            record = self._load_recover(job_id)
            future = self._futures.get(job_id)
            if record["status"] == "queued" and future and future.cancel():
                record.update(status="cancelled", finished_at=_now())
                try:
                    self._save(record)
                    self._remember(record)
                finally:
                    self._release(job_id)
                return self._public(record)
            explanation = ("Job belongs to another active server; cancel it through the owning connection."
                           if record["status"] in _ACTIVE and future is None else
                           "Running or completed operations cannot be rolled back; inspect status.")
            return {**self._public(record), "cancellation": explanation}

    def close(self):
        with self._lock:
            self._closed = True
            for job_id in list(self._futures):
                if self._read(job_id)["status"] == "queued":
                    self.cancel(job_id)
        self._executor.shutdown(wait=True, cancel_futures=True)
