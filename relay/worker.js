/**
 * Phone MCP relay - a permanent public URL for phone-mcp on Cloudflare Workers (free plan).
 *
 * The phone (bridge.py in Termux) connects with an outbound WebSocket to  /<TOKEN>/phone
 * Claude calls the connector at                                           /<TOKEN>/mcp
 * The Worker forwards every request to the phone and returns its answer.
 *
 * Each token gets its own Durable Object (named by the SHA-256 of the token), so the
 * Worker stores no secrets: a request with a different token can never reach your phone.
 * The server on the phone checks the token again.
 */

const TIMEOUT_MS = 150_000; // the slowest tool (shell) may take 120 s
const FORWARD_HEADERS = ["accept", "content-type", "mcp-protocol-version", "mcp-session-id", "last-event-id", "user-agent"];
const RETURN_HEADERS = ["content-type", "mcp-session-id", "allow", "cache-control"];
const NULL_BODY = new Set([101, 204, 205, 304]);

function json(status, obj) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}

function rpcError(status, message) {
  return json(status, { jsonrpc: "2.0", id: null, error: { code: -32000, message } });
}

async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env) {
    const parts = new URL(request.url).pathname.split("/").filter(Boolean);
    if (parts.length === 0) {
      return new Response("phone-mcp relay is running.\n", { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    const [token, endpoint] = parts;
    if (parts.length !== 2 || token.length < 32 || !/^[A-Za-z0-9_-]+$/.test(token) || !["mcp", "health", "phone"].includes(endpoint)) {
      return new Response("Not found", { status: 404 });
    }
    const stub = env.PHONE.get(env.PHONE.idFromName(await sha256hex(token)));
    const headers = new Headers(request.headers);
    headers.set("x-bridge-kind", endpoint === "phone" ? "phone" : "http");
    headers.set("x-bridge-path", "/" + endpoint);
    return stub.fetch(new Request(request, { headers }));
  },
};

export class PhoneBridge {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.pending = new Map();
    // The phone's keep-alive "ping" is answered by Cloudflare without waking this object.
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  async fetch(request) {
    if (request.headers.get("x-bridge-kind") === "phone") return this.acceptPhone(request);

    const path = request.headers.get("x-bridge-path") || "/mcp";
    const sockets = this.ctx.getWebSockets();
    if (sockets.length === 0) {
      return path === "/health"
        ? json(503, { ok: false, phone: "disconnected", relay: true })
        : rpcError(503, "The phone is not connected to the relay (is start.sh running in Termux?)");
    }
    const ws = sockets[sockets.length - 1];

    const id = crypto.randomUUID();
    const headers = {};
    for (const h of FORWARD_HEADERS) {
      const v = request.headers.get(h);
      if (v) headers[h] = v;
    }
    const body = request.method === "GET" || request.method === "HEAD" ? "" : await request.text();

    const reply = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("timeout"));
      }, TIMEOUT_MS);
      this.pending.set(id, { resolve, timer, parts: [] });
    });

    try {
      ws.send(JSON.stringify({ id, method: request.method, path, headers, body }));
    } catch {
      const p = this.pending.get(id);
      if (p) clearTimeout(p.timer);
      this.pending.delete(id);
      return rpcError(503, "The connection to the phone was lost");
    }

    let r;
    try {
      r = await reply;
    } catch {
      return rpcError(504, "The phone did not answer in time");
    }
    const out = new Headers({ "x-relay": "phone-mcp" });
    for (const h of RETURN_HEADERS) {
      if (r.headers && r.headers[h]) out.set(h, r.headers[h]);
    }
    const status = Number(r.status) || 502;
    return new Response(NULL_BODY.has(status) ? null : r.body || "", { status, headers: out });
  }

  acceptPhone(request) {
    if ((request.headers.get("upgrade") || "").toLowerCase() !== "websocket") {
      return new Response("Expected WebSocket", { status: 426 });
    }
    // Only one phone connection at a time: close older ones (e.g. after a network change).
    for (const old of this.ctx.getWebSockets()) {
      try { old.close(4000, "replaced"); } catch {}
    }
    const pair = new WebSocketPair();
    this.ctx.acceptWebSocket(pair[1]);
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  async webSocketMessage(ws, message) {
    if (typeof message !== "string") return;
    if (message === "ping") {
      ws.send("pong");
      return;
    }
    let data;
    try {
      data = JSON.parse(message);
    } catch {
      return;
    }
    const p = this.pending.get(data.id);
    if (!p) return;
    // Large replies (screenshots) arrive in parts to stay under WebSocket message limits.
    if (typeof data.parts === "number" && data.parts > 1) {
      p.parts[data.part] = data.body || "";
      if (p.parts.filter((x) => x !== undefined).length < data.parts) return;
      data.body = p.parts.join("");
    }
    clearTimeout(p.timer);
    this.pending.delete(data.id);
    p.resolve(data);
  }

  async webSocketClose(ws, code, reason) {
    try { ws.close(code, reason); } catch {}
  }

  async webSocketError(ws) {
    try { ws.close(1011, "error"); } catch {}
  }
}
