# Follow-ups

Known gaps carried past the MVP, with enough context to act on each without
re-deriving it. Ordered by whether a user can notice.

Items closed by the final review's fix wave are recorded at the bottom so a
future reader does not re-open them.

## 1. The daemon never detects a *live* Roon disconnect

`_status` is set to `"unreachable"` only inside `RoonSession.start()`'s connect
attempts. Once it reaches `"ok"`, nothing sets it back. If the websocket drops
while the daemon is running, `status` stays `ok` and the zone data simply goes
stale — the bar keeps showing the last track as though nothing happened,
instead of the error state the spec's §7.4 promises (`unreachable` → error
glyph → "Roon Core unreachable").

This has not bitten in testing because `roonapi` reconnects underneath; a
transient `AttributeError` was observed during live verification doing exactly
that, self-healing. A Core that goes down properly — ROCK rebooting, network
dropping — would leave the widget confidently wrong rather than honestly
broken, which is the failure mode the whole severity design exists to avoid.

It also blocks a spec requirement: "on losing the Roon connection, withdraw the
MPRIS name" is currently wired only to daemon shutdown, because there is no
live-disconnect signal to wire it to.

**Fixing it** means giving `RoonSession` a way to observe the websocket dying
(roonapi exposes callbacks) and distinguishing a durable loss from a reconnect
blip. This is the highest-value item here.

## 2. `position()` clamps to `length` only while playing

A paused zone reporting `position > length` returns the unclamped value, so the
seek bar could render past its end. Low probability (depends on Roon's data),
cosmetic.

## 3. The subscribe handshake calls `conn.sendall()` under `Server._lock`

A stalled new client can block `broadcast()` to every other subscriber, and
block further subscribes, for as long as that send blocks. This extends the
pre-existing design, where `broadcast()` already sends under the lock — it was
inherited rather than chosen. The code now carries a comment saying so, which
makes the decision explicit but does not change the availability characteristic.

No longer theoretical: browse makes concurrent socket traffic routine.
Before this feature, the daemon mostly held one long-lived subscribe
connection open per client; now every keystroke-driven browse op (search,
activate, enter, back, queue) opens its own short-lived connection and does
a synchronous round-trip while that subscribe connection is still live. The
accept-and-thread path this hazard depends on is now exercised continuously
during normal use, not occasionally — raising the odds that a slow or
stalled peer's `sendall()` actually coincides with a subscribe handshake or
a broadcast. This work does not fix it; revisit if a subscriber is ever slow
or remote.

## 4. `setup.sh` uses `cp` where the modelled script uses `ln -s`

`stappmus.audio:51-57` symlinks the unit and guards against replacing an
unrelated service file. `cp` is non-atomic (an interrupted copy leaves a
truncated unit) and will clobber whatever sits at the target. Low practical risk
given the unique unit name, but the script we modelled on deliberately does
better.

## 5. `Cache._prune()`'s `getmtime` is unguarded

`listdir`/`unlink` are guarded; `getmtime` is not. Two rapid track changes both
completing fetches could race a `FileNotFoundError` out of a function whose
docstring promises best-effort.

## 6. Nothing enforces `Model.js`'s ES3-subset or no-mutable-module-state rules

Both constraints exist because `Model.js` is loaded by both node and Qt's V4,
and both are currently checked by human review on every change. A lint-style
test would close a whole class of regression. This is the constraint most likely
to be broken by someone who has not read `AGENTS.md`.

## 7. Untested branch: `formatTime` with `h > 0 && m >= 10`

E.g. `4200` → `"1:10:00"`. Correct by construction, but the minute-padding rule
has a branch no test exercises.

## 8. The `status` verb serializes outside its guard

`server.py`'s `browse` branch was fixed so that `json.dumps` runs inside the
same `try` as the guard that catches it: a reply that fails to serialize
becomes a `roon_error` response instead of an uncaught exception. The
pre-existing `status` verb (`server.py:119-125`) still has the original
shape — `json.dumps(self._session.snapshot())` runs inside a `try` that
only catches `OSError`. A `TypeError` from a non-serializable snapshot would
propagate out of `_handle`, skip `conn.close()`, and leave the client's
`readline()` blocking forever, which is exactly the freeze the `browse`
branch was fixed to prevent.

Latent today because `RoonSession.snapshot()`'s payload is plain,
JSON-serializable data — nothing currently puts a non-serializable value in
it. The fix is the same shape as the `browse` fix: broaden the `except` (or
move the serialization inside a `try`/`except Exception`) so `conn.close()`
is guaranteed to run regardless of what `snapshot()` returns.

## 9. The browse session dict is unbounded

`RoonSession._browse_sessions` (`core.py:179`, populated by
`browse_session()` at `core.py:439-451`) creates one `BrowseSession` per
`multi_session_key` and never evicts one. The key comes straight off the
wire, in the `browse` request's `session` field, with no validation.

The socket is 0600 in the user's own runtime dir, so the realistic failure
mode is a buggy consumer — a client that mints a fresh key per request
instead of reusing one, or a future second consumer (an MCP server, say)
that never converges on a stable key — leaking sessions over a long daemon
uptime, not an attacker. This is a memory-leak guard, not a security fix.
The remedy is an LRU cap on `_browse_sessions`, evicting the
least-recently-used session once some bound is hit.

## 10. Paging is implemented in the protocol but unreachable from the UI

`BrowseSession.page(offset)`, the `page` op and `tonearmctl browse page` all
work and are tested. `BrowsePane.qml` never calls them, so only the first 100
rows of a level are reachable from the widget.

This is invisible for search results, which are narrow — the measured
`"Oingo Boingo"` case returns 21 albums and 44 tracks. It becomes visible on a
common single-word search against a large library, where `Tracks` could exceed
100. The fix is to call `page` when the `ListView` nears its end and append,
which also needs the daemon to return rows for an offset without resetting the
cursor — `page` already does exactly that.

## 11. No progress indicator during a search

A `browse` search round-trip (spawn `tonearmctl browse search`, wait for
Roon's reply) shows nothing while in flight — no spinner, no "Searching…"
text. `BrowsePane.qml`'s `hasContent` includes `busy` (`BrowsePane.qml:58`),
so the pane and the submitted query stay on screen for the round-trip rather
than vanishing, which is why this hasn't looked outright broken in testing.
But nothing on screen says work is happening, so a slow search — a large
library, a loaded Core — reads as a hang. The fix is a busy indicator bound
to the existing `busy` property; no new state is needed, just something
visible while it's true.

## 13. `tonearmctl` sets no socket timeout anywhere

`scripts/tonearmctl` calls `socket.connect()`, `sendall()` and
`makefile("r").readline()` on a socket left in blocking mode with no
`settimeout()` on any path. A daemon that accepts the connection and then
never answers — the exact shape of the Roon `tcp_port` hang recorded in
`AGENTS.md`, and of any deadlock inside `_handle` — leaves `readline()`
blocked forever. There is no timeout, no retry and no way out but a signal.

Pre-existing, not introduced by browse: `status` has the identical exposure,
and `setup.sh --check` uses `status` as its health probe, so a hung daemon
turns the health check itself into a hang.

Browse makes it reachable from the UI, and it **combines with the popup's own
Esc handling into an unclosable popup**: `BrowsePane` gates every op on
`busy`, and `busy` is only cleared when a reply arrives. A `browse` request
that never gets an answer therefore pins `busy` true forever. (The fix wave
made `handleBack()` return false when `busy` suppressed the send, so `Esc`
now falls through to closing the popup instead of being swallowed — that
closes the *unclosable* half. The wedged relay process and the permanently
`busy` pane remain.)

The fix is a `settimeout()` on the socket covering connect and the
single-reply reads (`subscribe`'s stream is the one path that legitimately
blocks indefinitely), plus a non-zero exit so `Service.qml`'s existing
respawn-with-backoff treats it like any other failed relay.

## 14. A reset lost to `busy` at popup close is discarded, not delayed

`resetPane()` routes through `_send`, which returns early when `busy` is true —
so closing the popup while a browse is in flight drops the reset. Nothing
retries it. The in-flight reply then lands, `_apply` clears `busy` and
`_applyLevel` **repopulates** `rows`/`path`. On the next open, `hasContent` is
true via `path.length > 0`, so the separator and the whole pane render.

That is exactly the idle-height regression items R13/R14 cost two fix rounds to
close, still reachable on this one path — press Esc or click away immediately
after Enter. Combined with item 13 (no socket timeout), a wedged daemon pins
`busy` forever and `resetPane()` can never fire at all.

Clearing state optimistically does not work — it loses to the same in-flight
reply. The fix is a `_resetPending` flag set by `resetPane()` when `_send`
returns false, honoured in `_apply` once `busy` clears.

## 15. `play()` re-reads the zone several times (TOCTOU)

`_opts()` calls the zone provider on every browse and load, so a single `play()`
reads it many times across the descent walk and the unwind. If the zone
*vanishes* between the `no_zone` guard and the action invoke, the C0 failure
returns in miniature: `played: true` over silence. If it merely *changes*, the
play lands in the new room, which is arguably what a repin should do.

Browse position is per-`multi_session_key`, not per-zone, so a mid-play repin
cannot corrupt the walk itself — only the final invoke's target matters, and it
uses the freshest value. Severity is low. The cheap close is to read the zone
once at the top of `play()` and thread it through as an `_opts(zone=…)`
override, leaving per-call reads everywhere else.

## 16. Browse threads call `Arbiter.observe()` outside `CachingSession`'s lock

`CachingSession` exists specifically to serialize `Arbiter`'s unlocked mutations
across threads — its docstring says so at length. But `CachingSession.browse()`
is a deliberate unlocked pass-through, and `selected_zone_id()` reaches
`_zones()` → `Arbiter.observe()`. So browse threads now mutate
`_last_state`/`_started_at`/`_counter` outside the lock added to protect them,
concurrently with subscriber `snapshot()` calls that hold it.

This is a frequency increase on a pre-existing hazard, not a new class of one:
`RoonSession._publish()` already bypasses the wrapper entirely, and `art.py`
records that. `observe()` was verified idempotent (1× vs 5× produce identical
state; 10× consumes one counter value), and under CPython each dict write and
`next(counter)` is atomic, so nothing corrupts. Worst case is `_last_followed`
computed from a momentarily mixed `_started_at` — a transient wrong zone in the
bar.

One visible consequence worth knowing: extra sampling between publishes can now
catch a zone that flaps playing→paused→playing and would previously have gone
unnoticed, so the followed zone can change because the user opened the browse
popup.

## 17. Clicking away from the search field leaves the key catcher blocked

`BrowsePane`'s `field.onActiveFocusChanged` handles only the gaining edge, so
losing focus by mouse — clicking elsewhere in the popup — never clears
`editing`. `PanelKeyCatcher` stays `blocked: true` while nothing holds field
focus, so keyboard navigation goes dead until the field is focused and released
again.

Pre-existing and untouched by the browse work, but browse is what made the popup
keyboard-driven enough for it to matter. The fix is to call `releaseSearch()` on
the losing edge.

## 18. Tidiness

- The `(started_at, id)` ranking tuple is duplicated between `Arbiter.observe()`'s
  recompute and `select()`'s active branch.
- `_try_port`'s docstring duplicates most of the `STOP_GRACE` module comment.
- `CachingSession.snapshot()` mutates the wrapped session's returned
  `now_playing` in place — safe only because `RoonSession.snapshot()` happens to
  build fresh dicts each call. Undocumented assumption for a wrapper whose
  docstring claims it wraps anything with `.snapshot()`.
- `Cache._last_thread` is test-support state on a production class, assigned
  outside the lock.
- `_IGNORED_IFACE_PREFIXES` is a non-exhaustive heuristic (misses `wg`, `tun`,
  `virbr`, `zt`); an unlisted virtual interface costs one wasted `/24` scan.
- `_local_networks()` does not dedup, so bonded or aliased interfaces on one
  `/24` get scanned twice. Results dedup by host, so harmless.
- Both `tests/__init__.py` and `tests/python/__init__.py` exist; only the latter
  is needed.
- `THEME_BACKGROUND` and `CONTRAST_FLOOR` are exported beyond the interface the
  plan specified.

## Closed

Recorded so they are not re-opened.

Closed by the popup redesign:

- **Search was undiscoverable.** The field is still hidden until `/` or a
  letter is pressed — the idle-height goal is intact — but the `ZONES` caption
  line now carries a `/  search` hint on its right, which was empty space, so
  the affordance cost no height at all. The hint is also clickable. This was
  item 12, and it means the README is no longer the only place a user can
  learn search exists.
- **Queue was a write-only control.** The popup can't show a queue, so its
  effect was invisible. Removing it from the UI also deleted
  `BrowsePane.hasSelection` and `Panel.qml`'s context-sensitive `q` branch —
  which existed only because `q` is both the queue key and the first letter of
  Queen. The daemon keeps the action (`play <n> queue`), still tested; the
  widget just never asks for it, same posture as item 10's `page`.

Closed after the final review:

- Systemd's default start limit (5 starts / 10s) could have capped the daemon's
  only retry mechanism, leaving the unit `failed` and silent rather than merely
  disconnected. It was unreachable in practice, but only because
  `sood.discover()`'s 6s floor makes a fail-restart cycle ~9s — an accidental
  margin no test guards. Now pinned with `StartLimitIntervalSec=0` in `[Unit]`.

Closed by the final review's fix wave:

- Incremental-volume outputs reported a fabricated `value: 0` instead of `None`,
  parking the popup slider at zero for a zone whose volume cannot be read.
- The daemon connected exactly once and never retried, so a first-run pairing
  window of ~25s was easy to miss.
- `notifyCoreUnreachable` / `notifyZoneChange` were declared in the manifest and
  documented in the README but never implemented.
- The `/24` discovery scan was not restricted to private address space.
- `setup.sh` required `curl`, which nothing uses.
- `import fcntl` at module top level made an otherwise-portable module
  Linux-only.
- Three docstrings/comments described behaviour the code does not have
  (`_raw_zones()`'s `RuntimeError`, `_seeded_api()`'s `ready` gate, `formatTime`'s
  "also catches NaN").
