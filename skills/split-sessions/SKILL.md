---
name: split-sessions
description: Split one question across N independent ChatGPT sessions seated on one meeting board, so each answers without seeing the others and the board seals before anyone can read. Use when independent judgement is worth the runtime cost and the answers might genuinely diverge. Do not use to make one session play several parts, and do not use when everyone is expected to converge.
---

# Split sessions

Run `bin/chatgpt_split_sessions.py`. It creates a meeting board, writes one mission per
seat, and hands the manifest to `bin/chatgpt_oracle_multi.py`, which already owns session
creation. This skill is the operator gesture; it is not a second runner.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_split_sessions.py" 4 `
  --project-root C:\dev\meeting-rooms `
  --question-file C:\dev\question.md
```

`--plan-only` writes the room and missions and launches nothing. `--dry-run` drives the
runner to the submission boundary. Neither opens a browser, so neither tells you whether
the browser path works.

## What a seat is

**One ordinary chat session.** Ordinary chat exposes no child-creation primitive — a live
session was checked on 2026-09-07 and created zero children — so the separateness comes
from opening separate sessions, never from asking one session to play several parts. A
run whose sessions do not report distinct locators fails as `MULTI_AGENT_SHARED_SESSION`
rather than reporting a successful review.

Repository-search and desktop-control plugins are access routes. They are not seats.

## Choosing the project root

Use a directory that exists for rooms, not the repository under discussion. Oracle holds
a session lock **per project root**, so pointing a split at a project that already has a
live Oracle run fails every lane with `PROJECT_SESSION_STILL_LIVE` before any browser
opens. The board does not care where it lives.

## Seats open one at a time

`--max-concurrency` defaults to 1. Simultaneous opens have failed together before, and a
default that reproduces that makes the first multi-seat run uninterpretable. Raise it
only once a sequential run is boring.

Start at two seats when the browser path has changed since it was last proven. Four seats
buy nothing extra when the failure is in the launch.

## A seat that never comes up

It is withdrawn and the rest still seal; the withdrawal rides in the sealed bytes so a
short bundle can never read as a full room. Two cases are refused on purpose:

- A seat that already answered is reported, not forced out. A room that cannot seal beats
  a bundle that quietly dropped an answer somebody gave.
- If **no** seat came up, the room is left untouched and the report says
  `no_seat_came_up`. Withdrawing there would drop seats until the floor refused the rest
  and then name the survivors as seated when no session exists for them.

## After the split

The board owns the phases — see `docs/MEETING_BOARD_V0.md` and
`bin/chatgpt_meeting_board.py`. Sessions submit, nobody reads, the room seals, and only
then is cross-examination opened on a named conflict. Convergence is a result: if the
seats agree, open no issue rather than manufacturing one.

## Bounds

Two to eight seats. Missions carry a token file path, never a token, because mission bytes
are hashed into the run receipt. Do not set `CODEX_THREAD_ID` to clear a lock — that
disables the ownership check rather than passing it, and a foreign task may be diagnosed
but never settled.
