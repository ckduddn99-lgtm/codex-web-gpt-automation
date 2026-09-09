# Server AI bus

The server bus keeps AI traffic out of Discord. SQLite messages are compact envelopes;
long text is stored once as an immutable artifact and referenced by a numeric `refs`
array.

```json
{"task_id":184,"from":"gemini","to":"codex","type":"implement","refs":[31,32],"priority":2}
```

Results use the same shape:

```json
{"task_id":184,"from":"codex","to":"gemini","type":"result","refs":[35],"status":"done"}
```

## Independence and consensus

Each worker claims only its addressed task. The question artifact is deduplicated, and
result refs are not revealed during collection. When all seats answer, the bus seals a
canonical result-ref bundle and publishes its SHA-256.

Consensus is a separate barrier. Every participant must acknowledge that exact bundle
hash and then explicitly mark its objection review complete. Objections remain open
until the participant that raised them resolves them, and a participant cannot add a
new objection after closing its review. Only after every review is closed can the
conductor publish the immutable proposal ref, which every participant must explicitly
approve. Missing, timed-out, rejected, or abstaining votes never count as consent.
Discord may render the status transition but must not receive prompt or answer
artifacts.

Seat colors are a presentation-only mapping returned by `status`, so color metadata is
not repeated in every bus message: Gemini blue, ChatGPT green, Codex yellow, and Claude
orange.

## Stages, and who drives them

A round is not one question. After the collection barrier seals it, the same seats still
have to acknowledge the exact bundle, close their objection review, and vote. Each of
those is one action per seat, so a task's identity includes its `stage`; `collect` is the
original question and is the only stage the bundle digest covers. Three separate reads of
the tasks table used to mean "the collection" without saying so, and each broke
differently once a later stage existed — a completed stage task changed the recomputed
digest (`BUNDLE_TAMPERED`), put an acknowledgement into the set of independent answers,
and made another seat's vote resolvable through `sealed-artifact`.

The conductor is deliberately split in two:

- **Gemini judges.** It decides what to ask, and it writes the final proposal from the
  sealed answers. That reaches a model the same way every seat's work does — a stage task
  addressed to the round's sender.
- **`bin/round_driver.py` counts.** Who acknowledged, whose review is closed, whether an
  objection is still open, how the votes fall. None of that is judgement, so an LLM could
  only add a way to get it wrong. It also removes the surface where a seat's answer could
  talk the conductor into finalizing: the driver reads seat text through a fixed
  vocabulary and never as instruction.

That split is what stops the participant who authored the proposal from also being the
one who decides it passed.

```bash
python3 bin/round_driver.py --db <db> advance --round-id release-1
```

`advance` performs the single next action and returns; it is safe to call repeatedly and
expects to be, so a timer or the Gemini worker can drive it without the program holding
state. It exits non-zero when a stage is blocked.

Seats answer stages in a fixed vocabulary on the **last line** of the reply — `ACK
<bundle sha256>`, `REVIEW_COMPLETE` or `OBJECT <line>`, `RESOLVED <line>` or
`STILL_OPEN`, `APPROVE`, `REJECT <line>` or `ABSTAIN`. Echoing the digest is what
separates a seat that read the bundle from a seat that said yes.

Anything else stops that stage and is reported: a seat that said nothing is waited for, a
seat that failed or wrote prose instead of a decision blocks, and neither is retried
because the work may already have run at the provider. Silence, failure, timeout,
rejection and abstention never become consent — `finalize` re-checks every barrier and
requires unanimous explicit approval, so the driver cannot grant what it did not collect.

## What crosses into Discord

`bin/board_notify.py` reads a driver payload and posts only what a person can act on:
consensus reached, consensus denied, a stage blocked on somebody, and a proposal now
waiting for approval. A round waiting on seats is normal operation and stays silent,
because a channel that reports every poll teaches the reader to stop looking at it.

```bash
python3 bin/round_driver.py --db <db> advance --round-id release-1   | python3 bin/board_notify.py --channel 일반
```

Repeats are suppressed for a cooldown, keyed on what actually differs -- the key includes
who is blocking, so a second seat failing is still news while the same one failing again
is not. Prompts and answers never cross; only the transition and a short reason do.

Writing goes through the seat bot, which Discord forbids from posting in the conductor
lane, and the notifier refuses that channel by name as well. The conductor token is
deliberately not on this host: a process able to read it could write the instructions it
is supposed to be following.

## The person's own lane

`bin/discord_gemini_bridge.py` carries a message from the instruction channel to Gemini
and posts the answer in the general channel. It is not a round: a round is a question put
to several seats whose independence has to be enforced, and a person asking Gemini
something is one request with one answer. It does take the same provider slot as the seat
workers, so a direct question cannot run a second heavyweight model beside a meeting.

```bash
python3 bin/discord_gemini_bridge.py --agy ~/.local/bin/agy
```

Bot messages in that channel are ignored, so the system cannot talk to itself through the
one lane meant to carry human intent. A failed answer says so in the channel and advances
the cursor anyway -- the model call may already have run, and a person who got nothing
cannot tell a broken bridge from a slow one.

## Durable goal backlog

Rounds remain single deliberation units; they are not stretched into multi-day project
state. Long-lived goals share this SQLite database and the immutable `artifacts` store,
but use separate `goals`, `goal_tasks`, and `backlog_transitions` tables because round
`tasks` are lease-driven stage executions bound to one `round_id`.

A goal and each child work item carry one explicit status: `open`, `in_progress`,
`blocked`, `completed`, or `user_decision_required`. Goals record an owner; child work
records an assignee. Entering either waiting state requires an explicit blocker/decision
artifact. Leaving it clears the live blocker ref but preserves the old immutable ref in
transition history. Child work never becomes completed because a worker went silent,
failed, timed out, abstained, opposed, or merely disappeared; completion is an explicit
state transition. A goal likewise refuses `completed` while any child work item is not
explicitly completed.

The backlog does not schedule heavyweight model execution and therefore does not create
a second provider path or an automatic retry loop. Existing workers continue to use
`provider.lock` for model/browser work. A backlog item may optionally name the round that
produced it, but it does not inherit consensus from that round and no meeting is started
automatically from backlog state.

Status views return metadata and artifact refs, not artifact bodies. `goal-artifact`
resolves text only when the ref belongs to that goal. Discord may consume the compact
`goal_transition` / `goal_task_transition` payloads, which contain IDs, old/new status,
ownership and the actor that changed state; prompt, answer, goal, task and blocker bodies
never cross that boundary.

```bash
python3 bin/chatgpt_server_bus.py --db <db> create-goal \
  --goal-id release-v2 --owner gemini --created-by gemini \
  --description-file /path/to/goal.md
python3 bin/chatgpt_server_bus.py --db <db> add-goal-task \
  --goal-id release-v2 --task-id packaging --assignee codex --created-by gemini \
  --description-file /path/to/task.md
python3 bin/chatgpt_server_bus.py --db <db> transition-goal-task \
  --goal-id release-v2 --task-id packaging --status in_progress --changed-by codex
python3 bin/chatgpt_server_bus.py --db <db> backlog-summary
```

The summary headline is child work: completed N / blocked N / user-decision-required N.
Goal status totals are returned separately so a caller never silently equates a finished
child item with a finished goal.

## Durable goal driver

`bin/server_goal_driver.py` is the thin judgement layer above the durable backlog. It is
not an executor and it does not reuse round tasks. A goal may live for days; each child
work item keeps its assignee and explicit status in SQLite, while Gemini is consulted only
at a management boundary where no assigned task is currently `open` or `in_progress`.

A manager turn is reserved in `goal_driver_runs` before Antigravity is called. A crash,
timeout, non-zero provider exit, invalid JSON, forbidden attempt to mark a task complete,
or an invalid/no-op mutation leaves that turn `attention_required`. The driver will not
call Gemini again for that goal until a person explicitly acknowledges the exact latest
run. This is the same no-automatic-retry rule as seat execution: the provider may already
have acted.

The shared `provider.lock` is acquired before the model call. Lock contention returns a
wait result and does not reserve a run or mark anything failed. On this Linux host the
driver also prepends `/snap/bin` to `PATH` and sets UTF-8 environment variables for the
provider process.

Gemini may add one concrete task, move a task among non-completed states, or move the
goal. It may never record child task completion; that belongs to the assignee or person
who actually observed the work. Goal completion is accepted only after at least one child
task exists and every child is already explicitly `completed`.

```bash
PYTHONUTF8=1 PATH=/snap/bin:$PATH python3 bin/server_goal_driver.py --db /path/to/bus.sqlite3 \
  start --goal-id release-v2 --goal-file /path/to/goal.md

PYTHONUTF8=1 PATH=/snap/bin:$PATH python3 bin/server_goal_driver.py --db /path/to/bus.sqlite3 \
  advance --goal-id release-v2

# Timer/operator sweep: choose the oldest manager-ready goal, but make at most one
# provider call in this process invocation.
PYTHONUTF8=1 PATH=/snap/bin:$PATH python3 bin/server_goal_driver.py --db /path/to/bus.sqlite3 \
  advance --all

PYTHONUTF8=1 PATH=/snap/bin:$PATH python3 bin/server_goal_driver.py --db /path/to/bus.sqlite3 \
  status --goal-id release-v2
```

A timer may call `advance --all` repeatedly because routine waiting is side-effect free:
active assigned work, a blocked/user-decision goal, a completed goal, provider-lock
contention, and an unresolved prior manager run all return without another model
submission. One invocation advances at most one manager-ready goal, so a timer cannot
fan out heavyweight provider calls. A stuck manager run does not starve another ready
goal; it is surfaced only when there is no other ready management boundary.

The timer does not execute `goal_tasks`. A task assigned to `chatgpt`, `codex`,
`claude`, or `gemini` still needs an execution path (or a person) to do the work and
explicitly record its state. This timer only resumes Gemini's backlog-management boundary.
The separate "call a meeting if you cannot handle it" judgement is intentionally absent;
that remains a Gemini policy decision outside the backlog implementation.

### Optional user-level timer (no sudo performed by automation)

The repository ships `deploy/systemd/user/board-goal-driver.service` and `.timer`. They
run as the logged-in service user, contain no `User=`/`Group=` override, put `/snap/bin`
first, share the existing `provider.lock`, and pipe only the driver's transition payload
to `board_notify.py`. The prompt, response, goal body and blocker body never enter the
Discord pipeline.

Installation remains a human/operator action:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/user/board-goal-driver.service ~/.config/systemd/user/
cp deploy/systemd/user/board-goal-driver.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now board-goal-driver.timer
systemctl --user status board-goal-driver.timer
systemctl --user list-timers board-goal-driver.timer
```

A user service manager normally stops when that user has no login session. If this server
must continue the timer after logout or across reboot, an administrator must explicitly
enable lingering for the service account (for example `loginctl enable-linger board`);
automation here never runs that command or `sudo`. Treat that as host provisioning, not
bus logic.

Discord still receives only backlog state-transition metadata through `board_notify.py`.
The manager prompt, response, goal/task descriptions, blockers, and attention details stay
in SQLite artifacts and never cross into Discord. The instruction channel remains
bot-write-forbidden.

## Minimal operator flow

```bash
python3 bin/chatgpt_server_bus.py --db /home/<service-user>/.local/state/ai-bus/bus.sqlite3 init
python3 bin/chatgpt_server_bus.py --db /home/<service-user>/.local/state/ai-bus/bus.sqlite3 create-round \
  --round-id release-1 --sender gemini --participants chatgpt,codex,claude \
  --question-file /path/to/question.md --priority 2
python3 bin/chatgpt_server_bus.py --db /home/<service-user>/.local/state/ai-bus/bus.sqlite3 status \
  --round-id release-1
```

`chatgpt-server-worker@<service-user>.service` polls only the `chatgpt` address. Each task launches an
ordinary ChatGPT web conversation from a throwaway copy of the manually authenticated
Chrome profile. An uncertain browser delivery becomes `attention_required` and is never
automatically requeued.

`gemini-server-worker@<service-user>.service` polls only the `gemini` address and
uses the service user's existing Antigravity `agy` login. It passes the compact task
packet on stdin so artifact text is not exposed in the process command line, keeps
Antigravity in plan mode, and applies the same no-automatic-retry rule.

`codex-server-worker@<service-user>.service` and
`claude-server-worker@<service-user>.service` use the service user's manually
authenticated official CLIs. Codex runs with a read-only sandbox; Claude runs in plan
permission mode with its tools, customizations, and MCP servers disabled. Both use
ephemeral sessions, receive task artifacts on stdin from an empty temporary working
directory, and apply the same no-automatic-retry rule.

All four workers reserve one shared advisory provider lock before claiming a task. This
keeps heavyweight AI/browser executions serialized on a small host and, importantly,
leaves a task pending when another seat owns the slot. The operating system releases
the lock if a worker crashes.

The manual-login Chrome is intentionally not part of the boot target. Start it only to
sign in or refresh authentication, then stop it before normal worker operation. noVNC,
VNC, and DevTools listen on loopback only; use an SSH tunnel for the one-time login.
CLI authentication caches are secrets: keep them under the service user's home, never
copy them into the repository, logs, Discord, or bus artifacts.
