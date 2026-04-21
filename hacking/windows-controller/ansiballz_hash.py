#!/usr/bin/env python
# Copyright: (c) 2026, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""
Phase 4 AnsiballZ cross-platform probe: build the payload for
ansible.builtin.ping, hash it three ways, and print a machine-readable
summary the CI compare job consumes.

Outputs (lines prefixed with the tag name):

  platform=<sys.platform>
  length=<bytes>
  sha256=<hex>                           # whole-payload hash
  inner_zip_sha256=<hex>                 # base64-decoded embedded zip
  inner_zip_members=<json list of [name, size, crc] tuples>

The wrapper text around the embedded zip contains some platform-dependent
bytes (Python's repr of a few literals, newline patterns in the template);
byte-identity there is not worth chasing. The embedded zip and its member
CRCs are the real cross-platform claim, and the compare job asserts those
match exactly.

Always asserts:
  - no CRLF anywhere in the outer payload
  - first bytes are a shebang
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import io
import json
import os
import re
import sys
import zipfile

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

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    module_path = os.path.join(repo_root, 'lib', 'ansible', 'modules', 'ping.py')

    loader = DataLoader()
    templar = Templar(loader=loader)

    task_vars = {'ansible_python_interpreter': '/usr/bin/python3'}

    built = modify_module(
        module_name='ansible.builtin.ping',
        module_path=module_path,
        module_args={'data': 'pong'},
        templar=templar,
        task_vars=task_vars,
    )
    return built.b_module_data


def _extract_embedded_zip(payload: bytes) -> bytes:
    """Pull the base64-encoded zip out of the AnsiballZ wrapper and decode it."""
    m = re.search(rb"'([A-Za-z0-9+/]{1000,}={0,2})'", payload)
    if not m:
        raise RuntimeError("Could not locate the embedded base64 zip inside the AnsiballZ wrapper.")
    return base64.b64decode(m.group(1))


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

    inner = _extract_embedded_zip(payload)

    with zipfile.ZipFile(io.BytesIO(inner)) as zf:
        members = sorted(
            [info.filename, info.file_size, int(info.CRC) & 0xFFFFFFFF]
            for info in zf.infolist()
        )

    print(f'platform={sys.platform}')
    print(f'length={len(payload)}')
    print(f'sha256={hashlib.sha256(payload).hexdigest()}')
    print(f'inner_zip_sha256={hashlib.sha256(inner).hexdigest()}')
    print(f'inner_zip_members={json.dumps(members)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
