#!/bin/bash
# Restart CCC (launchd job homebrew.mxcl.ccc) when its port stops answering.
# Needs 3 consecutive failed probes (~90s) and at most one restart per 10 minutes.
URL=${CCC_URL:-http://127.0.0.1:8090/}
STATE=/tmp/ccc-watchdog.state
fails=$(cat "$STATE.fails" 2>/dev/null || echo 0)
if curl -s -m 5 -o /dev/null "$URL"; then echo 0 > "$STATE.fails"; exit 0; fi
fails=$((fails + 1)); echo "$fails" > "$STATE.fails"
[ "$fails" -lt 3 ] && exit 0
last=$(cat "$STATE.last" 2>/dev/null || echo 0); now=$(date +%s)
[ $((now - last)) -lt 600 ] && exit 0
echo "$now" > "$STATE.last"; echo 0 > "$STATE.fails"
echo "$(date) CCC unresponsive, restarting"
launchctl kickstart -k "gui/$(id -u)/homebrew.mxcl.ccc"
