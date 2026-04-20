from __future__ import annotations

import contextlib
import sys
import typing as t


if sys.platform == 'win32':
    import msvcrt as _msvcrt

    @contextlib.contextmanager
    def named_mutex(path: str) -> t.Iterator[None]:
        """
        Lightweight context manager over `msvcrt.locking` to provide IPC locking via a shared filename.
        Entering the context manager blocks until the lock is acquired.
        The lock file will be created automatically, but creation of the parent directory and deletion of the lockfile are the caller's responsibility.
        """
        with open(path, 'a+') as file:
            file.seek(0)
            fd = file.fileno()
            # LK_LOCK retries internally for ~10s then raises OSError; loop to make it truly blocking.
            while True:
                try:
                    _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                file.seek(0)
                try:
                    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
else:
    import fcntl

    @contextlib.contextmanager
    def named_mutex(path: str) -> t.Iterator[None]:
        """
        Lightweight context manager wrapper over `fcntl.flock` to provide IPC locking via a shared filename.
        Entering the context manager blocks until the lock is acquired.
        The lock file will be created automatically, but creation of the parent directory and deletion of the lockfile are the caller's responsibility.
        """
        with open(path, 'a') as file:
            fcntl.flock(file, fcntl.LOCK_EX)

            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)
