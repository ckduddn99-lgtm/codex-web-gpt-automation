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
