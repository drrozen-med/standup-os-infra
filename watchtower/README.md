# Watchtower

Read-only portfolio monitor. One snapshot, one decision queue, one brief.

```
lanes.json      repos grouped into lanes (edit when a repo is added or retired)
decisions.json  the queue of things only the owner can decide
bin/watchtower-collect.py  reads GitHub, writes latest.json + brief.md
```

## What it does

For every repo in `lanes.json`: default-branch CI, open PRs (CI state, idle
days), open issues labelled `release-blocker` / `security` / `P0`. A repo the
token cannot see is reported as `NO ACCESS`, never skipped. `brief.md` lists open
decisions first (priority, then deadline, with days left), then one section per lane.

It never writes to GitHub and never merges, deploys, spends or messages anyone.

## Deploy

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
When a decision is made, set `"status": "done"` and add `"answer"`; lane managers act on
the answer. Nobody but the owner marks a money, data-deletion, deploy or outreach
decision as done.

## Guardrails for lane managers

Lane managers and workers must not, without an owner answer recorded here:
spend money, merge to a default branch, deploy to production, run production writes,
delete branches or data, or send outreach to a person. They raise a decision instead.

## Session hygiene

Any session above 80% context hands off to a fresh session with a written summary.
Session-level state (blocked, review-ready, context use) comes from the daily
"Portfolio morning brief" routine, which reads Claude Code sessions; this collector
covers GitHub only.
