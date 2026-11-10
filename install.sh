#!/data/data/com.termux/files/usr/bin/bash
# Phone MCP - one-time installation in Termux.   Run:  bash ~/phone-mcp/install.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== 1/3 Updating Termux and installing packages (a few minutes) =="
yes | pkg upgrade -y
pkg install -y python android-tools openssh curl
pkg install -y python-pillow || echo "!! Pillow could not be installed - it is only needed when the fast helper cannot run on this phone."

echo "== 2/3 Configuration =="
if [ ! -f "$DIR/config.env" ] || ! grep -q '^PHONE_MCP_TOKEN=.\+' "$DIR/config.env"; then
  TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat > "$DIR/config.env" <<EOF
# Phone MCP configuration. The token is the key to your phone - never share it.
PHONE_MCP_TOKEN=$TOKEN
PHONE_MCP_PORT=8765
PHONE_MCP_SERIAL=localhost:5555
PHONE_MCP_SHOT_WIDTH=720
PHONE_MCP_LOG=$DIR/actions.log
# Permanent URL through the Cloudflare Worker in relay/ (set it with set-relay.sh):
PHONE_MCP_RELAY_URL=
EOF
  chmod 600 "$DIR/config.env"
  echo "Generated a new secret token."
else
  echo "config.env already exists - left unchanged."
fi
chmod +x "$DIR"/*.sh

echo "== 3/3 Check =="
python -c "import ast, sys; [ast.parse(open(f, encoding='utf-8').read()) for f in sys.argv[1:]]" "$DIR/phone_mcp.py" "$DIR/bridge.py"
command -v adb >/dev/null && command -v ssh >/dev/null
[ -f "$DIR/helper/phone-mcp-helper.jar" ] || echo "!! helper/phone-mcp-helper.jar is missing - everything works, but several times slower."
echo
echo "Done. Next: turn on Wireless debugging and run   bash $DIR/pair.sh"
