# Copyright: (c) 2026, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""
Platform compat shim for POSIX-only primitives.

Controller code should import from this module rather than reaching for
`fcntl`, `termios`, `pwd`, `grp`, or `signal.SIGALRM` directly, so the
Windows controller (and any other non-POSIX controller in the future) can
run with graceful degradation instead of import-time failures.

Design:
- Functions are cheap to call and do the platform dispatch inline.
- When no sensible Windows equivalent exists, the function raises
  NotImplementedError (e.g. `open_pty`).
- Callers that only need "do it if available" semantics use the no-op
  fallbacks (e.g. `setsid`, `killpg`, `register_at_fork`).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import signal
import sys
import threading
import typing as t

IS_WINDOWS = sys.platform == 'win32'


# ---- process / identity ----------------------------------------------------


def getuid() -> int:
    """Return the real user id, or 0 on platforms without getuid()."""
    return os.getuid() if hasattr(os, 'getuid') else 0


def geteuid() -> int:
    """Return the effective user id, or 0 on platforms without geteuid()."""
    return os.geteuid() if hasattr(os, 'geteuid') else 0


def setsid() -> None:
    """Start a new session if the platform supports it, else no-op."""
    if hasattr(os, 'setsid'):
        os.setsid()


def killpg(pgid: int, sig: int) -> None:
    """Signal a process group if supported, else no-op.

    On Windows there is no POSIX-style process group, so callers should
    not rely on this for correctness. The regular `os.kill(pid, sig)`
    still runs via TerminateProcess semantics.
    """
    if hasattr(os, 'killpg'):
        os.killpg(pgid, sig)


def register_at_fork(*, after_in_child: t.Callable[[], None]) -> None:
    """Register an after-fork-in-child handler if supported, else no-op."""
    if hasattr(os, 'register_at_fork'):
        os.register_at_fork(after_in_child=after_in_child)


# ---- file locking ----------------------------------------------------------


if IS_WINDOWS:
    import msvcrt as _msvcrt

    @contextlib.contextmanager
    def flock_exclusive(path: str) -> t.Iterator[None]:
        """Blocking exclusive lock over `path` using msvcrt.locking.

        msvcrt.locking(LK_LOCK) blocks for ~10 seconds then raises; wrap in
        a retry loop for truly-blocking semantics. Locks 1 byte at offset 0.
        """
        with open(path, 'a+') as fh:
            fh.seek(0)
            fd = fh.fileno()
            while True:
                try:
                    _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                fh.seek(0)
                try:
                    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
else:
    import fcntl as _fcntl

    @contextlib.contextmanager
    def flock_exclusive(path: str) -> t.Iterator[None]:
        """Blocking exclusive lock over `path` using fcntl.flock."""
        with open(path, 'a') as fh:
            _fcntl.flock(fh, _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(fh, _fcntl.LOCK_UN)


# ---- PTY -------------------------------------------------------------------


def open_pty() -> tuple[int, int]:
    """Open a pty pair. Raises NotImplementedError where pty is unavailable."""
    try:
        import pty as _pty
    except ImportError as ex:
        raise NotImplementedError("pty is not available on this platform") from ex
    return _pty.openpty()


# ---- terminal size ---------------------------------------------------------


def get_terminal_columns(default: int = 80) -> int:
    """Return the terminal column count.

    Cross-platform via stdlib `shutil.get_terminal_size`, which uses
    TIOCGWINSZ on POSIX and GetConsoleScreenBufferInfo on Windows.
    Returns `default` if the query fails entirely.
    """
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except (OSError, ValueError):
        return default


# ---- alarm / timeout -------------------------------------------------------


HAS_SIGALRM = hasattr(signal, 'SIGALRM')


class _WindowsAlarmState:
    """Module-level state for the threading.Timer-based alarm on Windows."""
    timer: threading.Timer | None = None


def install_alarm_handler(handler: t.Callable[[int, t.Any], t.Any]) -> t.Callable[[int, t.Any], t.Any] | None:
    """Install the given handler as the SIGALRM handler on POSIX.

    Returns the previously installed handler (as `signal.signal` does), or
    `None` on platforms without SIGALRM. The returned value can be used
    with `restore_alarm_handler` to put things back.
    """
    if HAS_SIGALRM:
        return signal.signal(signal.SIGALRM, handler)
    return None


def restore_alarm_handler(previous: t.Callable[[int, t.Any], t.Any] | int | None) -> None:
    """Restore an alarm handler previously returned by install_alarm_handler."""
    if HAS_SIGALRM and previous is not None:
        signal.signal(signal.SIGALRM, previous)


def start_alarm(seconds: int, handler: t.Callable[[int, t.Any], t.Any] | None = None) -> bool:
    """Arm an alarm that fires `handler` after `seconds` seconds.

    On POSIX this uses `signal.alarm`, and returns True when a previous
    alarm was already armed (matching the POSIX semantics). On Windows
    there is no SIGALRM; a threading.Timer fires `handler` instead.

    Caveat: the threading.Timer callback runs in a helper thread and
    cannot interrupt C-level blocking code in the main thread. Callers
    that depend on SIGALRM's signal-based interrupt behavior should not
    rely on this being a perfect replacement.

    Pass `seconds=0` to cancel any armed alarm.
    """
    if HAS_SIGALRM:
        return bool(signal.alarm(seconds))

    # Windows fallback
    if _WindowsAlarmState.timer is not None:
        _WindowsAlarmState.timer.cancel()
        _WindowsAlarmState.timer = None
    if seconds <= 0 or handler is None:
        return False

    def _fire() -> None:
        try:
            handler(0, None)
        except Exception:
            pass

    timer = threading.Timer(seconds, _fire)
    timer.daemon = True
    timer.start()
    _WindowsAlarmState.timer = timer
    return False


def cancel_alarm() -> None:
    """Cancel any pending alarm."""
    if HAS_SIGALRM:
        signal.alarm(0)
        return
    if _WindowsAlarmState.timer is not None:
        _WindowsAlarmState.timer.cancel()
        _WindowsAlarmState.timer = None


# ---- non-blocking IO -------------------------------------------------------


def set_nonblocking(fd: int) -> bool:
    """Set fd non-blocking where supported. Returns True on success."""
    if not hasattr(os, 'set_blocking'):
        return False
    try:
        os.set_blocking(fd, False)
        return True
    except OSError:
        return False


# ---- user / group lookup ---------------------------------------------------


try:
    import pwd as _pwd_mod
except ImportError:
    _pwd_mod = None  # type: ignore[assignment]

try:
    import grp as _grp_mod
except ImportError:
    _grp_mod = None  # type: ignore[assignment]


HAS_PWD = _pwd_mod is not None
HAS_GRP = _grp_mod is not None


def lookup_user_name(uid: int) -> str | int:
    """Map a uid to a username via pwd, else return the uid unchanged.

    On Windows there is no POSIX uid-to-name mapping from the stdlib, so
    the integer is returned as-is. Callers that display this value should
    handle both str and int.
    """
    if _pwd_mod is not None:
        try:
            return _pwd_mod.getpwuid(uid).pw_name
        except (KeyError, TypeError):
            pass
    return uid


def lookup_group_name(gid: int) -> str | int:
    """Map a gid to a group name via grp, else return the gid unchanged."""
    if _grp_mod is not None:
        try:
            return _grp_mod.getgrgid(gid).gr_name
        except (KeyError, TypeError):
            pass
    return gid
