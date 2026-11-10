#!/data/data/com.termux/files/usr/bin/bash
# Phone MCP - restart only the server (e.g. after an update). The public URL stays the same.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
[ -f config.env ] || { echo "!! config.env is missing - run install.sh first"; exit 1; }
set -a; . ./config.env; set +a
PORT="${PHONE_MCP_PORT:-8765}"

if [ -f server.pid ]; then
  kill "$(cat server.pid)" 2>/dev/null
  sleep 1
fi
nohup python "$DIR/phone_mcp.py" >> "$DIR/server.log" 2>&1 &
echo $! > "$DIR/server.pid"

# With the relay, the bridge is restarted too, so that an update reaches it (the URL stays the same).
# A localhost.run tunnel is left alone: restarting it would change the URL.
if [ -n "$PHONE_MCP_RELAY_URL" ]; then
  [ -f tunnel.pid ] && kill "$(cat tunnel.pid)" 2>/dev/null
  nohup python "$DIR/bridge.py" >> "$DIR/bridge.log" 2>&1 &
  echo $! > "$DIR/tunnel.pid"
fi

for _ in $(seq 1 12); do
  if OUT="$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/$PHONE_MCP_TOKEN/health" 2>/dev/null)"; then
    echo "Server restarted: $OUT"
    if [ -f tunnel.pid ] && kill -0 "$(cat tunnel.pid)" 2>/dev/null; then
      echo "The public URL is unchanged. If Claude shows a connection issue, press Reconnect on the connector."
    else
      echo "!! The tunnel/bridge is not running - start everything with: bash $DIR/start.sh"
    fi
    exit 0
  fi
  sleep 0.5
done
echo "!! The server did not start:"; tail -n 20 "$DIR/server.log"; exit 1
