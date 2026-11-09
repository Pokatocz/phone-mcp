#!/usr/bin/env python3
"""
Phone MCP bridge - gives the phone a permanent public URL through the relay Worker.

The phone opens an outbound WebSocket to the Cloudflare Worker in relay/. The Worker
forwards Claude's HTTP requests over that socket; this script passes them to the local
phone_mcp.py server and sends the answers back. It reconnects by itself when the
network changes. Standard library only.

Environment (start.sh loads it from config.env):
  PHONE_MCP_RELAY_URL   Worker URL, e.g. https://phone-mcp-relay.NAME.workers.dev
  PHONE_MCP_TOKEN       the secret token (same as the server's)
  PHONE_MCP_PORT        local server port (default 8765)
"""
import base64
import http.client
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

RELAY = os.environ.get("PHONE_MCP_RELAY_URL", "").rstrip("/")
TOKEN = os.environ.get("PHONE_MCP_TOKEN", "")
PORT = int(os.environ.get("PHONE_MCP_PORT", "8765"))
PING_EVERY = 25        # seconds between keep-alive messages
DEAD_AFTER = 70        # seconds of silence after which the connection is considered dead
PART_CHARS = 200_000   # replies longer than this are sent in parts (WebSocket message size limits)
RETURN_HEADERS = ("content-type", "mcp-session-id", "allow", "cache-control")


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _mask(data, key):
    n = len(data)
    if not n:
        return data
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


class WebSocket:
    """Minimal RFC 6455 client: text messages, fragmentation, ping/pong, close."""

    def __init__(self, url, timeout=20):
        u = urllib.parse.urlparse(url)
        secure = u.scheme in ("wss", "https")
        port = u.port or (443 if secure else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        host = u.hostname if not u.port else f"{u.hostname}:{u.port}"
        sock.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"User-Agent: phone-mcp-bridge\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during the handshake")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("handshake response too long")
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status:
            raise ConnectionError(f"the relay refused the connection: {status}")
        # Keep-alive traffic arrives every PING_EVERY seconds, so a longer silence means a dead link.
        sock.settimeout(DEAD_AFTER)
        self.sock = sock
        self.lock = threading.Lock()
        self.closed = False

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def send(self, payload, opcode=1):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        n = len(payload)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        key = os.urandom(4)
        frame = bytes(header) + key + _mask(payload, key)
        with self.lock:
            self.sock.sendall(frame)

    def recv(self):
        """Returns one complete message as bytes, or None for a pong frame. Answers pings itself."""
        message = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            key = self._recv_exact(4) if b2 & 0x80 else None
            data = self._recv_exact(n)
            if key:
                data = _mask(data, key)
            if opcode == 0x9:
                self.send(data, 0xA)
                continue
            if opcode == 0xA:
                return None
            if opcode == 0x8:
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005
                raise ConnectionError(f"the relay closed the connection ({code} {data[2:].decode('utf-8', 'replace')})")
            if opcode in (0x1, 0x2):
                message = data
            elif opcode == 0x0:
                message += data
            if fin:
                return message

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.send(struct.pack(">H", 1000), 0x8)
        except Exception:
            pass
        for action in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                action()
            except Exception:
                pass


def forward(req):
    """Pass one request from the relay to the local server; returns the reply for the relay."""
    method = req.get("method") or "POST"
    body = (req.get("body") or "").encode("utf-8")
    headers = {k: v for k, v in (req.get("headers") or {}).items() if k != "user-agent"}
    if body:
        headers["content-length"] = str(len(body))
    t0 = time.time()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=140)
        conn.request(method, "/" + TOKEN + (req.get("path") or "/mcp"), body=body or None, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        out_headers = {h: resp.getheader(h) for h in RETURN_HEADERS if resp.getheader(h)}
        status = resp.status
        conn.close()
    except Exception as e:
        status, out_headers = 502, {"content-type": "application/json"}
        data = json.dumps({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32000, "message": f"The server on the phone does not answer: {e}"}})
    log(f"{method} {req.get('path')} -> {status} ({int((time.time() - t0) * 1000)} ms, {len(data)} chars)")
    return {"id": req.get("id"), "status": status, "headers": out_headers, "body": data}


def send_reply(ws, reply):
    body = reply["body"]
    if len(body) <= PART_CHARS:
        ws.send(json.dumps(reply, ensure_ascii=False))
        return
    total = (len(body) + PART_CHARS - 1) // PART_CHARS
    for i in range(total):
        part = dict(reply, body=body[i * PART_CHARS:(i + 1) * PART_CHARS], part=i, parts=total)
        ws.send(json.dumps(part, ensure_ascii=False))


def session(ws):
    stop = threading.Event()

    def keepalive():
        while not stop.wait(PING_EVERY):
            try:
                ws.send("ping")
            except Exception:
                return

    def handle(req):
        try:
            send_reply(ws, forward(req))
        except Exception as e:
            log(f"could not send the reply: {e}")

    threading.Thread(target=keepalive, daemon=True).start()
    try:
        while True:
            raw = ws.recv()
            if raw is None or raw == b"pong":
                continue
            try:
                req = json.loads(raw)
            except ValueError:
                continue
            if isinstance(req, dict):
                threading.Thread(target=handle, args=(req,), daemon=True).start()
    finally:
        stop.set()
        ws.close()


def main():
    if not RELAY or len(TOKEN) < 32:
        sys.exit("PHONE_MCP_RELAY_URL or PHONE_MCP_TOKEN is missing - start the bridge with start.sh.")
    ws_url = RELAY.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + f"/{TOKEN}/phone"
    delay = 1
    while True:
        try:
            ws = WebSocket(ws_url)
            log(f"connected to {RELAY}/<token>/phone")
            delay = 1
            session(ws)
        except socket.timeout:
            log("the relay went silent - reconnecting")
        except Exception as e:
            log(f"relay connection: {e}")
        time.sleep(delay)
        delay = min(delay * 2, 30)


if __name__ == "__main__":
    main()
