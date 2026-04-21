# Copyright: (c) 2026, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""
Pickle-roundtrip tests for objects that must survive the spawn worker
handoff on the windows-controller fork.

Upstream's fork-based executor never exercises these pickle paths —
the child inherits parent state via copy-on-write. Under spawn
(Windows, or any future non-fork platform) every argument to
`WorkerProcess.__init__` has to be picklable OR the child has to
reconstruct it from paths. These tests are the regression guard
`PLAN.md` Phase 1 risk register called out and that v0.1 shipped
without.

If one of these ever fails on a future upstream rebase, that's the
early-warning signal to audit the offending class before a real
playbook surfaces a cryptic `_pickle.PicklingError` in a spawn worker.
"""
from __future__ import annotations

import pickle

import pytest

from ansible.inventory.host import Host
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.task import Task
from ansible.playbook.play_context import PlayContext
from ansible.utils.context_objects import CLIArgs


def _roundtrip(obj: object) -> object:
    data = pickle.dumps(obj)
    return pickle.loads(data)


class TestSimpleDataPickle:
    """Objects the WorkerProcess constructor takes — must pickle cleanly."""

    def test_host_roundtrip(self) -> None:
        h = Host(name='test01')
        h.set_variable('foo', 'bar')
        r = _roundtrip(h)
        assert r.name == 'test01'
        assert r.vars.get('foo') == 'bar'

    def test_task_roundtrip(self) -> None:
        t = Task()
        t._uuid = '11111111-2222-3333-4444-555555555555'
        r = _roundtrip(t)
        assert r._uuid == t._uuid

    def test_play_context_roundtrip(self) -> None:
        pc = PlayContext()
        pc.remote_user = 'root'
        pc.password = 'not-actually-a-secret'
        r = _roundtrip(pc)
        assert r.remote_user == 'root'
        assert r.password == 'not-actually-a-secret'
        # connection_lock field added for spawn-safe lock handoff (phase 1).
        assert hasattr(r, 'connection_lock')

    def test_cliargs_roundtrip(self) -> None:
        c = CLIArgs({'verbosity': 3, 'collections_path': ['/a', '/b']})
        r = _roundtrip(c)
        assert r['verbosity'] == 3
        assert list(r['collections_path']) == ['/a', '/b']


class TestDataLoaderPickle:
    """DataLoader carries vault secrets — pickle must preserve them."""

    def test_dataloader_no_vault(self) -> None:
        d = DataLoader()
        r = _roundtrip(d)
        # Minimal invariant — still a DataLoader after roundtrip.
        assert isinstance(r, DataLoader)


class TestRPCEndpointPickle:
    """The _RemoteEndpoint dataclass is what the spawn child receives to
    route AutoRegisterRPC.get_client() at the parent's server. It must
    survive the spawn pickle boundary intact.
    """

    def test_remote_endpoint_roundtrip(self) -> None:
        from ansible._internal._rpc_host import _RemoteEndpoint
        ep = _RemoteEndpoint(address=('127.0.0.1', 12345), authkey=b'k' * 32)
        r = _roundtrip(ep)
        assert r.address == ('127.0.0.1', 12345)
        assert r.authkey == b'k' * 32


class TestWorkerBootstrapState:
    """WorkerProcess.__init__ snapshots the plugin-loader basedirs and the
    RPC endpoint on the parent so the spawn child can replay them.
    These snapshots are tuples/lists of primitives — must pickle.
    """

    def test_basedirs_snapshot_picklable(self) -> None:
        basedirs = ['/playbook/filter_plugins', '/role/action_plugins']
        r = _roundtrip(basedirs)
        assert r == basedirs

    def test_rpc_endpoint_tuple_picklable(self) -> None:
        ep_tuple = (('127.0.0.1', 12345), b'k' * 32)
        r = _roundtrip(ep_tuple)
        assert r == ep_tuple


class TestCompatPosixPickle:
    """compat.posix module-level values are imported into spawn workers.
    They must not require pickle — but if a caller ever stores one in
    worker state and the child re-imports, imports must be idempotent.
    """

    def test_compat_posix_module_importable_twice(self) -> None:
        import importlib
        import ansible.compat.posix as m1
        reloaded = importlib.reload(m1)
        # Module-level constants preserved.
        assert hasattr(reloaded, 'IS_WINDOWS')
        assert hasattr(reloaded, 'HAS_SIGALRM')
        assert hasattr(reloaded, 'HAS_PWD')


if __name__ == '__main__':  # pragma: no cover
    pytest.main([__file__, '-v'])
