#!/data/data/com.termux/files/usr/bin/bash
# Phone MCP - start the server and its public URL, then print the connector URL.
# With PHONE_MCP_RELAY_URL set: permanent URL through the Cloudflare Worker (relay/).
# Without it: a temporary localhost.run tunnel whose URL changes on every start.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
[ -f config.env ] || { echo "!! config.env is missing - run first: bash $DIR/install.sh"; exit 1; }
set -a; . ./config.env; set +a
PORT="${PHONE_MCP_PORT:-8765}"
SERIAL="${PHONE_MCP_SERIAL:-localhost:5555}"

# Keep the phone from putting Termux to sleep.
termux-wake-lock 2>/dev/null || true

# Keep logs from growing without bound.
for f in server.log bridge.log actions.log; do
  if [ -f "$f" ] && [ "$(wc -c < "$f")" -gt 1000000 ]; then
    tail -n 2000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
done

# 1) ADB
adb start-server >/dev/null 2>&1
connected() { adb -s "$SERIAL" get-state 2>/dev/null | grep -q '^device'; }
connected || { adb connect "$SERIAL" >/dev/null 2>&1; sleep 1; }
if ! connected; then
  # Typically after a phone reboot. pair.sh --auto reconnects when it can do so without questions.
  echo "ADB is not connected, trying to reconnect..."
  bash "$DIR/pair.sh" --auto
fi
if ! connected; then
  echo "!! ADB is not connected. Turn on Wireless debugging (Settings > Developer options) and run: bash $DIR/pair.sh"
  exit 1
fi

# 2) Server
bash "$DIR/stop.sh" --quiet
nohup python "$DIR/phone_mcp.py" >> "$DIR/server.log" 2>&1 &
echo $! > "$DIR/server.pid"
STARTED=""
for _ in $(seq 1 20); do
  curl -fsS --max-time 3 "http://127.0.0.1:$PORT/$PHONE_MCP_TOKEN/health" >/dev/null 2>&1 && { STARTED=1; break; }
  sleep 0.5
done
[ -n "$STARTED" ] || { echo "!! The server did not start:"; tail -n 20 "$DIR/server.log"; exit 1; }

# 3) Public URL
if [ -n "$PHONE_MCP_RELAY_URL" ]; then
  URL="${PHONE_MCP_RELAY_URL%/}"
  nohup python "$DIR/bridge.py" >> "$DIR/bridge.log" 2>&1 &
  echo $! > "$DIR/tunnel.pid"
  TUNNEL_LOG="$DIR/bridge.log"
else
  # trycloudflare.com quick tunnels are not used: Claude's connector does not work with them.
  command -v ssh >/dev/null || { echo "!! ssh is missing - run: pkg install -y openssh"; exit 1; }
  nohup ssh -n -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -R "80:127.0.0.1:$PORT" nokey@localhost.run > "$DIR/tunnel.log" 2>&1 &
  echo $! > "$DIR/tunnel.pid"
  TUNNEL_LOG="$DIR/tunnel.log"
  URL=""
  for _ in $(seq 1 40); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.lhr\.life' "$DIR/tunnel.log" | tail -n 1)"
    [ -n "$URL" ] && break
    sleep 1
  done
  [ -n "$URL" ] || { echo "!! The tunnel did not come up:"; tail -n 20 "$DIR/tunnel.log"; exit 1; }
fi

# 4) Check from the outside (a new address can take a few seconds to resolve)
echo "Checking the public URL (up to a minute)..."
OK=""
for _ in $(seq 1 20); do
  curl -fsS --max-time 8 "$URL/$PHONE_MCP_TOKEN/health" >/dev/null 2>&1 && { OK=1; break; }
  sleep 2
done

FULL="$URL/$PHONE_MCP_TOKEN/mcp"
OLD="$(cat "$DIR/connector-url.txt" 2>/dev/null)"
echo "$FULL" > "$DIR/connector-url.txt"
chmod 600 "$DIR/connector-url.txt"
echo
if [ -n "$OK" ]; then
  echo "=== Running ==="
else
  echo "=== Started, but the public URL does not answer yet - try again in a minute (log: $TUNNEL_LOG) ==="
fi
echo "Connector URL (add it to Claude as a custom connector):"
echo
echo "$FULL"
echo
if [ -n "$OLD" ] && [ "$OLD" != "$FULL" ]; then
  echo "!! NOTE: the URL is DIFFERENT from last time. In Claude, remove the connector and add it again"
  echo "!! with this URL, otherwise Claude reports that it could not reach the server."
  echo
fi
[ -z "$PHONE_MCP_RELAY_URL" ] && echo "This URL is temporary. For a permanent one see \"Permanent URL\" in the README."
echo "Saved in $DIR/connector-url.txt   |   Stop with: bash $DIR/stop.sh"
