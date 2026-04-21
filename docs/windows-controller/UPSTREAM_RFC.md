# RFC: Decouple ansible-core from hardcoded `fork()` and stray POSIX imports

**Status:** draft
**Target:** upstream `ansible/ansible` `devel`
**Author origin:** `opsiclear-admin/ansible-windows` fork

## Abstract

Propose two small, behaviorally-neutral changes to ansible-core that
remove the module-import-time dependency on `fork()` and on direct
imports of POSIX-only stdlib modules (`fcntl`, `termios`, `pty`, `pwd`,
`grp`). Both changes are invisible on POSIX (where `fork` is still the
default and all those imports succeed) but unblock downstream forks
that want to run ansible-core as a controller on non-POSIX platforms
(Windows, and in principle anywhere CPython runs).

## Motivation

The upstream project's stated position is that Ansible cannot run on
Windows as a control node. The position is accurate *today*, but the
reason is not architectural — it's **two specific single-line
assumptions** that turn every Windows import of `ansible.cli` into a
hard failure. Every downstream attempt to run ansible-core on Windows
either patches these two lines privately or gives up and tells users
to use WSL.

The position could continue ("Windows is unsupported") while still
shipping these two changes — they cost nothing to POSIX users, and
they stop the downstream pain.

## Change 1: platform-dispatched multiprocessing context

### File
`lib/ansible/utils/multiprocessing.py`

### Current
```python
context = multiprocessing.get_context('fork')
```

### Proposed
```python
_start_method = 'spawn' if sys.platform == 'win32' else 'fork'
context = multiprocessing.get_context(_start_method)
```

### Impact
- POSIX: `'fork'` is still selected. Zero behavior change. Byte-
  for-byte identical behavior on Linux and macOS (where default
  was already forced off upstream's `spawn` default).
- Windows: `'spawn'` is selected. `ansible.cli` importable; no
  behavior guaranteed, but the crash at module-import-time is
  gone. Downstream forks can then do whatever additional work is
  needed for actual functionality.
- No CI changes required upstream. Add a Windows lane later if
  desired.

### Risk
Theoretically a POSIX distributor could have relied on the
`ValueError: cannot find context for 'fork'` exception on Windows as
a way to detect Windows. Unlikely and trivial to fix downstream.

## Change 2: `ansible.compat.posix` — graceful-degrade shim

### Files added
`lib/ansible/compat/posix.py` — new module (~200 lines)

### Files migrated
- `lib/ansible/_internal/_locking.py`: `fcntl.flock` →
  `compat.posix.flock_exclusive` (context manager)
- `lib/ansible/utils/display.py`: `os.register_at_fork` →
  `compat.posix.register_at_fork`; `fcntl.ioctl(TIOCGWINSZ)` →
  `compat.posix.get_terminal_columns` (which uses
  `shutil.get_terminal_size` — already stdlib, cross-platform)
- `lib/ansible/executor/process/worker.py`: `os.setsid` / `os.killpg`
  → `compat.posix.setsid` / `compat.posix.killpg`
- `lib/ansible/_internal/_errors/_alarm_timeout.py`: `signal.SIGALRM`
  → `compat.posix.start_alarm` / `.cancel_alarm` (threading.Timer
  fallback on platforms without SIGALRM, with a documented caveat)
- `lib/ansible/_internal/_templating/_template_vars.py`: `pwd.getpwuid`
  → `compat.posix.lookup_user_name`

### Impact
- POSIX: each shim function calls the real POSIX primitive. Zero
  behavior change.
- Non-POSIX: functions that have no meaningful substitute (e.g.
  `setsid`) become no-ops; functions with a stdlib equivalent (e.g.
  terminal size via `shutil`) route through it; functions with no
  substitute at all (e.g. `open_pty`) raise `NotImplementedError`.

### Risk
A library or plugin outside ansible-core that imports
`ansible.utils.display` and relies on `fcntl` being imported as a
side-effect would break. Plausible zero occurrences in the ecosystem.

## What this RFC does NOT propose

- Shipping Windows controller support upstream.
- Spawn-safe worker-handoff rewrite (`WorkerBootstrap`, plugin loader
  re-init in the child).
- Fixing the ~dozen other silent-on-Windows bugs we found (embedded
  `"` in ssh args, backslash-vs-forward-slash in zip paths, literal
  `/__init__.py` suffix checks, `COLLECTIONS_PATH` pathspec split, …).
- Deleting any upstream symlinks or changing their targets.

All of those are large, have behavior-visible side effects, and are
the right scope for the downstream fork, not for upstream.

## Why these two specifically

1. They are both **single-file** and **single-purpose**. Reviewable
   in minutes, rebaseable trivially.
2. They have **zero POSIX behavior change** — easy to prove by
   `git diff` and a unit-test matrix.
3. They unblock **every** downstream Windows-controller attempt. Many
   people have paid the cost of patching these two lines privately;
   paying it once upstream retires the tax.
4. They don't commit upstream to supporting Windows. The docs can
   (and should) still say "Windows as a control node is not
   supported; use WSL." The changes just remove a hard-coded refusal
   at import time.

## Alternatives considered

- **Maintain the hard refusal** (status quo). Current result: every
  new Windows user files the same `AttributeError` / `ValueError`
  issue, gets the same "use WSL" response. High support cost per
  affected user, zero downstream progress.
- **Ship Windows-controller officially**. Out of scope for this RFC,
  and possibly out of scope forever — that's the maintainer's call.
  This RFC is compatible with any decision there.
- **Land changes 1 and 2 behind an opt-in env var**. Adds complexity
  for zero benefit — the POSIX path is unchanged either way.

## Suggested review / merge plan

1. RFC discussion on forum.ansible.com.
2. PR with Change 1 only. Single-line diff + existing test matrix.
3. If accepted, PR with Change 2 (the shim + migrations) as a
   separate follow-up.

Both PRs can be prepared from `opsiclear-admin/ansible-windows` at
commit `befa49021c` or later by cherry-picking the two relevant
commits:

- `e15a9ca76c windows-controller: get ansible --version running on Windows`
  — contains Change 1 and the minimum-viable version of Change 2.
- `7fc7a65035 windows-controller: phase 2 posix compat shim`
  — the full compat shim and migrations.

(Both commits do some Windows-specific housekeeping that would be
split out of the upstream-targeted PRs.)
