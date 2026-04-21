# Gap audit + close-out plan

**Status:** planning — execution follows.

## Known gaps from session context (pre-audit inventory)

### A. Architectural / performance
1. Persistent worker pool designed + scaffolded, **not wired** (~1.1 s spawn cost per task remains)
2. `multiprocessing.Manager()` startup cost paid on every ansible-playbook invocation (~300 ms) even when no plugin calls `connection_lock`
3. No Python 3.14 CI lane
4. No performance-regression guard in CI — spawn import time could bloat silently
5. `_SSHSession` pool disabled by default (requires `ANSIBLE_SSH_USETTY=false`)

### B. Fragile / opt-in features
6. `_SSHSession` marker protocol — theoretical collision if remote cmd output contains the end-marker string
7. `_SSHSession` stderr drain 500 ms race window
8. `_SSHSession` no inactivity timeout — stuck remote shell hangs until ansible task timeout
9. `_alarm_timeout` on Windows can't interrupt C-level blocking code (uses `threading.Timer`)
10. `connection_lock` silently no-op when neither `fcntl` nor `Manager().Lock()` is configured
11. Interactive prompt `_win32_prompt_until` msvcrt path never exercised in automated test
12. Git Bash / MinTTY prompt path routes to `AnsiblePromptNoninteractive` (not fixed)

### C. Unvalidated connection paths
13. Kerberos WinRM auth — uninstalled `pywinrm[kerberos]`, untested
14. CredSSP WinRM auth — untested
15. Certificate-based WinRM auth — untested
16. `become: yes` with password prompt on SSH-to-Linux — sudo-prompt state machine behavior on Windows spawn unverified
17. SSH pool under real network latency — only tested on loopback WSL
18. Long-running workers (hours) — marker protocol not stress-tested at that timescale
19. `paramiko_ssh` plugin (in `ansible.netcommon` collection) — untouched
20. PSRP connection plugin — imports clean but connection path not probed

### D. Unvalidated target matrix
21. Windows 10 target — not tested (Win 11 only)
22. Windows Server 2019 / 2022 targets — not tested
23. macOS as target over SSH — untested (should work; not probed)
24. Linux distros other than Ubuntu 24.04 — untested (RHEL, Alpine musl-libc edge cases)
25. Non-`ansible.builtin` collection modules beyond `ansible.windows` — FQN regex fix untested broadly (`community.general`, `microsoft.ad`, `chocolatey.chocolatey`)

### E. Known silent bugs deferred
26. `bin/ansible*` shebang symlinks still materialized as text on Windows clone (cosmetic; pip entry points work)
27. Default `COLLECTIONS_PATHS` contains `/usr/share/ansible/collections` which resolves to `E:\usr\share\...` on Windows — weird UX
28. `DEFAULT_MANAGED_STR` `strftime` tokens (`%H`, `%M`) — Windows locale differences untested
29. `pipe` filter on Windows uses `cmd.exe` — complex shell pipelines may break (only simple `echo hello` tested)
30. `os.chmod(path, 0o400)` calls on SSH pubkey — Windows ignores most mode bits
31. Remaining upstream symlinks: `bin/ansible*` (10 files), `.github/*` (2 files), `.azure-pipelines/commands/*` (7 files) — irrelevant to runtime but clutter the tree

### F. Untested playbook features
32. `serial:` / rolling updates
33. `debug` strategy plugin
34. `--check` (check mode)
35. `--diff` (diff mode)
36. Custom connection plugin adjacent to playbook
37. Custom dynamic inventory plugin (script + plugin)
38. `ansible_collections/` collection adjacent to playbook (not installed via galaxy)
39. Very deep role trees (role A → role B → role C)
40. Tags with `--skip-tags` + `--tags` combinations
41. `vars:` in group_vars/host_vars directory structure
42. `!vault` inline tags for encrypted strings mid-playbook
43. Handlers with `listen:` topics (not just `notify:`)

### G. Upstream drift / maintenance
44. 35+ commits diverging from upstream `devel`. No documented rebase procedure.
45. `ssh.py` has the largest diff (high upstream churn risk)
46. `display.py` diff is non-trivial (5 separate patches)
47. UPSTREAM_RFC drafted but not filed as actual PR upstream

### H. Security hygiene
48. Vault-encrypted playbook (whole-file encryption, not just vars_files) untested
49. Per-task `!vault` strings via `ansible_vars_from_vault` lookup untested
50. Credential handling patterns not audited for the stress suite

### I. CI gaps
51. No cross-runner network target (CI only does self-target)
52. No perf-regression gate
53. No matrix across Windows Server 2019/2022, only `windows-2022`
54. Only Python 3.12/3.13 — 3.14 missing
55. No test for stress playbooks in CI — they only run on dev box

## Audit execution plan

### Three parallel Opus agents, independent lenses

**Agent 1 — Code-grep audit (Explore subagent, model=opus)**
Task: scan `lib/ansible/**` for Windows-unsafe patterns we haven't fixed. Specifically grep for:
- `os.fork`, `os.setsid`, `os.killpg`, `os.register_at_fork`, `os.getuid`, `os.geteuid` outside `compat.posix`
- `import fcntl`, `import pty`, `import termios`, `import pwd`, `import grp`, `import syslog`, `import resource` not inside `try:`
- `signal.SIG*` constants that don't exist on Windows (`SIGALRM`, `SIGHUP`, `SIGQUIT`, `SIGCHLD`)
- `selectors.` / `select.select` called on pipe fds
- Hardcoded `/` path literals in controller code
- Literal `'/__init__.py'` suffix checks
- Literal `'/'.join` or `':'.join` where `os.pathsep` is right
- `re.compile` with literal `/` that would fail on `\`
- `subprocess.Popen(start_new_session=True)` outside already-fixed spots
- `str(PurePath)` in any zip-filename context
- `os.path.isabs` calls whose input might be POSIX-literal
Return a categorized list: (a) definitely broken on Windows, (b) suspicious, worth probing, (c) already covered.

**Agent 2 — Design / architecture audit (Plan subagent, model=opus)**
Task: review `docs/windows-controller/*.md`, the commit log, and the README against claimed behavior. Look for:
- Invariants we claim but don't test
- Design decisions that look elegant on paper but produce fragility in practice
- The persistent-worker-pool design — is it complete enough to implement from?
- The UPSTREAM_RFC — is it convincing enough to file?
- Phase plan coverage — anything from PLAN.md not addressed?
- Security posture gaps
- Upstream drift mitigations missing
Return: (a) invariant claims without test evidence, (b) fragile elegance, (c) doc/code mismatches.

**Agent 3 — Integration / coverage audit (general-purpose, model=opus)**
Task: inspect actual test coverage against realistic production use:
- Read the stress suite playbooks + CI jobs
- Identify feature combinations not exercised (check mode × delegate_to, async × until, vault-encrypted playbook, serial rolling, etc.)
- Check what happens with mass-fail scenarios, timeout cascades
- Probe real-world-like hybrid playbooks (SSH + WinRM in one play)
- Audit credential-handling hygiene in our tests
Return: (a) high-value test combinations we haven't exercised, (b) production-realistic risks, (c) CI additions worth making.

### Merge + close-out

After all three return:
1. Deduplicate overlapping findings
2. Categorize by severity: **must-fix** (breaks real use), **should-fix** (fragile), **nice** (hygiene)
3. For each category, propose elegant minimal fix
4. Produce an ordered close-out plan with estimated effort per item
5. Implement must-fix items this session; should-fix next session; nice documented as known issues

## Success criteria for this pass

- Every must-fix gap has a committed fix, a test proving it's fixed, and green CI
- Every should-fix has either a fix or an explicit decision to defer with rationale
- Every nice is documented with one-line rationale in `docs/windows-controller/KNOWN_ISSUES.md`
- No gap goes un-categorized
- Merge report is its own commit: `docs/windows-controller/AUDIT_MERGE.md`
