# Research meeting v1

This is a separate controller, not a replacement for the existing analysis,
implementation, or bounded debate paths. It adds public-source research,
agent-authored objections, deduplicated follow-up research jobs and a terminal
meeting log. It does not make changes to a project or prove the final solution.

## Execution model

Four independent logical participants are fixed in v1:

| Participant | Initial assignment | Meeting participation |
|---|---|---|
| analyst | Read the exact private project | Claims, revisions and questions |
| researcher | Investigate the approved public brief, preferring primary sources | Independent review |
| scout | Independently investigate public cases and counterexamples | Independent review |
| skeptic | Inspect assumptions in the private task | Object only when warranted |

The controller gives each participant a bounded opportunity to review newly
delivered messages. The model chooses `claim`, `object`, `revise`, `research`,
`pass` or `agree`; the controller does not manufacture a rebuttal or instruct a
specific participant to oppose another. This is spontaneous *agent-authored*
participation, not cognition without a model call. v1 uses bounded polling
rounds, not continuous free-for-all generation or relevance-ranked subscriptions.

A research request names one or more preapproved public topic IDs. Duplicate
requests for the same topic share one follow-up job. The host sends the approved
public text, never the requesting participant's private rationale, meeting
messages or notes, to that job. Its answer returns to the meeting as an ordinary
attributed, sourced contribution. A failed/abstaining research job remains
unresolved; it does not authorize an automatic retry or count as completed
research. Requesting a new, unapproved topic requires a newly reviewed plan,
not a fabricated approval token or replay of an existing run.

Default limits are two review rounds, 24 total provider calls, three additional
research jobs and at most two concurrent calls. Hard bounds are 1..3 rounds,
9..40 calls, 0..6 additional jobs and 1..5 simultaneous calls. Four final reviews
and one synthesis call are reserved before admitting optional work. The total
call budget includes initial research, ordinary reactions, final reviews,
follow-up research and synthesis. It is not a measured token or cost guarantee.

Every live turn is a fresh regular Oracle session using `gpt-5.6`, `extra-high`,
`model_strategy=select`, `research=off` (no Deep Research mode), read-only missions
and the v1 task-outcome contract. Ordinary web tools are requested in the public
missions. No Pro upgrade, frozen runner, direct browser control, nested Oracle,
workflow restart or same-conversation follow-up is used.

## Public/private boundary

The plan contains two deliberately separate inputs:

- `task`: the private project question, available only in private meeting turns.
- `public_brief`: an explicitly prepared public summary and a map of approved
  topic IDs to public research questions.

The host never derives or automatically approves the public brief by copying
private code, logs or messages. Review its exact text before approving the plan
hash. No automatic secret detector can certify that arbitrary text is public.

Live research additionally requires an existing **empty, separate public
workspace** which is not a parent or child of the private root. Both exact roots
must already pass the official DevSpace root qualification. The controller does
not add allowed roots, change app permissions, create another worktree or forge
a task identity. Public turns receive missions only under the public root; the
controller rejects unexpected files, links, directory changes or changed public
mission bytes. Private turns are instructed not to browse and instead request
approved topic IDs.

This is data minimization plus mission-level policy, **not a security sandbox**.
An account-wide connector may still expose other roots, and private turns may
have browser tools available. Do not use this v1 for data requiring enforced
network isolation or a capability-limited public connector. Dedicated connector
ACLs and verifiable per-tool web traces are outside this implementation.

## Review and evidence contracts

Each delivered snapshot binds `seen_through`. Replies must reference delivered
message IDs. An objection stays open until its own author explicitly withdraws
it with a revision/agreement rationale; neither a chair nor the synthesizer can
silently erase it. `pass`, a failed call, unreviewed work and explicit agreement
are separate states.

All four closing reviews receive the same decision snapshot. New objections,
substantive revisions or last-minute withdrawals cannot be counted as unanimity
on a changed snapshot. Missing agreements, pending research or open objections
produce an inconclusive synthesis, not success. Each initial public researcher
must also return a source-backed contribution. An initial `pass` is retained in
`pending_initial_research`; later unanimous closing reviews cannot conceal that
missing investigation or authorize a retry. The synthesizer receives this list
and must preserve the controller's decision and remaining work.
`solution_verified` remains false even when the meeting reaches consensus.

Source cards retain URL, title, claim, a short excerpt, publication date (or
null), access date, and applicability/version scope. Canonical URLs group repeated
sources; multiple cards citing one page are not independent sources. Fragments
are removed, meaningful paths and query strings are retained. Credential-bearing,
non-HTTPS, local/private literal and nonstandard-port identifiers are rejected.
The controller does not dereference submitted URLs, resolve DNS or treat URL
syntax as proof of reachability, source quality or factual correctness.

Cards are marked `agent_reported_unverified`. A native Oracle completion and a
citation are not independently verifiable proof that a search/open tool was used.
`web_search_verified=false` remains explicit. Private-context and public-research
limits are instructions to the provider, not an assertion that every model will
obey every instruction. Outputs and external material remain untrusted data.

## Plan, inspect, run

Prepare a private UTF-8 task file and a public JSON brief such as:

```json
{
  "summary": "Research durable session recovery in public browser automation tools.",
  "topics": {
    "durability": "Find official documentation about durable conversation identifiers and browser disconnects.",
    "counterexamples": "Find public issue reports that distinguish saved output from active browser sessions."
  }
}
```

Create a new plan under the exact private project. The public root is optional
for planning but mandatory for actual web research. These commands do not grant
permission to operate on an unresolved historical run.

```text
python bin/chatgpt_research_meeting.py plan --project-root <PRIVATE_ROOT> --task-file <PRIVATE_TASK_FILE> --public-brief <PUBLIC_BRIEF_JSON> --public-root <REGISTERED_EMPTY_PUBLIC_ROOT> --plan <PRIVATE_ROOT>/.workflow/research-001/plan.json
```

The command writes only a new plan and returns its SHA-256. It does not call a
provider. Inspect the exact plan, especially the public brief, roots and limits.
Only after explicit plan approval and **all existing user-specific and native
admission restrictions are satisfied** may the live command be used:

```text
python bin/chatgpt_research_meeting.py run --plan <PLAN_JSON> --expected-plan-sha256 <REVIEWED_SHA256>
```

Live execution requires the actual native `CODEX_THREAD_ID`. Never set it to a
made-up UUID to make this command pass. Existing no-submission/replacement bans,
including unresolved historical incidents, are not superseded by this mode.
The runner retains native process/profile/task ownership. An uncertain turn ends
new admissions and leaves its exact run for its legitimate owner; there is no
automatic settlement, recovery, restart, replay or replacement.

## Terminal meeting view

The run records a one-use `run/` directory beside the plan. Provider reservations,
requests, structured responses, original Oracle references, source cards and
message relationships are persisted. Event records are ordered and hash-chained;
existing runs and artifacts are never overwritten. Hashes detect drift against
stored anchors; they are not signatures against an attacker able to replace all
local artifacts.

During one controller run, a bounded set of unbuffered read-only file handles
avoids repeatedly opening the same events and sealed turn artifacts. This caches
neither bytes nor successful validation: every check re-reads and hashes the full
bounded content, validates the current path against the open file identity, and
retains the ancestor/link/reparse checks. Same-size edits with restored timestamps
still fail closed. Handles close on every exit, including setup/publication errors;
viewers and other runs never share them. Durable writes and no-overwrite publication
are unchanged.

In a separate terminal, view the conversation without running agents:

```text
python bin/chatgpt_research_meeting.py view --events-dir <PLAN_DIRECTORY>/run/events
python bin/chatgpt_research_meeting.py view --events-dir <PLAN_DIRECTORY>/run/events --follow-seconds 120
```

Control characters are escaped rather than executed by the terminal. Viewing is
read-only and bounded; it does not open browsers, select participants, poll an
Oracle session or perform background work after the requested observation ends.
This is a terminal view, not a desktop chat application.

## Verification and limitations

`tests/test_chatgpt_research_meeting.py` injects deterministic providers. Every
injected-provider run is labeled `simulation=true` and has zero verified native
sessions, regardless of what the fake provider claims. Synthetic dissent is a
controller test, not a demonstration of genuine independent GPT cognition or
actual internet research. Native sessions require exact root/task/parent/mission,
terminal harvested outcome, ownership/browser receipts, output hashes and unique
conversation URLs and slugs. The adapter does not weaken native settlement rules.

`tests/test_chatgpt_research_meeting_io.py` additionally checks bounded handle
reuse, same-size/same-timestamp tampering, replaced/reparse identities, old request
mutation and cleanup after uncertain children. These tests do not skip content
verification to meet a timing target.

No actual live run or release publication is implied by shipping these files.
The existing fast-gate wall-clock budget remains separate from functional tests.
A failed budget must be reported, not expanded or hidden by reducing coverage.
