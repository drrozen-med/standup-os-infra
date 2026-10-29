# standup-os-infra

tmux + systemd deployment configuration for StandupOS.

## What's extracted here

- `systemd/` — systemd service files for all agents
- `bin/` — launcher and utility scripts
- `watchtower/` — read-only portfolio monitor and decision queue (see `watchtower/README.md`)

## Deployment

```bash
# Copy systemd services
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mm-agent@rafael mm-agent@shani mm-agent@alex mm-agent@dan mm-agent@itamar

# Or use the install script
bash src/agents/deploy/install-systemd-services.sh
```

## Environment

```bash
export STANDUP_OS_ROOT=/opt/standup-os
export STANDUP_OS_SESSION_DIR=$STANDUP_OS_ROOT/sessions
```

## Prerequisites

- tmux installed
- bun runtime for TypeScript scripts
- Python 3.11+
- LLM API keys configured in credentials dir

## Agent sessions

Each agent runs in two tmux sessions:
- `mm-{platform}-{agent}-companion` — executive/companion
- `mm-{platform}-{agent}-worker` — worker

Managed by `session-watchdog.sh` (auto-restart, rotation).

## Related repos

- [standup-os-core](https://github.com/drrozen-med/standup-os-core) — AI agent system
- [standup-os-api](https://github.com/drrozen-med/standup-os-api) — FastAPI backend

## Tools

- [`ccc-navigator/`](ccc-navigator/README.md): a Repo → Group → Session sidebar for Claude Command Center, with
  nicknames, your own groups and Orca workspaces as groups. Run `python3 ccc-navigator/navigator.py`.
