from __future__ import annotations

import typing as t

from ansible.compat.posix import flock_exclusive


def named_mutex(path: str) -> t.ContextManager[None]:
    """
    Cross-platform IPC mutex via a shared filename.

    Delegates to `ansible.compat.posix.flock_exclusive`, which uses
    `fcntl.flock` on POSIX and `msvcrt.locking` on Windows.

    The lock file is created automatically; creation of the parent
    directory and deletion of the lockfile are the caller's responsibility.
    """
    return flock_exclusive(path)
