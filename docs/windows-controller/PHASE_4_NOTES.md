# Phase 4 — AnsiballZ cross-platform notes

## Status: closed

As of commit `bd408862e9`, the Phase 4 CI compare job confirms **full
byte-identity** for the canonical probe module (`ansible.builtin.ping`)
between a Windows controller and a Linux controller:

```
Windows outer=6b7fa0c8…  inner=cc589a5e…
Linux   outer=6b7fa0c8…  inner=cc589a5e…
Zip member set is identical across platforms.
Shared members (56) all have matching size + CRC.
Embedded-zip content is byte-identical across platforms (inner sha256 matches).
```

Both the outer AnsiballZ wrapper AND the embedded zip hash match. The
56 bundled module_utils / wrapper sources appear under identical zip
paths with identical CRCs.

## What was fixed to get here

1. **`.gitattributes`** — `**.py` does not recurse in git's attribute
   matcher; it's parsed as `*.py`, matching only direct children.
   Replaced with a global `*.py text eol=lf` (plus `*.yml`, `*.yaml`,
   and explicit CRLF carve-outs for `*.ps1`, `*.psm1`, and everything
   under `lib/ansible/executor/powershell/`).

2. **`ZipInfo.create_system`** — Python's zipfile stamps the creator-OS
   byte (0 = MS-DOS/Windows, 3 = Unix) into every entry header based on
   the host platform. Pinned to `3` in `module_common._make_zinfo`
   because the payload is destined for a POSIX target regardless of
   controller OS.

3. **`_find_module_utils` path composition** — `str(rel_path)` on
   Windows yields backslash-separated strings; those flowed straight
   into `zipfile.ZipInfo.filename` and produced invalid POSIX zip
   paths. Replaced with `rel_path.as_posix()`.

4. **`LegacyModuleUtilLocator._find_module`** (`module_common.py:659`)
   — `info.origin.endswith('/__init__.py')` never matched on Windows
   because `importlib.machinery.PathFinder.find_spec` returns paths
   with backslashes. Every module_utils package was silently demoted
   to a single-file module, producing entries like
   `module_utils/_internal.py` instead of
   `module_utils/_internal/__init__.py`. Switched to
   `os.path.basename(info.origin) == '__init__.py'`, which is
   separator-agnostic.

5. **`CORE_LIBRARY_PATH_RE`** (`module_common.py:152`) — the regex is
   compiled against POSIX-style paths, but the incoming `module_path`
   on Windows uses backslashes. The regex never matched, so every
   core module fell through to the `ansible.legacy.<name>` fallback
   FQN in `_find_module_utils`, producing zip entries like
   `ansible/legacy/ansible/builtin/ping.py` instead of
   `ansible/modules/ping.py`. Fixed by normalizing both `site_packages`
   at module import time and inbound `module_path` inside
   `_get_ansible_module_fqn` to forward slashes before the regex match.

## CI gate

The `cross-platform AnsiballZ byte-identity` job now enforces:

- **Hard fail** on any shared zip member disagreeing on (size, CRC).
- **Hard fail** if either hash step produces no hash at all.
- **Informational print** on outer/inner SHA256 identity.
- **Warning** (no longer a failure path in practice) if zip members
  diverge structurally between platforms, since that state no longer
  occurs.

## Future — reproducibility guards worth adding

- A test that runs this probe for one module per collection namespace
  (core `ansible.builtin`, plus a representative
  `ansible.windows` / `community.general` when available) to catch
  regressions in the collection FQN regex path.
- A test that also exercises a module with binary data / unusual
  encoding to make sure the LF normalization doesn't corrupt payloads.
