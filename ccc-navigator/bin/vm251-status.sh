#!/bin/bash
# vm251-status.sh - runs on the server as user claude-agent, from cron every 3 min
# Writes JSON to ~/agent-status.json atomically

OUT="$HOME/agent-status.json"
TMP="$OUT.tmp.$$"

# Initialize with error handling
{
  GENERATED_AT=$(date +%s)
  HOST="251"
  
  # Start building JSON
  echo -n "{\"generated_at\":$GENERATED_AT,\"host\":\"$HOST\",\"containers\":[" > "$TMP"
  
  FIRST_CONTAINER=1
  # Get containers with sudo -n incus list
  if CONTAINERS=$(sudo -n incus list -f csv -c ns 2>/dev/null); then
    # Process each line
    while IFS=',' read -r NAME STATE; do
      [ -z "$NAME" ] && continue
      # Only include running containers for agents? The requirement says "lists containers with `sudo -n incus list -f csv -c ns` and agent processes per RUNNING container"
      if [ "$STATE" = "RUNNING" ] || [ "$STATE" = "running" ]; then
        if [ $FIRST_CONTAINER -eq 0 ]; then
          echo -n "," >> "$TMP"
        fi
        FIRST_CONTAINER=0
        echo -n "{\"name\":\"$NAME\",\"state\":\"$STATE\",\"agents\":[" >> "$TMP"
        FIRST_AGENT=1
        # Get agent processes
        AGENTS=$(sudo -n incus exec "$NAME" -- pgrep -fa 'claude|codex|devin|opencode|droid|hermes' 2>/dev/null | grep -v "pgrep" || true)
        if [ -n "$AGENTS" ]; then
          while IFS= read -r LINE; do
            [ -z "$LINE" ] && continue
            # Extract PID (first field)
            PID=$(echo "$LINE" | awk '{print $1}')
            # Get cmd (rest of line, first 120 chars)
            CMD=$(echo "$LINE" | cut -c$((${#PID}+2))- | cut -c1-120 | sed 's/\\/\\\\/g; s/"/\\"/g')
            if [ $FIRST_AGENT -eq 0 ]; then
              echo -n "," >> "$TMP"
            fi
            FIRST_AGENT=0
            echo -n "{\"pid\":$PID,\"cmd\":\"$CMD\"}" >> "$TMP"
          done <<< "$AGENTS"
        fi
        echo -n "]}" >> "$TMP"
      fi
    done <<< "$CONTAINERS"
  fi
  
  # Add host-level processes under pseudo-container "host"
  if [ $FIRST_CONTAINER -eq 0 ]; then
    echo -n "," >> "$TMP"
  fi
  FIRST_CONTAINER=0
  echo -n "{\"name\":\"host\",\"state\":\"running\",\"agents\":[" >> "$TMP"
  FIRST_AGENT=1
  HOST_AGENTS=$(pgrep -fa 'paperclip|hermes' 2>/dev/null | grep -v "pgrep" || true)
  if [ -n "$HOST_AGENTS" ]; then
    while IFS= read -r LINE; do
      [ -z "$LINE" ] && continue
      PID=$(echo "$LINE" | awk '{print $1}')
      CMD=$(echo "$LINE" | cut -c$((${#PID}+2))- | cut -c1-120 | sed 's/\\/\\\\/g; s/"/\\"/g')
      if [ $FIRST_AGENT -eq 0 ]; then
        echo -n "," >> "$TMP"
      fi
      FIRST_AGENT=0
      echo -n "{\"pid\":$PID,\"cmd\":\"$CMD\"}" >> "$TMP"
    done <<< "$HOST_AGENTS"
  fi
  echo -n "]}" >> "$TMP"
  
  echo -n "]}" >> "$TMP"
  
} 2>&1 | {
  # If there was any error output, capture it
  ERR=$(cat)
  if [ -n "$ERR" ] && [ ! -s "$TMP" ]; then
    # If TMP wasn't written properly, write error
    GENERATED_AT=$(date +%s)
    echo "{\"generated_at\":$GENERATED_AT,\"host\":\"251\",\"containers\":[],\"error\":\"$(echo "$ERR" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' ')\"}" > "$TMP"
  elif [ -n "$ERR" ] && [ -s "$TMP" ] && ! grep -q '"error"' "$TMP"; then
    # Append error to existing JSON if possible - simpler to just rewrite
    GENERATED_AT=$(date +%s)
    # Try to add error
    echo "{\"generated_at\":$GENERATED_AT,\"host\":\"251\",\"containers\":[]}" > "$TMP"
  fi
}

# Atomic write
mv "$TMP" "$OUT" 2>/dev/null || cp "$TMP" "$OUT" 2>/dev/null
rm -f "$TMP" 2>/dev/null
