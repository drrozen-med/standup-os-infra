# Watchtower

Read-only portfolio monitor. One snapshot, one decision queue, one brief.

```
lanes.json      repos grouped into lanes (edit when a repo is added or retired)
decisions.json  the queue of things only the owner can decide
bin/watchtower-collect.py  reads GitHub, writes latest.json + brief.md
```

## What it does

Two layers, live first.

**Live (seconds old):** every agent session active in the last 7 days, read from
the local CCC Navigator. Its triage worker labels each session every two minutes
from local rules (nothing leaves the Mac): `working`, `needs-you` (approval
waiting, or its last message asks you something), `blocked`, `done`, `idle`,
`stale`. The brief shows, per lane, who needs you and why (the agent's own
question), what is blocked, what is running, and your notes on those sessions.
Sessions map to lanes by project name: `repos` plus the `match` substrings in
`lanes.json`; anything else is listed under "Other projects".

**GitHub (refreshed at most hourly, cached in between):** default-branch CI,
open PRs (CI state, idle days), open issues labelled `release-blocker` /
`security` / `P0`. A repo that cannot be read is flagged, never skipped.

`brief.md` lists open decisions first (priority, then deadline, with days left),
then one section per lane with the live picture and GitHub flags together.

It never writes to GitHub or to any session, and never merges, deploys, spends
or messages anyone.

## Run on the Mac (live mode, the default)

The live layer needs the navigator, so Watchtower runs where the sessions run:

```bash
# launchd job, every 3 minutes: ~/Library/LaunchAgents/com.standup-os.watchtower.plist
launchctl kickstart -k gui/$(id -u)/com.standup-os.watchtower
cat ~/.ccc-navigator/watchtower/brief.md        # the brief
python3 ccc-navigator/nav.py brief --table      # same live layer, instant
```

GitHub is read with `GITHUB_TOKEN` if set, otherwise through the signed-in `gh`
CLI. `WATCHTOWER_GITHUB=0` turns that layer off; `WATCHTOWER_GITHUB_EVERY`
changes the refresh interval (seconds, default 3600).

## Deploy on a server (GitHub layer only)

A server cannot see the Mac's sessions, so there the brief has decisions and
GitHub only.

```bash
sudo install -m755 bin/watchtower-collect.py bin/watchtower-run.sh /usr/local/libexec/
sudo install -d /usr/local/share/standup-os-watchtower /etc/standup-os /opt/standup-os/watchtower-state
sudo install -m644 watchtower/lanes.json watchtower/decisions.json /usr/local/share/standup-os-watchtower/
sudo cp systemd/watchtower.service systemd/watchtower.timer /etc/systemd/system/

# /etc/standup-os/watchtower.env (chmod 600, owner claude-agent):
#   STANDUP_OS_ROOT=/opt/standup-os
#   GITHUB_TOKEN=<fine-grained, read-only>

sudo systemctl daemon-reload
sudo systemctl enable --now watchtower.timer
sudo systemctl start watchtower.service   # first run now
cat /opt/standup-os/watchtower-state/brief.md
```

`GITHUB_TOKEN`: fine-grained PAT on the `drrozen-med` account, all repos, read-only:
Metadata, Contents, Pull requests, Issues, Checks, Commit statuses.

Local test without installing: `WATCHTOWER_STATE_DIR=/tmp/wt python3 bin/watchtower-collect.py`

## Working the decision queue

`decisions.json` is the source of truth for what is waiting on the owner. Entry fields:
`id, lane, priority (1-3), question, options, deadline (YYYY-MM-DD or null), source, status`.
Statuses: `open` (waiting on the owner), `delegated` (a lane is handling it), `done`
(add `"answer"` and `"decided_by"`). The control tower decides reversible, $0 operational
calls itself (CI re-runs, read-only checks, opening PRs) and marks them `decided_by:
control-tower`; lane managers escalate to the control tower, not to the owner. Nobody but the owner marks a money, data-deletion, deploy or outreach
decision as done.

## Guardrails for lane managers

Lane managers and workers must not, without an owner answer recorded here:
spend money, merge to a default branch, deploy to production, run production writes,
delete branches or data, or send outreach to a person. They raise a decision instead.

## Session hygiene

Any session above 80% context hands off to a fresh session with a written summary.
Session-level state (working, needs-you, blocked, done) comes from the live
layer above.
