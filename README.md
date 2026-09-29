# evidence-gated-e2e-loop

A small loop that accepts AI-written Playwright tests **only on evidence left in files**, never on the
author's self-report, and then replays the accepted tests **without any AI**.

- Success is **computed, not reported**: a harvester recomputes every result from ledger records, packets and file hashes.
- The **denominator never shrinks**: seven mutually exclusive outcomes must sum to the manifest size, enforced in code.
- **Four false-success patterns** are pinned by tests, and `scripts/red_check.py` proves each test goes red when its guard is removed.

## Demo (real output)

Measured 2026-09-29 on macOS 26.3 (arm64), Python 3.12.13, Playwright 1.63.0, with `make demo` (23.8 s wall).

```
$ loop run --workers 3
run r-20260929-163317-6809c8  items=3  (manifest 3)

| outcome | count | items |
|---|---:|---|
| SUCCESS | 2 | login: 3/3 approved; checkout: 2/2 approved |
| PARTIAL | 1 | cart: 2/2 approved, CART-0002 known-defect exception (not counted as success) |
| FAILED | 0 |  |
| BLOCKED_EXTERNAL | 0 |  |
| WAITING_DECISION | 0 |  |
| RUNNING | 0 |  |
| NOT_STARTED | 0 |  |
| **sum = denominator** | 3 = 3 | OK |

cases: 7 approved / 8 total · 1 known-defect exception · 0 externally blocked · loop-failed items 0 · runnable approval rate 7/8 (A/(N-E))
review: 9 step verdicts · 9 agreed · 0 disagreed · 0 unsupported PASS/BLOCKED downgraded

$ BUG=cart_total loop replay          # plant a bug in the sample app, replay approved tests
login/LOGIN-0001  approved -> stable
login/LOGIN-0002  approved -> stable
login/LOGIN-0003  approved -> stable
cart/CART-0001  approved -> drift   (expected "Total: 30,000" / observed "Total: 25,000")
cart/CART-0003  approved -> stable
checkout/CHECKOUT-0001  approved -> stable
checkout/CHECKOUT-0002  approved -> stable
reviewer/AI calls: 0 (loop.verify never imported)
```

A clean `loop replay` afterwards moves `CART-0001 drift -> stable`.

## How it works

```
              AUTHOR AXIS (AI may help here)                       REPLAY AXIS (no AI)
 manifest ─▶ queue ─▶ worker: AUTHOR ─▶ TRYOUT ─▶ APPROVE ─┐      approved ─▶ run ─▶ stable
 (frozen N)   (file,  ▲      scripts    1 run,     2 more   │                   └──▶ drift
             fcntl)   │      exist      all pass   runs +   ▼
                      │                 except     blind    ledger.jsonl (hash-locked approvals)
                      └─ STOP / budget  registered review       │
                         at every stage exceptions              ▼
                         boundary                        harvester: recompute 7 outcomes, sum == N
```

- `loop/queue.py` — durable file queue, work-stealing claims, claim/finish ownership check, backoff
  (`RESOURCE_WAIT` 300 → 600 → 1200 s, then `RESOURCE_EXHAUSTED`), dead-owner reclaim (dry-run first),
  STOP file, lease pool. No subprocess ever runs while a lock is held.
- `loop/execution.py` — stages, per-item budget (time, launches, same-failure retries, leases → `PAUSED_BUDGET`),
  workers, replay. Replay has no import path to reviewer code.
- `loop/evidence.py` — ledger transitions; any stage past `authored` must point at packets on disk.
- `loop/harvest.py` — pure read; queue `done` and worker stdout are not results.
- `loop/verify.py` — minimal blind review (below).

## Four false-success patterns

| Pattern | What goes wrong | Guard | Test |
|---|---|---|---|
| Blocked-reason abuse | "script missing", "404", "login failed" filed as an external block to leave the denominator | Only `permission`/`payment`/`provisioning` with a product/service owner; the harvester re-checks stored blocks | `test_block_reason_abuse_is_counted_as_loop_failure` |
| Stage stamp without evidence | A stage is marked done with no result file | Ledger refuses a transition whose packets are missing or empty | `test_stage_stamp_without_tryout_packet_is_refused` |
| Undecidable resource release | Release "succeeds" without knowing whose lease it was, or without removing it | Release returns true only after removing a lease this attempt owns; others' are left | `test_release_frees_my_lease_and_skips_someone_elses` |
| Zombie read as alive | `kill(pid, 0)` succeeds on an exited, unreaped child, so a dead worker is never reclaimed | Reap own children, then check process state | `test_zombie_child_is_not_alive` |

`python scripts/red_check.py` removes each guard, runs its test (must fail), restores it (must pass):

```
OK  guard removed -> RED | restored -> GREEN | 1 block-reason abuse: accept any reason
OK  guard removed -> RED | restored -> GREEN | 2 stage stamp: no packet check
OK  guard removed -> RED | restored -> GREEN | 3a release: skip ownership check
OK  guard removed -> RED | restored -> GREEN | 3b release: report success without unlinking
OK  guard removed -> RED | restored -> GREEN | 4 liveness: bare kill(pid, 0)
```

## Verification layer (minimal) — credit

The generator-is-not-the-verifier discipline, two-reviewer blind verification and the approval step are
concepts **I learned in a previous team, where they were designed jointly by the team**. This repository
keeps only a minimal version to show the mechanics:

- Reviewers get an allowlisted observation packet (step, expectation, observation, screenshot path) — never the author's verdict or each other's answers. The loop assigns reviewer ids.
- A PASS must quote text found in the observation; BLOCKED needs a machine signal; otherwise → `INCONCLUSIVE`. FAIL is never downgraded.
- Disagreement stays unconfirmed; one reviewer or the same reviewer twice is an insufficient sample. A third-party adjudicator with a reason is supported as a function.
- Both default reviewers are one deterministic rule program (`reviewers/rules.py`, `lenient` / `strict`). This shows process separation, not independent judgment. Any command that reads the packet on stdin and writes verdicts on stdout can replace it.

Verdicts: `PASS` · `FAIL` · `BLOCKED` · `INCONCLUSIVE` · `NOT_RUN`.

## Run it

```bash
python -m pip install -e . && python -m playwright install chromium
make demo        # run, planted-bug replay, clean replay
make test        # 69 tests + red_check
```

The test scripts under `testware/` are checked in; the AUTHOR stage only checks they exist and match
`cases.json`. This repository is the reference implementation of the loop parts (queue, budget, STOP, backoff).

## Related

- pom-scout [link]

## License

MIT © Taemin Kim
