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
