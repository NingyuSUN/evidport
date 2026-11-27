"""Persistent advisory locks for local MCP processes (standard library only)."""
from __future__ import annotations

import errno
import os
import stat
import threading
import time
from pathlib import Path

from .store import EvidenceError


class FileLease:
    """An exclusive OS lock whose descriptor may pass from submitter to worker.

    Lock files are never deleted or replaced: all processes must lock the same
    inode. The OS releases the lease if its owner process exits unexpectedly.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._file = None

    def acquire(self, *, blocking=True, timeout=30.0):
        if self._file is not None:
            raise RuntimeError("File lease is already held")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout < 0):
            raise ValueError("Lock timeout must be nonnegative or None")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise EvidenceError("Unsafe local MCP lock file")
        try:
            before = self.path.lstat() if self.path.exists() else None
            if before and (not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400):
                raise EvidenceError("Unsafe local MCP lock file")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            stream = os.fdopen(os.open(self.path, flags, 0o600), "r+b", buffering=0)
            opened = os.fstat(stream.fileno())
            current = self.path.lstat()
            if (not stat.S_ISREG(opened.st_mode) or self.path.is_symlink()
                    or getattr(current, "st_file_attributes", 0) & 0x400
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                stream.close()
                raise EvidenceError("Local MCP lock file changed while opening")
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if os.fstat(stream.fileno()).st_size == 0:
                        stream.seek(0)
                        stream.write(b"\0")
                    self._file = stream
                    return True
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        stream.close()
                        raise
                    if not blocking or (deadline is not None and time.monotonic() >= deadline):
                        stream.close()
                        return False
                    time.sleep(.02)
        except OSError as exc:
            if "stream" in locals() and self._file is None:
                stream.close()
            raise EvidenceError("Cannot open or acquire local MCP lock") from exc
        except BaseException:
            if "stream" in locals() and self._file is None:
                stream.close()
            raise

    def release(self):
        stream, self._file = self._file, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class OperationLock:
    """Combine a process-local RLock with a reentrant cross-process file lock."""

    def __init__(self, path: Path, local_lock=None, *, timeout=30.0):
        self.path = Path(path)
        self.timeout = timeout
        self._local = local_lock if local_lock is not None else threading.RLock()
        self._depth = threading.local()
        self._lease = FileLease(path)

    def __enter__(self):
        started = time.monotonic()
        acquired = self._local.acquire() if self.timeout is None else self._local.acquire(timeout=self.timeout)
        if not acquired:
            raise EvidenceError("Project operation is busy; retry after the current operation finishes")
        depth = getattr(self._depth, "value", 0)
        try:
            remaining = None if self.timeout is None else max(0.0, self.timeout - (time.monotonic() - started))
            if depth == 0 and not self._lease.acquire(timeout=remaining):
                raise EvidenceError("Project operation is busy in another process; retry after it finishes")
            self._depth.value = depth + 1
            return self
        except BaseException:
            self._local.release()
            raise

    def __exit__(self, *_exc):
        depth = self._depth.value - 1
        self._depth.value = depth
        try:
            if depth == 0:
                self._lease.release()
        finally:
            self._local.release()
