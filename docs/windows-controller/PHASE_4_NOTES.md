# Phase 4 — AnsiballZ cross-platform notes

## What is verified

As of commit `2d64ddcf54` on `windows-controller`, the Phase 4 CI job
demonstrates the following for `ansible.builtin.ping`:

- No CRLF anywhere in the outer AnsiballZ payload on either platform.
- Valid shebang (`#!/usr/bin/python3`) at byte 0 on either platform.
- For every module_utils / wrapper source that appears in *both* the
  Windows-built and Linux-built payloads, the zip-member size and CRC
  match exactly. That means the bytes shipped to the target are
  byte-identical for the shared set.
- Gitattributes now actually force `text eol=lf` on every `.py` file
  under the repo — the prior `**.py` patterns did not recurse.
- `module_common._make_zinfo` pins `create_system=3` so zip metadata
  doesn't leak the controller's host OS.
- `_find_module_utils` uses `PurePath.as_posix()` instead of `str()` so
  zip member paths don't pick up Windows backslash separators.

## What remains — tracked as phase 4.5

The compare job still flags two structural divergences:

1. **Package-dir vs single-file flattening.** A Windows controller
   bundles `ansible/module_utils/_internal.py` (a standalone file),
   whereas Linux bundles `ansible/module_utils/_internal/__init__.py`
   (the package init). The CRC + size of the two entries is nearly
   identical — same source, different write path. Windows is
   collapsing `foo/__init__.py` down to `foo.py` somewhere in the
   ModuleDepFinder path walk. Likely suspects:

   - `importlib.resources.files('ansible').parent` on Windows vs POSIX
   - `pathlib.PurePath.parts` behavior across an editable `pip -e .`
     install (Windows wheels lay out site-packages differently)
   - `ModuleDepFinder._find_toplevel` naming convention

2. **`ansible.legacy` collection prefix.** Windows lists the ping
   payload under `ansible/legacy/ansible/builtin/ping.py`, where
   Linux writes `ansible/modules/ping.py`. The zip CRCs are identical
   (`2325 / 151128253`) so the *content* is the same; the namespacing
   is not. The module FQN resolution appears to route through the
   "legacy" collection virtual namespace on Windows. Needs investigation
   in `plugins/loader.py` FQN resolution under a Windows editable install.

Both issues are structural (same content, different zip member name).
Neither produces a different byte-for-byte *module body* on the wire —
the remote interpreter sees the same bytes. But the zip namespace
divergence would break `import ansible.modules.ping` on the remote side
if one platform ships the `legacy/builtin/` path and another ships
`modules/`.

## Phase 4 exit criterion — status

Per `PLAN.md`: *"Golden-output test: ZIP payload assembled on Windows
byte-identical to the one assembled on Linux for a canonical module set."*

Status: **partially achieved**. Bytes of the shared set match, zip is
valid, line endings are clean. Full byte-identity is blocked on the two
structural items above. CI warns on structural divergence; content CRCs
are required to match or the build fails.
