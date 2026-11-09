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
    check("17 tools", len(names) == 17 and "batch" in names, names)
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


def test_tools(s):
    text, err, _ = s.call("phone_status")
    check("phone_status parsed", all(x in text for x in ("Screen: on", "Locked: no", "com.example.chat/",
                                                         "Battery: 82% (discharging), 33.3", "1080x2400",
                                                         "Example Phone 1, Android 16")), text)
    text, err, _ = s.call("ui_tree")
    check("ui_tree header", text.startswith("App: com.example.chat | screen 1080x2400 | 7 elements"), text)
    check("ui_tree merges row label", '#3 LinearLayout "Zoë | Sending the tomato photos 🍅"' in text, text)
    check("ui_tree hides zero-size element", "New chat" not in text)
    check("ui_tree clamps off-screen tap point", "@(1000,2280)" in text, text)
    text, _, _ = s.call("ui_tree", filter="george")
    check("ui_tree filter", "| 1 elements" in text and "George - work" in text, text)

    text, err, _ = s.call("tap_element", index=3)
    check("tap_element", not err and "(540,390)" in text, text)
    check("tap_element out of range", s.call("tap_element", index=99)[1])
    text, err, _ = s.call("tap_text", text="george")
    check("tap_text", not err and "(540,570)" in text, text)
    check("tap_text prefers exact + clickable", "#1 ImageButton" in s.call("tap_text", text="Search")[0])
    check("tap_text miss is an error", s.call("tap_text", text="nothing here")[1])

    text, err, content = s.call("screenshot")
    image = content[0]
    check("screenshot image", image["type"] == "image" and len(image["data"]) > 1000, text)
    check("screenshot scale note", "Screen 1080x2400 px" in text, text)
    scaled = "image 720x1600" in text
    text, err, _ = s.call("tap", x=360, y=130, space="screenshot")
    check("tap in screenshot space", ("(540,195)" if scaled else "(360,130)") in text, text)
    check("long press", "Long-pressed" in s.call("tap", x=5, y=6, long=True)[0])
    check("swipe", "Swiped (1,2) -> (3,4)" in s.call("swipe", x1=1, y1=2, x2=3, y2=4)[0])
    check("scroll", s.call("scroll", direction="down")[0] == "Scrolled down.")

    text, err, _ = s.call("type_text", text="Hi & 'quoted' 100%sure\nline2", submit=True)
    check("type ascii", not err and "via input" in text, text)
    check("type unicode without keyboard fails", s.call("type_text", text="café")[1])
    text, err, _ = s.call("type_text", text="café", strip_diacritics=True, clear_first=True)
    check("type with accents removed", not err and "accents removed" in text, text)
    check("press_key", s.call("press_key", key="back", times=2)[0] == "Pressed back x2.")
    check("press_key rejects injection", s.call("press_key", key="rm -rf /")[1])

    check("open_app by alias fallback", s.call("open_app", app="messages")[0] == "Opened com.android.mms.")
    check("open_app by package", s.call("open_app", app="com.android.chrome")[0] == "Opened com.android.chrome.")
    check("open_app fuzzy", s.call("open_app", app="Maps")[0] == "Opened com.vendor.maps.")
    check("open_app monkey fallback", s.call("open_app", app="com.vendor.mail")[0] == "Opened com.vendor.mail.")
    text, err, _ = s.call("open_app", app="calculator")
    check("open_app ambiguous", not err and "com.miui.calculator" in text and "Several" in text, text)
    check("open_app unknown", s.call("open_app", app="doesnotexist")[1])
    check("list_apps", "com.vendor.maps" in s.call("list_apps", filter="VENDOR")[0])
    check("open_url adds scheme", s.call("open_url", url="example.com")[0] == "Opened https://example.com.")

    text, _, _ = s.call("notifications")
    check("notifications", text.startswith("2 notifications") and "accountant" in text and "Old one" not in text, text)
    check("wait", s.call("wait", seconds=0.2)[0] == "Waited 0.2 s.")
    check("wait until_text", s.call("wait", seconds=1, until_text="Zoë")[0].startswith("Found:"))
    text, err, _ = s.call("shell", command="echo hello")
    check("shell", text.startswith("exit code 3\nhello") and "[stderr]\nwarn" in text, text)

    log = s.adb_commands()
    typed = repr("input text 'Hi%s&%s'\"'\"'quoted'\"'\"'%s100%' && input text sure && input keyevent 66 && input text line2")
    check("ascii typing: one shell call, literal %s kept", typed in log, log[-1500:])
    check("press_key repeats in one call", "input keyevent KEYCODE_BACK KEYCODE_BACK" in log)
    check("raw screencap used", "['exec-out', 'screencap']" in log or not scaled)
    check("launch via resolve-activity", "am start -n" in log)


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


def test_unicode(port):
    s = Server(port, MOCK_ADBKB="1")
    try:
        text, err, _ = s.call("type_text", text="Zoë, here are the photos 🍅", submit=True)
        check("type unicode via ADBKeyBoard", not err and "ADBKeyBoard" in text, text)
        log = s.adb_commands()
        check("IME switched and restored", "ime set com.android.adbkeyboard/.AdbIME" in log
              and "ime set com.google.android.inputmethod.latin" in log, log[-800:])
        check("status reports keyboard", "ADBKeyBoard): installed" in s.call("phone_status")[0])
    finally:
        s.stop()


def test_fallbacks(port):
    s = Server(port, MOCK_NO_SESSION="1", MOCK_RAW_BAD="1")
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
    s = Server(port + 1, MOCK_OFFLINE="1")
    try:
        text, err, _ = s.call("tap", x=1, y=2)
        check("offline device is reported", err and "ADB is not connected" in text, text)
        health = json.loads(urllib.request.urlopen(s.base + "/health").read())
        check("health shows disconnected", health["adb"] == "disconnected", health)
    finally:
        s.stop()


def main():
    s = Server(18910)
    try:
        test_protocol(s)
        test_connection_reuse(s)
        test_tools(s)
        test_observe_and_batch(s)
        server_log = open(s.server_log).read()
        check("token never logged", TOKEN not in server_log)
        check("no tracebacks", "Traceback" not in server_log, server_log[-600:])
    finally:
        s.stop()
    test_unicode(18920)
    test_fallbacks(18930)
    print(f"{checks - len(failures)}/{checks} checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
