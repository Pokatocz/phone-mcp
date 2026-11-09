#!/data/data/com.termux/files/usr/bin/bash
# Phone MCP - generate a new secret token. The old connector URL stops working.
DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="$DIR/config.env"
[ -f "$CFG" ] || { echo "!! config.env is missing - run install.sh first"; exit 1; }
NEW="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
sed -i '/^PHONE_MCP_TOKEN=/d' "$CFG"
echo "PHONE_MCP_TOKEN=$NEW" >> "$CFG"
chmod 600 "$CFG"
echo "A new token is saved. Run: bash $DIR/start.sh"
echo "Then remove the connector in Claude and add it again with the URL that start.sh prints."
