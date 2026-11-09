#!/data/data/com.termux/files/usr/bin/bash
# Phone MCP - stop the server and its public URL. Claude can no longer reach the phone.
DIR="$(cd "$(dirname "$0")" && pwd)"
for f in server tunnel; do
  if [ -f "$DIR/$f.pid" ]; then
    kill "$(cat "$DIR/$f.pid")" 2>/dev/null
    rm -f "$DIR/$f.pid"
  fi
done
# Leftovers started by hand or by an older version
pkill -f "$DIR/phone_mcp.py" 2>/dev/null
pkill -f "$DIR/bridge.py" 2>/dev/null
pkill -f "nokey@localhost.run" 2>/dev/null
if [ "$1" != "--quiet" ]; then
  termux-wake-unlock 2>/dev/null
  echo "Stopped."
fi
true
