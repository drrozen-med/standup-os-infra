# Project owners (masterplan #79, phase 1)

Each project has an AI owner: a long-running Claude session that acts as the project's
manager. Its memory lives in files, so a fresh session can take over at any time.

## Files

One folder per project: `~/.ccc-navigator/owners/<slug>/`, where `<slug>` is the basename of the
Navigator project key (`/Users/urirozen_m4/projects/boardsBridge` -> `boardsBridge`).
Env `OWNER_DIR` overrides the root (tests).

| File | Written by | What |
|---|---|---|
| `goals.md` | Uri (owner may propose edits in its log) | Dated outcome goals, guardrails, decision rights |
| `log.md` | owner, supervisor, scoreboard | Append-only. One line per entry: `- YYYY-MM-DD HH:MM [actor] text` |
| `status.json` | owner, through `bin/owner.py status` only | Current status, validated (below) |
| `scoreboard.json` | `bin/owner-scoreboard.py` | Computed from Watchtower's GitHub snapshot |

The old single-file layout `owners/<repo>.md` is migrated once: its `## Log` lines move into `log.md`.

## status.json

```json
{
  "updated_at": "2026-11-03T09:15:00+02:00",
  "session": "claude:<sid>",
  "state": "working | waiting-on-uri | blocked | picking-next",
  "summary": "one line a person reads on the project row (<= 120 chars)",
  "doing": "what is in progress now",
  "next": "the next concrete action",
  "shipped": ["what moved since the last status, e.g. merged #123"],
  "needs_uri": ["decision ids in the navigator inbox"],
  "blocked": null
}
```

`blocked`, required when `state` is `blocked`:
`{"what": "...", "tried": ["way 1", "way 2"], "unblocker": "supervisor | uri | <who>", "by": "YYYY-MM-DD"}`

### Validation (rule 1 of #79: no passive status)

`bin/owner.py status set` rejects the status, writes nothing, and exits 1 with every reason when:
- a field is missing, or `state` is not one of the four values;
- `summary`, `doing` or `next` is empty, or `summary` is over 120 chars;
- `state` is `blocked` and `blocked` lacks `what`, at least 2 different `tried` entries, `unblocker` or `by`;
- `state` is `waiting-on-uri` and `needs_uri` is empty;
- `summary` or `doing` reads as passive (case-insensitive: "nothing changed", "nothing new",
  "no changes", "no progress", "still waiting", "stuck", "can't", "cannot", "unable to",
  "permission", "not possible from this environment") unless `state` is `blocked` with a valid `blocked`.

On success it sets `updated_at` and `session` (from `--session` or env `OWNER_SESSION`),
writes the file atomically, and appends `[owner] <state>: <summary>` to `log.md`.

## Owner registry in the Navigator

- State: `owners: {<project key>: <session key>}` in `~/.ccc-navigator/state.json`, with change-log
  entries like every other mutation (undoable).
- HTTP: `POST /api/owner {"repo": <project key>, "key": <session key or "">}` and the v1 action
  `project.owner` with the same params.
- CLI: `nav owner set <project> <session>`, `nav owner clear <project>`, `nav owner ls`
  (`<project>` accepts the key, slug or display name).
- Tree: each repo in `/api/tree` gets `owner`: `null`, or
  `{"key", "summary", "state", "updated_at", "stale", "problem"}` read from `status.json`.
  `stale` is true when the owner session finished a turn (Stop hook) after `updated_at`, or
  `updated_at` is older than 24 h. `problem` is a short text: "no status yet",
  "no status this turn", "status 26h old".
- The Stop hook (`/api/hook`) for a session that is a registered owner checks `status.json`:
  if `updated_at` is older than that turn's end, append `[navigator] turn ended without a status update`
  to `log.md` (at most once per turn).

## UI

- Project row: a 🧠 button when the project has an owner. Click opens the owner session in the
  main pane (same as opening the Super-co-pilot). Next to the name, the owner's `summary` in
  one muted line; a warning style when `stale` or `state` is `blocked` / `waiting-on-uri`.
- Session menu (Claude sessions): "Make this the owner of <project>" / "Stop being the owner".

## Asking Uri: every question has a kind

An owner never writes "stuck". It asks with `nav.py ask --kind <kind>` (refused without a kind, with the list
below in the error), and the kind decides who gets the question (`docs/supervisor.md`, "The gate"):

- `approval`: reversible and inside the owner's rights. Needs `--default <one of the options>` and
  `--undo "how to roll it back"`. Uri sees it under "Going ahead unless you object"; when the deadline passes
  (tomorrow if none is given) the default is the answer and the owner acts on it.
- `problem`: something broken or shared (a full server, a provider down). Needs `--topic <short-name>`; the same
  topic from several projects is one item on the supervisor's desk, not five in Uri's inbox.
- `errand`: it needs Uri's own hands (his login, 2FA, a payment screen).
- `choice`: a real choice that is his. Money, messages to real people, clinical content, deleting data, accounts
  and access and legal terms are always this, whatever kind the owner gives.

`nav.py decisions --for auto --table` shows an owner's approvals and when they go ahead; `--went-ahead` the ones
that did.

## Owner prompt

`bin/owner.py brief <slug>` prints the prompt that starts or restarts an owner: who it is, the
rules of #79 (and rule 7: every question has a kind), its goals, the scoreboard, its status, the last 10 log entries, the toolbelt, and the
exact `owner.py status set` command it must run at the end of every turn.
