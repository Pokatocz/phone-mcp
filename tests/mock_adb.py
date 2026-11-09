#!/usr/bin/env python3
"""A fake `adb` for testing phone_mcp.py without a phone. Logs every command to $MOCK_LOG.

Environment switches:
  MOCK_OFFLINE=1     every device command fails with "device not found"
  MOCK_ADBKB=1       the ADBKeyBoard package is reported as installed
  MOCK_NO_SESSION=1  `adb shell -T` exits at once (forces one-shot commands)
  MOCK_RAW_BAD=1     raw screencap reports an unsupported pixel format (forces the PNG path)
"""
import os
import re
import struct
import sys
import zlib

LOG = os.environ.get("MOCK_LOG", "/tmp/mock_adb.log")
W, H = 1080, 2400

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
"""

PACKAGES = ["com.example.chat", "com.android.chrome", "com.android.settings", "com.android.mms",
            "com.vendor.maps", "com.vendor.mail", "com.miui.calculator", "com.google.android.calculator"]


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
        return UI.encode(), 0
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
    if args[:1] == ["-s"]:
        args = args[2:]
    cmd = args[0] if args else ""
    out = sys.stdout.buffer
    offline = os.environ.get("MOCK_OFFLINE")
    if cmd == "connect":
        log(args)
        out.write(b"connected to localhost:5555\n")
    elif cmd == "get-state":
        if offline:
            sys.stderr.write("error: device 'localhost:5555' not found\n")
            sys.exit(1)
        out.write(b"device\n")
    elif cmd == "exec-out":
        log(args)
        out.write(png_frame() if "-p" in args else raw_frame())
    elif cmd == "shell" and args[1:] == ["-T"]:
        session()
    elif cmd == "shell":
        if offline:
            sys.stderr.write("error: device 'localhost:5555' not found\n")
            sys.exit(1)
        data, rc = shell(args[1])
        out.write(data)
        if args[1].startswith("echo hello"):
            sys.stderr.write("warn\n")
        sys.exit(rc)


if __name__ == "__main__":
    main()
