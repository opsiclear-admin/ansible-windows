# Known issues — windows-controller fork

Everything that the three-agent audit (see `AUDIT_MERGE.md`) surfaced
but that we **did not fix** in the Phase A/B/D remediation pass, with a
one-line rationale each.

Last updated after commit `be180dbd7b`.

## Should-fix but deferred (S-items)

Tracked from the audit; each has a concrete resolution plan in a
future session.

### SSH session pool fragility

- **S1 marker collision.** The `_SSHSession` end-marker uses
  `secrets.token_hex(12)` (24 hex chars). A remote command whose
  stdout contains exactly `__ANSIBLE_END_STDOUT_<24hex>_<digit>\n` would
  be mis-parsed. Collision probability is astronomically low for normal
  use; adversarial input is a separate attack surface we don't claim to
  defend. **Fix plan:** add defensive detection (marker appearing
  before the command-completion point) and bail to legacy ssh.
  **Workaround:** set `ANSIBLE_SSH_USETTY=true` to disable the pool.

- **S2 stderr cross-task leak.** `_stderr_buf` bytes from task N that
  arrive after `_MAX_READ_WAIT_AFTER_RC` (500 ms) are carried into
  task N+1's stderr. Affects operators who route stderr for policy
  (`failed_when`, `changed_when`) and trust it. **Fix plan:** reset
  `_stderr_buf = b''` at exec entry, not exec exit; add a marker
  timeout that abandons the session rather than contaminate.
  **Workaround:** disable pool.

- **S3 `_PipePoller` phantom EOF events on Windows.** `mark_closed`
  discards a name from the open set but the reader thread can still
  enqueue a trailing EOF event that a later `read_events` call sees.
  **Fix plan:** on `mark_closed`, drain queue of the named stream
  before returning. **Workaround:** disable pool.

### Verify-prompt guard airtight

- **S4 `display.py:1151` `os.getpgrp/tcgetpgrp`.** The Windows-side
  `_win32_prompt_until` at line 1046-1050 returns before reaching the
  POSIX-only branch. Confirmed correct today but one innocent refactor
  could swap the order. **Fix plan:** move the POSIX-only lines
  inside an explicit `if sys.platform != 'win32':` block for belt-and-
  braces.

### Test/coverage infrastructure deltas

- **S5 `winrm.py:383` `start_new_session=True`.** No-op on Windows
  (Python ignores it) and the `kinit` path is POSIX-only, so harmless.
  Still inconsistent. **Fix plan:** guard on `sys.platform` for
  symmetry with other Popen calls in this fork.

- **S6 Only 9 unit tests added.** Pickle roundtrip for the spawn-
  handoff objects — not a proper test matrix. **Fix plan:** add
  unit tests for: `_SSHSession` marker parser, `_PipePoller` teardown
  ordering, `flock_exclusive` backoff, `compat.posix.alarm` fallback.

- **S7 AnsiballZ CI gate tests 1 module.** `ansible.builtin.ping`
  only. Per `PHASE_4_NOTES.md` the follow-up test list is "one module
  per collection namespace + one binary-data module". **Fix plan:**
  parameterize `ansiballz_hash.py` to accept a module FQN and run it
  three times in CI (builtin + ansible.windows + community.general).

- **S8 README overstates evidence (corrected).** The original v0.1
  README claimed "10-task WinRM playbook" — CI runs 5. Fixed in this
  session. See `README.windows-controller.md` current text for the
  calibrated claims.

- **S13 Drift-detect CI not wired.** Nothing currently rebases
  against upstream `devel` weekly. **Fix plan:** see
  `DRIFT_PROCEDURE.md` — scheduled workflow described but not yet
  automated as a recurring job.

### Design docs incomplete

- **S9 `PERSISTENT_WORKER_POOL.md`** doesn't cover backpressure,
  crash-recovery semantics, shutdown signaling, or the full
  state-isolation surface. **Fix plan:** address in the wiring PR —
  design doc should evolve with implementation.

- **S10 `UPSTREAM_RFC.md`** is a draft, not a PR. Missing: CI
  evidence on POSIX, changelog fragment, two-PR split, forum thread.
  **Fix plan:** before filing, run the full ansible-core sanity + unit
  matrix with the proposed changes, prepare clean bisect-friendly
  commits, post to forum first.

### Coverage gaps

- **S15 Windows-target path shapes** (spaces, UNC, long,
  non-ASCII): not in the stress suite. Every `win_copy` / `win_stat`
  example uses simple paths. **Fix plan:** add
  `hacking/windows-controller/stress/windows_target_paths.yml`
  in a future session.

- **S16 Missing feature combinations**: `serial:` + rolling updates,
  `async:` + `poll:` + `until:`, `--check` + `--diff`, `free`
  strategy, `block/rescue/always` with real failure (we only test
  skipped blocks), handler `listen:` topics. **Fix plan:** Agent 3
  suggested five concrete probes — build them out progressively.

- **S17 Hybrid SSH+WinRM one play**: untested. Both connection types
  in a single TaskQueueManager invocation. **Fix plan:** add
  `hybrid_ssh_winrm.yml` probe.

- **S18 Mid-session failure recovery**: sshd restart mid-play, stuck
  remote shell, controller disk full, Ctrl-C during fanout. **Fix
  plan:** Agent 3's `mid_session_drop.yml` design.

- **S19 Vault flows other than `vars_files`**: whole-playbook
  encryption, inline `!vault`, multiple vault IDs, password scripts.
  **Fix plan:** `vault_flows.yml` probe.

### Security-posture items

- **S21 `os.chmod(path, 0o400)`.** Windows ignores most mode bits;
  SSH private keys written on Windows don't get the usual protection.
  **Workaround:** users must rely on NTFS ACLs rather than POSIX mode.
  **Fix plan:** on Windows, after chmod, apply an ACL that denies
  `Users`/`Authenticated Users` read access. Wrap in
  `compat.posix.restrict_file_access(path)`.

- **S22 Spawn IPC vs fork COW security posture.** Under fork the
  child's memory (including decrypted vault values) is COW-private.
  Under spawn the pickled state crosses a Windows named pipe, which
  is default-accessible to the same user's other processes. This is
  a security-posture change documented here so operators know.
  **Mitigation:** users with strict same-user isolation requirements
  should run ansible-playbook in a dedicated sandbox account.

## Nice-to-have (N-items) — cosmetic or low-impact

- **N1 `fnmatch` case-insensitivity on Windows.** Collection ignore
  patterns may over-match on Windows. Low-impact; users rarely hit.
- **N2 `{{ ANSIBLE_HOME ~ "/tmp" }}`** cosmetic mixed-slash default.
  `C:\Users\foo\.ansible/tmp` is visually ugly but Windows tolerates.
- **N3 `utils/plugin_docs.py:264` `path.startswith('/')`**. Low-
  reach code path.
- **N4 `DEFAULT_MANAGED_STR` `strftime`.** Windows locale tokens
  differ but the default string uses only `%Y-%m-%d %H:%M:%S` which
  is locale-independent.
- **N5 `pipe` filter with complex shell pipelines on Windows.** Uses
  cmd.exe; anything beyond `echo hello` may break. Document in the
  filter's sidecar YAML as "best-effort on Windows controllers."
- **N6 Remaining upstream symlinks** (`bin/ansible*`, `.github/*`,
  `.azure-pipelines/*.sh`). Irrelevant on Windows at runtime — pip
  entry points generate `ansible.exe` stubs. Tree is visually ugly.

## Explicitly out of scope

- **Kerberos / CredSSP / certificate-based WinRM auth.** Requires
  `pywinrm[kerberos]`, working `klist`, or a PKI. Plumbing works; not
  probed. Users targeting AD-joined Windows fleet should expect this
  to require their own validation before trusting it.
- **`paramiko_ssh`** (in `ansible.netcommon`). Never touched.
- **`network_cli` / `netconf` / `httpapi`** connection plugins. These
  go through the POSIX-only `ansible-connection` stub — now guarded
  (commit `eef3e33da8` M7) to fail fast with a clear error on
  Windows rather than an ImportError.
- **`local` connection plugin**. Disabled on Windows at `_connect`
  with a clean error pointing users at `winrm`/`psrp`.
- **Persistent worker pool.** Designed, scaffolded, **not wired**.
  Per-task spawn cost remains ~1.1 s on Windows. See
  `PERSISTENT_WORKER_POOL.md` for the implementation plan.

## How to graduate an item off this list

1. Reproduce the issue with a minimal probe.
2. Fix with a targeted patch + unit or stress test.
3. Re-run the safety gate (`hacking/windows-controller/check_unsafe_patterns.py`).
4. Delete the relevant line from this file in the same commit.
