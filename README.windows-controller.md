# ansible-windows — Windows-native Ansible controller

A fork of [`ansible/ansible`](https://github.com/ansible/ansible) that can
run as a **control node on native Windows** (Python 3.12/3.13, no WSL,
no Cygwin). Drives remote targets over `winrm`, `psrp`, and `ssh`;
everything in the executor and module-assembly pipeline is Windows-safe.

Forked from upstream `devel` at commit `7611079116`. See
[`docs/windows-controller/PLAN.md`](docs/windows-controller/PLAN.md) for
the original phased implementation plan and
[`docs/windows-controller/PHASE_4_NOTES.md`](docs/windows-controller/PHASE_4_NOTES.md)
for the AnsiballZ byte-identity story.

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

## What works

- `ansible --version`, `ansible-config`, `ansible-inventory`,
  `ansible-playbook --syntax-check`.
- Full playbook execution: templated vars, loops over lists/dicts,
  `register`, `when`, `set_fact`, `assert`, `block`/`rescue`/`always`,
  `include_tasks`, `import_playbook`, handlers, `meta: flush_handlers`,
  `free` and `linear` strategies.
- Multi-host plays with spawn-based workers (picklable
  `WorkerBootstrap` reinitializes plugin loaders in each child).
- `winrm` and `psrp` connection plugins.
- `ssh` connection plugin for Linux targets — full module round-trip
  (setup, copy, slurp, assert, file state, chained tasks).
- AnsiballZ payloads are **byte-for-byte identical** to those built on
  a Linux controller for the same module inputs (verified in CI for
  `ansible.builtin.ping`).

## What doesn't yet

- Interactive prompts (`vars_prompt`, become-password prompts) raise
  `AnsiblePromptNoninteractive`. `msvcrt.getch`-based replacement owed.
- `alarm_timeout` on Windows uses `threading.Timer` — cannot interrupt
  C-level blocking code, so per-task `timeout:` is best-effort.
- `local` connection plugin is intentionally disabled on Windows; use
  `winrm`/`psrp`/`ssh`.
- In-process SSH connection pool (Phase 5.5) not yet built — each task
  spawns a fresh `ssh` process, expect ~3–10× slower than POSIX +
  ControlMaster for multi-task plays against one host.
- `async` tasks (fire-and-forget + poll) and `ansible-vault` encrypt/
  decrypt haven't been exercised; likely work but unverified.

## Windows-specific environment

- `PYTHONUTF8=1` is effectively required. ansible's locale check
  accepts UTF-8 mode as an alternative to a UTF-8 locale, since
  `locale.getlocale()` on Windows still reports the ANSI codepage.
- For SSH targets, ssh's `ControlMaster=auto`/`ControlPersist=60s`
  defaults from `ssh_common_args` are stripped at build time on
  Windows because Microsoft OpenSSH rejects them at parse time. One-
  time warning on first use.
- Set `ANSIBLE_SSH_PIPELINING=True` for better throughput; each SSH
  invocation currently opens a new TCP connection without the pool.

## CI

A GitHub Actions workflow on `windows-2022` exercises three gates on
every push to this branch:

1. **Import smoke + `ansible --version`** on Python 3.12 and 3.13.
2. **Linux regression** on `ubuntu-latest` — the same editable install
   must still work there.
3. **Cross-platform AnsiballZ byte-identity** — builds
   `ansible.builtin.ping` on both platforms and asserts the zip
   members all agree on `(size, CRC)` and the inner SHA256 matches.

## License

GPLv3 — same as upstream `ansible-core`. See [`COPYING`](COPYING).
