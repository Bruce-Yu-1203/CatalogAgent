"""Exclusive process ownership of a database via a nonblocking fcntl.flock (Linux/macOS)."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class LockHeldError(RuntimeError):
    pass


class DatabaseLock:
    """Held for the whole crawl/resume. The lock file is never deleted while a process may own it."""

    def __init__(self, database: str | Path):
        self.path = Path(str(database) + ".lock")
        self._fd: int | None = None

    def acquire(self) -> DatabaseLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(fd)
            raise LockHeldError(f"another process owns {self.path}") from e
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> DatabaseLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
