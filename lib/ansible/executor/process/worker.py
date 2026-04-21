# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import errno
import io
import os
import signal
import sys
import textwrap
import traceback
import types
import typing as t

from multiprocessing.queues import Queue

from ansible._internal import _task
from ansible.errors import AnsibleError
from ansible.executor.task_executor import TaskExecutor
from ansible.executor.task_queue_manager import FinalQueue, STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO
from ansible.inventory.host import Host
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.task import Task
from ansible.playbook.play_context import PlayContext
from ansible.utils.context_objects import CLIArgs
from ansible.utils.display import Display
from ansible.utils.multiprocessing import context as multiprocessing_context
from ansible.vars.manager import VariableManager

__all__ = ['WorkerProcess']

display = Display()

current_worker = None


class WorkerQueue(Queue):
    """Queue that raises AnsibleError items on get()."""
    def get(self, *args, **kwargs):
        result = super(WorkerQueue, self).get(*args, **kwargs)
        if isinstance(result, AnsibleError):
            raise result
        return result


class WorkerProcess(multiprocessing_context.Process):  # type: ignore[name-defined]
    """
    The worker thread class, which uses TaskExecutor to run tasks
    read from a job queue and pushes results into a results queue
    for reading later.
    """

    def __init__(
            self,
            *,
            final_q: FinalQueue,
            task_vars: dict,
            host: Host,
            task: Task,
            play_context: PlayContext,
            loader: DataLoader,
            variable_manager: VariableManager,
            shared_loader_obj: types.SimpleNamespace,
            worker_id: int,
            cliargs: CLIArgs
    ) -> None:

        super(WorkerProcess, self).__init__()
        # takes a task queue manager as the sole param:
        self._final_q = final_q
        self._task_vars = task_vars
        self._host = host
        self._task = task
        self._play_context = play_context
        self._loader = loader
        self._variable_manager = variable_manager
        self._shared_loader_obj = shared_loader_obj

        # NOTE: this works due to fork, if switching to threads this should change to per thread storage of temp files
        # clear var to ensure we only delete files for this child
        self._loader._tempfiles = set()

        self.worker_queue = WorkerQueue(ctx=multiprocessing_context)
        self.worker_id = worker_id

        self._cliargs = cliargs

        # Snapshot the controller's adjacent plugin-search basedirs (as strings)
        # so the spawn child can replay them; under fork this is inherited COW
        # via the plugin_loader globals. See _bootstrap_spawn_child.
        from ansible.plugins.loader import _get_adjacent_plugin_basedirs_snapshot
        self._extra_plugin_basedirs = _get_adjacent_plugin_basedirs_snapshot()

    def _term(self, signum, frame) -> None:
        """In child termination when notified by the parent"""
        from ansible.compat.posix import killpg
        signal.signal(signum, signal.SIG_DFL)

        try:
            killpg(self.pid, signum)
            os.kill(self.pid, signum)
        except OSError as e:
            if e.errno != errno.ESRCH:
                signame = signal.strsignal(signum)
                display.error(f'Unable to send {signame} to child[{self.pid}]: {e}')

        # fallthrough, if we are still here, just die
        os._exit(1)

    def start(self) -> None:
        """
        multiprocessing.Process replaces the worker's stdin with a new file
        but we wish to preserve it if it is connected to a terminal.
        Therefore dup a copy prior to calling the real start(),
        ensuring the descriptor is preserved somewhere in the new child, and
        make sure it is closed in the parent when start() completes.
        """

        # FUTURE: this lock can be removed once a more generalized pre-fork thread pause is in place
        with display._lock:
            super(WorkerProcess, self).start()

    def _hard_exit(self, e: str) -> t.NoReturn:
        """
        There is no safe exception to return to higher level code that does not
        risk an innocent try/except finding itself executing in the wrong
        process. All code executing above WorkerProcess.run() on the stack
        conceptually belongs to another program.
        """

        try:
            display.error(e)
        except BaseException:
            # If the cause of the fault is OSError being generated by stdio,
            # attempting to log a debug message may trigger another OSError.
            # Try printing once then give up.
            pass

        os._exit(1)

    def _bootstrap_spawn_child(self) -> None:
        """
        When running under the spawn start method (Windows, or any future non-fork
        platform), the child process starts with a fresh Python interpreter and does
        not inherit the parent's context.CLIARGS or plugin_loader state. Restore both
        before any plugin lookup. Under fork this is a no-op because context.CLIARGS
        was copied-on-write from the parent.
        """
        from ansible import context as _context
        if _context.CLIARGS:
            return  # fork child or already bootstrapped
        from ansible.module_utils.common.collections import is_sequence
        from ansible.plugins.loader import init_plugin_loader, add_all_plugin_dirs
        _context.CLIARGS = self._cliargs
        cli_collections_path = self._cliargs.get('collections_path') or []
        if not is_sequence(cli_collections_path):
            cli_collections_path = [cli_collections_path]
        init_plugin_loader(cli_collections_path)
        # Replay playbook-adjacent plugin dirs (filter_plugins/, action_plugins/, etc.)
        # that the CLI registered on the parent before spawning us.
        for basedir in self._extra_plugin_basedirs:
            try:
                add_all_plugin_dirs(basedir)
            except Exception:
                pass

    def _detach(self) -> None:
        """
        The intent here is to detach the child process from the inherited stdio fds,
        including /dev/tty. Children should use Display instead of direct interactions
        with stdio fds.
        """
        try:
            from ansible.compat.posix import setsid, IS_WINDOWS
            setsid()
            # Build stdin open mode. O_NONBLOCK is POSIX-only and is a no-op on the
            # Windows nul device anyway.
            stdin_mode = os.O_RDWR
            if hasattr(os, 'O_NONBLOCK') and not IS_WINDOWS:
                stdin_mode |= os.O_NONBLOCK
            # Create new fds for stdin/stdout/stderr, but also capture python uses of sys.stdout/stderr
            for fds, mode in (
                    ((STDIN_FILENO,), stdin_mode),
                    ((STDOUT_FILENO, STDERR_FILENO), os.O_WRONLY),
            ):
                stdio = os.open(os.devnull, mode)
                for fd in fds:
                    os.dup2(stdio, fd)
                os.close(stdio)
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            sys.stdin = os.fdopen(STDIN_FILENO, 'r', closefd=False)
            # Close stdin so we don't get hanging workers
            # We use sys.stdin.close() for places where sys.stdin is used,
            # to give better errors, and to prevent fd 0 reuse
            sys.stdin.close()
        except Exception as e:
            display.error(f'Could not detach from stdio: {e}')
            os._exit(1)

    def run(self) -> None:
        """
        Wrap _run() to ensure no possibility an errant exception can cause
        control to return to the StrategyBase task loop, or any other code
        higher in the stack.

        As multiprocessing in Python 2.x provides no protection, it is possible
        a try/except added in far-away code can cause a crashed child process
        to suddenly assume the role and prior state of its parent.
        """
        self._bootstrap_spawn_child()
        # Set the queue on Display so calls to Display.display are proxied over the queue
        display.set_queue(self._final_q)
        self._detach()
        # propagate signals
        signal.signal(signal.SIGINT, self._term)
        signal.signal(signal.SIGTERM, self._term)

        try:
            with _task.TaskContext.create(task=self._task, task_vars=self._task_vars, host_name=self._host.name):
                return self._run()
        except BaseException:
            self._hard_exit(traceback.format_exc())

    def _run(self) -> None:
        """
        Called when the process is started.  Pushes the result onto the
        results queue. We also remove the host from the blocked hosts list, to
        signify that they are ready for their next task.
        """

        global current_worker

        current_worker = self

        self._task.squash()

        te = TaskExecutor(
            self._host,
            self._play_context,
            self._loader,
            self._shared_loader_obj,
            self._final_q,
            self._variable_manager,
        )

        utr = te.run()
        utr.finalize_registered_values()

        self._host.vars = dict()
        self._host.groups = []

        for name, stdio in (('stdout', sys.stdout), ('stderr', sys.stderr)):
            if data := stdio.getvalue():  # type: ignore[union-attr]
                display.warning(
                    (
                        f'WorkerProcess for [{self._host}/{self._task}] errantly sent data directly to {name} instead of using Display:\n'
                        f'{textwrap.indent(data[:256], "    ")}\n'
                    ),
                    formatted=True
                )

        # manually update the original task object's values with final post-loop-resolved values since callbacks consult them
        self._task.ignore_errors = utr.ignore_errors
        self._task.ignore_unreachable = utr.ignore_unreachable

        try:
            self._final_q.send_task_result(self._host, self._task, utr)
        except Exception as ex:
            try:
                raise AnsibleError("Task result omitted due to queue send failure.") from ex
            except Exception as ex_wrapper:
                host = Host(name=self._host.name)

                task = Task()
                task._uuid = self._task._uuid

                utr = _task.UnifiedTaskResult.create_from_action_exception(ex_wrapper)
                utr.finalize_registered_values()

                # ignore the real task result and don't allow result object contribution from the exception (in case the pickling error was related)
                # also use a synthesized host and task object to avoid issues with values from them, particularly task_fields
                self._final_q.send_task_result(host, task, utr)


# --- PERSISTENT WORKER POOL (scaffolding) --------------------------------------
#
# Not wired. See docs/windows-controller/PERSISTENT_WORKER_POOL.md for the
# design, including the incremental landing plan. This class exists so future
# sessions have a concrete subclass to iterate on rather than starting from
# scratch, and so readers can see the intended shape without chasing a design
# doc into code.
#
# Current state: importable, compiles, does nothing until a strategy plugin
# submits jobs to its input queue and the TaskQueueManager is taught to create
# a pool instead of spawning one WorkerProcess per task.

import dataclasses


_PERSISTENT_WORKER_SENTINEL = object()


@dataclasses.dataclass(frozen=True, slots=True)
class _TaskJob:
    """Payload submitted to a pooled worker's input queue.

    Every field must already be pickle-safe under the spawn worker path we
    built in Phase 1. No new pickle-safety work required here; the field
    list is a subset of the current WorkerProcess.__init__ kwargs.
    """
    worker_id: int
    task: Task
    host: Host
    task_vars: dict
    play_context: PlayContext
    loader: DataLoader
    variable_manager: VariableManager


class _PersistentWorker(multiprocessing_context.Process):
    """Long-lived worker that drains `_TaskJob`s from an input queue.

    Designed to replace the per-task `WorkerProcess` on Windows so the ~1.1s
    Python-import cost is paid once per worker lifetime rather than once per
    task. See docs/windows-controller/PERSISTENT_WORKER_POOL.md.

    Current state: skeleton. Run loop, bootstrap reuse, and isolation
    context manager are sketched but not exercised.
    """

    def __init__(
        self,
        *,
        final_q: FinalQueue,
        input_q: Queue,
        cliargs: CLIArgs,
        extra_plugin_basedirs: t.Sequence[str],
        worker_id: int,
    ) -> None:
        super().__init__()
        self._final_q = final_q
        self._input_q = input_q
        self._cliargs = cliargs
        self._extra_plugin_basedirs = list(extra_plugin_basedirs)
        self.worker_id = worker_id

    def _bootstrap(self) -> None:
        """Same plugin-loader + CLIARGS bootstrap the per-task worker does."""
        from ansible import context as _context
        if _context.CLIARGS:
            return
        from ansible.module_utils.common.collections import is_sequence
        from ansible.plugins.loader import init_plugin_loader, add_all_plugin_dirs
        _context.CLIARGS = self._cliargs
        collections_path = self._cliargs.get('collections_path') or []
        if not is_sequence(collections_path):
            collections_path = [collections_path]
        init_plugin_loader(collections_path)
        for basedir in self._extra_plugin_basedirs:
            try:
                add_all_plugin_dirs(basedir)
            except Exception:
                pass

    def run(self) -> None:
        # Connect Display to the controller's queue so the worker's logging
        # flows back rather than being dropped.
        display.set_queue(self._final_q)
        self._bootstrap()
        while True:
            job = self._input_q.get()
            if job is _PERSISTENT_WORKER_SENTINEL:
                return
            if not isinstance(job, _TaskJob):
                display.error(f"persistent worker received non-job payload: {type(job).__name__}")
                continue
            with _JobIsolation():
                self._run_one(job)

    def _run_one(self, job: _TaskJob) -> None:
        """Execute a single task. Intentionally narrow for v1 — no
        per-host connection caching yet, no ThrottleBucket support, etc.
        Those land in subsequent PRs per the design doc."""
        # Adapter over TaskExecutor mirroring the per-task WorkerProcess._run
        # flow. Not exercised yet — see design doc for wiring plan.
        raise NotImplementedError(
            "persistent worker task execution is not wired yet; see "
            "docs/windows-controller/PERSISTENT_WORKER_POOL.md"
        )


class _JobIsolation:
    """Per-task state isolation for a persistent worker.

    Snapshots `os.environ`, `sys.path`, and the root logger's handler list
    on entry and restores them on exit so one task can't leak state into
    the next. Under fork/per-task-spawn this was free because each task
    got a fresh interpreter.
    """

    def __enter__(self) -> '_JobIsolation':
        import logging
        self._env = os.environ.copy()
        self._sys_path = list(sys.path)
        self._log_handlers = list(logging.root.handlers)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import logging
        # Restore env by diff, not wholesale replace, so anything added by the
        # caller after snapshot is removed and anything we removed is put back.
        current = set(os.environ)
        for k in current - set(self._env):
            os.environ.pop(k, None)
        for k, v in self._env.items():
            if os.environ.get(k) != v:
                os.environ[k] = v
        sys.path[:] = self._sys_path
        logging.root.handlers[:] = self._log_handlers
