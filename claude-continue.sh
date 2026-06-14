#!/bin/bash
# claude-continue.sh
# Sends "continue" to all Claude Code sessions at a given time.
# Usage: ./claude-continue.sh 14:00
#        ./claude-continue.sh 14:00 --all   (sends to all sessions)

TARGET_TIME="$1"
MODE="$2"

if [ -z "$TARGET_TIME" ]; then
  echo "Usage: $0 HH:MM [--all]"
  exit 1
fi

TODAY=$(date +%Y-%m-%d)
TARGET=$(date -j -f "%Y-%m-%d %H:%M" "$TODAY $TARGET_TIME" +%s 2>/dev/null)

if [ -z "$TARGET" ]; then
  echo "Invalid time. Use the format HH:MM, e.g. 14:00"
  exit 1
fi

NOW=$(date +%s)
# If the time has already passed today, schedule it for tomorrow
if [ "$TARGET" -le "$NOW" ]; then
  TARGET=$((TARGET + 86400))
fi

WAIT=$((TARGET - NOW))
echo "Sending 'continue' at $TARGET_TIME (waiting $((WAIT / 60)) min)..."
sleep "$WAIT"

if [ "$MODE" = "--all" ]; then
  FILTER="true"
else
  FILTER='(sessionName contains "claude" or sessionName contains "✳")'
fi

osascript <<EOF
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        set sessionName to name of s
        if $FILTER then
          tell s to write text "continue"
        end if
      end repeat
    end repeat
  end repeat
end tell
EOF

echo "Done."
