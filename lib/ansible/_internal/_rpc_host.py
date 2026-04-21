"""
Internal Worker->Controller RPC support module.

Implements a minimal multiprocessing context for an in-process LocalManager RPC server hosted on the primary Ansible controller process.
"""

from __future__ import annotations

import collections.abc as c
import dataclasses
import signal
import threading
import typing as t

from multiprocessing.context import BaseContext
from multiprocessing.managers import BaseManager, Server
from multiprocessing.process import BaseProcess

from ..module_utils._internal._concurrent._futures import DaemonThreadPoolExecutor

_server_ready: threading.Event = threading.Event()


class LocalNotAProcess(BaseProcess):
    """Minimal BaseProcess impl that runs `target` locally in a thread instead of a subprocess."""

    def __init__(self, *posargs, target, args, **kwargs):
        super().__init__(*posargs, **kwargs)

        self._args = args
        self._kwargs = kwargs
        self._target = target
        self._tpe = DaemonThreadPoolExecutor()

    def start(self):
        if threading.current_thread() is not threading.main_thread():
            # temporary signal patch is only safe under the main thread; guaranteed to be restored before it can be used
            raise RuntimeError("Local RPC server must be started from the main thread.")

        original_signal = signal.signal

        try:
            # signal.signal raises if called from a non-main thread, disable it until we're past that point of the server startup
            signal.signal = lambda *args, **kwargs: None

            # the only target this should see is _run_server
            # start cannot return until Server.serve_forever is called (our custom subclass sets the _server_ready event)
            self._tpe.submit(self._target, *self._args, **self._kwargs)

            if not _server_ready.wait(5):
                raise TimeoutError("Local RPC server did not start.")
        finally:
            signal.signal = original_signal  # always restore default signal impl


class LocalContext(BaseContext):
    """Minimal Context impl to support in-proc LocalManager."""

    Process = LocalNotAProcess


class LocalServer(Server):
    """Customized Server for in-proc LocalManager."""

    def serve_forever(self):
        _server_ready.set()
        return super().serve_forever()


@dataclasses.dataclass(frozen=True)
class _RemoteEndpoint:
    """Address/authkey pair pointing at a parent-process LocalManager.

    Used by spawn-born workers: the child cannot inherit the parent's
    already-running manager via fork COW, and calling `shared_instance()`
    fresh would spin up a *new* server with a new authkey — subsequent
    `get_client()` calls would then connect to the child's own empty
    server instead of the controller's. Instead the parent hands the
    child this endpoint during bootstrap (`WorkerProcess._bootstrap_spawn_child`),
    and the child sets it via `LocalManager.configure_remote_endpoint()`
    before any RPC call.
    """
    address: t.Any
    authkey: bytes


class LocalManager(BaseManager):
    """Customized BaseManager for in-proc usage."""

    _Server = LocalServer  # override BaseManager to use custom Server subclass
    _shared_instance: t.ClassVar[t.Self | None] = None
    _shared_lock = threading.Lock()
    _remote_endpoint: t.ClassVar[_RemoteEndpoint | None] = None

    @classmethod
    def configure_remote_endpoint(cls, address: t.Any, authkey: bytes) -> None:
        """Called by a spawn-born worker to route shared_instance() at the
        parent's already-running manager rather than starting a fresh one.
        Idempotent; no-op on a fork child because the shared instance is
        inherited via COW.
        """
        cls._remote_endpoint = _RemoteEndpoint(address=address, authkey=authkey)

    def __init__(self):
        type(self)._current = self  # HACK: ew

        super().__init__(serializer='pickle', ctx=LocalContext())

    @property
    def authkey(self) -> bytes:
        return self._authkey

    @classmethod
    def register(
        cls,
        typeid: str,
        callable: t.Callable | None = None,
        proxytype: type | None = None,
        exposed: c.Sequence[str] | None = None,
        method_to_typeid: c.Mapping[str, str] | None = None,
        create_method: bool = True,
    ) -> None:
        with cls._shared_lock:
            super().register(
                typeid=typeid,
                callable=callable,
                proxytype=proxytype,
                exposed=exposed,
                method_to_typeid=method_to_typeid,
                create_method=create_method,
            )

    @classmethod
    def shared_instance(cls) -> t.Any:
        """Access a lazily-created LocalManager singleton, or — on a spawn
        worker that has been configured via `configure_remote_endpoint` — a
        proxy object exposing the parent's address and authkey so that
        `get_client()` downstream connects to the parent's server rather
        than starting a fresh server inside the worker.
        """
        if cls._remote_endpoint is not None:
            return cls._remote_endpoint
        if not cls._shared_instance:
            with cls._shared_lock:
                if not cls._shared_instance:
                    instance = cls()
                    instance.start()

                    cls._shared_instance = instance

        return cls._shared_instance


@dataclasses.dataclass(kw_only=True)
class AsyncRPCOperation:
    """Wrapper around a deferred invocation with a waitable completion event."""

    event: threading.Event = dataclasses.field(default_factory=threading.Event)
    impl: t.Callable
    args: tuple
    kwargs: dict[str, object]

    _response: object = None
    _exception: BaseException | None = None

    # FUTURE: reimplement as a lighter-weight future/asyncio executor if RPC volume gets significantly higher

    @property
    def result(self) -> object:
        if self._exception:
            raise self._exception

        return self._response

    def dispatch(self) -> None:
        try:
            self._response = self.impl(*self.args, **self.kwargs)
        except BaseException as ex:
            self._exception = ex

        self.event.set()


class AutoRegisterRPC:
    """Base class for an RPC implementation which automatically registers its methods."""

    def __init_subclass__(cls, **kwargs):
        instance = cls()
        LocalManager.register(cls.__name__, lambda: instance)

    @classmethod
    def get_client(cls) -> t.Self:
        """Get the RPC client for this implementation."""
        rpc_host = LocalManager.shared_instance()  # this assumes the caller was forked from the controller after the manager was started

        rpc_client = BaseManager(address=rpc_host.address, authkey=rpc_host.authkey)
        rpc_client.register(cls.__name__)
        rpc_client.connect()

        return getattr(rpc_client, cls.__name__)()
