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
  from Orca's profile database.

Nicknames and groups are stored in `~/.ccc-navigator/state.json`. CCC's and Orca's data are only read, never written.

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

## How Orca workspaces are read

Orca keeps per-profile state in SQLite at `<userData>/profiles/<profileId>/profile-state.db`
(`profile_state_documents` table; the `repos`, `worktreeMeta` and `folderWorkspaces` rows). The navigator copies
the database (with its WAL) to a temp folder and reads the copy, at most every 10 s and only when it changed.
A workspace's name is its `displayName`, else its current branch (what Orca shows). Orca renames a workspace's
folder after the first prompt, so a session pointing at the old folder is matched by branch instead.

## Tests

```bash
cd ccc-navigator && python3 -m unittest discover -s tests
```
