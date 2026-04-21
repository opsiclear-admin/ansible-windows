# Audit merge — three independent Opus passes

Three parallel audits ran on branch `windows-controller` at commit
`7587a9394f` (tag `v0.1-windows`):

- **Agent 1 (code grep):** exhaustive regex scan of `lib/ansible/**`
  for Windows-unsafe patterns. 60 hits categorized A/B/C.
- **Agent 2 (design):** read every doc + commit log, assessed
  invariants-vs-evidence, fragile-elegance patterns, RFC
  shippability, production readiness.
- **Agent 3 (coverage):** audited test coverage against realistic
  ansible usage — feature combinations, failure modes, hybrid plays,
  credential hygiene, scale risks.

This document merges their findings into a single ordered close-out
plan.

## Merged severity matrix

Legend: **M** = must-fix (real user hits today), **S** = should-fix
(fragile; documentable but risky), **N** = nice-to-have.

### Must-fix (M) — concrete code bugs, finite scope

| # | File:Line | Bug | Source | Fix shape |
|---|---|---|---|---|
| M1 | `utils/display.py:224` | `os.getuid()` in `getpass.getuser()` fallback — `AttributeError` on Windows when `getuser()` fails | A1 | Replace with `compat.posix.getuid()` |
| M2 | `galaxy/collection/gpg.py:52,77` | `os.pipe()` + `Popen(pass_fds=…)` — `ValueError` on Windows (`pass_fds` unsupported). Breaks `ansible-galaxy collection install --signature-check`. | A1 | Windows path: skip signature verification with a clear warning, OR use a temp file instead of pipe |
| M3 | `cli/galaxy.py:1443` | `":".join(collections_path)` for user-visible error message — ambiguous on Windows (`C:\...:C:\...`) | A1 | `os.pathsep.join(...)` |
| M4 | `plugins/action/copy.py:141` | `u"/".join(parent_dir_list[:parent])` fed to `os.stat` — fails on Windows local path reassembly | A1 | Use `os.path.join` / rebuild as list-based walk |
| M5 | `plugins/action/fetch.py:154` | `dest.startswith("/")` on controller-side `dest` path | A1 | `os.path.isabs(dest)` |
| M6 | `_internal/_rpc_host.py:160` | `LocalManager.shared_instance()` assumes fork — under spawn, child creates a *fresh* Manager with a different authkey. Inventory RPC from any worker silently talks to a dead manager. | A2 | Explicit `(address, authkey)` handoff via `WorkerBootstrap`, as `PLAN.md` promised but never delivered |
| M7 | `cli/scripts/ansible_connection_cli_stub.py:5,120,127,272` | Top-level `import fcntl`, `signal.SIGALRM`, `os.pipe+fork_process` — whole file breaks on Windows import | A1 | File is POSIX-only by design (persistent-connection socket worker). Guard the module to raise a clean error on Windows rather than AttributeError-at-import |
| M8 | `compat/posix.py:82-89` | `flock_exclusive` on Windows uses `msvcrt.locking(LK_LOCK, 1)` in a tight retry loop with no sleep — pegs a CPU core under contention, no timeout escape for stuck AV locks | A2 | Add exponential backoff sleep between retries; add an overall timeout with a clean exception |

### Should-fix (S) — documented fragility, not immediately broken

| # | Area | Concern | Source |
|---|---|---|---|
| S1 | `_SSHSession` marker collision | 24-hex token low but nonzero; no defensive detection | A2 |
| S2 | `_SSHSession` stderr cross-task leak | `self._stderr_buf` bytes from task N can tail into task N+1 if marker arrives late | A2 |
| S3 | `_PipePoller` teardown phantom EOF events on Windows | `mark_closed` doesn't stop the reader thread; trailing EOF can poison next session | A2 |
| S4 | `display.py:1151` `os.getpgrp/tcgetpgrp` | Inside `prompt_until` non-Win32 branch; verify the `sys.platform == 'win32'` guard at line 1046-1050 is tight | A1, A2 |
| S5 | `winrm.py:383` `start_new_session=True` | No-op on Windows but inconsistent; `kinit` path itself is POSIX-only | A1 |
| S6 | **Zero unit tests added** | No pickle-roundtrip for Task/Host/PlayContext/DataLoader; every invariant is stress-run proof | A2 |
| S7 | **AnsiballZ byte-identity gate is 1 module** | `ansible.builtin.ping` only; structural divergence is a warning, not a fail | A2 |
| S8 | **README overstates evidence** | 10-task WinRM claim = 5 tasks in CI; "free strategy" claimed untested; vault handoff under spawn not audited | A2 |
| S9 | `PERSISTENT_WORKER_POOL.md` gaps | Backpressure, crash recovery, shutdown signaling, state-isolation scope all underspecified | A2 |
| S10 | `UPSTREAM_RFC.md` not PR-ready | No CI evidence, no changelog fragment, two-PR split undone, forum discussion not done | A2 |
| S11 | Stress-suite credential hygiene | `vault_pass.txt` with plaintext `vaultpass`; hardcoded key paths in inventories; CI writes password to tmp file then bash-interpolates | A3 |
| S12 | CI has no exit-code assertions | `failure_mix` should assert exit=3 (unreachable), CI just checks step runs | A3 |
| S13 | No grep-audit CI sanity gate | The scan agent 1 ran by hand should be a CI step | A2 |
| S14 | No rebase procedure / drift-detect CI | 35+ commits diverging; no documented process | A2 |
| S15 | Stress suite not wired into CI | All hand-run; new user PRs don't get durability signal | A3 |
| S16 | Windows-target path shapes untested | Spaces, UNC, long (>MAX_PATH), non-ASCII, reserved names, trailing dot | A3 |
| S17 | Feature combinations missing | `serial:` + `max_fail_percentage:`, `async:` + `poll:` + `until:`, `--check` + `--diff`, `free` strategy, `block/rescue/always` with real failure, handler `listen:` topics | A3 |
| S18 | Hybrid SSH+WinRM one-play untested | Fork's dual-transport support inside one TaskQueueManager | A3 |
| S19 | Mid-session failure recovery | sshd restart mid-play, stuck shell, controller disk full, ctrl-C during fanout | A3 |
| S20 | Vault flows other than `vars_files` | Whole-playbook encryption, inline `!vault`, multiple vault IDs, password scripts | A3 |
| S21 | `os.chmod(path, 0o400)` silent no-op on Windows | SSH pubkey path has no effective permission restriction on Windows | A2 |
| S22 | Windows spawn IPC vs fork COW security posture | Vault secret pickling across named-pipe IPC — bigger blast surface than COW | A2 |

### Nice (N) — polish

N1. `fnmatch` case-insensitivity on Windows (collection ignore patterns)
N2. `config/base.yml:876` `{{ ANSIBLE_HOME ~ "/tmp" }}` cosmetic mixed slashes on Windows
N3. `utils/plugin_docs.py:264` `path.startswith('/')` — low risk, probe context
N4. `DEFAULT_MANAGED_STR` `strftime` locale tokens on Windows
N5. `pipe` filter complex pipelines via cmd.exe
N6. bin/ansible* / .azure-pipelines / .github symlinks materialize as text on Windows clone (irrelevant at runtime)
N7. `utils/context_objects.py` warning noise for deprecated aliases (unchanged from upstream)

## Close-out plan — ordered by leverage

### Phase A: must-fix code bugs (this session)
Fix M1–M8 with tests proving the fix. Bounded, concrete.

### Phase B: test infrastructure (this session)
1. Wire the full stress suite into CI as an ssh-self-target companion job
2. Add exit-code assertions to the failure-mix job
3. Add a grep-audit sanity gate CI step that fails on new unsafe patterns
4. Add a minimal pickle-roundtrip unit test for Task/Host/PlayContext
5. Extend the AnsiballZ byte-identity CI gate to cover 3 modules across 2 collections

### Phase C: coverage expansion (this session if time, else next)
1. `hybrid_ssh_winrm.yml` — one play, both transports
2. `mid_session_drop.yml` — sshd restart during the play
3. `serial_rolling.yml` — `serial:` + `max_fail_percentage:` + deterministic failure
4. `async_fire_forget.yml` — `async:` + `poll: 0` + `async_status`
5. `windows_target_paths.yml` — spaces / UNC / long / non-ASCII on Windows target

### Phase D: documentation honesty (this session)
1. Rewrite README claims to match CI evidence (the `free` strategy / 10-task WinRM / vault handoff claims)
2. `docs/windows-controller/KNOWN_ISSUES.md` — every S and N item documented
3. `docs/windows-controller/DRIFT_PROCEDURE.md` — concrete rebase recipe
4. `PERSISTENT_WORKER_POOL.md` — address the section-3 gaps (backpressure, crash recovery, shutdown, signals, state-isolation scope)
5. `UPSTREAM_RFC.md` — split into two clean patches + pre-write changelog fragment

### Phase E: the big one (future session)
- `WorkerBootstrap` formalization — land the PLAN.md promise properly
- Wire `_PersistentWorkerPool` — the ~1.1 s/task bottleneck
- Real remote-target CI (cross-runner network)

## Success criteria

For this session:
- All eight M items have targeted fixes + evidence
- Phase B 1–3 landed (stress-in-CI + exit codes + grep gate)
- Phase D honesty pass completed (README accurate; KNOWN_ISSUES/DRIFT_PROCEDURE committed)
- AUDIT_MERGE.md (this file) committed as permanent record

Everything else: explicit deferral in KNOWN_ISSUES.md.

## The three audit reports in full

Appended in-line below for permanent record. (See also git log for the
parallel `Agent` tool invocations that produced them.)
