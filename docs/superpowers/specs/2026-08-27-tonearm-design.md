# tonearm — design

**Date:** 2026-08-27
**Status:** approved, not yet implemented
**Scope:** a Roon now-playing and transport widget for the Omarchy shell, plus the
Python sidecar that speaks Roon's protocol.

`tonearm` is the widget. `tonearmd` is the daemon. The name is settled: the tonearm
is the small precise arm that rides the record, tiny next to the deck it is attached
to, and the part you actually touch. That is this widget's relationship to the Core.

---

## 1. What this is

A bar module that shows what is playing, and a popup that controls it. Backed by a
daemon because Roon has no REST API — discovery is SOOD over UDP, transport is MOO, a
bespoke framing over WebSocket — so a QML-only shell config cannot talk to it.

**Not** a TUI. **Not** a separate app window. **Not** a library browser.

---

## 2. Environment, verified

Everything below was measured on this machine on 2026-08-27, not inferred.

| Thing | Value |
|---|---|
| Shell | Omarchy shell — `quickshell -n -p /usr/share/omarchy/shell` |
| Quickshell | 0.3.1 (Arch) |
| System Python | 3.14.7 at `/usr/bin/python` |
| Roon Core | **`yavin`** — ROCK appliance, Roon 2.71 build 1683, `192.168.50.118` |
| Core ports | **`9330` MOO/WS *and* HTTP images** · `9003` SOOD · `55000` HTTPS (403) · `9150` advertised but dead |

### 2.1 The corrections that shaped this design

**This is an Omarchy shell plugin, not a bespoke Quickshell component.** The bar is
Omarchy's, with a plugin system at `~/.config/omarchy/plugins/`. `colophon`, `galley`
and `headway` are the precedent for structure, naming and theming; their conventions
are load-bearing here and are not re-litigated in this document.

**Multicast SOOD discovery does not work on this LAN.** Repeated queries to
`239.255.90.90:9003` and to the broadcast address drew no reply. A unicast query to
`192.168.50.118:9003` answered immediately with the Core's name, version and ports.
The AP filters multicast. **`RoonDiscovery` will not find this Core**, so the host is
configuration, not a discovery result. See §7.2 for the fallback that does work.

**The album-art shortcut is CONFIRMED, not merely plausible.** After pairing, real
`image_key`s were fetched over plain HTTP: `GET :9330/api/image/<key>?scale=fit&width=256&height=256`
returned `200`, `image/jpeg`, a valid 256×256 baseline JPEG of the actual cover, with
no authentication. Cover art in QML is a plain `Image { source: url }`.

**The MOO WebSocket is on port 9330, NOT the 9150 that SOOD advertises.** This is the
single most expensive finding of the build, and it is why the extension never appeared
in Roon Remote for the first several attempts. Measured with raw WebSocket upgrade
requests against Roon 2.71 (build 1683):

| Endpoint | Result |
|---|---|
| `ws://host:9150/api` | TCP connects, then **no handshake response at all** — hangs |
| `ws://host:9330/api` | **`HTTP/1.1 101 Switching Protocols`** |
| `wss://host:55000/api` | `403 Forbidden` |

`roonapi` 0.1.6 dials SOOD's `tcp_port` (9150). On this Roon version that port accepts
TCP but never answers, so `RoonApi.__init__` blocks forever with `blocking_init=True`,
the registration request is never delivered, and the Core therefore has no extension to
show — presenting to the user as "no extensions discovered" with no error anywhere.
Registration over 9330 succeeded immediately and returned a token.

**`tonearmd` must therefore dial `http_port` for MOO, falling back to `tcp_port`** for
older Cores where the advertised port is the live one. Do not trust SOOD's `tcp_port`
alone, and never let a connect attempt block unboundedly — the failure is silent.

**`roonapi`'s dependency metadata is over-declared.** It names `requests`, `six`,
`ifaddr` and `websocket-client`. Its only **required** third-party import is
`websocket`, which is already present in system Python. `roonapisocket.py:10`
also imports `simplejson`, but inside a `try`/`except` with a fallback to stdlib
`json`, so it is optional and adds no dependency. (An earlier draft of this
document said "imports only `websocket`" — that came from a grep anchored at
line start, which missed every indented import inside a `try` block. The
conclusion stands; the wording did not.)

**`Quickshell/ColorQuantizer` exists** and takes a `source` URL, returning a `colors`
palette with `depth` and `rescaleSize` controls. This is what makes the visual
direction affordable. It is used nowhere in the Omarchy shell — treat it as
live-shell-verify-only.

**Omarchy already arbitrates media keys.** `XF86AudioPlay/Pause/Next/Prev` route
through `omarchy-shell media …`, backed by `Quickshell.Services.Mpris`, and
`SHIFT + XF86AudioPlay` switches media source. Binding those keys directly to tonearm
would hijack them from every other player. MPRIS is therefore the correct integration
point, not a luxury.

---

## 3. Decisions

| Question | Decision |
|---|---|
| MVP scope | Now-playing, play/pause/next/prev, **seek, volume, zone switching** |
| Daemon lifecycle | **systemd user service** + `tonearmctl` relay over `Process` |
| Media keys | **MPRIS in `tonearmd`, in the MVP** |
| Multi-zone | **Auto-follow the most recently started zone; a pin overrides** |
| Failure display | **Never hide.** Severity glyph + tooltip, per the house `barState` contract |
| Logic split | **Thin daemon, fat `Model.js`** |
| Visual direction | **Theme chrome; album-art accent on the seek fill only** |
| Bar module | Fixed-width art thumbnail + play state. **No track title** — unbounded width |
| Dependencies | System packages + **vendored `roonapi`**. No venv |
| `zones` in payload | Every push |
| Low-contrast cover | Fall back to the theme accent |

---

## 4. Architecture

```
systemd --user
└── tonearmd.service          (Restart=on-failure, journald)
    ├─ pyroon ──MOO/WS──▶ Roon Core  "yavin" 192.168.50.118:9150
    ├─ MPRIS  ──D-Bus──▶ org.mpris.MediaPlayer2.tonearm
    │                     └─▶ omarchy media service → XF86Audio* keys, OSD, source switcher
    └─ unix socket  $XDG_RUNTIME_DIR/tonearm/sock
           ▲                                    ▲
           │ NDJSON state (persistent)          │ one JSON command, one exit
           │                                    │
   Process[scripts/tonearmctl subscribe]  Process[scripts/tonearmctl playpause]
           │                                    │
           └────────────┬───────────────────────┘
                        │
              ssandys.tonearm — Service.qml · Model.js · Panel.qml
                        │
                        └─ album art: Image { source: "http://192.168.50.118:9330/api/image/<key>?…" }
```

Three units, not two. `tonearmctl` is what makes the systemd path affordable: a plugin
hot-reload kills only a thin relay, never the Roon connection.

| Unit | Owns | Knows nothing about |
|---|---|---|
| `scripts/tonearmd` | Roon connection, pairing token, zone subscription, reconnect, zone arbitration, MPRIS | QML, display strings |
| `scripts/tonearmctl` | Socket client. `subscribe` streams NDJSON; any other verb sends one command and exits | Roon, rendering |
| `ssandys.tonearm` | Rendering, formatting, seek interpolation, severity, accent choice | Roon, MOO, D-Bus |

### 4.1 Why one-shot commands

`Process.command` assigned mid-run is silently ignored, and a failed spawn never emits
`exited()` — both recorded in `headway/AGENTS.md`. One process per button press
sidesteps both: nothing is reassigned, and `onRunningChanged` is the drain signal.

---

## 5. The interface

### 5.1 State — one JSON line per change on `tonearmctl subscribe` stdout

```json
{ "v": 1,
  "status": "ok",
  "core":  { "host": "192.168.50.118", "http_port": 9330, "name": "yavin" },
  "zone":  { "id": "1601…", "name": "Living Room", "state": "playing", "pinned": true,
             "volume": { "value": 62, "min": 0, "max": 100, "step": 1, "muted": false },
             "position": 271, "length": 585,
             "now_playing": { "title": "Blue Train", "artist": "John Coltrane",
                              "album": "Blue Train", "image_key": "a1b2…",
                              "art_path": "/run/user/1000/tonearm/art/a1b2….jpg" } },
  "zones": [ { "id": "1601…", "name": "Living Room", "state": "playing" },
             { "id": "77c2…", "name": "Study",       "state": "stopped" } ] }
```

`status` ∈ `connecting · unpaired · unreachable · ok`.
`zone.state` ∈ `playing · paused · stopped · loading`. `zone` is `null` when there is
no followed zone.

Three deliberate choices:

- **`image_key`, not a URL.** Keeps the daemon out of presentation and lets the widget
  request `width=256` for display and `width=64` for the quantizer off the same key.
- **`status` is orthogonal to `zone.state`.** One is about the daemon and Core, the
  other about the music. Collapsing them makes "broken or just idle?" unanswerable.
- **No timestamp in the payload.** `Service.qml` stamps arrival on receipt, so
  `Model.js` interpolates against a clock it owns. No cross-process clock assumption,
  and the seek math stays pure.

`zones` ships in every push. It is small, and always sending it removes a class of
staleness bug rather than trading it for bytes.

### 5.2 Commands — one JSON object in, process exits

```
playpause · play · pause · next · previous
seek <seconds>            absolute
volume <n> · mute · unmute
zone pin <id> · zone unpin
subscribe                 stream state until killed
discover · status         setup-time; status backs setup.sh --check
```

There is no `pair` verb. Pairing needs no command: `RoonApi` blocks until the
extension is enabled by hand in Roon Remote, so the daemon simply reports
`status: "unpaired"` until it succeeds.

Exit codes from `tonearmctl`: `0` ok, `2` usage, `3` daemon not running.
`Service.qml` distinguishes a dead daemon from a bad invocation by that code and
backs off rather than respawning in a tight loop.

---

## 6. Module decomposition

```
~/Src/tonearm/
├── manifest.json          kinds:["bar-widget"], entryPoints.barWidget:"Panel.qml",
│                          barWidget.defaults + barWidget.schema
├── Panel.qml              bar button + popup. Rendering only.
├── Service.qml            I/O only: subscribe Process, one-shot commands, arrival stamping
├── Model.js               ALL logic. Dual-loadable under node and Qt V4.
├── scripts/
│   ├── tonearmd           the daemon        (#!/usr/bin/python)
│   ├── tonearmctl         socket client
│   └── vendor/roonapi/    5 files, Apache-2.0, LICENSE and NOTICE retained
├── systemd/tonearmd.service
├── setup.sh               dependency check, unit install, --check health mode
├── bin/{dev,dev-watch,test}
├── tests/
├── AGENTS.md  README.md  LICENSE  preview.png
└── docs/
```

`bin/dev` and `bin/dev-watch` are copied **byte-identical** from galley and derive
plugin identity from `manifest.json` at runtime. `tests/manifest.test.js` asserts
neither contains a plugin-specific literal outside a comment. `bin/test` gains one
stanza — `python -m unittest` — alongside the existing manifest, `bash -n`, `qmllint`
and `node --test` steps.

### 6.1 `Model.js` surface

Every function pure, every one reachable from `node --test`.

| Function | Returns |
|---|---|
| `barState(state, recvMs, nowMs)` | `{ severity, glyph, showArt }` — `glyph` varies with play state (playing / paused / idle / fault); `showArt` is false when there is no `image_key` |
| `tooltipText(state)` | `"Blue Train — John Coltrane · Living Room"` |
| `artUrl(state, px)` | the `:9330` URL for a given pixel size |
| `position(zone, recvMs, nowMs)` | interpolated seek, clamped to `length` |
| `formatTime(sec)` / `formatRemaining(...)` | `"4:31"` / `"−6:14"` |
| `zoneList(state)` | ordered zones, pinned first |
| `pickAccent(colors, bgHex)` | most saturated palette entry clearing a contrast floor, else the theme accent |
| `nextRetryDelay(attempt)` | relay respawn backoff, capped at 30s |

### 6.2 Settings schema

`barWidget.defaults` and `barWidget.schema` in `manifest.json`, following galley's and
headway's shape. tonearm is push-based, so unlike them it carries **no poll intervals**.

| Key | Type | Default | Label |
|---|---|---|---|
| `accentFromArt` | boolean | `true` | Tint the seek bar with the album's color |
| `artSizePx` | integer 96–256, step 8 | `118` | Album art size in the popup |
| `showVolume` | boolean | `true` | Show the volume slider |
| `notifyCoreUnreachable` | boolean | `true` | Notify when the Roon Core becomes unreachable |
| `notifyZoneChange` | boolean | `false` | Notify when the followed zone changes |

Everything the *daemon* needs — Core host and port, pairing token, pinned zone — lives
in `~/.config/tonearm/config.json`, not here. The daemon cannot read `shell.json`, and
splitting its configuration across both would be the same mistake as §7.1's pin.

### 6.3 Inherited invariants

From `headway/AGENTS.md`, all of which apply unchanged:

- **No mutable module state in `Model.js`.** Without `.pragma library` every importing
  component gets its own instance; `.pragma library` in turn makes node refuse to parse
  the file. State is passed per call. A cache or memo table reintroduces the bug and no
  test in the repo will catch it.
- **QML-safe ES3-ish subset**: no arrow functions, spread, template literals,
  `let`/`const`, `Object.assign`, `.includes(`, `.endsWith(`. `String.fromCodePoint` is
  the verified exception, and is how `BAR_GLYPH` must be built rather than typed.
- **`hasOwnProperty` on any table keyed by upstream data.** A bare `TABLE[key]` walks
  the prototype chain; `"constructor"` returns a truthy inherited member.
- **Total-order comparators.** Qt's V4 `Array.prototype.sort` is not stable and node's
  is, so no node test can catch a partial order. `zoneList` carries an explicit
  tie-breaker on zone id.
- **Declare `implicitWidth`/`implicitHeight`** on the root, and `fontFamily`, `dim`,
  `barIcon` — `Ui/Panel.qml` provides none of them, and the failure mode is a widget
  that renders nothing with nothing logged.
- **`StdioCollector` needs `waitForEnd: true`**, and bare `text`, not `this.text`.
- **One `Component.onCompleted` per component.** A duplicate makes the component fail
  to instantiate silently.
- **`qmllint` exiting 0 is no information.** It has passed a `Panel.qml` that could not
  parse. Verify with a brace-balance count, `./bin/dev up`, the journal, and by looking
  at it.

---

## 7. Behavior

### 7.1 Zone arbitration

Follow the most recently started playing zone. Selecting a zone in the popup **pins**
it; the bar stays there until unpinned. With nothing pinned and nothing playing, `zone`
is `null` and the bar shows the idle state.

**The daemon owns the pin.** The widget sends `zone pin <id>` and renders whatever
`zone.pinned` comes back as; it never stores the pin itself. Persisting it in the
plugin's `shell.json` entry instead would split arbitration across two processes and
leave MPRIS — which has no access to plugin settings — following a different zone than
the bar. It lives in `~/.config/tonearm/config.json` with the host and token.

"Most recently started" is tracked by the daemon as the transition into `playing`, not
by wall-clock position, so a long-running zone does not outrank one you just started.

### 7.2 Discovery

`tonearmctl discover` runs three steps and stops at the first that answers:

1. Multicast SOOD to `239.255.90.90:9003`.
2. On silence, TCP-scan the local `/24` for an open `:9330`.
3. Unicast SOOD to each hit on `:9003` to confirm identity and read name and ports.

This is the exact sequence that located `yavin`; step 1 fails on this network and steps
2–3 succeed. Result persists to `~/.config/tonearm/config.json`, the token beside it.

**Step 2 must not derive "the local /24" from a routing trick.** The obvious
implementation — open a UDP socket, `connect()` to an arbitrary address, and read
back `getsockname()` — returns the **Tailscale** address on this machine, not the
LAN address. Measured: it yields `100.94.206.126`, because `ip route get` for any
off-link destination resolves via `tailscale0 table 52` while an exit node is
active. The scan would then sweep `100.94.206.0/24` and silently find nothing.
Enumerate interface addresses directly instead. This is a second, independent
environmental hazard from the multicast filtering above, and it is invisible
until an exit node happens to be enabled.

### 7.3 Pairing

A one-time manual step. First run registers the extension; it is enabled by hand in
Roon Remote → Settings → Extensions; the token then persists. Until that happens
`status` is `unpaired` and the tooltip says so.

### 7.4 Failure states

| Condition | `status` | Severity | Bar module | Tooltip |
|---|---|---|---|---|
| Socket missing / daemon down | relay exits | `error` | glyph, error tint | `tonearmd not running` |
| Daemon up, no token | `unpaired` | `warn` | glyph | `Enable tonearm in Roon → Settings → Extensions` |
| Paired, Core not answering | `unreachable` | `error` | glyph | `Roon Core unreachable` |
| Connected, nothing playing | `ok` | `ok` | dim glyph, no art | `Nothing playing · yavin` |
| Playing | `ok` | `ok` | art thumbnail + state | `Blue Train — John Coltrane · Living Room` |

**The trap this design creates.** With `tonearmd` down, `tonearmctl subscribe` exits
immediately, and a `Process { running: true }` that respawns on exit becomes a tight
fork loop. `Service.qml` respawns on `nextRetryDelay(attempt)`, capped at 30s. The
drain signal is `onRunningChanged`, not `onExited`.

**MPRIS is not load-bearing.** If D-Bus registration fails, `tonearmd` logs it and
keeps serving the socket: the widget works, only the media keys do not. On losing the
Roon connection the MPRIS name is withdrawn rather than left advertising a stale track.

### 7.5 Visual

Popup chrome, text, transport buttons and zone rows all come from `colors.toml`.
Exactly one element takes its color from the cover: the seek fill.

The accent pipeline is `ColorQuantizer` → `Model.pickAccent(quantizer.colors,
background)`. Extraction is a C++ primitive; the *decision* — which entry, and the
contrast floor against `#0c0b0c` — is a pure function taking an array of colors and
returning a hex string, testable without a shell, a Core or an image. Covers that
clear no entry fall back to the theme accent.

**`ColorQuantizer` cannot load a remote URL.** Measured in a live Quickshell
probe: pointed at `http://<core>:9330/api/image/<key>` it emits **zero** colors;
pointed at the identical bytes as a local `file://` it emits eight. A plain
`Image` loads the same remote URL fine (`status: Ready`, 256×256) — so this
affects only the accent extraction, not the art display.

Left unfixed, direction C would silently do nothing: `pickAccent` would receive an
empty array on every track and always return the theme accent, presenting as "the
feature was never implemented" with no error anywhere.

**Therefore `tonearmd` caches a small copy of the cover** to
`$XDG_RUNTIME_DIR/tonearm/art/<image_key>.jpg` when the track changes, and the
payload carries `art_path` (nullable) beside `image_key`. The widget then uses:

- `Image.source` → the **remote** `:9330` URL, at display resolution
- `ColorQuantizer.source` → `file://` + `art_path`, a 64px copy

This keeps the daemon owning I/O and the widget owning rendering: the daemon
emits a *path*, never a rendering decision. `art_path` is null when there is no
art or the fetch failed, and the widget falls back to the theme accent — the same
path a low-contrast cover already takes.

Also measured: `String(color)` from `ColorQuantizer` yields `#rrggbb` (7 chars)
with alpha 1.0, not the `#aarrggbb` that `normalizeHex` also tolerates. The
defensive alpha-stripping is harmless and stays.

The bar module is a fixed-width **state glyph only** — no text and no album art.
Title and artist live in `tooltipText()`. The right-hand bar section already carries
18 widgets; an unbounded track title does not belong there.

**Revised after seeing it running.** The design originally called for a small cover
thumbnail beside the glyph. Built and deployed, it did not survive contact with the
real bar: at a ~16px icon slot inside a 40px bar the thumbnail is roughly 10px of
image and reads as a coloured blur, not as art. It was removed at the user's
request once they saw it. Album art still appears in the popup at `artSizePx`
(118 by default), where it has room to be legible, and `Model.artUrl` remains for
that use. This is the one decision in this document that was settled by looking
rather than by reasoning.

---

## 8. Dependencies and install

| Need | Source |
|---|---|
| `websocket-client` | already in system Python |
| `dbus-next` | `omarchy pkg add python-dbus-next` — official `extra`, 0.2.3-8 |
| `roonapi` 0.1.6 | **vendored** into `scripts/vendor/roonapi/` |

Arch does not package `roonapi`. The AUR carries 0.1.4 — behind upstream, zero votes,
untouched since May 2023 — so depending on it means adopting it. Vendoring pins the
version, needs no network at install, and costs 1,599 lines of Apache-2.0 code in-tree
whose LICENSE and NOTICE must be retained.

This is a deliberate, documented break from headway's *zero runtime dependencies*
invariant. Roon's protocol makes it unavoidable. `stappmus.audio` is the precedent for
a Python daemon in a plugin: system packages under `/usr/bin/python`, a `systemd/` unit,
and a `setup.sh` that collects missing dependencies into a `missing+=()` array and names
them. tonearm follows that shape, including `setup.sh --check`.

`setup.sh` writes `~/.config/systemd/user/tonearmd.service` and runs
`systemctl --user enable --now`. Run once.

---

## 9. Testing

| Layer | Tested by | Covers |
|---|---|---|
| `Model.js` | `node --test` | severity, tooltip, seek interpolation, `artUrl`, `pickAccent`, zone ordering, backoff |
| `tonearmd` | `python -m unittest` (stdlib, no pytest) | zone arbitration, state normalization, reconnect backoff, MPRIS metadata mapping |
| `tonearmd` end-to-end | recorded-fixture Core | the state machine without a live Roon |
| `Panel.qml`, `Service.qml`, `ColorQuantizer` | **live shell only** | rendering, `Process`, quantizer behavior |

Every decision worth testing belongs in `Model.js` or in `tonearmd`'s pure functions.
`Service.qml` and `Panel.qml` hold only I/O and rendering, precisely so the unverifiable
surface stays small. When adding logic, decide which side of that line it belongs on
before writing it.

---

## 10. Out of scope

**Library browse.** Roon's Browse service is session-stateful and not addressable:
`browse_browse({pop_all, hierarchy})`, then `browse_load()` to page, then
`browse_browse({item_key})` to descend, with no stable identifiers to jump to. It forces
a navigation-stack state machine and is the bulk of roon-tui's 913-line `app/mod.rs`.
The real Roon remote handles discovery; tonearm does now-playing and transport.

Also out: queue view, shuffle and repeat toggles, Roon Radio thumbs up/down, zone
grouping and ungrouping, multiple Cores.

---

## 10a. Post-MVP directions, and what they imply

Two features are on the radar: **search** (pick music and albums to play from the
widget) and an **MCP server** (control Roon from an LLM). Neither is scheduled.
Recorded here because both bear on architecture, and the constraints are not
obvious from the code.

### The load-bearing constraint: one Roon client

**Roon pairing is per-extension.** A second independent Roon client means a second
entry in Roon Remote → Settings → Extensions, a second manual approval, and a
second token to manage. So `tonearmd` stays the *only* thing that talks to Roon,
and everything else — the widget, an MCP server, anything later — is a **consumer
of `tonearmd`'s socket**. This is not a stylistic preference; it is what keeps the
pairing story to one click.

### Search belongs in `tonearmd`, and does not force a repo split

`roonapi` provides `browse_browse`, `browse_load`, `list_media` and `play_media`,
so the capability is there. Browse state is **connection-scoped**, so whoever owns
the Roon connection must own the browse session — that is `tonearmd`. Search
becomes new socket verbs; the widget consumes them exactly as it consumes
transport today. Same shape as the current design.

**The trap: the vendored `roonapi` has no `multi_session_key`.** There is exactly
one browse session per `RoonApi` instance. So two concurrent consumers *will*
clobber each other's navigation — you are part-way down an album list in the
popup, the LLM runs a search, your list resets under you. Upstream MOO supports
multiple browse sessions; the Python library does not expose it. Solving this
means either serialising browse access in `tonearmd`, or adding session-key
support to the vendored library. **A repo split does not fix it** — it is a
concurrency problem inside the daemon, and it is the first thing to design before
either feature ships.

Note also that §10's reason for excluding browse still stands: it is
session-stateful and not addressable, and it is the bulk of roon-tui's
913-line state machine. Search is the expensive feature, not the cheap one.

### MCP is what should trigger a repo split

`omarchy plugin add <git-url>` does a `git clone` and moves **the whole repo** into
`$PLUGINS_DIR/<id>`. The repo *is* the plugin directory. So an MCP server living
here means every plugin user clones an MCP server they will never run, alongside
its dependencies. That is the strongest argument for separating.

Recommended shape when the time comes:

- `tonearm` — the plugin: manifest, QML, and (for now) the daemon.
- a second repo — the MCP server, talking to the same `$XDG_RUNTIME_DIR/tonearm/sock`.

Splitting the **daemon** out of the plugin repo is a separate question and should
be resisted for now: today's one-command `setup.sh` install depends on
co-location, and `stappmus.audio` is precedent for a daemon shipping inside a
plugin. Revisit only if the daemon acquires consumers that have nothing to do with
the bar.

### A third idea: a product icon in the bar

Replace the bar's state glyph with an icon that identifies *tonearm* rather than
playback state. Two candidates already present in VictorMono Nerd Font, both of
which render cleanly at bar size and were checked against the theme:

    U+0EFBD  fa-record_vinyl    a record with a centre label
    U+F0025  md-album
    U+0EDE9  fa-compact_disc

**What it would fix.** Two things, one of them a tradeoff already knowingly
accepted. First, §7.5 records that removing the cover-art badge left the widget
visually indistinguishable from its eighteen monochrome neighbours until you learn
its position; a record icon restores that distinctiveness without the
unreadable-at-10px problem the art had. Second, it sidesteps a small ambiguity
noticed in use: the bar is a **status indicator** (paused ⇒ pause bars) while the
popup's button is an **action** (paused ⇒ offers play), so the same two glyphs
mean opposite things a hundred pixels apart. Both readings are individually
conventional and the ambiguity was judged not worth fixing on its own — but a
product icon removes it as a side effect.

**What it would cost.** The bar stops showing play/paused at a glance. That state
would live only in the tooltip and the popup, unless it is carried some other way
— colour, a small overlay, or an opacity change between playing and idle. Decide
that deliberately rather than discovering it after the swap: the severity colouring
(`ok`/`warn`/`error`) must survive regardless, since the failure states in §7.4
depend on it.

### Marketplace submission

Intended eventually, **gated on search shipping** — the author will not submit
until browse exists. Guidelines:
`https://github.com/HANCORE-linux/omarchy-plugin-marketplace/blob/main/SUBMISSION.md`

Submission is a GitHub issue on `HANCORE-linux/omarchy-plugin-marketplace` via the
`gh` CLI, titled `[Plugin]: plugin_name`. Automated validation checks repository
structure and Omarchy Quattro compatibility, and an Automated Security Baseline
runs static analysis on the exact validated commit. It is explicitly *not* a
security review.

Readiness against the published requirements, as of the MVP:

| Requirement | Status |
|---|---|
| `manifest.json` in repo root | ✅ |
| `preview.png` (≤50 MB, ≤40 MP; the marketplace resizes) | ✅ |
| Root README with installation **and removal** instructions | ⚠️ removal missing |
| Root license file documenting the licence **and external dependencies** | ⚠️ ours is plain MIT |
| Plugin ID outside the reserved `omarchy.*` namespace | ✅ `ssandys.tonearm` |

**Decide the plugin ID before the first submission — IDs are permanent and cannot
be reused if retired or renamed.** `ssandys.tonearm` is valid and matches the
sibling plugins, but the marketplace *prefers* the namespaced form
`io.github.<name>.<plugin>` — e.g. `io.github.ssandys.tonearm`, which is the shape
`io.github.teevans.apple-tv-remote` already uses in this very plugin directory.
Changing it later is not possible, and it would also move the install path,
the systemd unit's `ExecStart`, and `setup.sh`'s `installed_root`.

**Two small gaps to close before submitting:**
- The README must document **removal**, not just installation. For tonearm that
  is more than deleting a directory: `systemctl --user disable --now
  tonearmd.service`, removing the unit file, and deleting
  `~/.config/tonearm/` (host, token, pinned zone).
- The root licence file must document **external dependencies**. Ours is plain
  MIT; the vendored Apache-2.0 `roonapi` and its `LICENSE`/`NOTICE` are recorded
  only in `scripts/vendor/README.md`. Surface them at the root.

**Submission metadata** (exact spellings required): category **`Widgets`** — note
`Audio`, which `manifest.json`'s `barWidget.category` currently uses for
Omarchy's own picker, is *not* a marketplace category. Tags, one to three from the
fixed list: **`media`**, **`bar`**, **`quickshell`**.

**Anticipate the security baseline.** tonearm ships a vendored third-party library
(1,599 lines of Apache-2.0 `roonapi`) and a Python daemon that opens a unix
socket, makes outbound HTTP and WebSocket connections, and scans the local `/24`
during discovery. All of that is legitimate and documented, but it is exactly what
static analysis flags. Being able to point at §2.1's measured findings and
`scripts/vendor/README.md` will help.

### The cheap thing to do now

**Treat the socket as a real interface rather than an internal detail.** The
payload already carries `v: 1`, but nothing yet says what that promises. Write the
protocol down — the verbs, the payload shape, what may change within a version and
what may not — and never let the widget reach around it into the daemon's
internals. `tonearmctl` already serves as the reference client and proves the
protocol is sufficient.

Do that, and splitting a consumer into its own repo later is `git mv` and a
README, not a refactor. Skip it, and the seam erodes quietly until it is one.

## 11. Risks

| Risk | Mitigation |
|---|---|
| MPRIS is the largest unknown — `PropertiesChanged`, `Position` and `Seek` semantics are fiddly and the spec unforgiving | Isolated: failure degrades to "media keys don't work". Metadata mapping is unit-tested |
| `ColorQuantizer` is unused anywhere in the Omarchy shell | Verify in the live shell early. Falls back to the theme accent, so a total failure is cosmetic |
| Vendored `roonapi` needs manual refresh | Pinned and recorded here; upstream is low-churn |
| Album art unconfirmed until pairing | Confirm with a real `image_key` as the first post-pairing check |
| Multicast may work on other networks and mask the fallback | Discovery always records which step answered |

---

## 12. Estimate

**2–3 days**, excluding library browse. The kickoff's 1–2 day figure predated MPRIS
moving into the MVP, and the vendoring and `setup.sh` work.
