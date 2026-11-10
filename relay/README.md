# phone-mcp relay

A Cloudflare Worker that gives the phone a permanent public URL. It runs on the free plan.

## Deploy

You need Node.js and a free Cloudflare account.

```sh
npx wrangler login
npx wrangler deploy
```

On Windows PowerShell use `npx.cmd` instead of `npx`. The deploy prints the Worker URL,
`https://phone-mcp-relay.NAME.workers.dev`. Opening it in a browser shows `phone-mcp relay is running.`

Then on the phone:

```sh
bash ~/phone-mcp/set-relay.sh https://phone-mcp-relay.NAME.workers.dev
bash ~/phone-mcp/start.sh
```

## Update

Run `npx wrangler deploy` again from this folder (add `--name my-name` if you deployed it under another name).
The URL stays the same; the phone reconnects within a few seconds. Updating needs no `wrangler login` if you
are still logged in on that computer.

Without a computer: in the Cloudflare dashboard open *Workers & Pages*, your Worker, *Edit code*, replace the
content of `worker.js` with this folder's `worker.js` and press *Deploy*.

## How it works

| Path | Who calls it | Purpose |
| --- | --- | --- |
| `/<token>/phone` | `bridge.py` on the phone | WebSocket the phone keeps open |
| `/<token>/mcp` | Claude | MCP requests, forwarded to the phone |
| `/<token>/health` | you | `200` with the phone's status, `503` when the phone is not connected |

Each token maps to its own Durable Object, named by the SHA-256 of the token. The object holds the phone's
WebSocket (with hibernation, so an idle connection costs nothing) and matches replies to requests by id.
Requests go to the newest connection that is still open and still sending its keep-alive pings, so a leftover
connection (after a network change, or a bridge that was killed) can never block the current one.
Replies larger than 200,000 characters arrive in parts and are joined before they are returned.

The Worker has no secrets and no storage. A request with the wrong token reaches an object with no phone attached
and gets `503`; the server on the phone verifies the token again for every request.
