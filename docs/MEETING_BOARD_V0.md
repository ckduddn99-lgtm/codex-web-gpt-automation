# Meeting board v0 — design

The multi-agent board is not implemented yet. A local, one-user/one-web-GPT
dialogue slice now exists in `bin/chatgpt_log_chat.py`; it proves the single
browser turn plus DevSpace long-poll transport without pretending to implement
the later multi-agent stage gates.

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
