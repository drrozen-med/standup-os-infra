#!/bin/bash
# Keep CCC (homebrew.mxcl.ccc) and the Navigator (com.standup-os.navigator) running.
# 1) A job that something switched off in launchd (disabled / unloaded) is switched back on at once and
#    reported in the Navigator's alert bar: kickstart alone cannot heal a disabled job (2026-11-08 outage, 22 h).
# 2) CCC is restarted when its port stops answering: 3 consecutive failed probes (~90s), at most once per 10 min.
# Run by launchd every 30 s (com.standup-os.ccc-watchdog). WATCHDOG_DRY=1 prints instead of acting (tests).
U="gui/$(id -u)"
AGENTS=${WATCHDOG_AGENTS_DIR:-$HOME/Library/LaunchAgents}
NAV=${WATCHDOG_NAV:-$HOME/projects/standup-os/ccc-navigator/nav.py}
URL=${CCC_URL:-http://127.0.0.1:8090/}
STATE=${WATCHDOG_STATE:-/tmp/ccc-watchdog.state}

run() { if [ -n "$WATCHDOG_DRY" ]; then echo "DRY: $*"; else "$@"; fi; }

for job in ${WATCHDOG_JOBS:-homebrew.mxcl.ccc com.standup-os.navigator}; do
    plist="$AGENTS/$job.plist"
    [ -f "$plist" ] || continue
    if launchctl print-disabled "$U" 2>/dev/null | grep -q "\"$job\" => disabled" || ! launchctl print "$U/$job" >/dev/null 2>&1; then
        echo "$(date) $job was switched off, switching it back on"
        run launchctl enable "$U/$job"
        run launchctl bootstrap "$U" "$plist" 2>/dev/null || { sleep 3; run launchctl bootstrap "$U" "$plist"; }
        # after a pause, so the Navigator itself is up to take the alert
        ( [ -z "$WATCHDOG_DRY" ] && sleep 60; NAV_ACTOR=watchdog run python3 "$NAV" alert raise --key "watchdog-$job" \
            --title "$job had been switched off; the watchdog switched it back on" \
            --body "Something disabled or unloaded it in macOS launchd. Log: /tmp/ccc-watchdog.log" ) &
    fi
done

fails=$(cat "$STATE.fails" 2>/dev/null || echo 0)
if curl -s -m 5 -o /dev/null "$URL"; then echo 0 > "$STATE.fails"; exit 0; fi
fails=$((fails + 1)); echo "$fails" > "$STATE.fails"
[ "$fails" -lt 3 ] && exit 0
last=$(cat "$STATE.last" 2>/dev/null || echo 0); now=$(date +%s)
[ $((now - last)) -lt 600 ] && exit 0
echo "$now" > "$STATE.last"; echo 0 > "$STATE.fails"
echo "$(date) CCC unresponsive, restarting"
run launchctl kickstart -k "$U/homebrew.mxcl.ccc"
