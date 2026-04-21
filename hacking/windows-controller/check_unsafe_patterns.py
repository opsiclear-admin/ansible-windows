#!/usr/bin/env python
# Copyright: (c) 2026, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""
Sanity gate for the windows-controller fork.

Walks ``lib/ansible/**`` (excluding target-side code) and fails if it
finds POSIX-only patterns that weren't caught by the compat shim or an
explicit guard. Each pattern has a `known_ok` allowlist of
(file_rel, substring_on_line) pairs that were audited and are safe
(run through `compat.posix`, inside `try: import`, or platform-gated).

Intended to run in CI on every push so a future upstream rebase can't
silently reintroduce a pattern this fork has already stamped out.

Exit 0 clean, 1 on new unsafe hits, 2 on misconfig.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
LIB_ROOT = os.path.join(REPO_ROOT, 'lib', 'ansible')

EXCLUDED_PREFIXES = (
    os.path.join('lib', 'ansible', 'modules'),
    os.path.join('lib', 'ansible', 'module_utils'),
    os.path.join('lib', 'ansible', '_internal', '_ansiballz'),
    os.path.join('lib', 'ansible', 'galaxy', 'data'),
)


@dataclass(frozen=True)
class Pattern:
    regex: str
    description: str
    known_ok: tuple[tuple[str, str], ...]


_PATTERNS: tuple[Pattern, ...] = (
    Pattern(r'\bos\.fork\b', 'os.fork — not on Windows', ()),
    Pattern(r'\bos\.setsid\b', 'os.setsid — use compat.posix.setsid', (
        ('lib/ansible/compat/posix.py', 'os.setsid()'),
        ('lib/ansible/executor/process/worker.py', "hasattr(os, 'setsid')"),
    )),
    Pattern(r'\bos\.killpg\b', 'os.killpg — use compat.posix.killpg', (
        ('lib/ansible/compat/posix.py', 'os.killpg'),
        ('lib/ansible/executor/process/worker.py', "hasattr(os, 'killpg')"),
    )),
    Pattern(r'\bos\.register_at_fork\b', 'os.register_at_fork — use compat.posix.register_at_fork', (
        ('lib/ansible/compat/posix.py', 'os.register_at_fork'),
    )),
    Pattern(r'\bos\.getuid\b', 'os.getuid — use compat.posix.getuid', (
        ('lib/ansible/compat/posix.py', 'os.getuid'),
        # display.py:224 — comment in the fixed-via-compat.posix fallback path
        ('lib/ansible/utils/display.py', 'os.getuid` does not exist on Windows'),
    )),
    Pattern(r'\bos\.geteuid\b', 'os.geteuid — use compat.posix.geteuid', (
        ('lib/ansible/compat/posix.py', 'os.geteuid'),
    )),
    Pattern(r'\bos\.getpgrp\b', 'os.getpgrp — POSIX only, guard with sys.platform', (
        ('lib/ansible/utils/display.py', 'os.getpgrp()'),  # prompt_until, guarded above
    )),
    Pattern(r'\bos\.tcgetpgrp\b', 'os.tcgetpgrp — POSIX only, guard with sys.platform', (
        ('lib/ansible/utils/display.py', 'os.tcgetpgrp(self._stdin_fd)'),
    )),
    Pattern(r'\bos\.uname\b', 'os.uname — not on Windows', ()),
    Pattern(r'^\s*import\s+fcntl\b', 'top-level import fcntl — guard with try/except', (
        ('lib/ansible/compat/posix.py', 'import fcntl'),
        ('lib/ansible/cli/scripts/ansible_connection_cli_stub.py', 'import fcntl'),
        # display.py: inside the `if sys.platform == 'win32' … else:` block
        ('lib/ansible/utils/display.py', 'import fcntl'),
        # connection plugins: inside try: import fcntl except ImportError
        ('lib/ansible/plugins/connection/__init__.py', 'import fcntl'),
        ('lib/ansible/plugins/connection/ssh.py', 'import fcntl'),
    )),
    Pattern(r'^\s*import\s+pty\b', 'top-level import pty — guard with try/except', (
        ('lib/ansible/compat/posix.py', 'import pty'),
        # connection plugins: inside try/except ImportError
        ('lib/ansible/plugins/connection/local.py', 'import pty'),
        ('lib/ansible/plugins/connection/ssh.py', 'import pty'),
    )),
    Pattern(r'^\s*import\s+termios\b', 'top-level import termios — guard with try/except', (
        # display.py: inside the `if sys.platform == 'win32' … else:` block
        ('lib/ansible/utils/display.py', 'import termios'),
    )),
    Pattern(r'^\s*import\s+pwd\b', 'top-level import pwd — guard with try/except', (
        # compat/posix.py: inside try/except ImportError
        ('lib/ansible/compat/posix.py', 'import pwd'),
    )),
    Pattern(r'^\s*import\s+grp\b', 'top-level import grp — guard with try/except', (
        # compat/posix.py: inside try/except ImportError
        ('lib/ansible/compat/posix.py', 'import grp'),
    )),
    Pattern(r'\bsignal\.SIGALRM\b', 'signal.SIGALRM — POSIX only, use compat.posix alarm helpers', (
        ('lib/ansible/compat/posix.py', 'signal.SIGALRM'),
        ('lib/ansible/cli/scripts/ansible_connection_cli_stub.py', 'signal.SIGALRM'),
    )),
    Pattern(r"b?'/__init__\.py'|b?\"/__init__\.py\"", 'literal /__init__.py suffix — use os.path.basename', (
        # module_common.py:660 — comment explaining the (fixed) bug
        ('lib/ansible/executor/module_common.py', '/__init__.py'),
        # module_common.py:1104 — constructing a zip path; zip uses / by spec
        ('lib/ansible/executor/module_common.py', "'/'.join(module_path_parts[:idx])"),
    )),
    Pattern(r'start_new_session\s*=\s*True', 'subprocess Popen start_new_session=True — gate on sys.platform', (
        ('lib/ansible/plugins/connection/ssh.py', "start_new_session"),
        ('lib/ansible/plugins/connection/winrm.py', 'start_new_session=True'),  # kinit POSIX-only
    )),
    Pattern(r"\bstr\(rel_path\)", 'str(PurePath) in zip-filename context — use .as_posix()', ()),
    Pattern(r"path\[0\]\s*==\s*['\"]/['\"]", 'literal path[0]==/ — use os.path.isabs', ()),
    Pattern(r"dest\.startswith\(['\"]/['\"]\)", 'dest.startswith("/") on controller path — use os.path.isabs', ()),
    Pattern(r"pass_fds\s*=", 'subprocess pass_fds — not supported on Windows', (
        ('lib/ansible/galaxy/collection/gpg.py', 'pass_fds'),  # POSIX-branch only
        # ssh.py:1458 — comment explaining sshpass is POSIX-only (already disabled on Windows)
        ('lib/ansible/plugins/connection/ssh.py', 'pass_fds'),
    )),
)


def _walk_scan_targets():
    for dirpath, dirnames, filenames in os.walk(LIB_ROOT):
        dirnames[:] = sorted(dirnames)
        rel_dir = os.path.relpath(dirpath, REPO_ROOT)
        if any(rel_dir == e or rel_dir.startswith(e + os.sep) for e in EXCLUDED_PREFIXES):
            continue
        for name in sorted(filenames):
            if not name.endswith(('.py', '.yml')):
                continue
            yield os.path.join(dirpath, name)


def _rel_posix(path: str) -> str:
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, '/')


def scan() -> list[tuple[str, int, str, str]]:
    hits: list[tuple[str, int, str, str]] = []
    compiled = [(p, re.compile(p.regex)) for p in _PATTERNS]

    for path in _walk_scan_targets():
        rel = _rel_posix(path)
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                lines = fh.readlines()
        except OSError:
            continue

        for line_no, line in enumerate(lines, start=1):
            # Skip pure-comment lines so prose discussing a pattern doesn't
            # count as a hit. This misses comments on the same line as code,
            # which is fine — those are rare and we want the flag if present.
            stripped = line.lstrip()
            if stripped.startswith('#') or stripped.startswith('//'):
                continue
            for pat, rx in compiled:
                if not rx.search(line):
                    continue
                allowed = any(
                    rel == ok_rel and ok_substr in line
                    for ok_rel, ok_substr in pat.known_ok
                )
                if allowed:
                    continue
                hits.append((rel, line_no, line.rstrip('\n'), pat.description))
    return hits


def main() -> int:
    hits = scan()
    if not hits:
        print("check-windows-safety: OK — no new unsafe patterns")
        return 0

    print(f"check-windows-safety: {len(hits)} unsafe pattern(s) found:\n", file=sys.stderr)
    for rel, ln, text, why in hits:
        print(f"  {rel}:{ln}  [{why}]", file=sys.stderr)
        print(f"    | {text.strip()}", file=sys.stderr)
    print("\nIf any of the above are intentional (platform-guarded or inside "
          "try/except ImportError), add them to the known_ok allowlist in "
          "hacking/windows-controller/check_unsafe_patterns.py.", file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
