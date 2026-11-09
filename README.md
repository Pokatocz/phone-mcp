# phone-mcp

Let Claude control your Android phone: read the screen, tap, swipe, type, open apps, read notifications.
The server runs **on the phone itself**, so no computer is involved. You type a request in the Claude app and Claude operates the same phone.

- No root. No PC after setup. No extra app to trust besides [Termux](https://termux.dev).
- Works as a **custom connector** in claude.ai and the Claude mobile apps (remote MCP, Streamable HTTP).
- Free **permanent URL** through a tiny Cloudflare Worker, so the connector keeps working across restarts and network changes.
- One Python file, standard library only (Pillow is optional and makes screenshots smaller and faster).

```
Claude app -> Anthropic servers -> relay (Cloudflare Worker) -> Termux on the phone -> ADB -> the phone's screen
```

> **This gives an AI model full control of your phone.** Read [Security](#security) before you use it.
> Tested on one device so far (Xiaomi, Android 16). Expect rough edges on other phones; reports are welcome.

## How it works

Android's *Wireless debugging* lets ADB connect over the network. Termux connects ADB to `localhost`, which gives a
shell with the same powers as a computer plugged in over USB: `input tap`, `uiautomator dump`, `screencap`, `am start`.
`phone_mcp.py` wraps those commands as MCP tools.

Claude's connectors need a public HTTPS URL. `bridge.py` opens an outbound WebSocket from the phone to a Cloudflare
Worker (`relay/`), and the Worker forwards Claude's requests through it. Nothing listens on the phone for the
internet, and no port forwarding is needed.

## Requirements

- Android 11 or newer (Wireless debugging), about 500 MB of free space
- Wi-Fi while turning Wireless debugging on (mobile data is fine afterwards)
- A Claude plan that supports custom connectors
- For the permanent URL: a free Cloudflare account and a computer with Node.js, once, for about five minutes

## Setup

### 1. Install Termux

Install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) or
[GitHub releases](https://github.com/termux/termux-app/releases). The Google Play build is restricted and not supported.

### 2. Get the code and install

In Termux (long-press the screen and choose *Paste* to paste a command, then press Enter):

```sh
pkg install -y git
git clone https://github.com/Pokatocz/phone-mcp ~/phone-mcp
bash ~/phone-mcp/install.sh
termux-setup-storage
```

The installer takes a few minutes. It adds Python, ADB and OpenSSH, and writes `config.env` with a random
secret token. If it stops at a question, press Enter. The last command asks for access to your files; allow it
(it is needed to install the optional keyboard from your Downloads folder).

**You should see:** `Done. Next: turn on Wireless debugging and run ...`

### 3. Connect ADB to the phone itself

1. Enable Developer options: *Settings > About phone*, tap *Build number* seven times
   (Xiaomi: *OS version*; Samsung: *Software information > Build number*). A new *Developer options* menu
   appears in Settings (on some phones under *System* or *Additional settings*).
2. Connect to Wi-Fi, open *Developer options > Wireless debugging* (tap the text, not only the switch) and
   turn it on.
3. Put Termux and Settings side by side: open the recent-apps screen, tap the Termux icon (or long-press its
   card) and choose *Split screen*, then pick Settings. This matters because the pairing dialog closes as soon
   as you switch apps.
4. In Termux run:

```sh
bash ~/phone-mcp/pair.sh
```

Wireless debugging shows **two different ports**, and the script asks for both:

| The script asks for | Where to find it |
| --- | --- |
| Pairing port and six-digit code (first time only) | Tap *Pair device with pairing code*. A dialog shows the code and an address like `192.168.1.23:37215`. Type the number after the colon. |
| Connection port | The main Wireless debugging screen, under *IP address & Port*, e.g. `192.168.1.23:41567`. Again the number after the colon. |

The script tries to detect the ports by itself and only asks when it cannot. If the phone asks
*Allow debugging?*, choose *Always allow*. The script then moves ADB to the fixed port 5555 and tells Android
not to kill Termux in the background.

**You should see:** `ADB connected: localhost:5555` and `Done. Start the server: ...`

Pairing is needed only once. The connection port changes every time Wireless debugging is turned on.

**Xiaomi / Redmi / POCO:** also enable *USB debugging (Security settings)* in Developer options, otherwise taps and
typing are rejected, and *Install via USB* if you want the script to install ADBKeyBoard.

### 4. Unicode typing (optional, recommended)

Plain `input text` only types ASCII. For accented letters, non-Latin scripts and emoji, download the
[ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard/releases) APK to your Downloads folder and run `pair.sh` again;
it installs the APK. The server switches to that keyboard only while it types and then restores yours.

### 5. Battery settings

Give Termux unrestricted battery use (*Settings > Apps > Termux > Battery > Unrestricted*). On Xiaomi also enable
*Autostart* and lock the Termux card in the recent-apps screen. Do not swipe Termux away.

### 6. Start

```sh
bash ~/phone-mcp/start.sh
```

**You should see** `=== Running ===` followed by the **connector URL**, `https://.../<token>/mcp`.
To copy it, long-press in Termux and choose *More > Select URL*; `cat ~/phone-mcp/connector-url.txt` prints it
again. To check that everything is up, open the URL in a browser with `/health` in place of `/mcp`; it should
show `{"ok": true, "adb": "device", ...}`.

Without a relay this is a temporary [localhost.run](https://localhost.run) address that changes on every start.
That is fine for a first try; for daily use set up the permanent URL below.

### 7. Add the connector to Claude

Custom connectors are added on the claude.ai website (a phone browser works), not inside the mobile app.
Once added, the connector is available in the apps too.

1. Open [claude.ai/customize/connectors](https://claude.ai/customize/connectors).
2. Choose **Add** > **Add custom connector**, give it a name such as `Phone` and paste the connector URL.
3. When asked about authentication, choose **No sign in**. The token in the URL is the key.
4. Open the new connector's tool permissions. Allow the tools you are comfortable with and keep `shell` and
   `batch` on *Needs approval*.
5. Start a **new** chat, enable the connector there (the **+** button > *Connectors*) and try:

- "What's the state of my phone?"
- "Read my notifications."
- "Open Settings and tell me how much storage is free."

## Permanent URL

Deploy the Worker once from a computer with [Node.js](https://nodejs.org) installed. Get the code there
(`git clone https://github.com/Pokatocz/phone-mcp`, or *Code > Download ZIP* on GitHub), open a terminal in the
`relay` folder and run:

```sh
npx wrangler login
npx wrangler deploy
```

The first command opens a browser to sign in to Cloudflare (a free account is enough).

On Windows PowerShell use `npx.cmd` instead of `npx`. The deploy prints a URL like
`https://phone-mcp-relay.NAME.workers.dev`. On the phone:

```sh
bash ~/phone-mcp/set-relay.sh https://phone-mcp-relay.NAME.workers.dev
bash ~/phone-mcp/start.sh
```

Add the printed URL to Claude one last time. It now stays the same after restarts, and the phone reconnects by
itself when it moves between Wi-Fi and mobile data. The Worker fits in Cloudflare's free plan. See
[relay/README.md](relay/README.md) for how it works.

## Daily use

| Situation | What to do |
| --- | --- |
| Not using it | `bash ~/phone-mcp/stop.sh` |
| Use it again | `bash ~/phone-mcp/start.sh` |
| Phone rebooted | Turn on Wireless debugging, then `bash ~/phone-mcp/pair.sh && bash ~/phone-mcp/start.sh` (press Enter at the pairing question) |
| Update to a new version | `cd ~/phone-mcp && git pull && bash restart-server.sh` (the URL stays the same) |
| Remove everything | `bash ~/phone-mcp/stop.sh`, delete the connector in Claude, `rm -rf ~/phone-mcp`, turn Wireless debugging off |
| The URL leaked | `bash ~/phone-mcp/new-token.sh`, start again, re-add the connector |

Android turns Wireless debugging off on reboot, so the pairing script has to run again after each reboot.
Leaving Termux is fine; closing it (`exit`, swiping it away) stops the server.

## Tools

| Tool | What it does |
| --- | --- |
| `phone_status` | Screen on/off, lock state, foreground app, battery, display size |
| `ui_tree` | The screen as a numbered list of elements with coordinates (2-4 s) |
| `screenshot` | A downscaled JPEG of the screen (about 0.5 s) |
| `tap`, `tap_element`, `tap_text` | Tap coordinates, an element from `ui_tree`, or an element found by text |
| `swipe`, `scroll` | Drag between two points; scroll the content |
| `type_text` | Type into the focused field |
| `press_key` | Back, home, recents, volume, media keys, notification shade, quick settings |
| `open_app`, `list_apps` | Launch an app by name or package; list installed packages |
| `open_url` | Open a web page or deep link (`tel:`, `sms:`, `geo:`, ...) |
| `notifications` | Read current notifications |
| `wait` | Sleep, or wait until a text appears |
| `shell` | Any command in the ADB shell |
| `batch` | Several tool calls in one request |

Two things keep it fast. Action tools take `observe: true` and then return the new screen in the same call, and
`batch` runs a known sequence (say, four keypad taps) in one round trip instead of four. Each round trip through
the model costs far more time than the tap itself.

## Security

- **The connector URL is the key to your phone.** Anyone who has it can do anything ADB can: read the screen,
  install apps, read notifications. Do not paste it into chats, screenshots or issue reports. Chat apps fetch link
  previews, which already counts as leaking it.
- The token is 256 bits of randomness, compared in constant time, and never written to the logs.
- The relay stores nothing. It routes by a hash of the token, and the phone checks the token again.
- Stop the server when you do not need it. `new-token.sh` invalidates an old URL at once.
- Every action is appended to `actions.log` (typed text is recorded as a length only).
- The model is instructed to ask before sending, buying, deleting or changing settings, and never to type
  credentials. That is an instruction, not a guarantee. Keep risky tools on *needs approval* in Claude.
- Web pages, messages and notifications that Claude reads on the phone can contain text written to manipulate it
  (prompt injection). Treat a session that browses untrusted content with the same care as running unknown code.
- `adb tcpip 5555` leaves ADB listening on the network until the next reboot. Other devices still need your
  on-screen approval to connect; never approve a debugging prompt you did not expect.
- Apps that set `FLAG_SECURE` (banking, password managers) return black screenshots.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Claude: "Couldn't reach the server" while `server.log` shows `POST mcp 200` | You are using a `trycloudflare.com` quick tunnel. Claude's connector does not work with those addresses. Use the relay or the built-in localhost.run tunnel. |
| Claude cannot connect after a restart | The temporary URL changed. Remove the connector and add the new URL, or set up the permanent URL. |
| "ADB is not connected" | Wireless debugging is off (reboot or network change). Run `pair.sh`. |
| `pair.sh`: "Could not connect on port ..." | The port changes whenever Wireless debugging is turned on. Read it again from the main Wireless debugging screen, not from the pairing dialog. |
| `SecurityException ... INJECT_EVENTS` (Xiaomi) | Enable *USB debugging (Security settings)*. |
| `[Process completed (signal 9)]` in Termux | Android killed Termux. Check the battery settings, run `pair.sh` and `start.sh` again. |
| Text is typed without accents | Install ADBKeyBoard (step 4). |
| Black screenshot | The screen is off, or the app blocks screenshots. |
| An element cannot be found | Games, maps and web views expose no UI tree. Ask Claude to use a screenshot. |
| Relay health shows `"phone": "disconnected"` | `bridge.py` is not running or has no network. Run `start.sh`; check `bridge.log`. |

Logs live in `~/phone-mcp`: `server.log` (requests, no secrets), `bridge.log`, `tunnel.log`, `actions.log`.
`https://<your-url>/<token>/health` shows whether the server and ADB are up.

## Development

```sh
python3 tests/run_tests.py
```

runs the server against a fake `adb` and checks the protocol, every tool and the fallbacks. No phone needed.

Configuration lives in `config.env`; the variables are documented at the top of `phone_mcp.py`.

## Limitations

- `ui_tree` relies on `uiautomator dump`, which waits for the screen to settle: 2-4 s, longer on animated screens.
- Wireless debugging must be re-enabled after every reboot.
- A PIN-locked phone cannot be unlocked by the server.
- The temporary localhost.run tunnel drops now and then; the relay does not have this problem.

## License

MIT, see [LICENSE](LICENSE).
