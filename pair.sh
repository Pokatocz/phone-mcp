#!/data/data/com.termux/files/usr/bin/bash
# Phone MCP - connect ADB in Termux to this same phone through Wireless debugging.
#   bash pair.sh          first time: pair + connect (asks for the pairing code)
#   bash pair.sh --auto   no questions: reconnect if that is possible without the user (start.sh uses it)
DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$DIR/config.env" ] && . "$DIR/config.env"
SERIAL="${PHONE_MCP_SERIAL:-localhost:5555}"
FIXED_PORT="${SERIAL##*:}"
AUTO=""; [ "$1" = "--auto" ] && AUTO=1
CP=""

connected() { adb -s "$1" get-state 2>/dev/null | grep -q '^device'; }

# Ports where Wireless debugging may be listening: the one announced over mDNS (not available on
# every phone), then every open local port in the range Android picks from.
candidates() {
  timeout 5 adb mdns services 2>/dev/null | grep _adb-tls-connect | grep -oE ':[0-9]+[[:space:]]*$' | tr -dc '0-9\n' | tail -n 1
  python - 2>/dev/null <<'PY'
import socket
found = []
for port in range(30000, 61000):
    s = socket.socket()
    s.settimeout(0.05)
    if s.connect_ex(("127.0.0.1", port)) == 0:
        found.append(str(port))
    s.close()
print("\n".join(found[:5]))
PY
}

# Try the candidate ports; sets CP to the one that accepts this (already paired) ADB client.
try_connect() {
  local p
  for p in $(candidates | awk 'NF && !seen[$0]++'); do
    timeout 5 adb connect "localhost:$p" >/dev/null 2>&1
    sleep 1
    if connected "localhost:$p"; then CP="$p"; return 0; fi
    adb disconnect "localhost:$p" >/dev/null 2>&1
  done
  return 1
}

adb start-server >/dev/null 2>&1

if connected "$SERIAL"; then
  [ -n "$AUTO" ] || echo "ADB is already connected ($SERIAL)."
else
  adb connect "$SERIAL" >/dev/null 2>&1; sleep 1
fi

if ! connected "$SERIAL"; then
  # A reboot turns Wireless debugging off. Termux may switch it back on by itself if an earlier run
  # of this script gave it the permission (see the end of this file). Harmless when it does not work.
  env -u LD_PRELOAD -u LD_LIBRARY_PATH /system/bin/cmd settings put global adb_wifi_enabled 1 >/dev/null 2>&1 && sleep 3

  echo "Looking for Wireless debugging..."
  if try_connect; then
    echo "Found Wireless debugging on port $CP."
  elif [ -n "$AUTO" ]; then
    exit 1
  else
    echo "Open: Settings > Developer options > Wireless debugging (turn it on; the phone must be on Wi-Fi)."
    echo "Tip: put Termux and Settings side by side (split screen), otherwise the pairing dialog closes."
    echo
    echo "FIRST-TIME PAIRING: in Wireless debugging tap \"Pair device with pairing code\"."
    read -r -p "Port from the pairing dialog (the number after the colon) - or just Enter if already paired: " PP
    if [ -n "$PP" ]; then
      read -r -p "Six-digit pairing code: " PC
      adb pair "localhost:$PP" "$PC" || { echo "!! Pairing failed - try again."; exit 1; }
    fi
    echo
    if try_connect; then
      echo "Found Wireless debugging on port $CP."
    else
      read -r -p "Port from the main Wireless debugging screen (\"IP address & Port\", the number after the colon): " CP
      adb connect "localhost:$CP"
      sleep 1
      connected "localhost:$CP" || { echo "!! Could not connect on port $CP. Check the number (it changes every time Wireless debugging is turned on)."; exit 1; }
    fi
  fi

  echo "Switching ADB to the fixed port $FIXED_PORT (lasts until the phone reboots)..."
  adb -s "localhost:$CP" tcpip "$FIXED_PORT" >/dev/null 2>&1
  sleep 3
  adb disconnect "localhost:$CP" >/dev/null 2>&1
  for _ in 1 2 3 4 5 6; do
    adb connect "$SERIAL" >/dev/null 2>&1
    connected "$SERIAL" && break
    sleep 1
  done
  connected "$SERIAL" || { echo "!! Could not connect to $SERIAL. If the phone shows \"Allow debugging?\", choose \"Always allow\" and run pair.sh again."; exit 1; }
  echo "ADB connected: $SERIAL"
fi

# Keep Android from killing Termux in the background, and let Termux turn Wireless debugging
# back on after a reboot (used above; experimental, not every phone honours it).
adb -s "$SERIAL" shell "device_config set_sync_disabled_for_tests persistent; \
  device_config put activity_manager max_phantom_processes 2147483647; \
  settings put global settings_enable_monitor_phantom_procs false; \
  dumpsys deviceidle whitelist +com.termux; \
  pm grant com.termux android.permission.WRITE_SECURE_SETTINGS" >/dev/null 2>&1

[ -n "$AUTO" ] && exit 0

# ADBKeyBoard: an optional fallback for typing non-English text into fields that refuse the normal way.
if adb -s "$SERIAL" shell pm list packages com.android.adbkeyboard | grep -q adbkeyboard; then
  echo "ADBKeyBoard is installed (fallback for Unicode typing)."
else
  APK="$(ls -t "$HOME"/storage/downloads/*.apk 2>/dev/null | grep -i keyboard | head -n 1)"
  if [ -n "$APK" ]; then
    echo "Installing ADBKeyBoard from $APK..."
    adb -s "$SERIAL" install -r "$APK" && adb -s "$SERIAL" shell ime enable com.android.adbkeyboard/.AdbIME >/dev/null 2>&1
  fi
fi

echo
echo "Done. Start the server:  bash $DIR/start.sh"
