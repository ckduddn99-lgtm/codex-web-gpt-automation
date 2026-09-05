# Fast gate performance verification — 2026-09-06

Measured on Windows with the existing checkout and Python 3.14 virtualenv.
Baseline HEAD: `ed16ae1ceff1d5296a50f8f480832c304e5ab21e`.
The initial measurements below include the uncommitted production optimizations
inherited from the preceding task; they are not measurements of pristine HEAD.

## Bottleneck and change

Repeated real Git initialization, initial commits, and two worktree creations per
strict fixture were dominated by process launch and filesystem cost. Concurrent
gate shards amplified that cost. Create one real repository template per module,
copy its bytes independently for each fixture, and use real `git worktree repair`
to relocate both worktree bindings. No hard links, object alternates, shared index,
mock Git, or removed assertions replace repository validation.

Production combines common-dir/HEAD queries, shares repository-global identity
within each barrier, and reads status once per audit. Actual repository binding
checks still precede returning writable manifests, but follow static validation.
The canonical pre-apply integrity barrier remains mandatory. Staged mission files
remain forbidden; writer mission exceptions do not apply to canonical cleanliness.

## Standalone measurements

Run `python scripts/profile_strict_multi.py` using the test virtualenv. It wraps
real subprocess calls for measurement, delegates execution unchanged, and includes
fixture cleanup. Phase totals overlap and must not be added together.

| Measurement | Initial 25 tests | Template-only, same 25 | Final 26 tests |
| --- | ---: | ---: | ---: |
| Git subprocesses | 219 | 172 | 210 |
| pytest wall time | 65.67 s | 38.24 s | 49.78 s |
| fixture builder wall time | 23.642 s / 13 calls | 5.303 s / 13 calls | 5.267 s / 15 calls |
| cleanup | 1.965 s | 2.048 s | 1.959 s |
| total profiled invocation | 68.219 s | 40.719 s | 52.288 s |

Final coverage adds actual fsck, independent refs/index/worktree identity, unchanged
template bytes, staged rename/mission rejection, and canonical mission rejection.
It therefore intentionally executes more Git checks than the intermediate 25-test run.

| Git command | Initial count / wall | Final count / wall |
| --- | ---: | ---: |
| init | 13 / 3.642 s | 1 / 0.265 s |
| initial commit | 13 / 5.285 s | 1 / 0.330 s |
| add | 13 / 3.022 s | 3 / 0.543 s |
| rev-parse | 95 / 21.411 s | 99 / 18.665 s |
| status | 39 / 8.986 s | 49 / 10.292 s |
| for-each-ref | 10 / 2.066 s | 19 / 3.431 s |
| worktree (all subcommands) | 36 / 13.576 s | 34 / 6.238 s |
| fsck | 0 | 2 / 0.779 s |
| mv | 0 | 1 / 0.188 s |
| update-ref | 0 | 1 / 0.210 s |

Initial worktree calls comprise 26 adds and 10 lists. Final worktree calls comprise
2 adds (0.662 s), 17 lists (3.075 s), and 15 repairs (2.501 s).

Final manifest load/validation: 42 calls, 12.117 s, including 60 Git rev-parse
calls (11.393 s). Deferring Git until static contracts pass removes 12 rev-parse
calls from invalid-manifest paths without omitting binding validation.
Final preflight: 4 calls, 6.454 s, 32 Git calls; lane audit: 12 calls,
5.280 s, 24 Git calls; repository barrier: 4 calls, 2.382 s, 12 Git calls.

## Final verification

| Check | Result |
| --- | --- |
| Focused fixture/status tests | 2 passed, 16.65 s |
| Strict multi tests | 15 passed, 46.12 s |
| Entire multi module | 26 passed, 49.78 s |
| Verification gate module | 10 passed, 0.28 s |
| Official default gate, final run 1 | exit 0, 95.07 s; multi 84.54 s |
| Official default gate, final run 2 | exit 0, 93.45 s; multi 82.44 s |

Both final runs used exactly `scripts/run_fast_gate.py --enforce-budget`, without
code changes between runs: 100-second budget, 3 default workers, 17 jobs, unchanged
44 targets and existing single deselection. Existing skips remain unchanged.
The user-reported original official gate took 279.16 s and failed its budget.
Parallel contention remains visible compared with the standalone 49.78-second
multi run; command-by-command parallel contention was not separately instrumented.
Two passing runs establish the requested repeat check, not a guarantee against
arbitrary host load; remaining margin is about 5–7 seconds.

The existing compat directory-link test emitted `PytestUnhandledThreadExceptionWarning`
with `_readerthread` UTF-8 `UnicodeDecodeError` in both official runs. No warning
filter was added. Existing external dirty files were preserved; the full repository
test suite and remote CI are not represented by the multi-module results above.

## Publication follow-up

Clean CI exposed a no-submission smoke dependency on the user's browser profile.
The smoke now creates an isolated empty profile outside its temporary project and
passes its canonical path to the real runner. It still submits no browser question.
Full-suite execution also exposed a synthetic foreign-task ID equal to the current
fixture ID; the mutation now asserts that its replacement really is different.

Further local runs reached 106.78 s (overlapping a separate focused run) and
102.36 s (without that overlap). These are budget failures, not stability passes.
Batching explicit runner nodes eight at a time instead of four reduces startup
processes from 17 to 14 jobs, without changing targets, assertions or the budget.
Verification grouping tests: 10 passed; identity mutation tests: 11 passed.
The resulting unchanged-code official runs passed in 87.72 s and 87.87 s,
with multi at 72.92 s and 74.79 s. Existing UTF-8 warnings remained visible.
