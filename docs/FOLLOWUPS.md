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

## 2. `StartLimitIntervalSec` is unset, and the margin protecting it is accidental

`systemd/tonearmd.service` sets `Restart=on-failure` / `RestartSec=3` but no
start-limit override, so systemd's defaults apply (5 starts / 10s). The daemon
cannot currently trip that limit — but only because `sood.discover()` has a hard
floor equal to its `timeout` parameter (default 6.0s): its receive loop runs to
completion even when no interface has a usable address. That makes a
fail-restart cycle ~9s, so at most 2 starts land in any rolling 10s window.

The margin falls out of a default timeout and a vendored library's
spin-until-joined behaviour, not out of a deliberate safeguard, and no test
guards it. Shortening that default — or swapping the library — would silently
reintroduce "the daemon never retries", in the harder-to-diagnose form of a
`failed` unit rather than a merely disconnected one.

**Fix:** `StartLimitIntervalSec=0` (or a generous burst) in the unit file.
One line of cheap insurance.

## 3. `position()` clamps to `length` only while playing

A paused zone reporting `position > length` returns the unclamped value, so the
seek bar could render past its end. Low probability (depends on Roon's data),
cosmetic.

## 4. The subscribe handshake calls `conn.sendall()` under `Server._lock`

A stalled new client can block `broadcast()` to every other subscriber, and
block further subscribes, for as long as that send blocks. This extends the
pre-existing design, where `broadcast()` already sends under the lock — it was
inherited rather than chosen. The code now carries a comment saying so, which
makes the decision explicit but does not change the availability characteristic.
Revisit if a subscriber is ever slow or remote.

## 5. `setup.sh` uses `cp` where the modelled script uses `ln -s`

`stappmus.audio:51-57` symlinks the unit and guards against replacing an
unrelated service file. `cp` is non-atomic (an interrupted copy leaves a
truncated unit) and will clobber whatever sits at the target. Low practical risk
given the unique unit name, but the script we modelled on deliberately does
better.

## 6. `Cache._prune()`'s `getmtime` is unguarded

`listdir`/`unlink` are guarded; `getmtime` is not. Two rapid track changes both
completing fetches could race a `FileNotFoundError` out of a function whose
docstring promises best-effort.

## 7. Nothing enforces `Model.js`'s ES3-subset or no-mutable-module-state rules

Both constraints exist because `Model.js` is loaded by both node and Qt's V4,
and both are currently checked by human review on every change. A lint-style
test would close a whole class of regression. This is the constraint most likely
to be broken by someone who has not read `AGENTS.md`.

## 8. Untested branch: `formatTime` with `h > 0 && m >= 10`

E.g. `4200` → `"1:10:00"`. Correct by construction, but the minute-padding rule
has a branch no test exercises.

## 9. Tidiness

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

## Closed by the final review's fix wave

Recorded so they are not re-opened:

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
