#!/bin/bash
# watchtower-run.sh — systemd wrapper for the read-only portfolio collector.
# Keeps all shell logic out of the unit file (see mm-agent-launch.sh for why).

set -euo pipefail

: "${STANDUP_OS_ROOT:?STANDUP_OS_ROOT must be set}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN must be set (read-only token, see watchtower/README.md)}"

export WATCHTOWER_CONFIG_DIR="${WATCHTOWER_CONFIG_DIR:-/usr/local/share/standup-os-watchtower}"
export WATCHTOWER_STATE_DIR="${WATCHTOWER_STATE_DIR:-$STANDUP_OS_ROOT/watchtower-state}"

exec /usr/bin/python3 -I /usr/local/libexec/watchtower-collect.py
