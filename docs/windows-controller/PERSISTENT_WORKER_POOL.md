# Persistent Worker Pool — design

**Status:** design + scaffolding only, not wired

## Why

Measured on Windows Python 3.13, a single `WorkerProcess` pays ~1.1 s
of pure Python import cost before it runs any user code:

```
ansible --version cold start:                        1.11 s
spawn subprocess + import ansible.cli + task_queue_manager:  1.14 s
1-task noop playbook against WSL:                    3.56 s
5-task × 10-host bulk, forks=10 (50 spawn workers):  64 s
```

Under fork (Linux, macOS), this cost is zero — the child inherits
the parent's already-loaded interpreter state via copy-on-write. On
Windows with spawn, every child imports ansible from cold. This is
the single largest remaining performance gap in the fork.

The pool reclaims that cost by keeping a fixed set of worker
processes alive across tasks. Each worker imports ansible once, then
drains tasks from a shared input queue.

## Execution model

### Current (per-task WorkerProcess)

```
TQM.__init__:
  create Manager().Lock() / FinalQueue / etc
  self._workers = [None] * forks

StrategyBase._queue_task(host, task, ...):
  w = WorkerProcess(task=task, host=host, ...)
  w.start()                      # spawn, import ansible, run task, exit
  self._workers[slot] = w
```

Every task is a fresh Python process. 1.1 s import tax per task on
Windows.

### Proposed (persistent worker pool)

```
TQM.__init__:
  create Manager().Lock() / FinalQueue
  self._worker_pool = _PersistentWorkerPool(
      size=forks,
      final_q=self._final_q,
      cliargs=context.CLIARGS,
      extra_plugin_basedirs=_get_adjacent_plugin_basedirs_snapshot(),
  )
  self._worker_pool.start()     # spawns `size` long-lived workers

StrategyBase._queue_task(host, task, ...):
  self._worker_pool.submit(
      TaskJob(task, host, task_vars, play_context, loader, ...),
  )

TQM.cleanup:
  self._worker_pool.stop()       # sentinel → workers exit cleanly
```

Each `_PersistentWorker`:

```
def run(self):
    self._bootstrap_spawn_child()   # same routine we already have
    while True:
        job = self._input_q.get()
        if job is _SENTINEL:
            return
        try:
            self._run_one(job)       # ~= current WorkerProcess._run
        except BaseException:
            self._send_hard_exit(traceback.format_exc())
            # worker stays alive — only this task failed
```

## Payload shape

`TaskJob` is a picklable dataclass containing everything the current
`WorkerProcess.__init__` takes. Every field must already be picklable
under the spawn path we built in Phase 1; no new pickle work.

```python
@dataclass(frozen=True)
class TaskJob:
    worker_id: int
    task: Task
    host: Host
    task_vars: dict
    play_context: PlayContext
    loader: DataLoader
    variable_manager: VariableManager
```

`shared_loader_obj` from the legacy constructor does not ride on the
queue — the worker reconstructs it on first task via
`plugin_loader.get_plugin_loader_namespace()` and caches the result.

## Per-worker caches

Running multiple tasks in one worker unlocks natural caching that
the per-task model never had:

| Cache | Scope | Benefit |
|---|---|---|
| `plugin_loader_namespace` | worker-lifetime | skips the re-init call that `_bootstrap_spawn_child` does today on every fresh spawn |
| Connection plugins | worker-lifetime, per-(host, transport) | Phase 5.5 SSH session pool now amortizes across many tasks, not just within one task's 4 execs. Multi-task plays against one host open one ssh per worker instead of one ssh per task. |
| Templar / Jinja env | worker-lifetime | one-time Jinja env construction, not per-task |
| AnsiballZ module cache | worker-lifetime, per-module | the `_CachedModule` on-disk cache still works, but in-memory hits on second use of the same module in the same worker |

## Correctness concerns

### Global state leakage

Running multiple tasks in one interpreter risks state leaking. The
per-task fork/spawn model was isolated. Known leakage sources:

1. **`os.environ`** — some modules mutate env. Before each task the
   worker snapshots and restores env.
2. **`sys.path`** — action plugins sometimes `sys.path.append`.
   Snapshot/restore.
3. **`logging`** — handlers added by a prior task's callback plugin.
   Reset logger at task boundary.
4. **`Display._final_q`** — already per-worker via `set_queue`.
   Unchanged.
5. **Random state** — some tasks seed `random`; not isolated under
   fork either. No change required.
6. **Module-level imports** — modules are imported fresh per task via
   AnsiballZ (remote side); controller-side module_utils imports
   persist across tasks but that's already the case via plugin_loader
   caching.

Mitigation: a `_JobIsolation` context manager that snapshots
`os.environ`, `sys.path`, and `logging.root.handlers` on `__enter__`
and restores on `__exit__`.

### Worker crash recovery

If one task crashes a worker (segfault, MemoryError, etc), the pool
must detect it and respawn. Current `TQM._cleanup_processes` polls
`is_alive()` — reuse the pattern but respawn on death rather than
fail the play.

### Signal handling

A `SIGINT` to the controller should propagate to all pool workers and
terminate them cleanly. Workers install `_term` handler identical to
current `WorkerProcess._term`.

## Incremental landing plan

1. **Ship `_PersistentWorker` class in `executor/process/worker.py`
   (not wired).** Subclass of current `WorkerProcess` that overrides
   `run()` to drain a queue. The skeleton is in this commit — future
   commits refactor the existing class rather than maintaining two.

2. **Add `_PersistentWorkerPool` manager class + env var gate.**
   `ANSIBLE_PERSISTENT_WORKERS=true` creates the pool; unset uses the
   legacy per-task path. Default stays legacy.

3. **Teach `StrategyBase._queue_task` to detect the pool and submit
   jobs instead of spawning.** Single branch at the submission site.

4. **Per-worker connection cache.** Worker keeps a dict keyed by
   (host, transport); `_run_one` looks up or creates. Biggest
   additional win layered on top of the SSH pool from Phase 5.5.

5. **Flip default on Windows.** After the matrix runs clean for a
   few release cycles.

## Estimated effort

- Steps 1–2 (skeleton + env-var gate): 1–2 sessions
- Step 3 (wire into strategy): 1 session, but careful, strategy base
  is complex
- Step 4 (connection cache): 1 session
- Step 5 (flip default): 0 code, 1 release cycle

Total: 4–5 focused sessions to production quality, assuming no
surprises in the strategy-base wiring.

## What the skeleton in this commit does

`_PersistentWorker` class added to `executor/process/worker.py`. It
compiles, can be imported, is not used by anything. It exists so:

- Future sessions have a concrete subclass to iterate on instead of
  starting from scratch.
- Reviewers can read the intended shape without chasing a design doc
  into code.
- CI continues to pass because nothing is wired.

Wiring (steps 2–5 above) is future work.
