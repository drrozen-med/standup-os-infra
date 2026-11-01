# CCC Navigator

A Repo → Group → Session sidebar for [Claude Command Center](https://github.com/amirfish1/claude-command-center) (CCC).
CCC's own session list groups by working folder, so every worktree becomes its own "project", and
headless runs and CCC's own title-generation calls bury the real sessions. This is a small separate
web app that reads CCC's API and shows the same sessions organised the way you actually work.

- **Repo:** sessions group under their *main* git repo. Claude Code worktrees (`.claude/worktrees/…`),
  `-wt-` folders and Orca workspaces all fold into the parent. Detection reads `.git` files, no subprocesses.
- **Group:** your own groups per repo (`infra`, `fe`, …). Sessions that ran in an **Orca workspace** are
  grouped under that workspace's name automatically; filing a session by hand overrides it.
- **Session:** shown by your nickname (falls back to CCC's title), with engine, worktree, live/needs-you state.
- **Hidden by default:** CCC's scratch/title-gen runs, headless `claude -p` / `codex exec` runs, archived or
  trashed sessions. Three or more sessions with the same opening prompt collapse into one "batch" row.
- **Sources:** everything CCC ingests (Claude, Codex, Devin, Cursor, …) via `GET /api/conversations?all=1`,
  plus **Pi** sessions read from `~/.pi/agent/sessions` (CCC doesn't read Pi), plus **Orca** workspaces read
  from Orca's profile database, plus **Outsourcerer** jobs from `~/.outsourcerer/jobs` (read-only), plus **Server .251** agents via `agent-status.json` over SSH.

Nicknames, groups, stars and hides live in `~/.ccc-navigator/state.json`. CCC's and Orca's data are only
read, never written.

## Cleaning up the list

Most of the value here is being able to see only what matters, so curation is a first-class feature:

- **Star** a project, a group or a session and it sorts first, is never proposed for hiding, and is never
  touched by a batch operation.
- **Hide** anything, at any level. Hiding is a tombstone, not a delete: `unhide` brings it back, and the
  **Show hidden** button reveals everything dimmed.
- **Auto-resurface**: a hidden item comes back on its own the moment it is running again or waiting on
  the user. Turn it off with `CCC_NAV_RESURFACE=0`.
- **`nav.py curate`** proposes what to hide: anything quiet for longer than `--older`, with no running
  process, not waiting on you, not starred, and with no Orca agent that spoke in that window. Dry-run by
  default; `--apply` writes it.

```bash
python3 ccc-navigator/nav.py curate --older 2d          # show the proposal
python3 ccc-navigator/nav.py curate --older 2d --apply  # do it
```

State is written atomically and snapshotted before every change: `state.json.bak` holds the previous
version and `backups/state-YYYYMMDD.json` keeps one dated copy per day for 30 days.

## Run

Needs Python 3.9+ (stdlib only) and CCC running.

```bash
python3 ccc-navigator/navigator.py                       # opens http://127.0.0.1:8095, CCC on :8090
python3 ccc-navigator/navigator.py --ccc http://localhost:9001 --port 8096
```

| Option | Default | |
|---|---|---|
| `--ccc` / `CCC_URL` | `http://localhost:8090` | CCC base URL |
| `--port` / `CCC_NAV_PORT` | `8095` | Navigator port (binds 127.0.0.1 only) |
| `--state` / `CCC_NAV_STATE` | `~/.ccc-navigator/state.json` | Nicknames and groups |
| `--pi-dir` | `~/.pi/agent/sessions` (or `$PI_CODING_AGENT_DIR/sessions`) | Pi sessions |
| `--orca-dir` / `ORCA_DATA_DIR` | `~/Library/Application Support/Orca` on macOS | Orca's data folder |
| `--no-browser` | | Don't open a browser tab |

## Use

- Click a session to see details, set a **nickname**, or move it to a group (or type a new group and press Enter).
- **Drag** a session onto a group header to file it.
- Keys: `/` search, `↑` `↓` move, `Enter` open in CCC, `r` rename, double-click a row to rename.
- **Open in CCC** opens the conversation in CCC's own window (`/?ccc_popout=conversation&conv=<id>`).
- With nothing selected, the right pane shows how each session's repo was detected and any folders it
  couldn't place. Check this first if something lands in the wrong place.

## Ride mode (voice)

`/ride` (also **Ride mode** at the top of the sidebar) reads the open decisions and the sessions waiting on
you aloud, one at a time, rewritten for the ear (no code, paths or links). Headphone buttons and the lock
screen control it: play/pause, next, and previous to repeat. **Hands-free talk** listens after each item:
answer a decision ("yes", "option two", "later"), say "more" to hear the full reply, reply to a session in
your own words, or say "talk to BoardsBridge" for a back-and-forth with any session. Every message is read
back with a moment to say "cancel"; anything that looks hard to undo (buy, deploy, merge, send, delete…) is
held under **Confirm when you stop** instead of being sent. Every agent reply in the chat also gets a
speaker button.

Everything runs on the Mac (`voice.py`): Kokoro-82M speaks (the macOS Hebrew voice reads Hebrew sentences),
local Whisper listens. Both are optional; without them `/api/voice/*` answers 503.

```bash
# speech: ~/.local/share/kokoro-venv (kokoro-onnx, soundfile) + ~/.local/share/kokoro/{kokoro-v1.0.onnx,voices-v1.0.bin}
# listening:
python3 -m venv ~/.local/share/whisper-venv && ~/.local/share/whisper-venv/bin/pip install mlx-whisper
```

`CCC_NAV_VOICE` (default `af_heart`), `CCC_NAV_VOICE_SPEED` (`1.1`), `CCC_NAV_WHISPER_MODEL`
(`mlx-community/whisper-large-v3-turbo`), `CCC_NAV_HEBREW_VOICE` (`Carmit`). Audio is cached in
`~/.ccc-navigator/voice/`. Talking hands-free needs the screen on (mount the phone); listening alone works
with it locked.

## How Orca workspaces are read

Orca keeps per-profile state in SQLite at `<userData>/profiles/<profileId>/profile-state.db`
(`profile_state_documents` table; the `repos`, `worktreeMeta` and `folderWorkspaces` rows). The navigator copies
the database (with its WAL) to a temp folder and reads the copy, at most every 10 s and only when it changed.
A workspace's name is its `displayName`, else its current branch (what Orca shows). Orca renames a workspace's
folder after the first prompt, so a session pointing at the old folder is matched by branch instead.

## Server .251 agent status

Install `bin/vm251-status.sh` on the server as user `claude-agent`. Add to crontab:

```bash
*/3 * * * * ~/bin/vm251-status.sh
```

The script writes `~/agent-status.json` atomically (write tmp then mv) with container/agent info from `incus` and host-level processes. Set `CCC_NAV_VM251=0` to disable in the navigator.

## Tests

```bash
cd ccc-navigator && python3 -m unittest discover -s tests
```
