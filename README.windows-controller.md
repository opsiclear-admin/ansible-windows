# ansible-windows — Windows-native Ansible controller

A fork of [`ansible/ansible`](https://github.com/ansible/ansible) that
can run as a **control node on native Windows** (Python 3.12/3.13, no
WSL, no Cygwin). Drives remote targets over `winrm`, `psrp`, and `ssh`.

Forked from upstream `devel` at commit `7611079116`. Current state:
**v0.1-windows — feasibility demo, use at own risk**. See
[`docs/windows-controller/KNOWN_ISSUES.md`](docs/windows-controller/KNOWN_ISSUES.md)
for the honest list of what works, what's fragile, and what's out of
scope.

## Install

```powershell
# Python 3.12 or 3.13 with UTF-8 mode
$env:PYTHONUTF8 = "1"

# Editable install in a venv
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e path\to\ansible-windows

# WinRM targets
.venv\Scripts\python.exe -m pip install pywinrm pypsrp

# SSH targets
# Needs OpenSSH for Windows (bundled with Windows 10/11 as an optional feature)
# Add-WindowsCapability -Online -Name OpenSSH.Client*
```

## Smoke test

```powershell
$env:PYTHONUTF8 = "1"
.venv\Scripts\ansible.exe --version
.venv\Scripts\ansible-playbook.exe -i inv.yml playbook.yml
```

## What's actually verified

Claims below map to either a CI gate (green every push) or a hand-run
artifact at a specific commit. Anything not listed here is not
verified — see `KNOWN_ISSUES.md` for the deferred items.

### CI-gated (every push, at [`.github/workflows/windows-controller.yml`](.github/workflows/windows-controller.yml))

- **Import smoke** on `windows-2022` × Python 3.12/3.13: `ansible`,
  `ansible.cli`, `ansible.executor.task_queue_manager` all import
  cleanly. `ansible --version` runs.
- **Linux regression** on `ubuntu-latest` × Python 3.12/3.13: same
  smoke passes on the POSIX path so the fork doesn't break upstream.
- **Cross-platform AnsiballZ byte-identity**: builds the
  `ansible.builtin.ping` AnsiballZ payload on both platforms and
  asserts every shared zip member agrees on (size, CRC), and that the
  inner-zip SHA256 matches. Multi-collection coverage is a follow-up
  in `KNOWN_ISSUES.md` (S7).
- **WinRM self-target smoke** on `windows-2022`: 5-task playbook against
  the runner's own WinRM (basic auth, NTLM-negotiable). Exercises
  `ansible.windows.win_ping`, `win_whoami`, `setup`, `assert`, `debug`.
  First task wraps in a retry loop to absorb WinRM warmup races.
- **SSH self-target smoke** on `ubuntu-latest`: 5-task playbook against
  the runner's own sshd (ping, command, assert, templated copy,
  slurp+b64decode+cleanup). Also runs the 15-probe `edge_cases.yml`
  stress playbook end-to-end. Also asserts `ansible-playbook` exits 4
  (all unreachable) against a deliberately-unreachable host.
- **Safety gate**: `hacking/windows-controller/check_unsafe_patterns.py`
  scans `lib/ansible/**` for POSIX-only patterns not caught by the
  compat shim. Fails the build on regressions.
- **Spawn-worker pickle roundtrip**: 9 pytest cases covering the objects
  that must survive the spawn handoff (Host, Task, PlayContext,
  CLIArgs, DataLoader, the `_RemoteEndpoint` RPC-handoff dataclass,
  basedirs snapshot, compat.posix reload).

### Hand-run, artifact archived

- **Linux target over SSH** — 10-task playbook against Ubuntu 24.04 in
  WSL2 (commit [`12e3505245`](https://github.com/opsiclear-admin/ansible-windows/commit/12e3505245)):
  `ok=10 changed=2 unreachable=0 failed=0`. Exercises ping, command,
  setup (full fact gather), templated copy, slurp, b64decode, file-state
  management.
- **Windows target over WinRM** — 10-task playbook against localhost
  with NTLM auth (commit `winlocal_playbook.yml` run):
  `ok=10 changed=2 failed=0`. `ansible.windows.win_ping`,
  `win_whoami`, `win_shell`, `win_copy`, `win_stat`, `win_file`,
  `setup` (picks up `ansible_os_family=Windows`, version, etc).
- **Stress suite** at `hacking/windows-controller/stress/` —
  100-task durability, 20-host fanout, 5MB unicode payload round-trip,
  3-good+3-bad failure isolation, 200-task memory stability,
  15 edge-case probes. All green against WSL Ubuntu.
- **Coverage probe** at `E:/ansible-test/coverage/cov.yml` (not
  committed — dev-box only): `include_role`, `delegate_to: localhost`,
  `run_once`, `tags:`, `environment:` env injection, custom filter
  plugin, custom action plugin, `until:` retries, vault-encrypted
  `vars_files`. 12/12 ok.

## Connection plugins

| Plugin | State |
|---|---|
| `winrm` | CI-gated via self-target on Basic auth. NTLM works manually. Kerberos / CredSSP / certificate auth not individually probed (see `KNOWN_ISSUES.md` out-of-scope section). |
| `psrp` | Imports cleanly; not individually CI-probed. Shares the pywinrm/pypsrp stack so is expected to work. |
| `ssh` | CI-gated via self-target on key auth. Full playbook against WSL Ubuntu validated manually. SFTP and SCP both work (local paths auto-normalized `\`→`/`). ControlMaster/ControlPersist stripped on Windows because Microsoft OpenSSH's AF_UNIX mux socket is unreliable. Phase 5.5 in-process session pool opt-in via `ANSIBLE_SSH_USETTY=false`. |
| `local` | Disabled on a Windows controller — raises a clear `AnsibleError` at `_connect` pointing users at `winrm`/`psrp`. |

## What doesn't work / caveats

- **Interactive prompts** (`vars_prompt`, become-password prompts) —
  a `_win32_prompt_until` implementation via `input()` / `getpass` +
  `msvcrt.getwch` exists but only the simple path (no timeout, no
  character-level detection) is exercised. Git Bash / MinTTY routes
  to `AnsiblePromptNoninteractive`.
- **`alarm_timeout` on Windows** uses `threading.Timer` — cannot
  interrupt C-level blocking code, so per-task `timeout:` is
  best-effort.
- **SSH session pool (Phase 5.5)** is off by default. Enabling via
  `ANSIBLE_SSH_USETTY=false` reduces per-task spawn overhead 15–60%
  on loopback (more on real networks) but loses `-tt` pty allocation
  for interactive sudo prompts.
- **Persistent worker pool** — per-task `~1.1 s` Python import cost on
  Windows spawn remains the dominant overhead. Design in
  [`docs/windows-controller/PERSISTENT_WORKER_POOL.md`](docs/windows-controller/PERSISTENT_WORKER_POOL.md);
  scaffold committed; not wired.
- **`async` tasks**, **`ansible-vault encrypt/decrypt`**, **role chains**,
  **custom filter/action plugins**, **`include_role`**, **`delegate_to`**,
  **`run_once`**, **`tags`** — all verified in the coverage probe, not
  in CI. See `KNOWN_ISSUES.md` S16 for the combinations not yet
  probed (serial/rolling, check/diff, free strategy,
  block/rescue/always with real failure, handler `listen:` topics).
- **Real-world production use** — not recommended for v0.1. See
  `KNOWN_ISSUES.md` "Explicitly out of scope" for the list of auth
  methods, flow patterns, and scale scenarios that haven't been
  validated.

## Windows-specific environment

- `PYTHONUTF8=1` is effectively required. ansible's locale check
  accepts UTF-8 mode as an alternative to a UTF-8 locale, since
  `locale.getlocale()` on Windows still reports the ANSI codepage.
- For SSH targets, ssh's `ControlMaster=auto`/`ControlPersist=60s`
  defaults from `ssh_common_args` are stripped at build time on
  Windows because Microsoft OpenSSH rejects them at parse time. One-
  time warning on first use.
- `ANSIBLE_SSH_USETTY=false` opts into the in-process session pool —
  recommended for multi-task plays against one host.
- For spawn-safe connection serialization, a `multiprocessing.Manager()`
  starts once per `ansible-playbook` invocation on Windows (~300 ms
  overhead). Accepted cost; not configurable.

## Documentation

- [`docs/windows-controller/PLAN.md`](docs/windows-controller/PLAN.md) —
  original phased implementation plan, with kill criteria
- [`docs/windows-controller/KNOWN_ISSUES.md`](docs/windows-controller/KNOWN_ISSUES.md) —
  honest list of deferred items, with resolution plans
- [`docs/windows-controller/DRIFT_PROCEDURE.md`](docs/windows-controller/DRIFT_PROCEDURE.md) —
  how to rebase against upstream `devel`
- [`docs/windows-controller/AUDIT_MERGE.md`](docs/windows-controller/AUDIT_MERGE.md) —
  three-agent gap audit and close-out plan
- [`docs/windows-controller/PHASE_4_NOTES.md`](docs/windows-controller/PHASE_4_NOTES.md) —
  AnsiballZ byte-identity story (5 bugs fixed, 3 structural fixes)
- [`docs/windows-controller/PERSISTENT_WORKER_POOL.md`](docs/windows-controller/PERSISTENT_WORKER_POOL.md) —
  design for the unwired pool
- [`docs/windows-controller/UPSTREAM_RFC.md`](docs/windows-controller/UPSTREAM_RFC.md) —
  draft RFC for proposing the two minimal shims upstream

## License

GPLv3 — same as upstream `ansible-core`. See [`COPYING`](COPYING).
