# AGENTS.md — extending tonearm

Read this before changing anything. Most of it is failures that already
happened on this branch, not advice.

tonearm is a Roon now-playing and transport widget for the Omarchy shell:
`tonearmd`, a Python daemon speaking Roon's MOO protocol over a unix socket,
plus a QML bar-widget plugin (`Panel.qml`) fed by `Service.qml`.

## Layer map

| File | Holds | Testable by |
|---|---|---|
| `Model.js` | All decidable logic: bar state/severity, glyph choice, tooltip text, accent-color selection and contrast, time formatting, seek-position interpolation, zone list/pin logic, volume fraction math, reconnect backoff, art URL construction. 25+ exports. | `node --test`, and a headless `qml6` probe |
| `Service.qml` | I/O only: spawns `tonearmctl subscribe`, parses NDJSON, tracks `state`/`receivedAt`, reconnect backoff, fire-and-forget command sends. No display logic. | **The live shell only** |
| `Panel.qml` | Rendering only: bar button, popup, seek bar, transport, volume, zone list. No tests by design — the unverifiable surface is kept as small as `Service.qml` allows and pushed no further. | **The live shell only** |
| `scripts/tonearm_lib/*` | The daemon: Roon connection (`core.py`), zone arbitration (`zones.py`), MPRIS publication (`mpris.py`), album-art caching (`art.py`), unix-socket server (`server.py`), config/token persistence (`config.py`), discovery (`sood.py`), CLI parsing (`cli.py`) | `python -m unittest` (`./bin/test` runs it via `discover -s tests/python -t .`) |
| `scripts/vendor/roonapi` | Vendored `roonapi` 0.1.6, Apache-2.0. **Do not modify.** `tests/python/test_vendor.py` guards its import surface across refreshes; see `scripts/vendor/README.md` for how to refresh it. | `test_vendor.py` only |

**Every decision worth testing lives in `Model.js` and `scripts/tonearm_lib`,**
and both are reachable from a real test runner. `Service.qml` and `Panel.qml`
hold only I/O and rendering precisely so the unverifiable surface stays as
small as possible. When you add logic, ask which side of that line it
belongs on before writing it.

## Invariants (inherited from headway, unchanged here)

1. **No pure module may hold mutable state without `.pragma library`.**
   Without it, every QML component that imports a `.js` file gets its own
   instance. `Model.js` has no module-level state to begin with — keep it
   that way; if you add a cache or memo table, you reintroduce the bug headway
   already found, and no `node --test` run will catch it.
2. **`hasOwnProperty` on any table keyed by upstream data.** Roon zone ids,
   image keys, and MPRIS-bound strings all originate outside this codebase. A
   bare `TABLE[key]` walks the prototype chain, so a key of `"constructor"`
   or `"toString"` returns a truthy inherited member instead of `undefined`.
3. **Total-order comparators for V4's unstable sort.** Qt's V4 `Array.sort`
   is not stable (node's has been since ES2019), so a comparator that treats
   ties as equal will visibly reshuffle tied entries between polls. Any sort
   added to `Model.js` needs an explicit tie-breaker.
4. **`implicitWidth`/`implicitHeight` plus `fontFamily`/`dim`/`barIcon` on the
   root.** Without the size properties the root is 0×0 and the widget renders
   nothing with no error logged. Without the style properties, a binding that
   reads them raises a `ReferenceError` invisible to `qmllint`.
5. **Exactly one `Component.onCompleted`.** QML rejects a duplicate and the
   whole component fails to instantiate, with nothing in the journal. Fold
   new startup work into the existing handler.
6. **A clean `qmllint` exit is no information.** It catches parse errors only
   — see "Nothing in this toolchain gates QML syntax" below.

## Traps

Every one of these cost real time during the build, and every one fails
silently. These are not hypotheticals; each was measured.

### Roon connection

| Trap | What happens |
|---|---|
| **The MOO WebSocket is on `http_port` (9330), NOT the `tcp_port` (9150) SOOD advertises.** | `roonapi` 0.1.6 dials `tcp_port` by default; on Roon 2.71 that port accepts a TCP connection and then never answers the handshake, so `RoonApi.__init__` blocks forever, registration is never delivered, and Roon lists **no extension at all** — "no extensions discovered", nothing logged anywhere. `core.py` tries `http_port` first and falls back to `tcp_port`. Never let a connect attempt block unboundedly. |
| **`roonapi` mutates `_api.zones` from its own lock-free thread.** | `RoonApiWebSocket` extends `Thread`; `_on_state_change` writes the zones dict in place with no locking. `core.py`'s `_raw_zones()` retries once on `RuntimeError` and degrades to `[]`. Nuance worth recording so nobody "proves" this wrong: on a normal GIL-enabled CPython, `list(d.values())` is one uninterrupted C call and cannot raise mid-materialisation — measured at 4,548+ reads under sustained size-changing mutation with zero errors, while a Python-level loop over the same dict raised after 3. The guard is forward-looking insurance for a free-threaded (PEP 703) build and any future refactor that splits the read across a bytecode boundary — not a bug reproducible today. |
| **`roonapi`'s `_zones`/`_outputs` are CLASS-level mutable defaults**, and the subscription writes by item mutation, so every instance shares them. | `core.py` pre-seeds instance dicts on the `RoonApi.__new__` handle before `__init__` starts any thread. Writing `.clear()` there instead of `= {}` would mutate the shared class dict and be catastrophic. |
| **`blocking_init=False` races the library's own `_get_zones()` prefetch** and deterministically clobbers real zone data with `{}` on any reconnect using a stored token. | The bound must be external (thread + join), not that flag. |
| **`roonapi`'s declared dependencies are over-declared** — it imports only `websocket`. | `test_vendor.py` guards this across refreshes. |
| **Two independent Tailscale hazards.** | An exit node can route the LAN address into the tunnel, making the Core unreachable even though it is on the same wire; and the UDP-connect-then-`getsockname()` trick for finding the local subnet returns the *tailnet* address instead, so `sood.discover()` enumerates interfaces directly rather than trusting that trick. |
| **Multicast SOOD draws no reply on this LAN.** Unicast does. | `RoonDiscovery` cannot find the Core here; `sood.discover()` falls back to a `/24` scan. If discovery ever returns `via: "multicast"`, the network changed. |
| **`_status` only becomes `unreachable` during initial connect.** | It never reverts once `"ok"`. A live Roon disconnect leaves `status: "ok"` in the daemon's snapshot with zone data going stale — this is a known limitation, documented in the README, not (yet) fixed. |

### Browse and search

| Trap | What happens |
|---|---|
| **Roon's search is not a hierarchy.** | It is an item inside `Library` carrying `input_prompt`, and the query rides with that item's `item_key`. Every attempt to use a top-level `search` hierarchy returns one item titled `No Results` and never errors — indistinguishable from an empty library. |
| **A stale `item_key` returns the browse ROOT with no error.** | Never re-walk to go back; use `pop_levels: 1`. This is why replies carry no `item_key` at all. |
| **A category row and an album row are both `hint: "list"`.** | They cannot be told apart before descending. `image_key` is null on categories but also null on art-less albums, so it must never be used to infer playability — that is what the `activate` op is for. |
| **Depth to a playable action is uneven.** | 1 descent for a track, 2 for an album. |
| **`multi_session_key` works through the vendored library untouched**, because `browse_browse` passes its opts dict to `_request` verbatim. | Two consumers do not clobber each other. |
| **Never use `list_media`/`play_media`.** | Both open with `pop_all: True` and reset the browse session as a side effect. |

### Art and color

| Trap | What happens |
|---|---|
| **`ColorQuantizer` cannot load remote URLs.** | Pointed at the Core's art URL it emits zero colours; the same bytes as a local `file://` give eight. A plain `Image` loads the remote URL fine. This is why `tonearmd` caches art to `$XDG_RUNTIME_DIR/tonearm/art/` and the payload carries `art_path` — without it the album-art accent silently does nothing and always falls back to the theme accent. |
| **`ColorQuantizer` is used nowhere else in the Omarchy shell.** | Live-shell verification only; there is no first-party reference usage to lean on. |
| **systemd's `RuntimeDirectory=tonearm` deletes the directory on stop.** | The art cache is wiped on every restart, so `art/` must be created on start, not lazily on first fetch. |
| **Album art does not work at bar-icon size.** | A cover thumbnail in the bar button is ~10px of image and reads as a coloured blur, not art. It was built, deployed, looked at, and removed. Art belongs in the popup, where it has room. |

### MPRIS and dbus-next

| Trap | What happens |
|---|---|
| **`dbus_property(access="read")` raises `TypeError` at IMPORT time** on dbus-next 0.2.3. | Must be `PropertyAccess.READ`. |
| **`PropertiesChanged` must be emitted for the `Can*` properties**, not only `PlaybackStatus` and `Metadata`. | omarchy's media-key handler returns "unhandled" if `CanPlay`/`CanPause`/`CanGoNext`/`CanGoPrevious` never change — the keys silently do nothing even though the player is on the bus. Also: consumers query `DesktopEntry` within seconds of the name appearing, and omarchy calls `Play`/`Pause` individually against `PlaybackStatus` rather than `PlayPause`. |
| **MPRIS `xesam:artist` is `as`.** | A bare string makes strict clients drop the whole metadata dict. |
| **MPRIS `mpris:trackid` must be a valid object path.** | Roon's image keys contain hyphens; unsanitized, some clients drop off the bus. |

### QML / Service / Process

| Trap | What happens |
|---|---|
| **A failed `Process` spawn never emits `exited()`** — confirmed by probe. | It goes straight to `running = false` without ever passing through `true`. `onRunningChanged` must be the drain signal; `onExited` would leave the relay dead forever the first time `tonearmd` (or `tonearmctl`) is missing. |
| **`tonearmctl subscribe` exits immediately when the daemon is down**, so `Service.qml` must back off. | Respawn-on-exit with no delay is a fork loop. |
| **Resolve paths with `pathFromUrl`/`decodeURIComponent`, never `.replace("file://", "")`.** | `Qt.resolvedUrl` percent-encodes, so a home directory containing a space leaves `%20` in the path, the spawn fails, and — because a failed spawn emits no `exited()` — the relay retries forever with nothing logged. `galley` and `colophon` both carry the helper; copy it verbatim. |
| **Do not name a root property `bar`.** | `Ui/Panel.qml` injects its own `bar` (the reference `BarIconButton { bar: root.bar }` needs). Declaring another is a QML duplicate-property error and the file will not compile at all. The computed bar state is named `display` for exactly this reason. |

### Verification tooling

| Trap | What happens |
|---|---|
| **`omarchy plugin validate` is a real gate but piping it into `head` masks its exit code.** | Capture `$?` directly or a failure reads as a pass. |
| **The brace-balance checker published in headway's `AGENTS.md` strips `//` comments BEFORE strings, so a string containing `//` eats the rest of its line.** | `"file://…"` is the obvious trigger, and `Service.qml`'s `pathFromUrl` contains exactly that. Demonstrated: a file with a genuine `+1` imbalance — one that will not parse — reports `0` under the published recipe. Since `qmllint` is separately known to be no information, that leaves *nothing* catching an unbalanced brace. Swap the substitution order so strings are blanked first: <br><br>`t = re.sub(r'"(\\.\|[^"\\])*"', '""', s)   # strings FIRST`<br>`t = re.sub(r'//[^\n]*', '', t)            # then comments`<br>`print(t.count('{') - t.count('}'))`<br><br>tonearm's own files are clean under both forms, so nothing shipped broken here — but the published recipe is unsound and the sibling plugins document it too. |
| **A manually-run `./scripts/tonearmd` and the installed service share the same socket and config paths.** | Starting one while the other runs unlinks the other's socket, silently breaking the widget until the service is restarted. This happened during development. Either stop the service first (`systemctl --user stop tonearmd.service`) or expect to restart it afterwards. |
| **A test fixture whose value coincides with the implementation's default proves nothing.** | This shipped **six separate times** during the build and was caught only in review, never by the suite -- the count keeps climbing, which is itself the useful signal: an arbitration test whose fixtures never let recency and id order disagree (a transition-blind implementation passed it); a `trackid` test whose fixture contained no character to sanitise (deleting the sanitiser passed it); an `artUrl` test using the same port as the fallback (ignoring the state value entirely passed it); a SOOD `to_core` port-extraction test using `"9150"`/`"9330"` -- identical to the defaults (a stub that always returned the default, skipping both the dict lookup and the string-to-int coercion, passed it); a "volume comes from the first output" test with only one output and an implementation-default `max`, so the "first output" property the test was named for was never exercised (reading any output, or the wrong one, passed it); and a "fixed-volume output reports none" test whose comment claimed to cover `incremental` outputs too but only ever constructed the "no volume object at all" case (a real `incremental` output still fell through to a fabricated `{"value": 0, ...}` slider and passed it). When writing a fixture, pick a value the implementation could not produce by accident. |

### Everything inherited from headway, unchanged

No mutable module state without `.pragma library`; `hasOwnProperty` on
upstream-keyed tables; total-order comparators for V4's unstable sort;
`implicitWidth`/`implicitHeight` plus `fontFamily`/`dim`/`barIcon` on the
root; exactly one `Component.onCompleted`; `qmllint` exit 0 is no
information.

## Nothing in this toolchain gates QML syntax

`omarchy plugin validate <folder>` is a real gate for `manifest.json` and no
gate at all for QML syntax (see headway's `AGENTS.md` for the measured
`qmllint` exit-0-on-unparseable-file case, which applies here unchanged).
What actually verifies a QML change in this repo:

1. `./bin/test` runs `qmllint` (parse errors only) over every top-level
   `*.qml` file, plus the corrected brace-balance check if you run it by
   hand — see the trap above for why the published version is unsound.
2. `./bin/dev up`, then read the shell's log:
   `journalctl --user _PID=$(pgrep -x quickshell) --since "1 minute ago"`.
   A `ReferenceError` in a binding shows up here and nowhere else.
3. Look at it. A widget can load without error and still render at 0×0.

## How to run things

```bash
./bin/test                    # manifest, bash syntax, qmllint, python (unittest), javascript (node --test)
./bin/dev up                  # deploy as ssandys.tonearm-dev, register, enable, restart the shell
./bin/dev down                # disable the dev plugin and restart the shell (no-op if nothing to disable)
./bin/dev deploy               # deploy only; never touches the running shell
./bin/dev status               # what is deployed, registered, enabled
./bin/dev-watch                 # redeploy on change
setup.sh                       # install the systemd unit for the INSTALLED copy, enable it now
setup.sh --check               # health check: service active? socket answering? paired?
omarchy plugin validate .      # manifest gate -- NOT a QML gate, see above. Capture $? directly.
```

`bin/dev` and `bin/dev-watch` are copied byte-identical from galley/headway
and derive plugin identity from `manifest.json` at runtime — do not hand-edit
them for tonearm-specific behaviour.

Note that `bin/dev` rewrites the display name in the **deployed** copy, so
the running panel reads `Tonearm (dev)`. That is why `preview.png` was
captured against a copy with the suffix stripped: a preview is a photograph
of what someone is about to install.

`setup.sh` probes `/usr/bin/python` explicitly, not whichever `python3` a
version manager puts first on `PATH` — the systemd unit runs under the
system interpreter, so that is the only interpreter whose importable modules
matter.

## Never edit `/usr/share/omarchy/`

It is package-managed and will be overwritten. Read it freely — it is the
best available documentation of the shell's own conventions. Change nothing
in it.
