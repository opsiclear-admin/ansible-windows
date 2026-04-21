#!/usr/bin/env python
# Copyright: (c) 2026, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""
Phase 4 byte-identity probe: build an AnsiballZ payload for ansible.builtin.ping
and print its SHA256. Run on both the Windows and Linux CI lanes; the final
compare job asserts the hashes match.

The datetime stamped into each zip entry is frozen to a fixed value so the
only bytes that move across platforms are those we care about — module source
assembly, wrapper code, embedded args, shebang. Line endings must stay LF.

Exit 0 = hash printed; exit 1 = unexpected CRLF or missing shebang.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import sys

os.environ.setdefault('PYTHONUTF8', '1')


def _freeze_datetime() -> None:
    """Replace ``datetime.datetime.now`` in module_common with a constant."""
    import ansible.executor.module_common as _mc

    _FROZEN = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)

    class _FrozenDT(_mc.datetime.datetime):  # type: ignore[name-defined,misc]
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:  # type: ignore[override]
            return _FROZEN.astimezone(tz) if tz is not None else _FROZEN.replace(tzinfo=None)

    _mc.datetime.datetime = _FrozenDT  # type: ignore[attr-defined,assignment]


def _bootstrap_ansible() -> None:
    from ansible import context
    from ansible.plugins.loader import init_plugin_loader
    from ansible.utils.context_objects import CLIArgs

    context.CLIARGS = CLIArgs({})
    init_plugin_loader([])


def _build_ping_payload() -> bytes:
    from ansible.executor.module_common import modify_module
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar

    # Resolve the builtin ping module relative to this file to avoid CWD drift.
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    module_path = os.path.join(repo_root, 'lib', 'ansible', 'modules', 'ping.py')

    loader = DataLoader()
    templar = Templar(loader=loader)

    # Pin a fixed remote interpreter so the shebang doesn't trigger interpreter
    # discovery (which would try to exec Python on a remote host).
    task_vars = {'ansible_python_interpreter': '/usr/bin/python3'}

    built = modify_module(
        module_name='ansible.builtin.ping',
        module_path=module_path,
        module_args={'data': 'pong'},
        templar=templar,
        task_vars=task_vars,
    )
    return built.b_module_data


def main() -> int:
    _bootstrap_ansible()
    _freeze_datetime()

    payload = _build_ping_payload()

    if b'\r\n' in payload:
        print('FAIL: CRLF line endings detected in AnsiballZ payload', file=sys.stderr)
        return 1

    if not payload.startswith(b'#!'):
        print(f'FAIL: expected shebang at byte 0, got {payload[:40]!r}', file=sys.stderr)
        return 1

    digest = hashlib.sha256(payload).hexdigest()
    print(f'platform={sys.platform}')
    print(f'length={len(payload)}')
    print(f'sha256={digest}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
