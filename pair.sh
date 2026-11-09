#!/data/data/com.termux/files/usr/bin/bash
# Phone MCP - connect ADB in Termux to this same phone through Wireless debugging.
# First time: pair + connect.   After a phone reboot: connect only (skip pairing with Enter).
DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$DIR/config.env" ] && . "$DIR/config.env"
SERIAL="${PHONE_MCP_SERIAL:-localhost:5555}"
FIXED_PORT="${SERIAL##*:}"

connected() { adb -s "$1" get-state 2>/dev/null | grep -q '^device'; }

# Best effort: read a Wireless debugging port from mDNS so it need not be typed. $1 = service name.
mdns_port() {
  local port=""
  for _ in 1 2 3; do
    port="$(adb mdns services 2>/dev/null | grep "$1" | grep -oE ':[0-9]+[[:space:]]*$' | tr -dc '0-9\n' | tail -n 1)"
    [ -n "$port" ] && break
    sleep 1
  done
  echo "$port"
}

adb start-server >/dev/null 2>&1

if connected "$SERIAL"; then
  echo "ADB is already connected ($SERIAL)."
else
  adb connect "$SERIAL" >/dev/null 2>&1; sleep 1
fi

if ! connected "$SERIAL"; then
  CP="$(mdns_port _adb-tls-connect)"
  if [ -n "$CP" ]; then
    echo "Found Wireless debugging on port $CP, connecting..."
    adb connect "localhost:$CP" >/dev/null 2>&1; sleep 1
  fi

  if [ -z "$CP" ] || ! connected "localhost:$CP"; then
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
    DETECTED="$(mdns_port _adb-tls-connect)"
    if [ -n "$DETECTED" ]; then
      read -r -p "Port from the main Wireless debugging screen (\"IP address & Port\") [detected $DETECTED, Enter to accept]: " CP
      CP="${CP:-$DETECTED}"
    else
      read -r -p "Port from the main Wireless debugging screen (\"IP address & Port\", the number after the colon): " CP
    fi
    adb connect "localhost:$CP"
    sleep 1
    connected "localhost:$CP" || { echo "!! Could not connect on port $CP. Check the number (it changes every time Wireless debugging is turned on)."; exit 1; }
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

echo "Telling Android not to kill Termux in the background..."
adb -s "$SERIAL" shell "device_config set_sync_disabled_for_tests persistent; \
  device_config put activity_manager max_phantom_processes 2147483647; \
  settings put global settings_enable_monitor_phantom_procs false; \
  dumpsys deviceidle whitelist +com.termux" >/dev/null 2>&1

# ADBKeyBoard = typing accented letters, non-Latin scripts and emoji (optional)
if adb -s "$SERIAL" shell pm list packages com.android.adbkeyboard | grep -q adbkeyboard; then
  echo "ADBKeyBoard is installed - Unicode typing works."
else
  APK="$(ls -t "$HOME"/storage/downloads/*.apk 2>/dev/null | grep -i keyboard | head -n 1)"
  if [ -n "$APK" ]; then
    echo "Installing ADBKeyBoard from $APK..."
    adb -s "$SERIAL" install -r "$APK" && adb -s "$SERIAL" shell ime enable com.android.adbkeyboard/.AdbIME >/dev/null 2>&1
  else
    echo "Tip: for Unicode typing download the ADBKeyBoard APK to Downloads and run pair.sh again (see README)."
  fi
fi

echo
echo "Done. Start the server:  bash $DIR/start.sh"
