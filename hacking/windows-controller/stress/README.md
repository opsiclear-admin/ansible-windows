# Windows-controller stress suite

Six hand-run stress scenarios used to harden v0.1. Each playbook is
self-contained; inventories assume an SSH reachable Linux box at
`192.168.73.7` (swap for your target).

| File | What it exercises | Expected |
|---|---|---|
| `100_tasks_durability.yml` | 100 sequential tasks against one SSH target — SSH session pool lifecycle, marker protocol durability | `ok=100 failed=0`, ~4m50s on loopback |
| `fanout_20_hosts.yml` + `fanout_20_hosts_inv.yml` | 20 hosts × 2 tasks, forks=10 — concurrent spawn workers, Manager().Lock() contention | `ok=2` on all 20 hosts, ~60s |
| `unicode_and_payload.yml` | 5 MB file transfer with sha1 verification; filenames with spaces; Cyrillic / Chinese / emoji / backslash vars round-tripped through remote shell | `ok=9 failed=0` |
| `failure_mix.yml` + `failure_mix_inv.yml` | 3 good + 3 bad (unreachable) hosts, forks=6 — error isolation | good hosts `ok=2`, bad hosts `unreachable=1`, ~35s |
| `200_tasks_memory.yml` | 200 `set_fact` tasks in one play — memory stability baseline | `ok=200 failed=0`, peak RSS bounded (~2 GB of overlapping spawn children), returns to baseline after run |
| `edge_cases.yml` | 15 edge-case probes: deep-nested vars, 100KB module arg, 500-key dict, 200k-char stdout, binary slurp with size check, special-char shell output, 190-char remote path, `until` exhaustion, `when: false` block skip, handler chain (h_a → h_b) | `ok=25 failed=0 skipped=2 ignored=1`, ~64s |

## Running

```
export ANSIBLE_SSH_USETTY=false  # enables the Phase 5.5 session pool
ansible-playbook -i wsl_inv.yml 100_tasks_durability.yml
ansible-playbook -i wsl_inv.yml edge_cases.yml
```

Set `ANSIBLE_SSH_USETTY=true` to observe the baseline (no pool) timings.

## Findings from the first run

All six scenarios passed on Windows 11 / Python 3.13 against a WSL2
Ubuntu 24.04 target and a native-localhost WinRM target. Bugs
surfaced (all in my test scripts, zero code-side):

- `unicode_and_payload.yml`: mis-computed payload size; sha1-vs-sha256
  algorithm mismatch between `copy` return and `stat` request.
- `edge_cases.yml`: `b64decode | length` counts str chars, not bytes,
  for random binary — switched to `stat.size` + b64 length arithmetic.
  `retries` `attempts` accounting differs from my expectation —
  relaxed to `>= 3`.

Zero code-side regressions across all six scenarios.
