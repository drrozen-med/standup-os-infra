#!/bin/bash
# mm-agent-launch.sh — systemd-compatible wrapper for mm-agent daemon
# Resolves shell variable interpolation issue in ExecStart unit file.
#
# systemd performs ${VAR} substitution BEFORE bash sees the command.
# The old unit file had: agent="${inst%-worker}" which systemd tried to
# interpolate as ${inst%-worker}, producing "Invalid environment variable name".
#
# Solution: pass %i as a plain positional argument, do all shell logic here.

instance="$1"

if [[ "$instance" == *-executive ]]; then
    session="${instance}"
    agent="$instance"
elif [[ "$instance" == *-worker ]]; then
    session="${instance}"
    agent="${instance%-worker}"
else
    session="${instance}-worker"
    agent="$instance"
fi

exec /home/claude-agent/.bun/bin/bun \
    ${STANDUP_OS_ROOT}/shared/tools/channels/mattermost/mm-agent-daemon.ts \
    --agent "$agent" \
    --session "$session"
