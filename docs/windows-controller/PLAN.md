# Windows-Native Ansible Controller — Implementation Plan

**Audited commit:** `7611079116` on `devel`
**Branch:** `windows-controller`
**Target:** Native Windows controller (no WSL, no Cygwin) — the user has mandated the native path.

## Scope

**In scope** — the controller on Windows:
- Parses playbooks, inventories, vars, vaults
- Drives remote targets via `winrm`, `psrp`, and (Phase 5) `ssh`
- Ships PowerShell modules to Windows targets and Python modules to Linux targets over SSH

**Out of scope** — deferred indefinitely:
- `local` connection plugin (runs POSIX modules on the controller)
- `become` via sudo/pty
- `paramiko_ssh` (lives in the `ansible.netcommon` collection anyway)
- `ansible-test` porting (uses `fcntl`; we use plain pytest on Windows)

## POSIX dependency audit — summary

Audit completed at commit `7611079116`. The full grep-level findings are in this document; the **single linchpin** is:

```
lib/ansible/utils/multiprocessing.py:15
context = multiprocessing.get_context('fork')
```

Everything else cascades from this one line (via the `from ansible.utils.multiprocessing import context` chain in `task_queue_manager.py` and `worker.py`).

But the **first** error on a Windows `import ansible.cli` is actually further upstream:

```
lib/ansible/utils/display.py:241
os.register_at_fork(after_in_child=...)
```

— because `Display` is a Singleton initialized at `cli/__init__` import time, and `register_at_fork` does not exist on Windows.

### Critical files

| File | Lines | Windows blocker |
|---|---|---|
| `lib/ansible/utils/multiprocessing.py` | 15 | `get_context('fork')` — linchpin |
| `lib/ansible/utils/display.py` | 33, 41, 44, 241, 971, 1019-1052 | `fcntl`, `termios`, `os.register_at_fork`, `TIOCGWINSZ` |
| `lib/ansible/executor/process/worker.py` | 94, 108-116, 149-175, 188-198 | "works due to fork" comment; `os.killpg`, `os.setsid`, `O_NONBLOCK`, SIGTERM |
| `lib/ansible/executor/task_queue_manager.py` | 53, 93, 173, 204, 209-210, 232, 244 | fork context import; `TemporaryFile` fd inheritance; RPC Manager shared_instance; SIGTERM/SIGINT signal handlers |
| `lib/ansible/_internal/_rpc_host.py` | 107-117, 150-166 | `LocalManager` auth relies on fork COW — spawn needs explicit `(address, authkey)` handoff |
| `lib/ansible/plugins/connection/__init__.py` | 8, 230, 235 | `fcntl.lockf` on inherited fd |
| `lib/ansible/plugins/connection/ssh.py` | 418, 424, 768, 1070, 1086-1087, 1124, 1130-1146, 1207-1366 | `import fcntl`, `import pty`; `start_new_session` POSIX-only Popen kwarg; `O_NONBLOCK` + `selectors` on pipe fds; sshpass pipe |
| `lib/ansible/_internal/_locking.py` | 4, 16, 21 | `fcntl.flock` |
| `lib/ansible/parsing/vault/__init__.py` | 20, ~1035 | `fcntl` for vault fd lock + `is_fd` check |
| `lib/ansible/_internal/_errors/_alarm_timeout.py` | 51, 66 | `signal.SIGALRM` (POSIX-only) |

---

## Phase 0 — Groundwork & decision gates

**Goal:** Establish the fork, lock in a Windows CI runner, persist the plan & kill criteria.

**Tasks:**
1. Create `windows-controller` long-lived branch off `devel`.
2. Add `"Operating System :: Microsoft :: Windows"` classifier to `pyproject.toml` alongside existing POSIX classifier.
3. Update `AGENTS.md` WSL-requirement note (later — leave for a PR that includes real working code).
4. Create `.github/workflows/windows-controller.yml` on `windows-2022` + Python 3.12/3.13 matrix; run `pip install -e .` + import smoke test. Expected initial failure: `register_at_fork` / `fork` context — **that failure is the Phase 1 starting point**.
5. Add `.gitattributes` rules to keep AnsiballZ wrapper source LF-terminated regardless of `core.autocrlf`.
6. Write `KILL_CRITERIA.md`.

**Exit criteria:**
- Windows CI lane runs and reports the expected `ImportError` / `ValueError`.
- Linux CI lane (existing) still green — regression baseline locked.
- This PLAN.md and KILL_CRITERIA.md committed.

**Effort:** 1 person-week.

---

## Phase 1 — Replace fork-based executor with spawn

**Goal:** `ansible -m win_ping -i winrm_inv.yml winhost` returns green on Windows.

This phase is the riskiest and highest-effort. Everything else is plumbing — this is architecture.

### Fork-dependent call sites

| File | Line | Change |
|---|---|---|
| `lib/ansible/utils/multiprocessing.py` | 15 | `context = multiprocessing.get_context('spawn' if sys.platform == 'win32' else 'fork')` |
| `lib/ansible/executor/process/worker.py` | 94 | Move `self._loader._tempfiles = set()` from constructor into `_run` (child-side init, not parent) — the current code comment says it works because of fork COW |
| `lib/ansible/executor/process/worker.py` | 108 | Wrap `os.killpg` in `hasattr(os, 'killpg')`; Windows fallback: `terminate()` |
| `lib/ansible/executor/process/worker.py` | 149-175 | Split `_detach` into `_detach_posix` + no-op `_detach_windows` |
| `lib/ansible/executor/process/worker.py` | 188-192 | Guard `signal.SIGTERM` handler (SIGTERM is synthetic on Windows) |
| `lib/ansible/executor/task_queue_manager.py` | 204 | Replace inherited-fd `TemporaryFile` lock with `multiprocessing.Manager().Lock()` — spawn workers cannot inherit the fd |
| `lib/ansible/executor/task_queue_manager.py` | 173 | Pass `LocalManager`'s `(address, authkey)` explicitly to each worker via a new `WorkerBootstrap` dataclass |
| `lib/ansible/executor/task_queue_manager.py` | 209-210, 232, 244 | Guard `SIGTERM` handlers; `os.kill` semantics on Windows |

### Pickle-safety — the hard part

Under spawn, every argument to `WorkerProcess.__init__` must pickle cleanly. Current call site: `lib/ansible/plugins/strategy/__init__.py:397`.

Risk ranking of the current args:
1. **`shared_loader_obj` (highest risk)** — from `plugin_loader.get_plugin_loader_namespace()`. Contains `connection_loader`, `module_loader`, callback/filter loaders holding compiled plugin refs. **Will not pickle.** Solution: do not pass it; re-initialize plugin loaders in the child via `init_plugin_loader()`.
2. `variable_manager` — holds full `inventory` reference. Deep graph. Re-initialize from paths in child.
3. `loader: DataLoader` — holds vault secrets. Audit `__reduce__` on vault secret classes in `parsing/vault/__init__.py`.
4. `task_vars: dict` — contains lazy Jinja objects (`LazyTemplateDict`). Must pickle.
5. `host: Host`, `task: Task`, `play_context: PlayContext` — core data objects. Historically picklable but needs validation.
6. `final_q: FinalQueue` — multiprocessing queue; pickles via spawn handoff protocol. OK.

### `WorkerBootstrap` design

New dataclass carrying only serializable state:
- Inventory paths, collection paths, ansible.cfg paths
- Vault secrets (base64 bytes + identity labels)
- `CLIARGS` dict
- RPC Manager `(address, authkey)` tuple
- Config overrides

Worker startup sequence (in child):
1. Restore `context.CLIARGS`
2. `plugin_loader.init_plugin_loader(...)` — rebuild loaders in-child
3. Reconnect to `LocalManager` via `BaseManager(address=..., authkey=...).connect()`
4. Rehydrate `Display` singleton and attach queue
5. Re-import `constants` (picks up ansible.cfg naturally under spawn)
6. Reconstruct inventory from paths (don't ship inventory itself)

**Milestone:** `ansible -i winrm_inv.yml -m win_ping winhost` → `SUCCESS` on Windows.

**Risks:**
1. Pickle tail (high × high): every new type in the call graph can break. Mitigation: comprehensive pickle-roundtrip test suite covering Task/Host/PlayContext/Inventory/VariableManager/DataLoader.
2. Spawn startup cost (med × high): ~150-400ms per worker vs ~10ms for fork. With `forks=5` + 10 tasks, multi-second regression. Mitigation: persistent worker pool (architectural, may slip to Phase 1.5).
3. Cross-process locking: `fcntl.lockf` on the connection_lockfd breaks under spawn. Mitigation: `Manager().Lock()` (simplest) or `msvcrt.locking` behind the Phase 2 shim.

**Effort:** 5 person-weeks (p50), 8 (p80).

**Exit criteria:** `win_ping` green on Windows; Linux regression suite still passes.

---

## Phase 2 — POSIX compat shim

**Goal:** `python -c "import ansible.cli; import ansible.executor.task_queue_manager"` exits 0 on Windows with no `ImportError`.

### New module: `lib/ansible/compat/posix.py`

Provides platform-dispatched implementations:

```
# Signatures only — do not implement yet
get_euid() -> int                       # 0 on Windows
register_at_fork(*, after_in_child) -> None       # no-op on Windows
flock_exclusive(fd_or_path) -> ContextManager     # fcntl.lockf | msvcrt.locking
open_pty() -> tuple[int, int]           # raises NotImplementedError on Windows
setsid() -> None                        # no-op on Windows
killpg(pgid, sig) -> None               # no-op or terminate on Windows
get_terminal_size() -> tuple[int, int]  # shutil.get_terminal_size() cross-platform
alarm(seconds, callback) -> Timer        # SIGALRM vs threading.Timer
set_nonblocking(fd) -> None             # fcntl.O_NONBLOCK vs threaded reader path
```

### Callers to migrate

| File | Line | Change |
|---|---|---|
| `lib/ansible/_internal/_locking.py` | 4, 16, 21 | Replace `fcntl.flock` → `compat.posix.flock_exclusive` |
| `lib/ansible/parsing/vault/__init__.py` | 20, ~1035 | `fcntl.lockf` → shim; `is_fd` check via `os.fstat` |
| `lib/ansible/utils/display.py` | 33, 41, 44, 241, 971, 1019-1052 | `register_at_fork` → no-op; `setraw`/`TIOCGWINSZ` → `shutil.get_terminal_size()`; prompt handling via `msvcrt.getch` on Windows |
| `lib/ansible/plugins/connection/__init__.py` | 8, 230, 235 | `fcntl.lockf` → shim |
| `lib/ansible/_internal/_errors/_alarm_timeout.py` | 51, 66 | `SIGALRM` → `threading.Timer` (behavioral difference: can't interrupt C-level blocking) |
| `lib/ansible/_internal/_templating/_template_vars.py` | 5 | `import pwd` guarded; `getpwnam` wrapped |

**Files NOT touched** (run only on remote target):
- `lib/ansible/module_utils/basic.py`, `module_utils/facts/**`, `module_utils/service.py`
- `lib/ansible/modules/**` (even `modules/user.py:509 import pty` — remote-side)

**Risks:**
1. Display's `prompt_until` with character-at-a-time + backspace: Windows `msvcrt.getch` has different semantics. Mitigation: v1 uses plain `getpass` (line-based); accept UX regression.
2. `_alarm_timeout` + `threading.Timer` cannot interrupt C-level blocking Python code. Document the limitation.

**Effort:** 2.5 person-weeks.

**Exit criteria:** Clean imports on Windows; Linux regression unchanged.

---

## Phase 3 — Connection plugin triage

**Goal:** A 10-task WinRM playbook runs green on Windows controller.

| Plugin | Action |
|---|---|
| `winrm`, `psrp` | No changes. Clean of POSIX imports; ready once executor (Phase 1) and shim (Phase 2) are in place. |
| `ssh` | Guard `import pty` and `import fcntl` behind platform check. Add early runtime guard raising `AnsibleError("ssh connection plugin on Windows controller is not yet implemented — see Phase 5")` at `Connection._connect`. |
| `local` | Block at `plugins/loader.py` resolution on Windows — raise `AnsibleError` with clear message. File remains on disk (avoids upstream merge conflicts). |
| `paramiko_ssh` | Not in core (netcommon collection). No action. |

**Milestone demo** (`test_winrm_playbook.yml`):
```yaml
- hosts: windows
  tasks:
    - ansible.windows.win_ping:
    - ansible.windows.win_whoami:
    - ansible.windows.win_command: {cmd: hostname}
    - ansible.windows.win_stat: {path: C:\Windows\System32}
    - ansible.windows.win_copy: {content: "hello", dest: C:\temp\t.txt}
    - ansible.windows.win_service: {name: Spooler, state: started}
    - ansible.windows.win_user: {name: testuser, state: absent}
    - ansible.windows.win_feature: {name: Telnet-Client, state: absent}
    - ansible.windows.win_reg_stat: {path: HKLM:\SOFTWARE\Microsoft}
    - ansible.builtin.debug: {msg: "done"}
```

**Risks:**
1. `pypsrp` wheel pinning for `cryptography` / `cffi` on Windows — nail versions in Phase 0.
2. WinRM `put_file` non-pipelined path needs validation.

**Effort:** 1.5 person-weeks.

**Exit criteria:** Demo playbook completes 10/10 green end-to-end.

---

## Phase 4 — Module assembly cleanup

**Goal:** AnsiballZ payloads assembled on Windows are byte-identical to those assembled on Linux for the same inputs.

| File | Line | Change |
|---|---|---|
| `lib/ansible/executor/module_common.py` | 1635 | Already writes LF-only via `b"\n".join(b_lines)` — add explicit unit test. |
| `lib/ansible/executor/module_common.py` | 1522-1541 | `_extract_interpreter` strips `\r` via `.strip()` — safe. Add CRLF fixture test. |
| `.gitattributes` | — | Add `* text=auto eol=lf` for wrapper template files to prevent CRLF leaking via `core.autocrlf=true` on Windows clones. (Done in Phase 0.) |

**Effort:** 1 person-week.

**Exit criteria** (demonstrated in Phase 5): golden-byte test for ansiballz payload across platforms.

---

## Phase 5 — SSH on Windows

**Goal:** `ansible-playbook -c ssh -i linux_inv linux_target` runs end-to-end from Windows controller.

**Decision:** wrap Microsoft's OpenSSH-for-Windows via `subprocess`. Rejected alternatives:
- `paramiko`: new large dependency, sync-only API
- `asyncssh`: cleaner long-term but 4-5 weeks of asyncio↔multiprocessing integration work; earmarked for v2

### Implementation in `lib/ansible/plugins/connection/ssh.py`

1. Lines 418, 424: guard `import pty` / `import fcntl` behind `sys.platform != 'win32'`. Define `_HAS_PTY = False` on Windows.
2. Lines 904-933 (ControlPersist): force `controlpersist = False` on Windows; emit one-time warning; document in plugin docstring.
3. Line 1070: remove `start_new_session=True` on Windows; replace with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`.
4. Lines 1130-1146 (pty branch): already try/except-guarded — falls through to `PIPE` path once import is guarded.
5. Lines 1207-1366 (`_bare_run` IO loop): rewrite with **threaded readers**. One thread per pipe reads into a byte queue; main thread pops with timeout. Avoids `fcntl.O_NONBLOCK` and `selectors` on pipe fds.
6. Lines 1074-1097 (`_sshpass_cmd`): disable on Windows — raise `AnsibleError` if `password_mechanism='sshpass'`. Valid mechanisms on Windows: `ssh_askpass`, `disable`.
7. Line 768: `os.getuid()`/`os.geteuid()` → `getpass.getuser()` + static `0` via shim.

### Optional Phase 5.5 — in-process connection pool

Keep long-lived `ssh` subprocess per-host per-worker; dispatch `exec_command` through it. Effectively in-process ControlMaster/ControlPersist replacement. Recovers most of the multiplexing perf loss from disabling ControlMaster. 2 person-weeks.

**Risks:**
1. Windows subprocess quoting differs — pass argv as list to avoid CreateProcess quoting drama.
2. No multiplexing = 3x-10x slowdown for multi-task plays — Phase 5.5 mitigates.
3. Microsoft OpenSSH bugs (long-running procs, stderr ordering). Pin to OpenSSH 9.5p1+.

**Effort:** 3 person-weeks (v1); +2 (pool, optional).

**Exit criteria:** 10-task playbook `-c ssh` against Linux target completes green; golden-byte ansiballz test passes.

---

## Phase 6 — Test matrix & upstream relationship

**Goal:** Sustainable Windows CI, defined fork maintenance strategy.

**Windows CI:**
- Matrix: `windows-2022`, Python 3.12 / 3.13
- Jobs: `import_smoke`, `unit_tests` (curated subset), `integration_winrm` (against a test Windows target), `integration_ssh_to_linux` (target Linux runner on same workflow)
- **Do not port `ansible-test`** — it's `fcntl`-heavy; use plain `pytest` + bespoke integration harness on Windows
- Exclude POSIX-mocked tests: `test/units/plugins/connection/test_ssh.py` → parallel `test_ssh_windows.py`; `test_local.py` excluded
- Target pass rate at Phase 6 end: 70%+ unit, 90%+ curated integration

**Upstream strategy** — fork with periodic sync:
- Ongoing 1 day/week rebase effort against upstream `devel`
- Contribute uncontroversial shims back (e.g., the `get_context` fix + `compat.posix` module) via RFC; everything else stays downstream until the fork is production-proven
- Do not pursue wholesale upstream merge until the fork has at least one production user

**Effort:** 2 person-weeks + ongoing.

---

## Totals

| Phase | Weeks (p50) | Cumulative |
|---|---|---|
| 0 — Groundwork | 1 | 1 |
| 1 — Spawn executor | 5 | 6 |
| 2 — Compat shim | 2.5 | 8.5 |
| 3 — WinRM/PSRP | 1.5 | 10 |
| 4 — Module assembly | 1 | 11 |
| 5 — SSH (v1) | 3 | 14 |
| 5.5 — SSH pool (opt) | 2 | 16 |
| 6 — CI & upstream | 2 | 18 |
| **Total p50** | **18 person-weeks** | |
| **Total p80** | **24 person-weeks** | |

**At one engineer:** 4.5–6 months of focused work.
**At two engineers:** 2.5–3.5 months.

---

## Top-3 risks (project-level)

| Risk | Likelihood × Impact | Mitigation |
|---|---|---|
| Pickle tail in Phase 1 has unbounded surface area | High × High | Comprehensive pickle-roundtrip test suite built early in Phase 1 |
| Windows perf regression turns the port into a toy (10x+ slower) | Medium × High | Persistent worker pool; SSH connection pool; must demo acceptable perf before Phase 6 |
| Upstream churn invalidates compat layer | Medium × Medium | Weekly (not monthly) rebase; keep shim minimal-footprint |
