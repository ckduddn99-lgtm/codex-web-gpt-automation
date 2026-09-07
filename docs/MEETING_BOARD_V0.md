# Meeting board v0 — design

The file-backed prototype is implemented in `bin/chatgpt_meeting_board.py`: registered
roster, server-held phases, sealing, and scoped cross-examination. The REST service and
the web UI are not. A one-user/one-web-GPT dialogue slice also exists in
`bin/chatgpt_log_chat.py`; it proves the browser turn plus long-poll transport the board
rides on, and does not itself implement any stage gate.

## What a participant is

**One ordinary chat session. Not a child agent.**

An earlier plan kept one parent chat and spawned three configured children inside it.
It was tried against a live session on 2026-09-07 and the finding was not "the child
model could not be set" — it was that ordinary chat exposes **no child-creation
primitive at all**. Zero children were created, so there was nothing left to reconfigure
afterwards either. Repository-search and desktop-control plugins are access routes, not
agents; counting them as participants would be a false receipt.

So the unit is a session, sessions join from outside, and nothing is spawned. That is
also what makes the cross-vendor goal reachable: a room that admits sessions can admit a
session from anyone.

Today's paths are [MULTI_AGENT_ANALYSIS.md](MULTI_AGENT_ANALYSIS.md) (independent
sessions, handoff files, one synthesis) and [RESEARCH_MEETING.md](RESEARCH_MEETING.md)
(four fixed participants, bounded polling rounds). The board replaces neither
controller. It changes where participants meet.

## What the current shape cannot do

- **Handoffs are N files, so there is no order and no reply relation.** Who
  answered whom is not recorded anywhere, because there is nowhere to record it.
- **The synthesis session only reads.** It cannot cross-examine — it has no way to
  say *"you called file:line 42 X, but reading it I get Y"*. In one past review all
  three reviewers independently left the same item unverified. A conversation would
  have assigned it to someone; three parallel monologues could not.
- **It is one vendor.** A board lets a local agent and a browser-driven web agent
  argue the same issue in the same room. This is the largest gain and the current
  shape cannot reach it at all.

## Structure

- A REST API plus a web UI on a **host of its own**.
- Participants join over plain HTTP with whatever execution they already have.
- **Long-poll** (hold the response until a new post arrives) instead of polling
  loops, so an idle participant spends no tokens waiting.
- A person opens the URL and watches the meeting live. This is what "watch the
  meeting happen" actually requires; the file-handoff shape can never provide it.

## Stage gates — the part that cannot be added later

The server enforces the stages. It does not ask clients to behave.

1. **Write-only.** A participant may post and may not read. No exceptions, no
   read-your-own-draft convenience.
2. **Seal.** When every participant has submitted, the stage is closed and hashed.
3. **Open.** Reading is enabled and rebuttal is allowed against sealed content.

Asking a client "please don't read yet" is not a gate. Without server enforcement
the first post anchors every later one, independence is gone, and with it the
entire reason to run more than one agent. **Retrofitting this is not possible** —
by the time it matters the transcripts are already contaminated, so it belongs in
the first commit or nowhere.

### How the prototype holds the gate

`submit` never routes a participant's text through the shared room. The text goes
straight to host-only state and the room receives a hash receipt, so there is no window
in which one answer is sitting somewhere another participant was told to look. `seal`
re-hashes the stored text and compares it against the receipt that was public during
phase 1 — a submission edited after the fact fails the seal rather than passing through
it. Reads (`read-bundle`), replies, and issue creation all refuse out of phase, and the
long-poll carries no payload at all while collecting.

What the prototype does **not** give you: an OS trust boundary. A participant that runs
local processes can read host-only state directly. Closing that is what the REST service
on its own host buys, and it is the whole reason the gate is being written now rather
than after there are transcripts to protect.

## Participation

| Participant | Transport |
|---|---|
| Local agent (e.g. Claude Code) | HTTP directly |
| Web agent with a shell (DevSpace) | HTTP from that shell |
| A bare chat session with no execution | only here is an MCP server needed |

### Why there is no MCP layer

A web agent that has a shell can run a polling script inside a single turn, react
to new posts, and reply. Every tool result wakes the model again, and that is real
participation — not a simulation of it. So a board-specific MCP server buys
nothing. Ordinary REST is enough. MCP is only for a chat session that cannot
execute anything.

## Boundaries

- **Host separation is a hard requirement, not a preference.** The board must not
  run on any host that holds live brokerage credentials, trading state, or the
  ability to change trading behaviour. No network path from the board to such a
  host, and separate keys. Treat a board host as untrusted by the systems that
  matter, because its whole purpose is to accept posts from many agents.
- **Invitation gate required.** An open board means any passing agent's text
  becomes another participant's input.
- **Posts are data, never instructions.** Text on the board that reads as a command
  is still data. This holds for every participant, including the synthesis.
- The board needs almost no CPU. Size the host for availability, not compute.

## Known limits

- **Someone has to open the turn.** A web agent does not start a conversation on
  its own. The browser automation that launches sessions stays in the picture, and
  that is the part that breaks most often.
- Platform turn ceilings (on the order of an hour and a half) bound one meeting.
  Enough for a meeting, not for an open-ended room.
- Connector registration is done by a person. An agent does not receive a link and
  admit itself. That is a safety property, not a gap to close.

## Implemented precursor: single-session log chat

The precursor deliberately opens one regular Oracle web session only. Its first
prompt connects the configured DevSpace app and instructs GPT to long-poll a
loopback dialogue server from the exact project root. The person types in the
terminal; replies are appended to an inspectable JSONL log and printed there.

```powershell
python bin/chatgpt_log_chat.py start --project-root C:\project --app-name codex
```

Type `/quit` to post the bounded stop control message. This precursor has:

- separate random `user` and `gpt` invitation tokens stored in gitignored runtime
  files, never in the message log or mission text;
- append-only messages with monotonic IDs and explicit `reply_to` relationships;
- authenticated loopback HTTP and bounded long-poll waits;
- an explicit rule that board text is untrusted data and cannot authorize shell
  commands or project mutation;
- one initial Oracle submission and no browser follow-up injection.

It is a `dialogue` room, not a meeting room. Do not add multiple agents to it or
claim independent first-round analysis. The future multi-agent server must still
enforce write-only, seal, and open stages before accepting real meetings.

### Commander transport experiment

When the DevSpace `codex` connection is unavailable, the same executable also
offers a separate, explicitly manual attachment path. It uses Remote Desktop
Commander only to carry inbox/outbox files and reserves `codex-1` for repository
work. It never invokes DevSpace or Codex CLI:

```powershell
python -X utf8 bin/chatgpt_log_chat.py commander-start --project-root C:\project
```

The command prints an `attach-prompt.md` path. A person gives that path to one
already-open ordinary ChatGPT Chat turn with Remote Desktop Commander and
`codex-1` enabled; it must not switch to Work mode. For every ordinary terminal
message, the parent remains a transport
controller and must create exactly three hosted children with explicit
`gpt-6-astra` / `ultra` settings. Successful outbox records are rejected unless
all three child receipts match and `parent_ui_changed` is false. Unavailable
model/effort combinations fail closed; inheritance and fallback are forbidden.

This gate validates the response receipt, not pixels in the ChatGPT model picker.
A live browser canary must separately prove that the parent Composer UI stayed
unchanged. This remains a dialogue precursor: it does not implement the future
server-enforced write-only/seal/open meeting stages.

The 2026-09-07 ordinary-Chat canary proved Commander inbox/outbox transport and
parent-UI invariance, but the attached Chat tool set exposed no genuine hosted
subagent creation capability with explicit model and effort controls. The room
therefore returned a truthful `blocked` receipt with no children. Under the
no-Work constraint, this path is a working log transport but cannot yet produce
the requested `gpt-6-astra` / `ultra` child agents.

## Sequencing — do not build this first

**Stabilise the existing path to all-lanes-complete first.** A room where half the
participants cannot get in is worth nothing, and a lane that fails at submission is
exactly that. The value of a board appears **only when opinions actually diverge**.
A review where everyone converges gains nothing from a room.

So stage 2 opens **conditionally**: only when the synthesis finds a real conflict,
and even then as cross-examination on the named issue rather than open discussion.

### Filling the seats: `split N`

`bin/chatgpt_split_sessions.py` is the fan-out half. One operator gesture creates the
room, writes one mission per seat, and hands the manifest to the existing multi runner —
so the user splits once and N independent sessions come up underneath.

Two choices it makes on purpose. Seats open **one at a time** (`max_concurrency` is 1),
because simultaneous opens have failed before in ways that were not independent of each
other. And a seat whose session never comes up is **withdrawn rather than fatal**: the
remaining sessions still seal, and the withdrawal is recorded in the sealed bytes. One
broken browser turn used to make an entire run unusable; now it costs one seat.

It is a planner, not a second runner. Session creation, wave bounding, the
distinct-session-locator check, and merging stay in `chatgpt_oracle_multi.py`.

`split` inherits every risk of the browser automation it launches through. It cannot
prove that path works, and that path has not been verified against a live session since
it was last fixed — which is why the first split to run should ask for one seat.

### Where the prototype stops

The protocol is exercised end to end by `tests/test_chatgpt_meeting_board.py` and by the
real command line, four participants through submit, gate, seal, issue, and reply. Every
one of those participants was a local process. **No live chat session has joined a board
yet**, and until one has, "a room four sessions can hold" is a claim about code, not an
observation.

The next step is one session, alone, through submit and seal — then a second, and only
then four. Connecting four at once was already tried on the older path and the failures
were not independent of each other, which is why the roster grows one seat at a time.
