#!/usr/bin/env python3
"""A fake `adb` for testing phone_mcp.py without a phone. Logs every command to $MOCK_LOG.

It also plays the helper program (helper/src) when it is started through `adb shell -T`.

Environment switches:
  MOCK_OFFLINE=1      every device command fails with "device not found"
  MOCK_ADBKB=1        the ADBKeyBoard package is reported as installed
  MOCK_NO_SESSION=1   `adb shell -T` exits at once (forces one-shot commands)
  MOCK_RAW_BAD=1      raw screencap reports an unsupported pixel format (forces the PNG path)
  MOCK_NO_HELPER=1    the helper program exits at once (as on a phone where it cannot run)
  MOCK_DUMP_FAIL=1    the helper cannot read the screen (forces the uiautomator fallback)
  MOCK_NO_FOCUS=1     the helper finds no focused text field
  MOCK_SCROLL=1       the screen content changes with every swipe, twice, then stays the same
  MOCK_SLIDING=1      the first two screen reads catch the window while it slides in from the right
  MOCK_VOLUME_STUCK=1 the command that sets the volume is ignored (only the volume keys work)
  MOCK_REBOOTED=1     as after a phone reboot: nothing on the fixed port until `adb tcpip`,
                      Wireless debugging listens on port 41234 (announced over mDNS)
"""
import json
import os
import re
import struct
import sys
import zlib

LOG = os.environ.get("MOCK_LOG", "/tmp/mock_adb.log")
STATE = os.path.dirname(LOG)
W, H = 1080, 2400
JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABQODxIPDRQSEBIXFRQYHjIhHhwcHj0sLiQySUBMS0dARkVQWnNiUFVtVkVGZIhlbXd7gYKBTmCNl4x9lnN+gXz/2wBDARUXFx4aHjshITt8U0ZTfHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHz/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCjRRRXlnin/9k="   # a tiny valid JPEG

UI = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][1080,2400]">
 <node index="0" text="Chats" resource-id="com.example.chat:id/toolbar_title" class="android.widget.TextView" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[42,120][400,200]"/>
 <node index="1" text="" resource-id="com.example.chat:id/menu_search" class="android.widget.ImageButton" package="com.example.chat" content-desc="Search" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[900,110][1000,210]"/>
 <node index="2" text="" resource-id="android:id/list" class="androidx.recyclerview.widget.RecyclerView" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="true" focused="false" scrollable="true" long-clickable="false" password="false" selected="false" bounds="[0,300][1080,2200]">
  <node index="0" text="" resource-id="com.example.chat:id/row" class="android.widget.LinearLayout" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[0,300][1080,480]">
   <node index="0" text="Zoë" resource-id="com.example.chat:id/name" class="android.widget.TextView" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[200,320][700,380]"/>
   <node index="1" text="Sending the tomato photos 🍅" resource-id="com.example.chat:id/msg" class="android.widget.TextView" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[200,390][1000,450]"/>
  </node>
  <node index="1" text="" resource-id="com.example.chat:id/row" class="android.widget.LinearLayout" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[0,480][1080,660]">
   <node index="0" text="George - work" resource-id="com.example.chat:id/name" class="android.widget.TextView" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[200,500][700,560]"/>
  </node>
 </node>
 <node index="3" text="" resource-id="com.example.chat:id/entry" class="android.widget.EditText" package="com.example.chat" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="true" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[40,2220][900,2340]"/>
 <node index="4" text="" resource-id="com.example.chat:id/fab" class="android.widget.ImageButton" package="com.example.chat" content-desc="New chat" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][0,0]"/>
 <node index="5" text="" resource-id="" class="android.widget.Switch" package="com.example.chat" content-desc="Mute" checkable="true" checked="true" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[920,2220][1200,2340]"/>
</node>
</hierarchy>"""

NOTIFICATIONS = """Current Notification Manager state:
  Notification List:
    NotificationRecord(0x0abc: pkg=com.example.chat user=UserHandle{0} id=1 tag=null importance=4 key=0|com.example.chat|1|null|10123: Notification(channel=individual))
      extras={
        android.title=String (Zoë)
        android.text=String (Got a minute? The accountant called)
        android.subText=null
      }
    NotificationRecord(0x0abd: pkg=com.google.android.gm user=UserHandle{0} id=7 tag=null importance=3 key=x: Notification(channel=mail))
      extras={
        android.title=String (Order #4521)
        android.text=String (New order from the shop)
      }
    NotificationRecord(0x0abe: pkg=com.android.systemui user=UserHandle{0} id=9 tag=null importance=1 key=y: Notification(channel=x))
      extras={
        android.title=null
      }
  Archive (3 notifications)
    NotificationRecord(0x0aaa: pkg=com.old.app user=UserHandle{0} id=1)
        android.title=String (Old one)
"""

STATUS = """  mWakefulness=Awake
  mCurrentFocus=Window{bd2acf3 u0 com.example.chat/com.example.chat.Main}
    mShowingDream=false mDreamingLockscreen=false
    isKeyguardShowing=false
  AC powered: false
  USB powered: false
  status: 3
  level: 82
  temperature: 333
Physical size: 1080x2400
device=Example Phone 1, Android 16
wifi=1 bt=0 airplane=0 zen=2
"""

PACKAGES = ["com.example.chat", "com.android.chrome", "com.android.settings", "com.android.mms",
            "com.vendor.maps", "com.vendor.mail", "com.miui.calculator", "com.google.android.calculator",
            "com.termux"]
KNOWN_KEYS = {"KEYCODE_BACK", "KEYCODE_HOME", "KEYCODE_ENTER", "KEYCODE_DEL", "KEYCODE_APP_SWITCH",
              "KEYCODE_VOLUME_UP", "KEYCODE_VOLUME_DOWN"}
SETTINGS = {"wifi_on": "1", "bluetooth_on": "0", "mobile_data": "1", "airplane_mode_on": "0", "zen_mode": "0",
            "ui_night_mode": "1", "accelerometer_rotation": "0", "volume": "4", "brightness": "0.06"}
SWITCH_COMMANDS = {     # command prefix -> (setting, value for the last word of the command)
    "svc wifi": ("wifi_on", {"enable": "1", "disable": "0"}),
    "svc data": ("mobile_data", {"enable": "1", "disable": "0"}),
    "cmd connectivity airplane-mode": ("airplane_mode_on", {"enable": "1", "disable": "0"}),
    "cmd notification set_dnd": ("zen_mode", {"on": "2", "off": "0"}),
    "cmd uimode night": ("ui_night_mode", {"yes": "2", "no": "1"}),
    "settings put system accelerometer_rotation": ("accelerometer_rotation", {"1": "1", "0": "0"}),
}


def setting(name, value=None):
    return state("set_" + name, value) or SETTINGS[name]


def volume_keys(codes):
    for code in codes:
        if code in ("KEYCODE_VOLUME_UP", "KEYCODE_VOLUME_DOWN"):
            setting("volume", str(int(setting("volume")) + (1 if code.endswith("UP") else -1)))


def state(name, value=None):
    """Tiny persistent state shared by the separate mock processes of one test server."""
    path = os.path.join(STATE, "mock_" + name)
    if value is None:
        return open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    return value


def scrolled():
    if os.environ.get("MOCK_SCROLL"):
        state("scrolls", str(int(state("scrolls") or 0) + 1))


def screen_xml(from_helper=False):
    xml = UI
    if os.environ.get("MOCK_SCROLL"):
        n = int(state("scrolls") or 0)
        if n:
            xml = xml.replace("George - work", "Page two" if n == 1 else "Privacy policy")
    if os.environ.get("MOCK_SLIDING"):
        n = int(state("reads") or 0)
        state("reads", str(n + 1))
        if n < 2:       # every position is one screen width too far to the right
            xml = re.sub(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                         lambda m: f"[{int(m[1]) + W},{m[2]}][{int(m[3]) + W},{m[4]}]", xml)
    if from_helper:     # the helper also reports editable fields and placeholder texts
        xml = xml.replace('text="" resource-id="com.example.chat:id/entry"',
                          'text="Message" editable="true" hint="true" resource-id="com.example.chat:id/entry"')
    return xml


def log(args):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(repr(args) + "\n")


def raw_frame():
    fmt = 22 if os.environ.get("MOCK_RAW_BAD") else 1
    row = bytearray(b"\xff\xff\xff\xff" * W)
    green = bytearray(b"\x00\xb0\x00\xff" * W)
    body = b"".join(bytes(green if 300 <= y < 480 else row) for y in range(H))
    return struct.pack("<IIII", W, H, fmt, 0) + body


def png_frame():
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * W for _ in range(H))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def shell(c):
    """Returns (output_bytes, exit_code) for a device shell command."""
    log(["shell", c])
    if "uiautomator dump" in c:
        return screen_xml().encode(), 0
    if c.startswith("ls /data/local/tmp/phone-mcp-helper-"):
        name = c.split()[1]
        return ((name + "\n").encode(), 0) if state("pushed") == name else (b"", 1)
    if c.startswith("rm -f /data/local/tmp/phone-mcp-helper-"):
        state("pushed", "-")
        return b"", 0
    if c.startswith("tail -n 5 /data/local/tmp/phone-mcp-helper.err"):
        return b"java.lang.ClassNotFoundException: PhoneMcpHelper\n", 0
    if c.startswith("input swipe"):
        scrolled()
        return b"", 0
    if c.startswith("cmd media_session volume --stream 3 --get"):
        return f"[V] volume is {setting('volume')} in range [0..30]\n".encode(), 0
    if c.startswith("cmd media_session volume --stream 3 --set"):
        if not os.environ.get("MOCK_VOLUME_STUCK"):
            setting("volume", c.split()[6])
        return b"", 0
    if c.startswith("input keyevent"):
        volume_keys(c.split()[2:])
        return b"", 0
    if "cmd display set-brightness" in c:
        setting("brightness", c.split()[-1])
        return b"", 0
    if "Display Brightness=" in c:
        return f"Display Brightness={setting('brightness')}\n".encode(), 0
    for prefix, (name, values) in SWITCH_COMMANDS.items():
        if c.startswith(prefix):
            setting(name, values[c.split()[-2] if c.endswith("2>&1") else c.split()[-1]])
            return b"", 0
    m = re.match(r"settings get (?:global|secure|system) (\w+)$", c)
    if m and m.group(1) in SETTINGS:
        return (setting(m.group(1)) + "\n").encode(), 0
    if c.startswith("svc bluetooth"):
        return b"java.lang.SecurityException: Permission denied\n", 255
    if "dumpsys window displays" in c:
        return f"cur={W}x{H}\nPhysical size: {W}x{H}\n".encode(), 0
    if "dumpsys notification" in c:
        return NOTIFICATIONS.encode(), 0
    if "mWakefulness" in c:
        return STATUS.encode(), 0
    if c.startswith("pm list packages com.android.adbkeyboard"):
        return (b"package:com.android.adbkeyboard\n" if os.environ.get("MOCK_ADBKB") else b""), 0
    if c.startswith("pm list packages"):
        return "".join(f"package:{p}\n" for p in PACKAGES).encode(), 0
    if "default_input_method" in c:
        return b"com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME\n", 0
    if c.startswith("am broadcast"):
        return b"Broadcasting: Intent { act=ADB_INPUT_B64 }\nBroadcast completed: result=0\n", 0
    if "resolve-activity" in c:
        m = re.search(r"LAUNCHER (\S+) \|", c)
        pkg = m.group(1).strip("'") if m else ""
        if pkg == "com.vendor.mail":
            return b"NO_LAUNCHER\n", 0
        return f"Starting: Intent {{ cmp={pkg}/.Main }}\n".encode(), 0
    if c.startswith("monkey"):
        return b"Events injected: 1\n", 0
    if c.startswith("am start"):
        return b"Starting: Intent { act=android.intent.action.VIEW }\n", 0
    if c.startswith("echo hello"):
        return b"hello\n", 3
    if c == "echo ready":
        return b"ready\n", 0
    if c.startswith("sleep-forever"):
        import time
        time.sleep(60)
    return b"", 0


def helper():
    """The helper program: one `command {json}` per line on stdin, one `@@{json}` reply per line."""
    if os.environ.get("MOCK_NO_HELPER") or os.environ.get("MOCK_OFFLINE"):
        sys.exit(1)
    stdin, out = sys.stdin.buffer, sys.stdout.buffer
    out.write(b"some library chatter that is not a reply\n")
    for line in iter(stdin.readline, b""):
        cmd, _, rest = line.decode("ascii").strip().partition(" ")
        a = json.loads(rest) if rest else {}
        if cmd == "quit":
            break
        if cmd != "ping":
            log(["helper", cmd, a])
        r = {"ok": True}
        if cmd == "ping":
            r.update(v=1, sdk=36)
        elif cmd == "dump":
            if os.environ.get("MOCK_DUMP_FAIL"):
                r = {"ok": False, "err": "no active window"}
            else:
                r.update(xml=screen_xml(True), keyboard=True, nodes=12, w=W, h=H, ms=5)
        elif cmd == "shot":
            left, top, right, bottom = a.get("region") or [0, 0, W, H]
            cw, ch = min(W, right) - left, min(H, bottom) - top
            scale = min(1.0, a.get("max_w", 720) / cw)
            r.update(jpeg=JPEG, w=W, h=H, left=left, top=top, ow=round(cw * scale), oh=round(ch * scale),
                     scale=scale, blank=False, ms=5)
        elif cmd == "swipe":
            scrolled()
        elif cmd == "key":
            bad = [k for k in a["codes"] if k not in KNOWN_KEYS and not k.isdigit()]
            if bad:
                r = {"ok": False, "err": "unknown key " + bad[0]}
            else:
                volume_keys(a["codes"])
        elif cmd == "settext":
            if os.environ.get("MOCK_NO_FOCUS"):
                r = {"ok": False, "err": "no focused field"}
        elif cmd == "clip_set":
            state("clip", a["s"])
        elif cmd == "clip_get":
            r["text"] = state("clip")
        elif cmd not in ("tap", "chars", "idle"):
            r = {"ok": False, "err": "unknown command " + cmd}
        out.write(b"@@" + json.dumps(r).encode("ascii") + b"\n")
        out.flush()


def session():
    """`adb shell -T`: commands arrive on stdin wrapped by ShellSession._exchange."""
    if os.environ.get("MOCK_NO_SESSION") or os.environ.get("MOCK_OFFLINE"):
        sys.exit(1)
    pending = []
    stdin, out = sys.stdin.buffer, sys.stdout.buffer
    for line in iter(stdin.readline, b""):
        text = line.decode("utf-8")
        m = re.match(r"\} </dev/null 2>&1; echo; echo (__PMCP_\w+__)\$\?", text)
        if not m:
            pending.append(text)
            continue
        cmd = "".join(pending)[2:].rstrip("\n")
        pending = []
        data, rc = shell(cmd)
        out.write(data + b"\n" + m.group(1).encode() + str(rc).encode() + b"\n")
        out.flush()


def main():
    args = sys.argv[1:]
    serial = ""
    if args[:1] == ["-s"]:
        serial, args = args[1], args[2:]
    cmd = args[0] if args else ""
    out = sys.stdout.buffer
    offline = os.environ.get("MOCK_OFFLINE")
    if os.environ.get("MOCK_REBOOTED"):
        port = (serial or (args[1] if len(args) > 1 else "")).rsplit(":", 1)[-1]
        reachable = port == "41234" or (port == "5555" and state("tcpip") == "5555")
        if cmd == "mdns":
            out.write(b"List of discovered mdns services\nadb-XYZ123-AbCdEf\t_adb-tls-connect._tcp\t192.168.1.23:41234\n")
            return
        if cmd == "tcpip":
            log(args)
            state("tcpip", args[1])
            out.write(b"restarting in TCP mode port: " + args[1].encode() + b"\n")
            return
        if cmd in ("connect", "get-state") and not reachable:
            sys.stderr.write(f"failed to connect to '{port}': Connection refused\n")
            sys.exit(1)
        offline = offline or (serial and not reachable)
    if cmd == "connect":
        log(args)
        out.write(b"connected to " + args[1].encode() + b"\n")
    elif cmd == "get-state":
        if offline:
            sys.stderr.write("error: device 'localhost:5555' not found\n")
            sys.exit(1)
        out.write(b"device\n")
    elif cmd == "exec-out":
        log(args)
        out.write(png_frame() if "-p" in args else raw_frame())
    elif cmd == "push":
        log(args)
        if offline:
            sys.stderr.write("error: device 'localhost:5555' not found\n")
            sys.exit(1)
        state("pushed", args[2])
        out.write(b"1 file pushed\n")
    elif cmd == "shell" and args[1:2] == ["-T"] and len(args) == 3 and "PhoneMcpHelper" in args[2]:
        helper()
    elif cmd == "shell" and args[1:] == ["-T"]:
        session()
    elif cmd == "shell":
        if offline:
            sys.stderr.write("error: device 'localhost:5555' not found\n")
            sys.exit(1)
        data, rc = shell(" ".join(args[1:]))
        out.write(data)
        if args[1].startswith("echo hello"):
            sys.stderr.write("warn\n")
        sys.exit(rc)


if __name__ == "__main__":
    main()
