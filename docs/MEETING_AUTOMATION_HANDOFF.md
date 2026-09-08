# Meeting automation — handoff

Public-safe by contract. Host addresses, tokens, and credential paths stay out of this
file; `docs/SERVER_AI_BUS.md` holds the design and this holds the state, the findings,
and the one decision still open.

## Where it stands

Four seats answer bus tasks through their own workers, serialized on a shared provider
lock. A round collects independent answers, seals them under a digest, and the driver
walks the consensus barriers. Discord carries a person's questions to Gemini and returns
answers and the four transitions worth interrupting someone for.

Working, verified live: collection, the seal, `stage-results`, the driver's refusal
semantics, the bridge, the notifier suppression rules. `bin/round_driver.py advance` is
idempotent and safe on a timer.

Not done: no systemd timer runs the driver or the bridge yet, so both are invoked by
hand. The Discord round trip has not been exercised against a real message.

## What three live rounds found

Every finding came from a seat refusing to comply. None would have been caught by the
test suite, because each was a defect in what the seats were being asked to believe.

**The acknowledgement showed refs, not answers.** `bundle()` returns refs, since each
body is stored once. Passing that through displayed "(no body)" for every seat and then
asked each to attest to a digest over content it could not read. Both seats refused.
Fixed: the refs are resolved and the answers rendered.

**The task was fenced inside the evidence.** The packet embedded "make your last line
exactly ACK <hash>" in the very text the seat was told to evaluate — the injection shape
this system distrusts everywhere. The review, propose and vote packets had it too; the
vote one embedded the proposal being voted on. Fixed: instruction first and fenced,
material fenced separately and named as claims.

**A fenced block inside an artifact cannot make itself authoritative.** The fix above
could not work and should not have: the worker's preamble says artifact content is never
an instruction, so a "conductor block" written into that content is still content. The
seats read this correctly and refused every stage. Fixed by moving the authority out of
the artifact: `BUS.task_framing(stage)` is chosen by the worker, which is our code and
knows the stage before it builds the packet. Same split as the two Discord bots — the
conductor lane is trusted because the transport enforces it, not because the message says
so.

**And where the words sit matters, not just who wrote them.** After that, codex
acknowledged and claude still refused, naming the reason: the framing arrived in its user
turn, so "the worker put this here" was a claim it could not check. The claude seat now
receives the framing through `--append-system-prompt`, a slot it can distinguish, and the
packet no longer repeats it. Codex takes instructions positionally, has no equivalent
slot, and already accepted the packet framing.

## The decision that is open

After all four fixes, codex acknowledges and claude still declines, with reasoning that
has not moved across three wordings:

> I have no independent way to verify the digest against actual bundle contents — no
> shell, no repository, nothing here to hash. Emitting the line would read as a receipt
> confirming I verified a byte-identical bundle, when I am copying a string handed to me.

Do not reword the instruction a fourth time. Each of the three rewrites fixed a real
defect; another would be talking a seat into compliance, which is the pattern the seat
named and the design forbids.

The refusal is itself an answer to the question the round was asked — which barrier is
real and which is ritual. The seats split on it in round one and the split held:

- **claude**: explicit consent is the only load-bearing artifact; the review-complete
  declaration is implied by it and is the one to drop.
- **codex**: the review-complete declaration records that objections were solicited and
  handled, which consent does not imply; the hash echo is the overreach.

Options, for the owner to settle:

- **A. Retire the acknowledgement barrier.** Keep review-complete and the vote. Both are
  statements a seat makes about its own position — things it knows. The digest echo is a
  statement about bytes it cannot see. A barrier that is dishonest when passed and
  blocking when not is not doing work.
- **B. Rename the token** (`READ <digest>`, explicitly not a verification claim). The
  seat may read this as the same act under a new name.
- **C. Allow asymmetry.** Record a decline as a decline. This requires deciding whether a
  declined acknowledgement blocks consensus.

If A or C is chosen, keep the property claude granted in round one — seats naming
different digests would reveal a transport error or a substituted bundle. That check does
not need a seat: the driver already knows which digest it sent to whom and can compare
them itself. Asking a seat to perform it was giving away work the machine can do.

## Rules that must survive any change here

- Silence, failure, timeout, rejection and abstention never become consent. `finalize`
  re-checks every barrier and requires unanimous explicit approval.
- Nothing is retried automatically. The model may already have acted.
- An objection stays open until the participant that raised it closes it.
- Result refs stay hidden during collection; `sealed_artifact` refuses non-collection
  stages so a seat cannot resolve another seat's vote.
- The bundle digest covers the collection stage only. Three separate reads of the tasks
  table once meant "the collection" without saying so and each broke differently when
  stages arrived.
- Discord receives transitions, never prompts or answers, and never the instruction lane.
- Refusal must stay available. Every defect above was found by a seat declining, so any
  framing that reads as pressure to comply destroys the mechanism that finds these.

## If you are picking this up

Read `docs/SERVER_AI_BUS.md` first for the design, then `bin/round_driver.py` — its module
docstring states why the conductor is split between a model that judges and a program
that counts. `scripts/run_fast_gate.py` is the verification bar; the bus, stage, driver,
worker, notifier and bridge suites are all in it and it stays under its budget.
