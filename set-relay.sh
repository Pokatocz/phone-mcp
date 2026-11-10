#!/data/data/com.termux/files/usr/bin/bash
# Phone MCP - use a permanent URL through the Cloudflare Worker in relay/.
# Usage:  bash ~/phone-mcp/set-relay.sh https://phone-mcp-relay.NAME.workers.dev
#         bash ~/phone-mcp/set-relay.sh off      (back to the temporary localhost.run tunnel)
DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="$DIR/config.env"
[ -f "$CFG" ] || { echo "!! config.env is missing - run install.sh first"; exit 1; }
URL="${1%/}"
if [ -z "$URL" ]; then
  echo "Usage: bash $0 https://phone-mcp-relay.NAME.workers.dev"; exit 1
fi
if [ "$URL" = "off" ]; then
  URL=""
elif ! echo "$URL" | grep -qE '^https://[A-Za-z0-9.-]+$'; then
  echo "!! The URL must look like https://phone-mcp-relay.NAME.workers.dev (nothing after the domain)"; exit 1
fi
sed -i '/^PHONE_MCP_RELAY_URL=/d' "$CFG"
echo "PHONE_MCP_RELAY_URL=$URL" >> "$CFG"
if [ -n "$URL" ]; then
  if curl -fsS --max-time 10 "$URL/" 2>/dev/null | grep -qi "relay"; then
    echo "The relay answers: $URL"
  else
    echo "!! No relay answers at $URL yet - check the URL printed by \"wrangler deploy\"."
  fi
  echo "Saved. Now run: bash $DIR/start.sh"
else
  echo "Relay turned off - start.sh will use a temporary localhost.run tunnel."
fi
