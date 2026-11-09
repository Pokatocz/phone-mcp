#!/usr/bin/env python3
"""
Phone MCP - let Claude control an Android phone.

Runs on the phone itself (Termux), drives the phone through ADB connected to
itself (Wireless debugging) and speaks MCP (Streamable HTTP) to Claude.
Standard library only; Pillow is optional (smaller and faster screenshots).

Configuration through environment variables (start.sh loads them from config.env):
  PHONE_MCP_TOKEN             secret token, part of the connector URL (required)
  PHONE_MCP_PORT              local server port (default 8765)
  PHONE_MCP_SERIAL            ADB device (default localhost:5555)
  PHONE_MCP_SHOT_WIDTH        max screenshot width in px (default 720)
  PHONE_MCP_LOG               action log file (default ~/phone-mcp/actions.log)
  PHONE_MCP_RESPONSE          "sse" (default) or "json" response format
  PHONE_MCP_PERSISTENT_SHELL  "0" disables the persistent ADB shell session
"""
import base64
import hmac
import io
import json
import os
import re
import select
import shlex
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.0"

TOKEN = os.environ.get("PHONE_MCP_TOKEN", "")
HOST = os.environ.get("PHONE_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PHONE_MCP_PORT", "8765"))
SERIAL = os.environ.get("PHONE_MCP_SERIAL", "localhost:5555")
ADB = os.environ.get("PHONE_MCP_ADB", "adb")
SHOT_WIDTH = int(os.environ.get("PHONE_MCP_SHOT_WIDTH", "720"))
LOG_FILE = os.environ.get("PHONE_MCP_LOG", os.path.expanduser("~/phone-mcp/actions.log"))
RESPONSE_MODE = os.environ.get("PHONE_MCP_RESPONSE", "sse").lower()
MAX_BODY = 1_000_000
MIN_TOKEN_LEN = 32

ADBKEYBOARD_PKG = "com.android.adbkeyboard"
ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
UI_DUMP_PATH = "/data/local/tmp/phone_mcp_ui.xml"

SERVER_INSTRUCTIONS = """\
This server controls the user's own Android phone through ADB.
How to work efficiently:
- ui_tree returns numbered elements with exact coordinates (takes 2-4 s). Act on them with tap_element or tap_text.
- screenshot is faster (~0.5 s) and needed for visual content (games, maps, photos, web pages). Coordinates read from a screenshot must be passed with space="screenshot".
- Save round trips: pass observe=true to an action to get the new ui_tree back in the same call, and use batch to run several actions in one call when you already know the steps (e.g. typing digits on a keypad).
- Element indexes are only valid until the screen changes.
- The user is often chatting from this same phone. Opening another app puts the Claude app in the background; that is expected.
- Ask the user before sending messages or emails, posting, buying, deleting data, calling someone or changing settings.
- Never type passwords, PINs, card numbers or other credentials. Ask the user to do that step themselves.
- Banking apps and similar block screenshots (they come back black). Tell the user instead of guessing.
"""


class ToolError(Exception):
    """An error returned to the model as a tool result (isError)."""


def stamp():
    return time.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# ADB
# ---------------------------------------------------------------------------

_adb_lock = threading.RLock()
_state = {"elements": [], "shot_scale": None, "screen": None, "screen_at": 0.0,
          "packages": None, "packages_at": 0.0, "adbkeyboard": False}

_DISCONNECTED = ("not found", "offline", "no devices", "unauthorized", "device still connecting",
                 "closed", "connection refused", "failed to connect")
NOT_CONNECTED_MSG = ("ADB is not connected to the phone. The user must turn on Wireless debugging "
                     "(Developer options) and run `bash ~/phone-mcp/pair.sh` in Termux, then try again.")


def _run(args, timeout=25):
    try:
        return subprocess.run([ADB, "-s", SERIAL] + args, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise ToolError("The 'adb' program was not found. In Termux run: pkg install android-tools")
    except subprocess.TimeoutExpired:
        raise ToolError(f"The ADB command did not finish within {timeout} s.")


def _reconnect():
    try:
        subprocess.run([ADB, "connect", SERIAL], capture_output=True, timeout=10)
        time.sleep(0.5)
    except Exception:
        pass


def adb_raw(args, timeout=25):
    """Run one adb command; reconnect once if the device dropped. Returns CompletedProcess."""
    with _adb_lock:
        p = _run(args, timeout)
        err = (p.stderr or b"").decode("utf-8", "replace").lower()
        if p.returncode != 0 and any(s in err for s in _DISCONNECTED):
            _reconnect()
            p = _run(args, timeout)
            err = (p.stderr or b"").decode("utf-8", "replace").lower()
            if p.returncode != 0 and any(s in err for s in _DISCONNECTED):
                raise ToolError(NOT_CONNECTED_MSG)
        return p


class ShellSession:
    """One long-lived `adb shell` process. Saves spawning an adb client for every command."""

    def __init__(self):
        self.proc = None
        self.buf = b""
        self.failures = 0
        self.disabled = os.environ.get("PHONE_MCP_PERSISTENT_SHELL", "1") == "0"

    def close(self):
        proc, self.proc, self.buf = self.proc, None, b""
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass

    def _start(self):
        self.close()
        self.proc = subprocess.Popen([ADB, "-s", SERIAL, "shell", "-T"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        out, rc = self._exchange("echo ready", 6)
        if rc != 0 or out.strip() != b"ready":
            raise OSError("unexpected reply from adb shell")

    def _exchange(self, cmd, timeout):
        marker = ("__PMCP_%s__" % uuid.uuid4().hex).encode()
        self.proc.stdin.write(b"{ " + cmd.encode("utf-8") + b"\n} </dev/null 2>&1; echo; echo "
                              + marker + b"$?\n")
        fd = self.proc.stdout.fileno()
        pattern = b"\n" + marker
        deadline = time.monotonic() + timeout
        scanned = 0
        while True:
            i = self.buf.find(pattern, max(0, scanned - len(pattern)))
            if i != -1:
                j = self.buf.find(b"\n", i + len(pattern))
                if j != -1:
                    out = self.buf[:i]
                    rc = int(self.buf[i + len(pattern):j].strip() or b"1")
                    self.buf = self.buf[j + 1:]
                    return out, rc
            scanned = len(self.buf)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError
            chunk = os.read(fd, 65536)
            if not chunk:
                raise EOFError
            self.buf += chunk

    def run(self, cmd, timeout):
        """Returns (output_bytes, exit_code), or None when the caller should use a one-shot adb call."""
        if self.disabled:
            return None
        if self.proc is None or self.proc.poll() is not None:
            try:
                self._start()
                self.failures = 0
            except Exception:
                self.close()
                self.failures += 1
                return None
        try:
            return self._exchange(cmd, timeout)
        except TimeoutError:
            self.close()
            raise ToolError(f"The command did not finish within {timeout} s.")
        except BrokenPipeError:
            self.close()      # nothing was sent, safe to fall back
            return None
        except (EOFError, OSError, ValueError):
            self.close()
            raise ToolError("The ADB connection dropped during the command. Check the screen before retrying.")


_session = ShellSession()


def sh(cmd, timeout=25, check=True):
    """Run a command in the phone's shell (user 'shell'); returns stdout+stderr as text."""
    with _adb_lock:
        res = _session.run(cmd, timeout)
        if res is None:
            p = adb_raw(["shell", cmd], timeout=timeout)
            res = (p.stdout + (p.stderr or b""), p.returncode)
            if _session.failures >= 3 and not _session.disabled:
                # The device answers one-shot commands but the session never starts: stop trying.
                _session.disabled = True
                sys.stderr.write(f"{stamp()} persistent adb shell is not available, using one-shot commands\n")
    out, rc = res[0].decode("utf-8", "replace"), res[1]
    if check and rc != 0:
        raise ToolError(f"Command failed ({rc}): {cmd}\n{out.strip()}".strip())
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def screen_size(max_age=5.0):
    """Current screen size in pixels (rotation-aware), cached for a few seconds."""
    if _state["screen"] and time.time() - _state["screen_at"] < max_age:
        return _state["screen"]
    out = sh("dumpsys window displays | grep -m1 -oE 'cur=[0-9]+x[0-9]+'; wm size", check=False)
    m = (re.search(r"cur=(\d+)x(\d+)", out) or re.search(r"Override size:\s*(\d+)x(\d+)", out)
         or re.search(r"Physical size:\s*(\d+)x(\d+)", out))
    if not m:
        raise ToolError("Could not determine the screen size.")
    return _remember_screen((int(m.group(1)), int(m.group(2))))


def _remember_screen(size):
    _state["screen"], _state["screen_at"] = size, time.time()
    return size


def to_screen(x, y, space):
    if space == "screenshot":
        scale = _state.get("shot_scale")
        if not scale:
            raise ToolError("No screenshot taken yet - call screenshot first, or use space='screen'.")
        return round(x / scale), round(y / scale)
    return round(x), round(y)


def _png_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return None


def _bounds(s):
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", s or "")
    return tuple(int(v) for v in m.groups()) if m else None


def _short_id(rid):
    return rid.split(":id/", 1)[1] if ":id/" in rid else rid


def _clip(text, n=70):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _fold(text):
    """Lower-case and strip accents, for forgiving name matching."""
    return unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()


# ---------------------------------------------------------------------------
# UI tree (uiautomator)
# ---------------------------------------------------------------------------

def dump_ui_xml():
    cmd = (f"uiautomator dump {UI_DUMP_PATH} >/dev/null 2>&1; "
           f"cat {UI_DUMP_PATH} 2>/dev/null; rm -f {UI_DUMP_PATH}")
    for attempt in range(3):
        out = sh(cmd, timeout=30, check=False)
        start = out.find("<?xml")
        if start == -1:
            start = out.find("<hierarchy")
        end = out.rfind("</hierarchy>")
        if start != -1 and end > start:
            return out[start:end + len("</hierarchy>")]
        time.sleep(0.5)
    raise ToolError("The UI tree is not available right now (screen animating, video playing or protected app). "
                    "Use screenshot instead.")


def parse_ui(xml_text):
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as e:
        raise ToolError(f"The UI tree could not be parsed ({e}). Use screenshot instead.")
    sw, sh_ = 0, 0
    first = root.find("node")
    b = _bounds(first.get("bounds")) if first is not None else None
    if b:
        sw, sh_ = b[2], b[3]
    elif _state.get("screen"):
        sw, sh_ = _state["screen"]
    screen_area = max(1, sw * sh_)
    elements = []
    packages = {}

    def descendant_texts(node, limit=3):
        texts = []
        for d in node.iter("node"):
            if d is node:
                continue
            t = (d.get("text") or d.get("content-desc") or "").strip()
            if t and t not in texts:
                texts.append(t)
                if len(texts) >= limit:
                    break
        return texts

    def walk(node, covered):
        b = _bounds(node.get("bounds"))
        pkg = node.get("package") or ""
        cls = (node.get("class") or "").rsplit(".", 1)[-1]
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        rid = _short_id(node.get("resource-id") or "")
        flag = lambda k: node.get(k) == "true"
        editable = "EditText" in cls or "AutoCompleteTextView" in cls
        interactive = (flag("clickable") or flag("long-clickable") or flag("scrollable")
                       or flag("checkable") or editable)
        label = text or desc
        if not label and (flag("clickable") or flag("long-clickable")):
            label = " | ".join(descendant_texts(node))
        area = (b[2] - b[0]) * (b[3] - b[1]) if b else 0
        visible = b and area > 0 and b[2] > 0 and b[3] > 0 and (not sw or b[0] < sw) and (not sh_ or b[1] < sh_)
        if visible and (interactive or (label and not covered)):
            flags = []
            if flag("clickable"):
                flags.append("click")
            if flag("long-clickable"):
                flags.append("long")
            if flag("scrollable"):
                flags.append("scroll")
            if editable:
                flags.append("edit")
            if flag("checkable"):
                flags.append("checked" if flag("checked") else "unchecked")
            if flag("selected"):
                flags.append("selected")
            if flag("focused"):
                flags.append("focused")
            if flag("password"):
                flags.append("password")
            if node.get("enabled") == "false":
                flags.append("disabled")
            # Tap point clamped to the visible screen (partly off-screen elements).
            x = (max(b[0], 0) + (min(b[2], sw) if sw else b[2])) // 2
            y = (max(b[1], 0) + (min(b[3], sh_) if sh_ else b[3])) // 2
            elements.append({
                "text": text, "desc": desc, "label": label, "id": rid, "cls": cls, "pkg": pkg,
                "x": x, "y": y, "flags": flags,
                "clickable": flag("clickable") or flag("long-clickable") or editable or flag("checkable"),
            })
            if pkg:
                packages[pkg] = packages.get(pkg, 0) + 1
        # Texts inside a labelled clickable element are already part of its label.
        covers = interactive and bool(label) and area < 0.4 * screen_area
        for child in node.findall("node"):
            walk(child, covered or covers)

    for top in root.findall("node"):
        walk(top, False)
    for i, el in enumerate(elements):
        el["index"] = i
    app = max((p for p in packages if p != "com.android.systemui"), key=packages.get, default="")
    if sw and sh_:
        _remember_screen((sw, sh_))
    return elements, app, (sw, sh_)


def format_element(el):
    parts = [f"#{el['index']}", el["cls"] or "?"]
    if el["label"]:
        parts.append(json.dumps(_clip(el["label"]), ensure_ascii=False))
    if el["desc"] and el["desc"] != el["label"]:
        parts.append(f"desc={json.dumps(_clip(el['desc'], 50), ensure_ascii=False)}")
    if el["id"]:
        parts.append(f"id={el['id']}")
    parts.append(f"@({el['x']},{el['y']})")
    if el["flags"]:
        parts.append("[" + ",".join(el["flags"]) + "]")
    return " ".join(parts)


def fresh_elements():
    elements, app, size = parse_ui(dump_ui_xml())
    _state["elements"] = elements
    return elements, app, size


def render_tree(flt="", limit=200):
    elements, app, size = fresh_elements()
    shown = elements
    flt = (flt or "").casefold()
    if flt:
        shown = [e for e in elements if flt in (e["label"] + " " + e["desc"] + " " + e["id"]).casefold()]
    head = f"App: {app or '?'} | screen {size[0]}x{size[1]} | {len(shown)} elements"
    if len(shown) > limit:
        head += f" (showing first {limit})"
    lines = [head] + [format_element(e) for e in shown[:limit]]
    if not shown:
        lines.append("(no matching elements - try screenshot)")
    return "\n".join(lines)


def find_text(elements, text, exact=False):
    t = text.casefold().strip()
    scored = []
    for el in elements:
        fields = [f.casefold() for f in (el["text"], el["desc"], el["label"], el["id"]) if f]
        if any(f == t for f in fields):
            score = 0
        elif not exact and any(t in f for f in fields):
            score = 1
        else:
            continue
        if not el["clickable"]:
            score += 2
        scored.append((score, el["index"], el))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [s[2] for s in scored]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS = {}

OBSERVE_PROP = {"type": "boolean", "default": False,
                "description": "Also return the new ui_tree after the action. Saves a separate ui_tree "
                               "call (adds 2-4 s to this one)."}
SPACE_PROP = {"type": "string", "enum": ["screen", "screenshot"], "default": "screen",
              "description": "'screen' = real screen pixels (ui_tree coordinates); "
                             "'screenshot' = pixels of the last (possibly downscaled) screenshot."}


def tool(name, description, properties=None, required=None, read_only=False, destructive=False, settle=None):
    """Register a tool. `settle` (seconds) marks an action tool and adds the `observe` option."""
    def register(fn):
        props = dict(properties or {})
        if settle is not None:
            props["observe"] = OBSERVE_PROP
        TOOLS[name] = {
            "fn": fn,
            "settle": settle,
            "spec": {
                "name": name,
                "description": description,
                "inputSchema": {"type": "object", "properties": props,
                                "required": required or [], "additionalProperties": False},
                "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive,
                                "openWorldHint": False},
            },
        }
        return fn
    return register


def text_result(text):
    return [{"type": "text", "text": text}]


def run_tool(name, args):
    """Run one tool and return its content list. Raises ToolError."""
    entry = TOOLS[name]
    t0 = time.time()
    try:
        content = entry["fn"](args)
        if entry["settle"] is not None and args.get("observe"):
            time.sleep(entry["settle"])
            try:
                content = content + text_result("Screen now:\n" + render_tree())
            except ToolError as e:
                content = content + text_result(f"(could not read the screen: {e})")
    except Exception:
        log_action(name, args, False, int((time.time() - t0) * 1000))
        raise
    log_action(name, args, True, int((time.time() - t0) * 1000))
    return content


BATTERY_STATUS = {"1": "unknown", "2": "charging", "3": "discharging", "4": "not charging", "5": "full"}


def _adbkeyboard_installed():
    if not _state["adbkeyboard"]:
        _state["adbkeyboard"] = ("package:" + ADBKEYBOARD_PKG
                                 in sh(f"pm list packages {ADBKEYBOARD_PKG}", check=False))
    return _state["adbkeyboard"]


@tool("phone_status",
      "Overview of the phone: screen on/off, lock screen, foreground app, battery, screen size. "
      "Call it first when unsure what state the phone is in.", read_only=True)
def t_phone_status(args):
    out = sh(
        "dumpsys power | grep -m1 'mWakefulness='; "
        "dumpsys window | grep -E 'mCurrentFocus=|isKeyguardShowing=|mDreamingLockscreen=|mShowingLockscreen=' | head -n 8; "
        "dumpsys battery | grep -E '^ +(level|status|temperature|[A-Za-z]+ powered):'; "
        "wm size; "
        "echo \"device=$(getprop ro.product.manufacturer) $(getprop ro.product.model), "
        "Android $(getprop ro.build.version.release)\"",
        check=False)

    def find(pattern, default="unknown"):
        m = re.search(pattern, out)
        return m.group(1) if m else default

    wake = find(r"mWakefulness=(\w+)")
    screen = {"Awake": "on", "Asleep": "off", "Dozing": "off (dozing)", "Dreaming": "on (screensaver)"}.get(wake, wake)
    locked = "yes" if re.search(r"(isKeyguardShowing|mDreamingLockscreen|mShowingLockscreen)=true", out) else "no"
    focus = find(r"mCurrentFocus=Window\{\S+ \S+ ([^}\s]+)\}")
    level = find(r"level: (\d+)")
    status = BATTERY_STATUS.get(find(r"status: (\d+)", ""), "unknown")
    if re.search(r"powered: true", out) and status == "discharging":
        status = "plugged in"
    temp = find(r"temperature: (\d+)", "")
    size = (re.search(r"Override size:\s*(\S+)", out) or re.search(r"Physical size:\s*(\S+)", out))
    lines = [
        f"Screen: {screen}",
        f"Locked: {locked}",
        f"Foreground: {focus}",
        f"Battery: {level}% ({status})" + (f", {int(temp) / 10:.1f} °C" if temp else ""),
        f"Display: {size.group(1) if size else 'unknown'} px",
        f"Device: {find(r'device=(.+)')}",
        f"Unicode typing (ADBKeyBoard): {'installed' if _adbkeyboard_installed() else 'not installed'}",
        f"Server: phone-mcp {VERSION}",
    ]
    return text_result("\n".join(lines))


def _capture_image(max_w):
    """Returns (bytes, mime, real_size, out_size, scale, blank)."""
    try:
        from PIL import Image
    except ImportError:
        Image = None
    im = None
    if Image is not None:
        # Raw capture skips PNG encoding on the phone (about 4x faster than `screencap -p`).
        p = adb_raw(["exec-out", "screencap"], timeout=30)
        data = p.stdout
        if p.returncode == 0 and len(data) > 16:
            w, h, fmt = struct.unpack("<III", data[:12])
            header = len(data) - w * h * 4
            if fmt == 1 and header in (12, 16) and 0 < w <= 16384 and 0 < h <= 16384:
                im = Image.frombuffer("RGBA", (w, h), data[header:], "raw", "RGBA", 0, 1)
    if im is None:
        p = adb_raw(["exec-out", "screencap", "-p"], timeout=30)
        data = p.stdout
        real = _png_size(data)
        if p.returncode != 0 or not real:
            raise ToolError("Screenshot failed: " + (p.stderr or b"").decode("utf-8", "replace")[:300])
        if Image is None:
            return data, "image/png", real, real, 1.0, False
        im = Image.open(io.BytesIO(data))
    real, scale = im.size, 1.0
    if im.width > max_w:
        scale = max_w / im.width
        im = im.resize((max_w, max(1, round(im.height * scale))), Image.BILINEAR, reducing_gap=2.0)
    im = im.convert("RGB")
    blank = im.convert("L").getextrema()[1] < 12
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return buf.getvalue(), "image/jpeg", real, im.size, scale, blank


@tool("screenshot",
      "Take a screenshot of the phone screen (~0.5 s). Returns an image, usually downscaled; the text "
      "gives the scale. Use it for visual content; use ui_tree when you need exact element coordinates.",
      {"max_width": {"type": "integer", "minimum": 240, "maximum": 1600,
                     "description": f"Max image width in px (default {SHOT_WIDTH})."}},
      read_only=True)
def t_screenshot(args):
    max_w = int(args.get("max_width") or SHOT_WIDTH)
    data, mime, real, out_size, scale, blank = _capture_image(max_w)
    _state["shot_scale"] = scale
    _remember_screen(tuple(real))
    note = (f"Screen {real[0]}x{real[1]} px, image {out_size[0]}x{out_size[1]} px (scale {scale:.4f}). "
            "To tap something seen in this image use tap(x, y, space='screenshot').")
    if blank:
        note += " The image is black - the screen is off, or the app blocks screenshots (banking, passwords)."
    return [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime},
            {"type": "text", "text": note}]


@tool("ui_tree",
      "Read the screen as a numbered list of elements: class, text, id, center coordinates and flags "
      "(click, scroll, edit, checked...). Then use tap_element(index). Exact, but takes 2-4 s.",
      {"filter": {"type": "string", "description": "Only elements whose text/description/id contains this."},
       "max_elements": {"type": "integer", "minimum": 10, "maximum": 600, "default": 200}},
      read_only=True)
def t_ui_tree(args):
    return text_result(render_tree(args.get("filter"), int(args.get("max_elements") or 200)))


def _tap(x, y, long=False):
    if long:
        sh(f"input swipe {x} {y} {x} {y} 700")
    else:
        sh(f"input tap {x} {y}")


@tool("tap",
      "Tap (or long-press) at coordinates.",
      {"x": {"type": "number"}, "y": {"type": "number"}, "space": SPACE_PROP,
       "long": {"type": "boolean", "default": False, "description": "Long press (~0.7 s)."}},
      ["x", "y"], settle=0.4)
def t_tap(args):
    x, y = to_screen(args["x"], args["y"], args.get("space", "screen"))
    _tap(x, y, bool(args.get("long")))
    return text_result(f"{'Long-pressed' if args.get('long') else 'Tapped'} at ({x},{y}).")


@tool("tap_element",
      "Tap an element by its number from the latest ui_tree.",
      {"index": {"type": "integer", "minimum": 0}, "long": {"type": "boolean", "default": False}},
      ["index"], settle=0.4)
def t_tap_element(args):
    elements = _state.get("elements") or []
    i = int(args["index"])
    if not elements:
        raise ToolError("No ui_tree yet - call ui_tree first.")
    if not 0 <= i < len(elements):
        raise ToolError(f"Element #{i} does not exist (the last ui_tree has {len(elements)} elements).")
    el = elements[i]
    _tap(el["x"], el["y"], bool(args.get("long")))
    return text_result(f"Tapped #{i} {_clip(el['label']) or el['id'] or el['cls']} at ({el['x']},{el['y']}).")


@tool("tap_text",
      "Find an element by its visible text, description or id on the current screen and tap it. "
      "Reads the screen itself (2-4 s), so no ui_tree call is needed first.",
      {"text": {"type": "string"}, "exact": {"type": "boolean", "default": False},
       "occurrence": {"type": "integer", "minimum": 1, "default": 1,
                      "description": "Which match to tap when there are several."},
       "long": {"type": "boolean", "default": False}},
      ["text"], settle=0.4)
def t_tap_text(args):
    elements, _, _ = fresh_elements()
    matches = find_text(elements, args["text"], bool(args.get("exact")))
    if not matches:
        raise ToolError(f"No element with text {args['text']!r} on screen. Use ui_tree or screenshot.")
    n = int(args.get("occurrence") or 1)
    if n > len(matches):
        raise ToolError(f"Only {len(matches)} matches found.")
    el = matches[n - 1]
    _tap(el["x"], el["y"], bool(args.get("long")))
    msg = f"Tapped {format_element(el)}."
    if len(matches) > 1:
        msg += f" ({len(matches)} matches; others: " + "; ".join(format_element(m) for m in matches[1:4]) + ")"
    return text_result(msg)


@tool("swipe",
      "Swipe from one point to another (drag, pull down, sliders...). For plain scrolling use scroll.",
      {"x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"},
       "duration_ms": {"type": "integer", "minimum": 50, "maximum": 5000, "default": 300}, "space": SPACE_PROP},
      ["x1", "y1", "x2", "y2"], settle=0.3)
def t_swipe(args):
    space = args.get("space", "screen")
    x1, y1 = to_screen(args["x1"], args["y1"], space)
    x2, y2 = to_screen(args["x2"], args["y2"], space)
    ms = int(args.get("duration_ms") or 300)
    sh(f"input swipe {x1} {y1} {x2} {y2} {ms}")
    return text_result(f"Swiped ({x1},{y1}) -> ({x2},{y2}) in {ms} ms.")


@tool("scroll",
      "Scroll the screen content. direction='down' shows content further down (finger moves up).",
      {"direction": {"type": "string", "enum": ["down", "up", "left", "right"]},
       "amount": {"type": "number", "minimum": 0.1, "maximum": 0.9, "default": 0.5,
                  "description": "Fraction of the screen to scroll."}},
      ["direction"], settle=0.3)
def t_scroll(args):
    w, h = screen_size()
    a = min(0.9, max(0.1, float(args.get("amount") or 0.5)))
    cx, cy = w // 2, h // 2
    dx, dy = int(w * a / 2), int(h * a / 2)
    d = args["direction"]
    if d == "down":
        x1, y1, x2, y2 = cx, cy + dy, cx, cy - dy
    elif d == "up":
        x1, y1, x2, y2 = cx, cy - dy, cx, cy + dy
    elif d == "right":
        x1, y1, x2, y2 = cx + dx, cy, cx - dx, cy
    elif d == "left":
        x1, y1, x2, y2 = cx - dx, cy, cx + dx, cy
    else:
        raise ToolError(f"Unknown direction {d!r}.")
    sh(f"input swipe {x1} {y1} {x2} {y2} 350")
    return text_result(f"Scrolled {d}.")


def _is_plain_ascii(text):
    return all(32 <= ord(c) < 127 or c == "\n" for c in text)


def _type_ascii(text):
    """Type printable ASCII with `input text`, all in as few shell calls as possible."""
    cmds = []
    lines = text.split("\n")
    for li, line in enumerate(lines):
        # `input text` turns "%s" into a space. Split a literal "%s" so it is typed as written.
        for piece in re.split(r"(?<=%)(?=s)", line):
            for i in range(0, len(piece), 120):
                chunk = piece[i:i + 120].replace(" ", "%s")
                if chunk:
                    cmds.append("input text " + shlex.quote(chunk))
        if li < len(lines) - 1:
            cmds.append("input keyevent 66")
    for i in range(0, len(cmds), 12):
        sh(" && ".join(cmds[i:i + 12]), timeout=40)


def _type_unicode(text):
    prev = sh("settings get secure default_input_method", check=False).strip()
    switched = prev != ADBKEYBOARD_IME
    if switched:
        sh(f"ime enable {ADBKEYBOARD_IME} >/dev/null 2>&1; ime set {ADBKEYBOARD_IME}", check=False)
        time.sleep(0.5)
    try:
        for i in range(0, len(text), 800):
            b64 = base64.b64encode(text[i:i + 800].encode("utf-8")).decode()
            out = sh(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}", check=False)
            if "result=" not in out and "Broadcast completed" not in out:
                raise ToolError("ADBKeyBoard did not accept the text: " + out.strip()[:200])
        time.sleep(0.3)
    finally:
        if switched and prev and prev != "null":
            sh(f"ime set {shlex.quote(prev)}", check=False)


def _clear_field():
    out = sh("input keycombination KEYCODE_CTRL_LEFT KEYCODE_A 2>&1", check=False)
    if "rror" in out or "nknown" in out or "sage" in out:   # older Android without `keycombination`
        sh("input keyevent KEYCODE_MOVE_END && input keyevent " + " ".join(["67"] * 80), check=False)
    else:
        sh("input keyevent 67", check=False)


@tool("type_text",
      "Type text into the focused field (tap the field first). Accented letters, non-Latin scripts and "
      "emoji need the ADBKeyBoard app on the phone; plain ASCII always works. Newline = Enter.",
      {"text": {"type": "string"},
       "clear_first": {"type": "boolean", "default": False, "description": "Delete the field's content first."},
       "submit": {"type": "boolean", "default": False, "description": "Press Enter after typing."},
       "strip_diacritics": {"type": "boolean", "default": False,
                            "description": "If ADBKeyBoard is missing, type 'cafe' instead of 'café'."}},
      ["text"], settle=0.3)
def t_type_text(args):
    text = args["text"]
    if args.get("clear_first"):
        _clear_field()
    method = "input"
    if _is_plain_ascii(text):
        _type_ascii(text)
    elif _adbkeyboard_installed():
        _type_unicode(text)
        method = "ADBKeyBoard"
    elif args.get("strip_diacritics"):
        stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        _type_ascii("".join(c for c in stripped if _is_plain_ascii(c)))
        method = "input (accents removed)"
    else:
        raise ToolError(
            "The text contains non-ASCII characters and the ADBKeyBoard app is not installed. "
            "Ask the user to install it (see README), or call again with strip_diacritics=true.")
    if args.get("submit"):
        sh("input keyevent 66")
    return text_result(f"Typed {len(text)} characters via {method}" + (" and pressed Enter." if args.get("submit") else "."))


KEYS = {
    "back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "recents": "KEYCODE_APP_SWITCH", "enter": "KEYCODE_ENTER",
    "delete": "KEYCODE_DEL", "forward_delete": "KEYCODE_FORWARD_DEL", "tab": "KEYCODE_TAB",
    "escape": "KEYCODE_ESCAPE", "space": "KEYCODE_SPACE", "power": "KEYCODE_POWER", "sleep": "KEYCODE_SLEEP",
    "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE",
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE", "next": "KEYCODE_MEDIA_NEXT", "previous": "KEYCODE_MEDIA_PREVIOUS",
    "camera": "KEYCODE_CAMERA", "search": "KEYCODE_SEARCH", "menu": "KEYCODE_MENU", "dpad_up": "KEYCODE_DPAD_UP",
    "dpad_down": "KEYCODE_DPAD_DOWN", "dpad_left": "KEYCODE_DPAD_LEFT", "dpad_right": "KEYCODE_DPAD_RIGHT",
    "move_home": "KEYCODE_MOVE_HOME", "move_end": "KEYCODE_MOVE_END",
}
SPECIAL_KEYS = {
    "wake": "input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard",
    "notifications": "cmd statusbar expand-notifications",
    "quick_settings": "cmd statusbar expand-settings",
    "collapse_panels": "cmd statusbar collapse",
}


@tool("press_key",
      "Press a system key or open a system panel. 'wake' turns the screen on and dismisses a lock "
      "screen that has no PIN. A raw KEYCODE_* name or number also works.",
      {"key": {"type": "string",
               "description": "One of: " + ", ".join(list(KEYS) + list(SPECIAL_KEYS)) + ", or KEYCODE_*"},
       "times": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1}},
      ["key"], settle=0.4)
def t_press_key(args):
    key = args["key"].strip()
    times = min(20, max(1, int(args.get("times") or 1)))
    low = key.lower()
    if low in SPECIAL_KEYS:
        sh(SPECIAL_KEYS[low], check=False)
    else:
        code = KEYS.get(low) or key.upper()
        if not re.fullmatch(r"KEYCODE_[A-Z0-9_]+|\d+", code):
            raise ToolError(f"Unknown key {key!r}.")
        sh("input keyevent " + " ".join([code] * times))
    return text_result(f"Pressed {key}" + (f" x{times}" if times > 1 else "") + ".")


APP_ALIASES = {
    "whatsapp": ["com.whatsapp"],
    "chrome": ["com.android.chrome"],
    "gmail": ["com.google.android.gm"],
    "youtube": ["com.google.android.youtube"],
    "maps": ["com.google.android.apps.maps"], "google maps": ["com.google.android.apps.maps"],
    "settings": ["com.android.settings"],
    "messages": ["com.google.android.apps.messaging", "com.android.mms", "com.samsung.android.messaging"],
    "phone": ["com.google.android.dialer", "com.android.contacts", "com.samsung.android.dialer"],
    "camera": ["com.android.camera", "com.google.android.GoogleCamera", "com.sec.android.app.camera"],
    "calendar": ["com.google.android.calendar", "com.xiaomi.calendar", "com.samsung.android.calendar"],
    "photos": ["com.google.android.apps.photos", "com.miui.gallery", "com.sec.android.gallery3d"],
    "clock": ["com.google.android.deskclock", "com.android.deskclock", "com.sec.android.app.clockpackage"],
    "files": ["com.google.android.documentsui", "com.mi.android.globalFileexplorer", "com.sec.android.app.myfiles"],
    "play store": ["com.android.vending"],
    "drive": ["com.google.android.apps.docs"],
    "instagram": ["com.instagram.android"], "facebook": ["com.facebook.katana"], "messenger": ["com.facebook.orca"],
    "spotify": ["com.spotify.music"], "telegram": ["org.telegram.messenger"],
    "signal": ["org.thoughtcrime.securesms"], "claude": ["com.anthropic.claude"], "termux": ["com.termux"],
}


def _list_packages(include_system=True, max_age=120.0):
    if include_system and _state["packages"] and time.time() - _state["packages_at"] < max_age:
        return _state["packages"]
    out = sh("pm list packages" + ("" if include_system else " -3"), check=False)
    pkgs = sorted(ln[8:].strip() for ln in out.splitlines() if ln.startswith("package:"))
    if include_system and pkgs:
        _state["packages"], _state["packages_at"] = pkgs, time.time()
    return pkgs


def _resolve_app(name):
    """Returns (package, candidates): exactly one of them is set."""
    low = name.casefold()
    for attempt in (0, 1):
        packages = _list_packages(max_age=120.0 if attempt == 0 else 0)
        installed = set(packages)
        if name in installed:
            return name, None
        for pkg in APP_ALIASES.get(low, []):
            if pkg in installed:
                return pkg, None
        key = re.sub(r"[^a-z0-9]", "", _fold(name))
        if key:
            exact = [p for p in packages if key in (re.sub(r"[^a-z0-9]", "", s) for s in p.casefold().split("."))]
            if len(exact) == 1:
                return exact[0], None
            cands = exact or [p for p in packages if key in re.sub(r"[^a-z0-9]", "", p.casefold())]
            if len(cands) == 1:
                return cands[0], None
            if cands:
                return None, cands
    return None, []


def _launch(pkg):
    q = shlex.quote(pkg)
    out = sh(f"c=$(cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {q} | tail -n 1); "
             f"case \"$c\" in */*) am start -n \"$c\" 2>&1;; *) echo NO_LAUNCHER;; esac", check=False)
    if "NO_LAUNCHER" in out or "Error" in out:
        out = sh(f"monkey -p {q} -c android.intent.category.LAUNCHER 1 2>&1", check=False)
        if "No activities found" in out or "monkey aborted" in out or "Error" in out:
            raise ToolError(f"App {pkg} has no launcher screen or could not be started.")


@tool("open_app",
      "Open an app by package name (e.g. com.whatsapp) or a common name (whatsapp, chrome, settings, "
      "camera...). If the name is ambiguous, returns the candidate packages.",
      {"app": {"type": "string"}}, ["app"], settle=1.0)
def t_open_app(args):
    name = args["app"].strip()
    pkg, cands = _resolve_app(name)
    if not pkg:
        if cands:
            return text_result(f"Several apps match {name!r} - call open_app with one package:\n"
                               + "\n".join(cands[:30]))
        raise ToolError(f"No installed app matches {name!r}. Use list_apps to see packages.")
    _launch(pkg)
    return text_result(f"Opened {pkg}.")


@tool("list_apps",
      "List installed apps (package names).",
      {"filter": {"type": "string"}, "include_system": {"type": "boolean", "default": False}},
      read_only=True)
def t_list_apps(args):
    pkgs = _list_packages(bool(args.get("include_system")), max_age=0)
    flt = (args.get("filter") or "").casefold()
    if flt:
        pkgs = [p for p in pkgs if flt in p.casefold()]
    return text_result(f"{len(pkgs)} packages:\n" + "\n".join(pkgs))


@tool("open_url",
      "Open a URL or deep link with the default app: https://..., tel:..., sms:..., mailto:..., geo:..., "
      "market://... (dialing, messages etc. only open the app - nothing is sent).",
      {"url": {"type": "string"}}, ["url"], settle=1.2)
def t_open_url(args):
    url = args["url"].strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        url = "https://" + url
    out = sh(f"am start -a android.intent.action.VIEW -d {shlex.quote(url)} 2>&1", check=False)
    if "Error" in out:
        raise ToolError(out.strip()[:300])
    return text_result(f"Opened {url}.")


@tool("notifications",
      "Read the current notifications (app, title, text).",
      {"filter": {"type": "string", "description": "Only notifications whose app/title/text contains this."},
       "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30}},
      read_only=True)
def t_notifications(args):
    out = sh("dumpsys notification --noredact", timeout=30, check=False)
    items, cur, started = [], None, False
    rec_re = re.compile(r"NotificationRecord\(0x[0-9a-f]+:?\s+pkg=(\S+)")
    field_re = re.compile(r"android\.(title|text|bigText|subText|conversationTitle)=(.*)$")
    for line in out.splitlines():
        s = line.strip()
        if started and s.startswith(("Archive", "mArchive", "Snoozed")):
            break
        m = rec_re.search(s)
        if m:
            started = True
            cur = {"app": m.group(1)}
            items.append(cur)
            continue
        if cur is None:
            continue
        m = field_re.search(s)
        if m and m.group(1) not in cur:
            val = m.group(2).strip()
            mm = re.match(r"^\w*String \((.*)\)$", val)
            if mm:
                val = mm.group(1)
            if val and val != "null":
                cur[m.group(1)] = val
    seen, lines = set(), []
    flt = (args.get("filter") or "").casefold()
    for it in items:
        title = it.get("title") or it.get("conversationTitle") or ""
        body = it.get("bigText") or it.get("text") or ""
        if not (title or body):
            continue
        key = (it["app"], title, body)
        if key in seen:
            continue
        seen.add(key)
        line = f"{it['app']} | {_clip(title, 60)} | {_clip(body, 160)}"
        if flt and flt not in line.casefold():
            continue
        lines.append(line)
    limit = int(args.get("limit") or 30)
    if not lines:
        return text_result("No notifications with text.")
    return text_result(f"{len(lines)} notifications (app | title | text):\n" + "\n".join(lines[:limit]))


@tool("wait",
      "Wait a moment (apps loading), optionally until some text appears on screen.",
      {"seconds": {"type": "number", "minimum": 0.2, "maximum": 30, "default": 2},
       "until_text": {"type": "string", "description": "Stop as soon as this text is on screen."}},
      read_only=True)
def t_wait(args):
    secs = min(30.0, max(0.2, float(args.get("seconds") or 2)))
    target = args.get("until_text")
    if not target:
        time.sleep(secs)
        return text_result(f"Waited {secs:g} s.")
    deadline = time.time() + secs
    while True:
        try:
            elements, _, _ = fresh_elements()
            hits = find_text(elements, target)
            if hits:
                return text_result(f"Found: {format_element(hits[0])}")
        except ToolError:
            pass
        if time.time() >= deadline:
            return text_result(f"Text {target!r} did not appear within {secs:g} s.")
        time.sleep(0.5)


@tool("shell",
      "Run any command in the phone's ADB shell (user 'shell': am, pm, settings, dumpsys, input, cmd, svc...). "
      "Full power - use it for things the other tools can't do. Confirm with the user before anything risky.",
      {"command": {"type": "string"},
       "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30}},
      ["command"], destructive=True)
def t_shell(args):
    timeout = min(120, max(1, int(args.get("timeout_s") or 30)))
    p = adb_raw(["shell", args["command"]], timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    text = out + (("\n[stderr]\n" + err) if err.strip() else "")
    if len(text) > 15000:
        text = text[:15000] + f"\n... (truncated, {len(text)} chars total)"
    return text_result(f"exit code {p.returncode}\n{text}".rstrip())


@tool("batch",
      "Run several tool calls in one request, in order, stopping at the first error. Much faster than "
      "separate calls when you already know the steps, e.g. tapping several ui_tree elements or "
      "tap + type_text + press_key. Remember that element indexes go stale once the screen changes.",
      {"actions": {"type": "array", "minItems": 1, "maxItems": 20,
                   "items": {"type": "object",
                             "properties": {"tool": {"type": "string", "description": "Tool name, e.g. tap_element."},
                                            "args": {"type": "object", "description": "Arguments for that tool."}},
                             "required": ["tool"], "additionalProperties": False}},
       "delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 150,
                    "description": "Pause between actions."}},
      ["actions"], destructive=True)
def t_batch(args):
    actions = args.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 20:
        raise ToolError("actions must be a list of 1-20 items.")
    delay = min(5000, max(0, int(args.get("delay_ms") if args.get("delay_ms") is not None else 150))) / 1000
    content, texts = [], []
    for n, action in enumerate(actions, 1):
        name = action.get("tool") if isinstance(action, dict) else None
        a = (action.get("args") or {}) if isinstance(action, dict) else {}
        if name not in TOOLS or name == "batch" or not isinstance(a, dict):
            texts.append(f"[{n}] invalid action: {json.dumps(action, ensure_ascii=False)[:120]}")
            raise ToolError("\n".join(texts) + f"\nStopped at action {n} of {len(actions)}.")
        try:
            for item in run_tool(name, a):
                if item.get("type") == "text":
                    texts.append(f"[{n}] {name}: {item['text']}")
                else:
                    content.append(item)
        except ToolError as e:
            texts.append(f"[{n}] {name} FAILED: {e}")
            raise ToolError("\n".join(texts) + f"\nStopped at action {n} of {len(actions)}.")
        if n < len(actions) and delay:
            time.sleep(delay)
    return content + text_result("\n".join(texts))


# ---------------------------------------------------------------------------
# MCP (JSON-RPC over Streamable HTTP, stateless)
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()
_tool_lock = threading.RLock()     # one tool call at a time: taps must not interleave


def log_action(name, args, ok, ms):
    shown = dict(args)
    if name == "type_text" and "text" in shown:
        shown["text"] = f"<{len(str(shown['text']))} chars>"
    if name == "batch":
        shown = {"actions": len(shown.get("actions") or [])}
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {'OK ' if ok else 'ERR'} {name} "
            f"{json.dumps(shown, ensure_ascii=False)[:300]} {ms} ms\n")
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        with _log_lock, open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def call_tool(name, args):
    if name not in TOOLS:
        return None
    with _tool_lock:
        try:
            return {"content": run_tool(name, args), "isError": False}
        except ToolError as e:
            return {"content": text_result(str(e)), "isError": True}
        except Exception as e:      # unexpected: report it to the model, keep the server running
            return {"content": text_result(f"Unexpected error: {type(e).__name__}: {e}"), "isError": True}


def handle_rpc(msg):
    """Handle one JSON-RPC message. Returns the reply, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in msg:             # notification (e.g. notifications/initialized)
        return None
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        # Only tools are implemented and they are the same in every protocol revision,
        # so the client's requested version is accepted as is.
        return ok({
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "phone-mcp", "title": "Phone control", "version": VERSION},
            "instructions": SERVER_INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [t["spec"] for t in TOOLS.values()]})
    if method == "tools/call":
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return err(-32602, "arguments must be an object")
        result = call_tool(params.get("name"), args)
        if result is None:
            return err(-32602, f"Unknown tool: {params.get('name')}")
        return ok(result)
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, f"Method not found: {method}")


def rpc_error_body(code, message):
    return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "phone-mcp"

    def log_message(self, fmt, *args):
        # The path is never logged: it contains the secret token. The kind is logged instead:
        # mcp / health = correct URL, "other-path" = wrong token or missing /mcp.
        kind = getattr(self, "_kind", None) or "other-path"
        status = args[1] if len(args) > 1 else ""
        sys.stderr.write(f"{stamp()} {self.command} {kind} {status}\n")

    def parse_request(self):
        # Reset per-request state (connections are reused).
        self._kind = None
        self._body_read = False
        return super().parse_request()

    def _path_kind(self):
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        kind = None
        if len(parts) == 2 and hmac.compare_digest(parts[0].encode(), TOKEN.encode()):
            kind = parts[1] if parts[1] in ("mcp", "health") else None
        self._kind = kind
        return kind

    def _read_body(self):
        """Read the request body, with Content-Length or Transfer-Encoding: chunked."""
        self._body_read = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    return None
                try:
                    size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                except ValueError:
                    return None
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass    # trailers
                    return b"".join(chunks)
                total += size
                if size < 0 or total > MAX_BODY:
                    return None
                chunks.append(self.rfile.read(size))
                self.rfile.readline()   # CRLF after the chunk
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        has_body = (self.headers.get("Content-Length") not in (None, "0")
                    or "chunked" in (self.headers.get("Transfer-Encoding") or "").lower())
        if has_body and not self._body_read:
            # An unread body would be parsed as the next request on this connection.
            self.close_connection = True
        self.send_response(code)
        if self.close_connection:
            self.send_header("Connection", "close")
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _detail(self, what):
        """A diagnostic line in server.log (no token, no message contents)."""
        h = self.headers
        sys.stderr.write(
            f"{stamp()}   > {what} | Accept={h.get('Accept') or '-'} | "
            f"MCP-Protocol-Version={h.get('MCP-Protocol-Version') or '-'} | "
            f"UA={(h.get('User-Agent') or '-')[:60]}\n")

    def do_GET(self):
        kind = self._path_kind()
        if kind == "health":
            state = "disconnected"
            try:
                p = subprocess.run([ADB, "-s", SERIAL, "get-state"], capture_output=True, timeout=5)
                if p.returncode == 0:
                    state = p.stdout.decode().strip()
            except Exception:
                pass
            return self._send(200, json.dumps({"ok": True, "adb": state, "version": VERSION}).encode())
        if kind == "mcp":
            # No server-initiated stream is offered; 405 is the spec-compliant answer.
            return self._send(405, extra={"Allow": "POST"})
        return self._send(404)

    def do_DELETE(self):
        return self._send(405 if self._path_kind() == "mcp" else 404, extra={"Allow": "POST"})

    def do_HEAD(self):
        kind = self._path_kind()
        return self._send(200 if kind == "health" else 405 if kind == "mcp" else 404, extra={"Allow": "POST"})

    def do_OPTIONS(self):
        if self._path_kind() is None:
            return self._send(404)
        return self._send(204, extra={"Allow": "GET, POST, OPTIONS"})

    def do_POST(self):
        if self._path_kind() != "mcp":
            return self._send(404)
        raw = self._read_body()
        if not raw:
            self._detail("POST with an empty or unreadable body")
            self.close_connection = True
            return self._send(400, rpc_error_body(-32600, "Bad body"))
        try:
            msg = json.loads(raw)
        except ValueError:
            self._detail(f"POST with invalid JSON ({len(raw)} B)")
            return self._send(400, rpc_error_body(-32700, "Parse error"))
        msgs = msg if isinstance(msg, list) else [msg]
        methods = [str(m.get("method")) for m in msgs if isinstance(m, dict)]
        if "initialize" in methods:
            self._detail("initialize")
        if isinstance(msg, list):
            reply = [r for r in (handle_rpc(m) for m in msg) if r is not None] or None
        else:
            reply = handle_rpc(msg)
        if reply is None:
            return self._send(202)
        payload = json.dumps(reply, ensure_ascii=False).encode("utf-8")
        extra = {}
        if "initialize" in methods:
            # The server is stateless, but some clients expect a session id.
            extra["Mcp-Session-Id"] = uuid.uuid4().hex
        accept = (self.headers.get("Accept") or "").lower()
        if "text/event-stream" not in accept or RESPONSE_MODE == "json":
            body, ctype = payload, "application/json"
        else:
            # Server-sent events, like most MCP servers answer.
            body, ctype = b"event: message\ndata: " + payload + b"\n\n", "text/event-stream"
            extra["Cache-Control"] = "no-cache, no-transform"
        return self._send(200, body, ctype=ctype, extra=extra)


def main():
    if len(TOKEN) < MIN_TOKEN_LEN:
        sys.exit(f"PHONE_MCP_TOKEN is missing or shorter than {MIN_TOKEN_LEN} characters. Start the server with start.sh.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"phone-mcp {VERSION} listening on http://{HOST}:{PORT}/<token>/mcp (ADB {SERIAL})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _session.close()


if __name__ == "__main__":
    main()
