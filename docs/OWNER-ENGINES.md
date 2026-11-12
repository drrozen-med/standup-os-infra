# Owner engines (GHI #103)

A project owner can run on any harness the Navigator knows how to drive, not only Claude Code.
The contract is small on purpose: a harness answers **three verbs**, everything else (briefs,
status.json, supervisor, check-ins) is engine-independent code that already exists.

## The three verbs

```python
ls_sessions(ctx, directory, limit)   # rows for the picker: {"id", "title", "updated", "directory"}
deliver(ctx, sid, text)              # -> short human status: "sent", "queued", "failed: ..."
transcript(ctx, sid)                 # -> {"updated": epoch_s, "messages": [...]} or None
```

They live in `ccc-navigator/engines.py` (the registry). `App.deliver()` consults it for
registered engines and keeps the Claude/Devin paths inline (they need CCC's "moved copy"
guard and delivery route). Pi is a thin wrapper over `pi_run.py`.

| engine | ls | deliver | transcript | notes |
|---|---|---|---|---|
| **claude**  | CCC rows (in-tree) | CCC `/api/inject-input` | CCC conversations | always available; registered from a session's menu |
| **pi**      | `pi_sessions()` (in-tree) | `pi_run.send` (resume jsonl headlessly) | `/api/pi/transcript` | one live run per session guard is in pi_run |
| **opencode**| `opencode session list --format json` | `opencode run --agent build -s <id>` (detached, logged) | `opencode session export <id>` | sessions' ids start with `ses_` |

## Switching an owner's harness

- **UI**: the project's context menu → **Owner on another harness…**. Pick the engine
  (only installed ones are offered; `t.engines` carries availability), pick one of that
  engine's sessions in the project folder, done. The last owner key per engine is remembered
  (`owner_by_engine` in state.json) and offered even when the session has no recent activity.
- **CLI**: `nav engines installed`, `nav engines ls opencode --dir ~/projects/airline`,
  and the ordinary `nav owner set <project> opencode:ses_xxx`.
- **Spawn**: `bin/owner-spawn <repo> opencode [--model provider/model]` builds the same
  versioned brief every other owner gets (`bin/owner.py brief <slug>` — goals, rules,
  scoreboard), starts the agent with it as the first turn, and registers the session.

## Session-id shape

OpenCode ids are `ses_<alnum>`; pi ids are uuids; claude ids are uuids. The generic send
endpoint (`/api/pi/send`) infers the engine from that shape unless the caller passes
`"engine"`. It creates an OpenCode session on demand when `-s` does not exist — that is
OpenCode's own semantics, so a mistyped id does not fail loudly; keep ids from the picker.

## Adding a 4th engine

1. Subclass `engines.Engine`, answer the three verbs (talking to your harness's CLI only —
   free models of some harnesses are client-gated and 403 raw HTTP).
2. Copy the "one live run per session" guard used by `OpenCode.deliver` / `pi_run.send`
   (two concurrent runs on one session interleave turns).
3. `engines.register(MyEngine())`.
4. Add tests to `tests/test_engines.py` with the CLI mocked; the suite must stay green.

Known gaps: `claude` spawning is still manual (CCC discovers sessions live; register from the
session menu), and the tree does not yet render opencode rows inline (use `nav engines ls`).
