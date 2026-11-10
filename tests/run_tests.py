#!/usr/bin/env python3
"""End-to-end tests for phone_mcp.py against a fake adb. Standard library only.

    python3 tests/run_tests.py
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOKEN = "test-token-0123456789abcdefghijklmnopqrstuvwxyz"
failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        failures.append(name)
        print(f"FAIL  {name}  {str(detail)[:300]}")


class Server:
    """phone_mcp.py running against the mock adb."""

    def __init__(self, port, **env):
        self.port = port
        self.tmp = tempfile.mkdtemp(prefix="phone-mcp-test-")
        self.adb_log = os.path.join(self.tmp, "adb.log")
        self.server_log = os.path.join(self.tmp, "server.log")
        full_env = dict(os.environ, PHONE_MCP_TOKEN=TOKEN, PHONE_MCP_PORT=str(port),
                        PHONE_MCP_ADB=os.path.join(HERE, "mock_adb.py"), MOCK_LOG=self.adb_log,
                        PHONE_MCP_LOG=os.path.join(self.tmp, "actions.log"), **env)
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "phone_mcp.py")], env=full_env,
                                     stdout=open(self.server_log, "w"), stderr=subprocess.STDOUT)
        self.base = f"http://127.0.0.1:{port}/{TOKEN}"
        for _ in range(50):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=2).read()
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not start: " + open(self.server_log).read())

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)

    def post(self, payload, url=None, accept="application/json, text/event-stream"):
        req = urllib.request.Request(url or self.base + "/mcp", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode()
                ctype = r.headers.get("Content-Type")
                if body.startswith("event:"):
                    body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
                return r.status, (json.loads(body) if body else None), ctype
        except urllib.error.HTTPError as e:
            return e.code, None, None

    def rpc(self, method, params=None):
        status, body, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        assert status == 200, (method, status)
        return body

    def call(self, name, **args):
        """Returns (text, is_error, content)."""
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = "\n".join(c["text"] for c in r["content"] if c["type"] == "text")
        return text, r["isError"], r["content"]

    def adb_commands(self):
        return open(self.adb_log, encoding="utf-8").read() if os.path.exists(self.adb_log) else ""


def test_protocol(s):
    init = s.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "1"}})["result"]
    check("initialize echoes version", init["protocolVersion"] == "2025-06-18", init)
    check("initialize has instructions", "ui_tree" in init["instructions"])
    check("notification -> 202", s.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202)
    tools = s.rpc("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    check("23 tools", len(names) == 23 and len(set(names)) == 23 and "batch" in names, names)
    check("action tools offer observe", all("observe" in t["inputSchema"]["properties"]
                                            for t in tools if t["name"] in ("tap", "type_text", "open_app")))
    check("read tools have no observe", all("observe" not in t["inputSchema"]["properties"]
                                            for t in tools if t["name"] in ("ui_tree", "screenshot", "batch")))
    check("ping", s.rpc("ping")["result"] == {})
    check("unknown method", s.rpc("nope/nope")["error"]["code"] == -32601)
    check("unknown tool", s.rpc("tools/call", {"name": "nope"})["error"]["code"] == -32602)
    check("wrong token -> 404", s.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                       url=f"http://127.0.0.1:{s.port}/{TOKEN}x/mcp")[0] == 404)
    status, body, _ = s.post([{"jsonrpc": "2.0", "id": 9, "method": "ping"},
                              {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    check("batch request", status == 200 and isinstance(body, list) and body[0]["id"] == 9, body)
    check("SSE by default", s.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})[2] == "text/event-stream")
    check("JSON when asked", s.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                    accept="application/json")[2] == "application/json")
    for method, path, want in (("GET", "/mcp", 405), ("GET", "/health", 200), ("DELETE", "/mcp", 405),
                               ("OPTIONS", "/mcp", 204), ("HEAD", "/health", 200), ("GET", "/x", 404)):
        conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
        conn.request(method, f"/{TOKEN}{path}")
        check(f"{method} {path} -> {want}", conn.getresponse().status == want)
        conn.close()


def test_connection_reuse(s):
    """A rejected request with a body must not poison the next request on the same connection."""
    conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    conn.request("POST", f"/{TOKEN}", body=b'{"x":1}')            # wrong path, body unread
    r = conn.getresponse()
    r.read()
    check("bad path -> 404", r.status == 404)
    conn.close()
    conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    body = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}).encode()
    conn.putrequest("POST", f"/{TOKEN}/mcp")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.putheader("Accept", "application/json")
    conn.endheaders()
    conn.send(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))
    r = conn.getresponse()
    check("chunked request body", r.status == 200 and json.loads(r.read())["id"] == 5)
    conn.request("POST", f"/{TOKEN}/mcp", body=body, headers={"Accept": "application/json"})
    r = conn.getresponse()
    check("keep-alive second request", r.status == 200 and json.loads(r.read())["id"] == 5)
    conn.close()


def test_tools(s, helper):
    """The same behaviour is expected with the helper program and with plain shell commands."""
    m = "helper" if helper else "shell"
    text, err, _ = s.call("phone_status")
    check(f"{m}: phone_status parsed", all(x in text for x in (
        "Screen: on", "Locked: no", "com.example.chat/", "Battery: 82% (discharging), 33.3", "1080x2400",
        "Example Phone 1, Android 16", "Wi-Fi on, Bluetooth off, airplane mode off, do not disturb on",
        "fast helper " + ("on" if helper else "off"),
        "Unicode typing: " + ("accessibility" if helper else "not available"))), text)
    text, err, _ = s.call("ui_tree")
    check(f"{m}: ui_tree header", text.startswith("App: com.example.chat | screen 1080x2400 | 7 elements"), text)
    check(f"{m}: keyboard state", ("keyboard shown" in text) == helper, text)
    check(f"{m}: ui_tree merges row label", '#3 LinearLayout "Zoë | Sending the tomato photos 🍅"' in text, text)
    check(f"{m}: ui_tree hides zero-size element", "New chat" not in text)
    check(f"{m}: ui_tree clamps off-screen tap point", "@(1000,2280)" in text, text)
    field = '#5 EditText hint="Message" id=entry @(470,2280) [click,long,edit,focused]' if helper \
        else "#5 EditText id=entry @(470,2280) [click,long,edit,focused]"
    check(f"{m}: text field and placeholder", field in text, text)
    text, _, _ = s.call("ui_tree", filter="george")
    check(f"{m}: ui_tree filter", "| 1 elements" in text and "George - work" in text, text)
    check(f"{m}: filter ignores accents", "| 1 elements" in s.call("ui_tree", filter="ZOE")[0])
    text, _, _ = s.call("read_text")
    lines = text.splitlines()
    check(f"{m}: read_text", lines[:5] == ["App: com.example.chat", "Chats", "[Search]", "Zoë",
                                           "Sending the tomato photos 🍅"] and "[Mute]" in lines
          and ("(Message)" in lines) == helper and "New chat" not in text, text)

    text, err, _ = s.call("tap_element", index=3)
    check(f"{m}: tap_element", not err and "(540,390)" in text, text)
    check(f"{m}: tap_element out of range", s.call("tap_element", index=99)[1])
    text, err, _ = s.call("tap_text", text="george")
    check(f"{m}: tap_text", not err and "(540,570)" in text, text)
    check(f"{m}: tap_text without accents", "(540,390)" in s.call("tap_text", text="zoe")[0])
    check(f"{m}: tap_text prefers exact + clickable", "#1 ImageButton" in s.call("tap_text", text="Search")[0])
    check(f"{m}: tap_text miss is an error", s.call("tap_text", text="nothing here")[1])
    if helper:
        check("helper: tap_text finds a placeholder", "#5 EditText" in s.call("tap_text", text="message")[0])

    text, err, content = s.call("screenshot")
    image = content[0]
    check(f"{m}: screenshot image", image["type"] == "image" and image["mimeType"] == "image/jpeg"
          and len(image["data"]) > 100, text)
    check(f"{m}: screenshot scale note", "Screen 1080x2400 px, image 720x1600 px" in text, text)
    text, err, _ = s.call("tap", x=360, y=130, space="screenshot")
    check(f"{m}: tap in screenshot space", "(540,195)" in text, text)
    text, err, _ = s.call("screenshot", region=[100, 300, 640, 480])
    check(f"{m}: screenshot region", not err and "image 540x180 px" in text and "region starts at 100,300" in text, text)
    check(f"{m}: tap inside a region", "(110,320)" in s.call("tap", x=10, y=20, space="screenshot")[0])
    check(f"{m}: bad region", s.call("screenshot", region=[5, 5, 5, 9])[1])
    check(f"{m}: long press", "Long-pressed" in s.call("tap", x=5, y=6, long=True)[0])
    check(f"{m}: swipe", "Swiped (1,2) -> (3,4)" in s.call("swipe", x1=1, y1=2, x2=3, y2=4)[0])
    check(f"{m}: scroll", s.call("scroll", direction="down")[0] == "Scrolled down.")

    text, err, _ = s.call("type_text", text="Hi & 'quoted' 100%sure\nline2", submit=True)
    check(f"{m}: type ascii", not err and ("via key events" if helper else "via input") in text, text)
    text, err, _ = s.call("type_text", text="café 🍅\nřádek", clear_first=True)
    if helper:
        check("helper: type unicode", not err and "via the accessibility API" in text, text)
    else:
        check("shell: unicode without a keyboard fails", err and "strip_diacritics" in text, text)
        text, err, _ = s.call("type_text", text="café", strip_diacritics=True, clear_first=True)
        check("shell: type with accents removed", not err and "accents removed" in text, text)
    check(f"{m}: press_key", s.call("press_key", key="back", times=2)[0] == "Pressed back x2.")
    check(f"{m}: press_key rejects injection", s.call("press_key", key="rm -rf /")[1])
    if helper:
        text, err, _ = s.call("press_key", key="KEYCODE_NOPE")
        check("helper: unknown key name", err and "Unknown key 'KEYCODE_NOPE'" in text, text)

    check(f"{m}: open_app by alias fallback", s.call("open_app", app="messages")[0] == "Opened com.android.mms.")
    check(f"{m}: open_app by package", s.call("open_app", app="com.android.chrome")[0] == "Opened com.android.chrome.")
    check(f"{m}: open_app fuzzy", s.call("open_app", app="Maps")[0] == "Opened com.vendor.maps.")
    check(f"{m}: open_app monkey fallback", s.call("open_app", app="com.vendor.mail")[0] == "Opened com.vendor.mail.")
    check(f"{m}: open_app takes the first installed alias",
          s.call("open_app", app="Calculator")[0] == "Opened com.google.android.calculator.")
    text, err, _ = s.call("open_app", app="vendor")
    check(f"{m}: open_app ambiguous", err and "com.vendor.mail" in text and "Several" in text, text)
    check(f"{m}: open_app unknown", s.call("open_app", app="doesnotexist")[1])
    check(f"{m}: close_app", s.call("close_app", app="chrome")[0] == "Stopped com.android.chrome.")
    check(f"{m}: close_app protects the connection", s.call("close_app", app="termux")[1])
    check(f"{m}: list_apps", "com.vendor.maps" in s.call("list_apps", filter="VENDOR")[0])
    check(f"{m}: open_url adds scheme", s.call("open_url", url="example.com")[0] == "Opened https://example.com.")

    text, _, _ = s.call("notifications")
    check(f"{m}: notifications", text.startswith("2 notifications") and "accountant" in text and "Old one" not in text, text)
    check(f"{m}: notifications filter", s.call("notifications", filter="zoe")[0].startswith("1 notifications"))
    check(f"{m}: wait", s.call("wait", seconds=0.2)[0] == "Waited 0.2 s.")
    check(f"{m}: wait until_text", s.call("wait", seconds=1, until_text="Zoë")[0].startswith("Found:"))
    text, err, _ = s.call("shell", command="echo hello")
    check(f"{m}: shell", text.startswith("exit code 3\nhello") and "[stderr]\nwarn" in text, text)

    check(f"{m}: set wifi", s.call("set_setting", setting="wifi", value="off")[0] == "wifi is now off.")
    check(f"{m}: set dark mode", s.call("set_setting", setting="dark_mode", value="on")[0] == "dark_mode is now on.")
    check(f"{m}: set volume", s.call("set_setting", setting="media_volume", value="50")[0] == "Media volume is now 15 of 30.")
    text, err, _ = s.call("set_setting", setting="brightness", value="40%")
    check(f"{m}: set brightness", not err and text.startswith("Brightness set to 40 %"), text)
    check(f"{m}: set_setting rejects a bad value", s.call("set_setting", setting="dark_mode", value="maybe")[1])
    text, err, _ = s.call("set_setting", setting="bluetooth", value="on")
    check(f"{m}: set_setting notices that nothing changed", err and "still off" in text and "Permission denied" in text, text)

    text, err, _ = s.call("clipboard_set", text="copied ✓ secret")
    if helper:
        check("helper: clipboard", not err and s.call("clipboard_get")[0] == "copied ✓ secret", text)
    else:
        check("shell: clipboard needs the helper", err and "helper" in text and s.call("clipboard_get")[1], text)

    log = s.adb_commands()
    check(f"{m}: settings commands", all(x in log for x in (
        "svc wifi disable", "cmd media_session volume --stream 3 --set 15", "cmd display set-brightness 0.400",
        "am force-stop com.android.chrome")), log[-1500:])
    check(f"{m}: launch via resolve-activity", "am start -n" in log)
    if helper:
        check("helper: used for input", all(x in log for x in (
            "['helper', 'tap', {'x': 540, 'y': 390, 'ms': 0}]", "['helper', 'tap', {'x': 5, 'y': 6, 'ms': 700}]",
            "['helper', 'swipe', {'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4, 'ms': 300}]",
            "['helper', 'swipe', {'x1': 540, 'y1': 1800, 'x2': 540, 'y2': 600, 'ms': 350, 'hold': 120}]",
            "['helper', 'key', {'codes': ['KEYCODE_BACK', 'KEYCODE_BACK']}]",
            "['helper', 'chars', {'s': \"Hi & 'quoted' 100%sure\\nline2\"}]",
            "['helper', 'settext', {'s': '', 'replace': True}]", "['helper', 'settext', {'s': 'café 🍅', 'replace': False}]",
            "['helper', 'settext', {'s': 'řádek', 'replace': False}]",
            "['helper', 'shot', {'max_w': 720, 'q': 72, 'region': [100, 300, 640, 480]}]")), log[-3000:])
        check("helper: no slow commands", "uiautomator dump" not in log and "screencap" not in log
              and "input " not in log, log[-1500:])
        check("helper: pushed once", log.count("['push'") == 1, log[:600])
    else:
        typed = repr("input text 'Hi%s&%s'\"'\"'quoted'\"'\"'%s100%' && input text sure && input keyevent 66 && input text line2")
        check("shell: ascii typing in one call, literal %s kept", typed in log, log[-1500:])
        check("shell: press_key repeats in one call", "input keyevent KEYCODE_BACK KEYCODE_BACK" in log)
        check("shell: raw screencap used", "['exec-out', 'screencap']" in log)
        check("shell: helper never started", "helper" not in log and "push" not in log)


def test_observe_and_batch(s):
    text, err, _ = s.call("tap", x=10, y=20, observe=True)
    check("observe appends ui_tree", not err and "Tapped at (10,20)." in text and "Screen now:\nApp: com.example.chat" in text, text)
    s.call("ui_tree")
    text, err, _ = s.call("batch", delay_ms=0, actions=[
        {"tool": "tap_element", "args": {"index": 1}},
        {"tool": "type_text", "args": {"text": "secret-text"}},
        {"tool": "press_key", "args": {"key": "enter"}}])
    check("batch runs all", not err and "[1] tap_element: Tapped #1" in text and "[3] press_key: Pressed enter." in text, text)
    text, err, _ = s.call("batch", delay_ms=0, actions=[
        {"tool": "press_key", "args": {"key": "home"}},
        {"tool": "tap_element", "args": {"index": 99}},
        {"tool": "press_key", "args": {"key": "back"}}])
    check("batch stops at first error", err and "[2] tap_element FAILED" in text and "[3]" not in text
          and "Stopped at action 2 of 3" in text, text)
    check("batch rejects nesting", s.call("batch", actions=[{"tool": "batch", "args": {"actions": []}}])[1])
    text, err, content = s.call("batch", delay_ms=0, actions=[{"tool": "screenshot"}, {"tool": "wait", "args": {"seconds": 0.2}}])
    check("batch passes images through", not err and content[0]["type"] == "image", text)
    actions = open(os.path.join(s.tmp, "actions.log"), encoding="utf-8").read()
    check("action log hides typed text", "secret-text" not in actions and "<11 chars>" in actions, actions[-400:])
    check("action log hides clipboard text", "copied" not in actions and "<15 chars>" in actions, actions[-400:])


def test_scroll_to_text(port, **env):
    s = Server(port, MOCK_SCROLL="1", **env)
    try:
        text, err, _ = s.call("scroll_to_text", text="privacy")
        check("scroll_to_text finds it", not err and text.startswith("Found after 2 scroll(s): #4 LinearLayout"), text)
        check("scroll_to_text when already visible", s.call("scroll_to_text", text="Privacy policy", exact=True)[0]
              .startswith("Found after 0 scroll(s)"))
        text, err, _ = s.call("scroll_to_text", text="not in the list")
        check("scroll_to_text stops at the end", err and "Reached the end" in text, text)
    finally:
        s.stop()


def test_unicode(port):
    s = Server(port, MOCK_ADBKB="1", PHONE_MCP_HELPER="0")
    try:
        text, err, _ = s.call("type_text", text="Zoë, here are the photos 🍅", submit=True)
        check("type unicode via ADBKeyBoard", not err and "ADBKeyBoard" in text, text)
        log = s.adb_commands()
        check("IME switched and restored", "ime set com.android.adbkeyboard/.AdbIME" in log
              and "ime set com.google.android.inputmethod.latin" in log, log[-800:])
        check("status reports keyboard", "Unicode typing: ADBKeyBoard" in s.call("phone_status")[0])
    finally:
        s.stop()
    s = Server(port + 1, MOCK_ADBKB="1", MOCK_NO_FOCUS="1")
    try:
        text, err, _ = s.call("type_text", text="žluťoučký")
        check("ADBKeyBoard used when the field refuses accessibility text", not err and "ADBKeyBoard" in text, text)
    finally:
        s.stop()
    s = Server(port + 2, MOCK_NO_FOCUS="1")
    try:
        text, err, _ = s.call("type_text", text="žluťoučký")
        check("unicode error names the reason", err and "no focused field" in text, text)
    finally:
        s.stop()


def test_fallbacks(port):
    s = Server(port, MOCK_NO_SESSION="1", MOCK_RAW_BAD="1", PHONE_MCP_HELPER="0")
    try:
        check("one-shot shell fallback", s.call("ui_tree")[0].startswith("App: com.example.chat"))
        for _ in range(3):
            s.call("press_key", key="home")
        text, err, content = s.call("screenshot")
        check("PNG fallback for unknown raw format", not err and content[0]["type"] == "image", text)
        check("PNG path used", "'screencap', '-p'" in s.adb_commands())
        check("session given up", "persistent adb shell is not available" in open(s.server_log).read())
    finally:
        s.stop()
    s = Server(port + 1, MOCK_NO_HELPER="1")
    try:
        for _ in range(3):
            check("works while the helper fails to start", s.call("ui_tree")[0].startswith("App: com.example.chat"))
        check("tap without the helper", not s.call("tap", x=1, y=2)[1])
        log = open(s.server_log).read()
        check("helper given up, reason logged", "the helper is not usable" in log and "ClassNotFoundException" in log, log[-500:])
        check("status shows the helper is off", "fast helper off" in s.call("phone_status")[0])
        check("only three start attempts", s.adb_commands().count("['push'") <= 1)
    finally:
        s.stop()
    s = Server(port + 2, MOCK_DUMP_FAIL="1")
    try:
        check("uiautomator used when the helper cannot read the screen",
              s.call("ui_tree")[0].startswith("App: com.example.chat | screen 1080x2400 | 7 elements"))
        check("helper stays on for the rest", not s.call("tap", x=1, y=2)[1]
              and "['helper', 'tap'" in s.adb_commands() and "fast helper on" in s.call("phone_status")[0])
    finally:
        s.stop()
    s = Server(port + 4, MOCK_SLIDING="1")
    try:
        text = s.call("ui_tree")[0]
        check("waits for a window that is still sliding in", text.startswith("App: com.example.chat | screen 1080x2400 | 7 elements")
              and "@(1000,2280)" in text and s.adb_commands().count("'dump'") == 3, text)
    finally:
        s.stop()
    s = Server(port + 5, MOCK_VOLUME_STUCK="1")
    try:
        text, err, _ = s.call("set_setting", setting="media_volume", value="50")
        check("volume keys when the volume command is ignored", not err and text == "Media volume is now 15 of 30."
              and s.adb_commands().count("KEYCODE_VOLUME_UP") == 11, text)
    finally:
        s.stop()
    s = Server(port + 3, MOCK_OFFLINE="1")
    try:
        text, err, _ = s.call("tap", x=1, y=2)
        check("offline device is reported", err and "ADB is not connected" in text, text)
        health = json.loads(urllib.request.urlopen(s.base + "/health").read())
        check("health shows disconnected", health["adb"] == "disconnected", health)
    finally:
        s.stop()


def test_scripts():
    """pair.sh --auto: reconnect after a reboot without asking anything, or give up quietly."""
    tmp = tempfile.mkdtemp(prefix="phone-mcp-test-")
    os.symlink(os.path.join(HERE, "mock_adb.py"), os.path.join(tmp, "adb"))
    os.symlink(sys.executable, os.path.join(tmp, "python"))

    def pair(**env):
        log = os.path.join(tempfile.mkdtemp(prefix="phone-mcp-test-"), "adb.log")
        full_env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], MOCK_LOG=log, HOME=tmp, **env)
        p = subprocess.run(["bash", os.path.join(ROOT, "pair.sh"), "--auto"], env=full_env, timeout=120,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr, (open(log).read() if os.path.exists(log) else "")

    rc, out, log = pair(MOCK_REBOOTED="1")
    check("pair --auto reconnects after a reboot", rc == 0 and "Found Wireless debugging on port 41234" in out
          and "ADB connected: localhost:5555" in out, out)
    check("pair --auto switches to the fixed port and sets up Termux", "['tcpip', '5555']" in log
          and "pm grant com.termux android.permission.WRITE_SECURE_SETTINGS" in log, log)
    rc, out, _ = pair(MOCK_OFFLINE="1")
    check("pair --auto gives up without asking", rc == 1 and "pairing" not in out.lower(), out)
    rc, out, _ = pair()
    check("pair --auto is silent when already connected", rc == 0 and out.strip() == "", out)


def main():
    test_scripts()
    for port, helper in ((18910, True), (18911, False)):
        s = Server(port, **({} if helper else {"PHONE_MCP_HELPER": "0"}))
        try:
            if helper:
                test_protocol(s)
                test_connection_reuse(s)
            test_tools(s, helper)
            test_observe_and_batch(s)
            server_log = open(s.server_log).read()
            check("token never logged", TOKEN not in server_log)
            check("no tracebacks", "Traceback" not in server_log, server_log[-600:])
        finally:
            s.stop()
    test_scroll_to_text(18915)
    test_scroll_to_text(18916, PHONE_MCP_HELPER="0")
    test_unicode(18920)
    test_fallbacks(18930)
    print(f"{checks - len(failures)}/{checks} checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
