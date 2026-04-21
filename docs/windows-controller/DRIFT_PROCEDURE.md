# Upstream drift procedure — keeping the fork alive

This fork carries ~40 commits of diverging work from upstream
`ansible/ansible:devel`. Over time upstream changes — especially in
high-churn files (`ssh.py`, `worker.py`, `display.py`) — will
conflict with our patches. This document is the standing operating
procedure for periodic rebases.

## Hot-spot files

The diff against fork-point `7611079116` concentrates in these
files. Check each during any rebase:

| File | Change shape |
|---|---|
| `lib/ansible/plugins/connection/ssh.py` | Largest diff (~525 lines): `_PipePoller`, `_SSHSession`, ControlMaster strip, User=/IdentityFile= quoting, sftp local-path normalization, pty/fcntl guards |
| `lib/ansible/executor/process/worker.py` | `_bootstrap_spawn_child`, RPC endpoint snapshot, plugin-basedir snapshot, `_PersistentWorker` scaffold |
| `lib/ansible/utils/display.py` | 5 patches: libc shim, conditional fcntl/termios/tty imports, `_win32_prompt_until`, register_at_fork guard, terminal-size via shutil |
| `lib/ansible/executor/task_queue_manager.py` | Manager().Lock() for spawn-safe connection locking |
| `lib/ansible/executor/module_common.py` | `create_system=3`, `as_posix()` for zip members, regex path normalization, `is_package` basename check |
| `lib/ansible/compat/posix.py` | New file. Unlikely to conflict but watch for new POSIX primitives upstream adds that need shim coverage. |
| `lib/ansible/_internal/_rpc_host.py` | `_RemoteEndpoint` + `configure_remote_endpoint` — the RPC handoff |
| `lib/ansible/plugins/connection/__init__.py`, `local.py` | `connection_lock` routing, pty/fcntl guards, `_connect` Windows guards |
| `lib/ansible/plugins/shell/__init__.py` | `posixpath.join` + `system_tmpdirs` double-isabs |
| `lib/ansible/config/manager.py` | `pathspec` smart-split for Windows |
| `lib/ansible/cli/__init__.py` | UTF-8-mode acceptance, blocking IO OSError catch |
| `lib/ansible/cli/arguments/option_helpers.py` | `os.pathsep.join` for --version output |
| `lib/ansible/cli/scripts/ansible_connection_cli_stub.py` | Windows guard |
| `lib/ansible/utils/collection_loader/_collection_finder.py` | `os.path.isabs` replacement |
| `lib/ansible/_internal/_datatag/_tags.py` | `os.path.isabs` replacement |
| `lib/ansible/_internal/_locking.py` | Uses `compat.posix.flock_exclusive` |
| `lib/ansible/_internal/_errors/_alarm_timeout.py` | Uses compat.posix alarm helpers |
| `lib/ansible/_internal/_templating/_template_vars.py` | `compat.posix.lookup_user_name` |
| `lib/ansible/parsing/dataloader.py` | `re.escape(os.path.sep)` |
| `lib/ansible/parsing/vault/__init__.py` | `os.fstat` instead of `fcntl.F_GETFD` |
| `lib/ansible/plugins/loader.py` | `_ADJACENT_PLUGIN_BASEDIRS` snapshot |
| `lib/ansible/plugins/action/copy.py` | `os.path.sep.join` instead of literal `'/'.join` |
| `lib/ansible/plugins/action/fetch.py` | `os.path.isabs` instead of `startswith('/')` |
| `lib/ansible/galaxy/collection/gpg.py` | Windows `--status-file` fallback |
| `lib/ansible/cli/galaxy.py` | `os.pathsep.join` for warning |
| `lib/ansible/config/ansible_builtin_runtime.yml` | `systemd` → `systemd_service` redirect |
| `lib/ansible/module_utils/basic.py` | conditional grp/fcntl/pwd imports |
| `lib/ansible/module_utils/ansible_release.py` | Real file (was symlink) |
| `lib/ansible/modules/systemd.py` | Removed (redirect replaces it) |
| `lib/ansible/plugins/test/*.yml` (15 files) | Real files (were symlinks) |

## Standing procedure

Before every rebase:

```bash
# 1. Ensure we're clean and tagged
git checkout windows-controller
git status --short      # must be empty
git log --oneline -1    # note current HEAD

# 2. Fetch upstream's latest
git remote add upstream https://github.com/ansible/ansible.git 2>/dev/null || true
git fetch upstream devel

# 3. Count and inspect what's changed in hot-spot files
git log 7611079116..upstream/devel --oneline -- \
    lib/ansible/plugins/connection/ssh.py \
    lib/ansible/executor/process/worker.py \
    lib/ansible/utils/display.py \
    lib/ansible/executor/task_queue_manager.py \
    lib/ansible/executor/module_common.py
# If the count is >10 in ssh.py, plan a 2-hour rebase. Under 3, usually <30 min.
```

Then the rebase itself:

```bash
git checkout -b rebase-attempt windows-controller
git rebase upstream/devel
# Expect conflicts in hot-spot files. Resolve each:
#
#  - Keep the Windows-specific branch (`if sys.platform == 'win32':`)
#    unless upstream explicitly deleted the surrounding function.
#  - Preserve `compat.posix.*` imports — upstream will not have them.
#  - Preserve the `.as_posix()` calls in `module_common.py` — any
#    upstream change to zip-member path construction should be audited
#    against the Phase 4 byte-identity claim.
#  - Preserve the `_bootstrap_spawn_child` bootstrap in `worker.py`.
```

After resolution:

```bash
# 4. Run the safety gate — catches pattern regressions
python hacking/windows-controller/check_unsafe_patterns.py
# If this fails, upstream reintroduced a POSIX-only pattern. Fix or
# add to the allowlist with justification.

# 5. Run the pickle tests — catches spawn-handoff regressions
python -m pytest test/units/executor/test_worker_pickle.py -v

# 6. Run the smoke — catches everything else
python -c "import ansible; import ansible.cli; import ansible.executor.task_queue_manager"
ansible --version

# 7. If a WSL/WinRM target is available, run the stress suite:
ANSIBLE_SSH_USETTY=false ansible-playbook \
    -i hacking/windows-controller/stress/fanout_20_hosts_inv.yml \
    hacking/windows-controller/stress/fanout_20_hosts.yml
ansible-playbook -i <your-wsl-inv.yml> \
    hacking/windows-controller/stress/edge_cases.yml
```

When CI is green:

```bash
git checkout windows-controller
git reset --hard rebase-attempt
git push --force-with-lease fork windows-controller
git push --force-with-lease ansible-windows windows-controller:main
```

## Scheduled drift-detect (not yet implemented)

A weekly CI job that merges `upstream/devel` into a throw-away branch
and reports conflict counts to an issue tracker would catch
divergence early. Left as a follow-up; the manual procedure above is
authoritative in the meantime.

## Kill criteria (per KILL_CRITERIA.md)

A rebase cycle that takes >1 engineer-day is a signal; two in a row is
a scope-cut trigger. Pin the upstream baseline and accept feature lag
rather than chase a moving target.

## Rebase dry-run right now

Commit `befa49021c` was the last pre-audit-cycle HEAD; commit
`be180dbd7b` (post-Phase A/B/D) is the current state. Between those we
added:

- 8 must-fix code bugs (M1-M8)
- safety-gate script + allowlist
- pickle-roundtrip test suite
- stress-suite smoke in CI
- exit-code assertion for all-unreachable
- this documentation

The grep audit passes, the tests pass, the CI is 11/11 green. If you
rebase to current upstream and the safety gate fails, that's your
signal — follow the standing procedure above.
