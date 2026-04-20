# Kill / Scope-Cut Criteria

Per project direction, **WSL is not a fallback**. These criteria trigger either a scope cut (ship a narrower target), a pause (gather more info before continuing), or a pivot in approach — **not** project abandonment.

## Scope-cut triggers

If any of these fire, cut scope rather than kill the project:

1. **Phase 1 + 4 weeks elapsed without green `win_ping`**
   → Cut scope: ship Phase 1 as "single-threaded controller" — set `forks=1` permanently, eliminate the multiprocessing hand-off entirely. Slower but simpler. Revisit spawn handoff as a Phase 1.5.

2. **Phase 3 + 8 weeks cumulative, WinRM playbook <95% reliable**
   → Cut scope: restrict supported targets to WinRM only (drop PSRP until WinRM is stable). PSRP moves to Phase 5.5.

3. **Phase 5 + 14 weeks cumulative, SSH exec >2s median after warmup**
   → Cut scope: ship v1 without Phase 5 at all. Windows controller supports WinRM/PSRP only; users wanting SSH-to-Linux wait for v2.

4. **Pickling blows up on a core Ansible extension** (popular callback, filter, custom connection plugin) whose fix requires upstream API changes
   → Cut scope: document unsupported extensions. If the list gets long (>5 popular extensions), pause and pursue upstream RFC.

## Pause triggers

If any of these fire, pause the project and reassess before continuing:

5. **Upstream churn routinely >1 engineer-day/week for rebase**
   → Pause. Either negotiate an upstream RFC track for the compat shim (so rebases stop being a battle), or switch to a pinned-upstream model (rebase quarterly, accept feature lag).

6. **Critical upstream feature requires new POSIX-only dependency** we cannot polyfill (e.g., `os.fork` for COW memory sharing, Linux `prctl`)
   → Pause. Evaluate whether the feature is actually needed for our scope. If yes, pivot approach — see #7.

## Pivot triggers

7. **Native subprocess/threaded-reader approach for SSH produces unacceptable perf** even with Phase 5.5 pool
   → Pivot: move SSH to `asyncssh` (previously rejected). Adds 4-5 weeks.

8. **Windows-specific perf regression is fundamental** (spawn startup cost dominates in any multi-host scenario)
   → Pivot: implement a persistent worker pool architecture. This is a bigger refactor than Phase 1 contemplates; would push totals to ~26 person-weeks.

## Hard stops

These are the *only* conditions that end the project:

9. **Target user base evaporates** — if the project was built for a specific constraint (air-gapped corporate, WSL-forbidden IT policy) and that constraint goes away, revisit whether the investment is still justified.

10. **Upstream merges their own Windows controller support**
    → Not a kill, a celebration. Migrate users to upstream; close the fork.
