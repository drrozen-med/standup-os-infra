#!/bin/bash
# Start a throwaway navigator for QA: http://127.0.0.1:8096
#  - CCC writes go to a read-only proxy (:8097) that fakes success and logs to qa/run/blocked-writes.log
#  - state is a copy of the real one (qa/run/state.json); the real ~/.ccc-navigator is never written
#  - Pi sends run /usr/bin/true instead of pi
#  - remote (phone) gate is on for Host "nav-sandbox.test", with its own pairing store in qa/run/remote
# Stop: qa/sandbox.sh stop
set -e
QA="$(cd "$(dirname "$0")" && pwd)"
RUN="$QA/run"
NAV="$QA/../ccc-navigator"
if [ "$1" = "stop" ]; then
  for f in "$RUN"/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done
  echo stopped; exit 0
fi
mkdir -p "$RUN/remote"
[ -f "$RUN/state.json" ] || cp ~/.ccc-navigator/state.json "$RUN/state.json"
python3 "$QA/ccc_readonly_proxy.py" 8097 "$RUN/blocked-writes.log" >"$RUN/proxy.log" 2>&1 &
echo $! >"$RUN/proxy.pid"
PI_BIN=/usr/bin/true CCC_NAV_PUBLIC_HOST=nav-sandbox.test CCC_NAV_REMOTE_DIR="$RUN/remote" \
CCC_NAV_PI_RUNS="$RUN/pi-runs" \
  python3 "$NAV/navigator.py" --no-browser --port 8096 --ccc http://localhost:8097 --state "$RUN/state.json" \
  >"$RUN/navigator.log" 2>&1 &
echo $! >"$RUN/navigator.pid"
echo "sandbox navigator: http://127.0.0.1:8096  (first load can take ~1-2 min on this Mac)"
