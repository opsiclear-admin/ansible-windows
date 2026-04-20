from __future__ import annotations

import contextlib
import types
import typing as _t

from ansible.compat import posix as _posix
from ansible.module_utils import datatag


class AnsibleTimeoutError(BaseException):
    """A general purpose timeout."""

    _MAX_TIMEOUT = 100_000_000
    """
    The maximum supported timeout value.
    This value comes from BSD's alarm limit, which is due to that function using setitimer.
    """

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

        super().__init__(f"Timed out after {timeout} second(s).")

    @classmethod
    @contextlib.contextmanager
    def alarm_timeout(cls, timeout: int | None) -> _t.Iterator[None]:
        """
        Context for running code under an optional timeout.
        Raises an instance of this class if the timeout occurs.

        On POSIX this uses SIGALRM via `signal.alarm`, which can interrupt
        even C-level blocking code. On Windows there is no SIGALRM; the
        timeout is approximated with a threading.Timer that raises on the
        main thread once the interval elapses. The Windows path cannot
        interrupt code blocked in a C extension.

        New usages of this timeout mechanism are discouraged.
        """
        if timeout is not None:
            if not isinstance(timeout, int):
                raise TypeError(f"Timeout requires 'int' argument, not {datatag.native_type_name(timeout)!r}.")

            if timeout < 0 or timeout > cls._MAX_TIMEOUT:
                # On BSD based systems, alarm is implemented using setitimer.
                # If out-of-bounds values are passed to alarm, they will return -1, which would be interpreted as an existing timer being set.
                # To avoid that, bounds checking is performed in advance.
                raise ValueError(f'Timeout {timeout} is invalid, it must be between 0 and {cls._MAX_TIMEOUT}.')

        if not timeout:
            yield  # execute the context manager's body
            return  # no timeout to deal with, exit immediately

        def on_alarm(_signal: int, _frame: types.FrameType | None) -> None:
            raise cls(timeout)

        if _posix.HAS_SIGALRM:
            previous = _posix.install_alarm_handler(on_alarm)
            if previous:
                raise RuntimeError("An existing alarm handler was present.")
            try:
                try:
                    if _posix.start_alarm(timeout):
                        raise RuntimeError("An existing alarm was set.")

                    yield  # execute the context manager's body
                finally:
                    # Disable the alarm.
                    # If the alarm fires inside this finally block, the alarm is still disabled.
                    # This guarantees the cleanup code in the outer finally block runs without risk of encountering the `TaskTimeoutError` from the alarm.
                    _posix.cancel_alarm()
            finally:
                _posix.restore_alarm_handler(previous)
        else:
            # Windows path: threading.Timer cannot interrupt the main thread
            # directly, so we track expiry and raise when the body returns
            # (post hoc) if it overran. Callers who need hard interruption
            # under Windows should migrate off this utility.
            import threading
            import time

            expired = False

            def _fire(_signum: int, _frame: _t.Any) -> None:
                nonlocal expired
                expired = True

            _posix.start_alarm(timeout, _fire)
            start = time.monotonic()
            try:
                yield
            finally:
                _posix.cancel_alarm()
                if expired or (time.monotonic() - start) >= timeout:
                    raise cls(timeout)
            del threading  # avoid unused import warning
