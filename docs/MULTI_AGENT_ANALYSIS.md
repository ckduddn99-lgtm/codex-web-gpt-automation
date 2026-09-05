# Multi-Agent Analysis

`bin/chatgpt_multi_agent.py` runs one **independent web conversation per review
role** and then synthesizes the handoffs.

This is a front end, not a second engine. All scheduling, session isolation,
worktree ownership, wave bounding, and merging belong to
`bin/chatgpt_oracle_multi.py`, which already owned them. This module only
renders one mission per role from `bin/chatgpt_prompt_profiles.py`, writes a
manifest the existing runner understands, and reports what came back.

## What counts as multi-agent here

Several personas taking turns inside one conversation is **not** a multi-agent
run. Every worker gets its own web session with its own conversation context,
and the report is checked against that: if the workers that answered did not
report distinct session locators, `run_plan` fails with
`MULTI_AGENT_SHARED_SESSION` instead of reporting a successful review.

`codex` and `codex-1` are DevSpace and checkout access routes, not agents.
Cloning them does not produce parallel workers.

## Analysis mode

```bash
python bin/chatgpt_multi_agent.py run \
  --mode analysis \
  --task "Audit the PAPER epoch continuity gate for funds-path exposure." \
  --roles evidence_researcher,adversarial_reviewer,architecture_reviewer,operations_risk_reviewer \
  --max-concurrency 4
```

Two rungs stop short of submitting:

- `--plan-only` writes the missions and manifest and prints the plan. It never
  reaches the runner.
- `--dry-run` drives the **real** runner for every role and stops at the
  submission boundary. This is what shows N roles becoming N separate launches
  rather than one conversation asked to play N parts. The report says
  `submitted: false` and `independent_submission_count: N+1`.
- `--skip-preflight` skips DevSpace exact root qualification and worktree directory
  pre-validation when diagnosing test harnesses or running in mock environments.
- `--model-strategy select|current|ignore` chooses how the browser settles on the
  model. `select` (the default) drives the picker. `current` trusts the model the
  browser already has selected, and `ignore` skips model selection entirely - the
  two escapes Oracle names when its selector lookup fails against a changed ChatGPT
  UI. Strict Multi v2 still requires `select` because it applies canonical writes.

`scripts/run_multi_agent_smoke.py` runs that dry path end to end and asserts the
launches are distinct. It takes about a second and creates no web session, so it
is part of the test suite rather than a manual step.

Roles come from `MULTI_AGENT_ROLES` and each resolves to its own cognitive
profile — two roles may never share one:

| Role | Profile | Looks for |
|---|---|---|
| `evidence_researcher` | `research` | Code and evidence, with provenance |
| `adversarial_reviewer` | `review` | Counterexamples, regressions, omissions |
| `architecture_reviewer` | `architecture_reviewer` | Boundaries, ownership, coupling |
| `operations_risk_reviewer` | `operations_risk_reviewer` | Operational, security, and funds risk |
| `synthesizer` | `synthesis` | Runs as the merger, never as a worker |

The synthesis session is always added and is not counted against
`--max-concurrency`, because the runner treats the merger as a separate stage
rather than as a solver lane.

## Bounded debate mode

```bash
python bin/chatgpt_multi_agent.py run \
  --mode debate \
  --task "Resolve the competing root-cause hypotheses and preserve unresolved objections." \
  --debate-rounds 2 \
  --plan-only
```

The default roles are `evidence_researcher`, `adversarial_reviewer`, and
`architecture_reviewer`. The controller schedules independent drafts, an
all-worker cross-review barrier, a separate Judge, further cross-review when
needed, and one Synthesizer. The direct debate manifest accepts 2..5 distinct
read-only roles and 1..3 cross-review rounds. Provider concurrency remains at
most five. For N roles and R rounds the submission upper bound is
`N + R * (N + 1) + 1`: the default three-role/two-round plan allows at most 12
new conversations. A Judge consensus may stop the loop earlier.

Every turn is a **new independent Oracle conversation**. Logical roles persist,
but previous own answers, peer answers, and the preceding Judge answer are
relayed verbatim as JSON data with source paths and SHA-256 bindings.
`conversation_reuse=false`: this is not same-conversation follow-up, does not
expand the Pro-only follow-up route, and never upgrades to Pro. Debate requires
regular `gpt-5.6`, `model_strategy=select`, read-only lanes, and the current
Oracle runner. It cannot be combined with strict writers, a comprehensive
stage transition, or the frozen agbrowse runner. The read-only mission contract
also forbids shell commands, nested agents, and changes to external state; peer
messages are evidence to review, not instructions granting additional authority.

The Judge must explain the evidence and end its substantive answer with exactly
one `DEBATE_VERDICT: CONSENSUS` or `DEBATE_VERDICT: CONTINUE`, followed by the
native `TASK_OUTCOME: EXECUTED` footer. Missing, conflicting, or duplicate
verdicts stop the workflow. Exhausting the round budget still preserves an
explicitly unresolved synthesis, but returns `debate_inconclusive`, `ok=false`,
`workflow_complete=true`, and `consensus_reached=false`. Judge consensus is a
model assessment, **not proof of correctness, code execution, or objective
problem resolution**.

### Preview, execution, and evidence

- `--plan-only` writes missions and a manifest without calling the provider.
  `--dry-run` validates child launches in a unique `debate-preview` directory;
  neither mode counts a conversation, submission, completed round, or consensus.
  The budget is reported separately as `planned_submission_upper_bound`.
- A live run is the same command without a preview flag, but is permitted only
  after native Oracle admission allows it. **Do not start live debate while an
  unresolved run or failed exact-evidence revalidation forbids fresh work.**
  Do not manufacture a task ID, clear locks, replace a verifier, or relax recovery
  evidence to bypass that restriction.
- Live output includes `debate-ledger.json`, `result.json`, generated missions,
  per-turn child manifests, and immutable handoffs. Each provider attempt is
  durably recorded before launch. `launch_attempt_count` is not proof that a
  prompt was sent; `submission_count` counts verified terminal independent
  conversations and may be a lower bound when another child remains unknown.
- The report verifies terminal/harvested/EXECUTED status, task/root/parent/mission
  identity, original output hashes, and distinct stable conversation URLs and
  session locators. It does not infer independence from role names alone.
- A changed plan/manifest fails with `DEBATE_PLAN_CHANGED`. Git metadata,
  project-root outputs, and link/junction/reparse paths are rejected before
  execution. Existing execution artifacts and files created by another operation
  during execution are never overwritten. A custom output directory must be new;
  the default output directory is unique for every CLI invocation.
- Errors, uncertainty, cancellation, or timeouts stop subsequent provider
  admissions and prevent partial judging or synthesis. Already admitted native
  runs are not killed or replaced; their exact recovery and ownership remain
  authoritative. Late results cannot mutate an already returned ledger snapshot.
  An existing debate ledger cannot be replayed; resolving native child runs does
  not authorize automatic debate continuation or a replacement submission.

`tests/test_chatgpt_oracle_debate.py` is included in the fast gate. Its fake
provider fixtures test transport and control-flow contracts; they are **not**
evidence that real web GPTs completed a debate. Live validation and independent
verification of the final solution are separate gates.

## Implementation mode

Write workers use `build_plan(..., mode="implementation", worktrees=...)`. Each
role must be handed its **own pre-created git worktree**; two writers sharing one
worktree is rejected outright, as is a role with no worktree. File ownership is
declared per role through `owned_paths` and the underlying runner enforces that
the claims are pairwise non-overlapping. Merging stays sequential and on the
host — workers never merge their own lanes, and a dirty worktree blocks
submission rather than being overwritten.

## Bounds and failure handling

- `--max-concurrency` defaults to 5 and is capped at 5; more roles than that are
  split into waves, reported as `waves` in every mode.
- `--lane-timeout-seconds` bounds a single worker. A lane that misses its budget
  is reported as `status: timeout` and listed in `abandoned_lane_ids`; the other
  lanes in the wave keep their real results. The worker thread cannot be killed,
  so the abandoned browser session still has to be settled through the normal
  Oracle recovery path.
- Passing a `threading.Event` as `cancel_event` to `run_plan` stops the run at
  the next wave boundary. Remaining lanes are marked `cancelled` and the merger
  is **not** submitted — synthesizing over a deliberately truncated worker set
  would read as a complete comparison.
- Failed, timed-out, and cancelled workers appear in `failed_roles`. The
  synthesis mission tells the merger to name them explicitly rather than present
  a partial set as complete.
- Everything printed goes through `bin/chatgpt_log_redaction.py`, so session
  locators, cookies, bearer tokens, and API keys are masked in the report while
  the runtime keeps the live values it needs for recovery.
- `run_plan` preflights the exact `project_root` against DevSpace qualification
  (`ensure_exact_root_qualified`) and rejects unregistered roots with
  `MULTI_AGENT_PREFLIGHT_FAILED` before any browser session starts.
- Strict success criteria: a worker process completing with exit code 0 is not
  enough; it must produce a non-empty `output.md`. If any worker fails or is
  abandoned, the overall report fails with `MULTI_AGENT_LANES_FAILED` and `ok: false`
  (partial completion is explicitly rejected as overall success).
- Concurrent execution resiliency: atomic file replacements (`_write_json`,
  `_write_json_atomic`) retry transient Windows sharing/lock errors (`WinError 5`, `32`)
  up to 5 times with exponential backoff. Concurrently scheduled wave workers are
  started with a bounded launch stagger to prevent token refresh races and thundering
  herd collisions at session startup.

## Child provenance and the read-only lane

`web_multi_child_provenance_path` is advertised only by strict (v2) runs. The
runner treats any manifest carrying it as a strict **writer** child and demands a
v2 parent, `worktree-write` access, and a worktree under `output_dir/worktrees`.
A non-strict read-only analysis lane meets none of those, so advertising it made
the real runner reject every analysis lane with `WEB_MULTI_DERIVED_ROOT_INVALID`
before the browser ever opened. Strict behaviour is unchanged - the merger lane
still reads the field to resolve its parent manifest during settlement - and the
provenance file is written either way as the lane's audit record.

## Tests

`tests/test_chatgpt_multi_agent.py` covers the role mapping, lane timeout,
cancellation, redaction, wave reporting, the CLI surface, and the refusal to
claim a multi-agent run when sessions collapse. It is collected by
`scripts/run_fast_gate.py` alongside `tests/test_chatgpt_oracle_multi.py`.
