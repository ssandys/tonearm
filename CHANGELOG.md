# Changelog

Notable changes to tonearm. Versions follow [semantic versioning](https://semver.org);
while the major version is 0, the minor version carries changes that would
otherwise be breaking.

## 0.11.7 — 2026-09-24

### Fixed

- **Zones the Core no longer lists are dropped on reconnect**
  ([#13](https://github.com/ssandys/tonearm/issues/13)). A zone that
  disappeared while the socket was down survived the reconnect: `tonearmctl
  status` kept listing it and the picker kept offering it, until the daemon
  was restarted — while Roon itself no longer showed it. Reported from a real
  session, with an endpoint powered off while the laptop slept and still
  reported as paused 23 seconds after reconnect.

  The cause is in vendored roonapi, which handles the `zones` and `outputs`
  subscription payloads in the same branch as the incremental `*_changed` and
  `*_added` events: it updates or inserts every id it receives and never
  removes one that is absent. Correct for an increment, wrong for a snapshot.
  Snapshots now drop what they omit; increments still only merge.

  Two further faults in the same handler, found while covering the first: a
  removal received while connected fired no state callback at all, so it
  reached consumers only when some later unrelated event happened to be
  published; and the removal path used a bare `del`, which raises `KeyError`
  inside a websocket callback when a removal races a snapshot that has already
  dropped the entry.

  These are local patches to a vendored library, so they are marked in the
  source, described in `scripts/vendor/README.md` with instructions to
  re-apply on a refresh and to re-check against upstream on a bump, and pinned
  by tests — a refresh that drops them fails the suite rather than a user.

## 0.11.6 — 2026-09-24

### Fixed

- **The bar stops blaming your network when the fault cannot be established**
  ([#11](https://github.com/ssandys/tonearm/issues/11)). "No route to your
  network" was decided by opening TCP to the default gateway and calling the
  network down if it did not answer. Plenty of healthy gateways do not answer
  TCP, so on such a network switching the Core off produced a network fault —
  the same wrong message the status was added to eliminate, merely inverted.
  CI proved it unprompted: eleven tests failed on a GitHub runner whose
  gateway ignores tcp/80 and whose network was perfect.

  The daemon now asks the kernel which source address it would use to reach
  the Core, which is a routing fact rather than a third party's manners, and
  costs nothing on the wire. A network fault is reported **only when it can be
  positively established** — the Core sits on a subnet this machine is
  directly on, and the kernel is nonetheless leaving by a different one, which
  is the tunnel-swallowing-the-LAN condition that prompted the status in the
  first place. Anything less certain keeps the older wording, because telling
  someone their network is down when it is not is the worse error.

  In particular a Core reached through a router — another VLAN, a wired
  segment — is no longer mistaken for a routing fault, and neither is a Core
  reachable only by a host route.

## 0.11.5 — 2026-09-24

Relocation, which could not work at all on some networks and said nothing
about it either way. Both fixes came out of one live outage: a Core that had
moved to a new DHCP address while the daemon sat on the old one reporting
`unreachable`.

### Fixed

- **Relocation can fall back to the LAN sweep when multicast is silent**
  ([#17](https://github.com/ssandys/tonearm/issues/17)). Discovery during an
  outage was multicast-only, on the premise that a Core which has moved is up
  and answering SOOD anyway. Measured on a real network, that premise is
  false — multicast answered 0 of 7 attempts, including four 12-second
  windows, while the `/24` sweep found the Core 3 times in 6 and a unicast
  probe to a known address answered 4 in 10. The Core speaks SOOD perfectly
  well; its multicast replies never arrive over that Wi-Fi, which is ordinary
  consumer-AP behaviour. On such a LAN relocation could never work, whatever
  was in the config.

  Windows may now sweep, bounded in time: the first four regardless — about
  94% cumulative at the measured hit rate, inside eight minutes, which is
  what beats a coin flip — then every eighth, so a Core returning at a new
  address overnight is still found. Space was already bounded, since only
  private `/24`s are ever scanned.

  Deliberately not gated on the Core's last-known subnet. That was the first
  design and it could lock itself out: a router swap puts everything on a new
  `/24`, the stored address is then in no local subnet, so no sweep is
  permitted, so the stored address is never refreshed, and the gate never
  reopens.

- **The daemon says why it did not adopt a Core it could see**
  ([#15](https://github.com/ssandys/tonearm/issues/15)). "Your Core is
  switched off" and "your Core is right there and I will not touch it" were
  identical in the journal — opposite problems with opposite remedies.
  Nothing about the refusal itself changed: a stored `unique_id` must still
  match exactly, and adopting an unmatched Core is the accidental capture
  that rule prevents.

  Reported once per outage rather than per poll, and each distinct conclusion
  once, so a discovery that flaps between finding the Core and finding
  nothing does not flap the journal with it.

### Known limits

A Core that moves to a *different* subnet is still not found, and a second
Core in another network environment is refused rather than adopted — the
identity check is right to refuse it, and there is nowhere to keep a second
Core's token or pin. Tracked as
[#16](https://github.com/ssandys/tonearm/issues/16) and
[#19](https://github.com/ssandys/tonearm/issues/19).

## 0.11.4 — 2026-09-23

No runtime change: the plugin behaves identically to 0.11.3. Released so the
verified marketplace snapshot sits on a tag, since `omarchy plugin add` clones
every tracked file and `bin/` is among them.

### Fixed

- **`bin/dev`'s shell guard no longer aborts when the restart command reports
  failure** ([#14](https://github.com/ssandys/tonearm/issues/14)).
  `omarchy restart shell` exits non-zero when it loses its own race — it prints
  "Omarchy shell did not become ready after restart" and gives up. Under
  `set -e` that aborted `restart_shell` at its first line, before the wait and
  the retry written for exactly that case, so the guard added in 0.11.3 never
  ran: `bin/dev` exited carrying omarchy's message and none of its own, having
  never once asked whether a shell was there.

  Measured both ways. With a dead shell the reviving retry was skipped, which
  is the failure the guard exists to prevent occurring through the guard. With
  a *healthy* shell `bin/dev` still exited 1 without ever pinging — a false
  negative on a desktop with nothing wrong with it, and the sharper argument
  for the fix: the exit code is not the question, whether a shell answers
  afterwards is.

  The guard shipped in 0.11.3 was ported from headway `dab5a13`, which carried
  this hole; headway fixed it the same day in `640605d`. The tests missed it
  because their `omarchy` shim could only exit 0 — the happy path of the very
  command whose unhappy path the function exists for.

## 0.11.3 — 2026-09-22

### Fixed

- **One subscription per shell, not one per monitor.** The bar instantiates a
  widget per bar surface and a surface exists per monitor, so everything
  inside the widget existed once per monitor — including the long-lived
  `tonearmctl subscribe`. A two-monitor desk held two persistent subscriptions
  to `tonearmd`, a three-monitor desk three, each one a Python process and a
  slot against `MAX_SUBSCRIBERS`. It is invisible on the single-monitor
  machine plugins get developed on, which is why it lasted.

  `Service.qml` is now a QML singleton (`qmldir`), refcounted by
  `attach()`/`detach()` so the relay runs only while a widget is alive to read
  it. `consumers` is clamped at zero rather than trusted to balance, because
  `Component.onDestruction` is not a guarantee — the same class of problem as
  a `Process` that never emits `exited()` on a failed spawn. The reconnect
  path is guarded on the same count: without that, detaching the last widget
  set `running = false`, which lands in `onRunningChanged`, which restarts the
  backoff — and the relay would respawn forever with nobody reading it.

  Measured on a live shell with a second surface (`hyprctl output create
  headless`), old and new running side by side as separate plugin ids: two
  surfaces gave the old copy two `tonearmctl subscribe` processes and the new
  one exactly one. Removing a surface left the relay up; disabling the last
  widget stopped it and it stayed stopped past the 30s backoff cap; two
  consecutive redeploys re-attached at zero rather than climbing.

- **A browse cursor per bar surface.** The singleton shares the relay, which
  is right — every monitor must agree about what is playing — but a browse
  cursor is not feed data, and all surfaces were still driving the daemon's
  one `widget` key. Navigating on one monitor re-rendered the pane on
  another; `level_id` made that fail safe rather than play the wrong album,
  so it surfaced as a pane that inexplicably snapped back.

  Each surface now derives its own key from its screen name
  (`widget-DP-7`), which is stable across a hot reload where a counter would
  not be, and bounded by the number of monitors. A surface created after
  startup briefly has no window to read a screen name from; it falls back to
  the shared key and the binding corrects itself once the window resolves —
  measured at about two seconds, long before a popup can be opened. The
  pane's own rows, cursor and path were never moved into the singleton: they
  are per-surface UI state, and sharing them would mean typing on one monitor
  changing what someone is reading on another.

## 0.11.2 — 2026-09-22

### Fixed

- **`tonearmctl browse` no longer drives the bar's cursor.** The daemon keeps
  one Roon browse cursor per session key, and that isolation is real and
  tested — but `cli.py` hard-coded `"session": "widget"` for every caller, so
  the key never distinguished anyone. A `tonearmctl browse search` typed in a
  terminal moved the popup's own cursor: the pane's next keystroke addressed
  rows it was no longer showing, and `server.py`'s comment claiming the real
  keys are `widget`, `mcp` and `cli` described an intent the code had never
  implemented.

  `browse` now takes an optional `--session <key>` and defaults to `cli`. The
  flag must precede the op, because `search` joins everything after its op
  into the search term — on the tail it would be searched for rather than
  parsed. An empty key is refused at the edge rather than forwarded, since the
  daemon reads `payload.pop("session", None) or "widget"` and an empty string
  there lands back on the widget's cursor, which is the collision being
  closed. The widget names itself explicitly through the new
  `Model.browseArgv`, so it keeps the `widget` key rather than inheriting a
  default.

  No protocol change: the wire field, its bound and the daemon's per-key
  isolation are all untouched. Verified against a live daemon — the same
  `browse reset` returned `level_id: 1` on the new `cli` cursor and
  `level_id: 8` on the `widget` cursor the bar had been driving.

  This does not by itself fix the multi-monitor case, where every bar surface
  still sends `widget` and shares one cursor between them; `level_id` already
  makes that fail safe rather than act on the wrong row.

## 0.11.1 — 2026-09-19

### Fixed

- **Dead subscribers no longer lock the widget out**
  ([#10](https://github.com/ssandys/tonearm/issues/10)). `MAX_SUBSCRIBERS`
  bounded the subscriber list, but a subscriber was only removed when a write
  to it failed — and writes only happen on state changes, so with nothing
  playing a client that had gone was never noticed. Every widget restart left
  a slot held by a closed peer; after sixteen, the daemon refused the real
  widget and stayed that way. Found on a live install: 856 refusals over four
  days, every slot `ESTAB` with `peer=*`, while `systemctl` reported the
  service active and a direct `status` probe answered `ok` — so the only
  symptom was the bar's fault glyph, which reads exactly like the daemon being
  down.

  Subscribers whose peer has closed are now reaped before a new subscribe is
  refused, and again on every broadcast. Liveness is read from the socket
  itself — a closed peer makes it readable at EOF — so it needs no traffic, no
  client timer and no protocol change, and `MSG_PEEK` leaves a chatty
  subscriber's bytes where a later reader will find them. The check errs
  towards alive: dropping a working widget is worse than holding a slot until
  the next sweep.

## 0.11.0 — 2026-09-19

A review pass over the whole codebase, plus the two outages that prompted it.

### Fixed

- **The album-art cache no longer raises while pruning itself**
  ([#3](https://github.com/ssandys/tonearm/issues/3)). `_prune()` guarded
  `listdir` and `unlink` but not the `getmtime` between them, so a file removed
  in that window — two art fetches finishing together, which is two quick track
  changes — raised `FileNotFoundError` out of a function documented
  best-effort, from a thread, leaving an unhandled traceback and a cache over
  its cap.
- **The `status` verb no longer leaks its connection**
  ([#4](https://github.com/ssandys/tonearm/issues/4)). A snapshot that would
  not serialize escaped an `except OSError` and skipped the close on the next
  line, so the handler thread died with the descriptor still open and the
  client waited on a reply that never came. The subscribe path had handled this
  since it was written; the defect was the decision reaching only one of the
  two places that needed it.
- **A network fault no longer masquerades as a dead Core**
  ([#2](https://github.com/ssandys/tonearm/issues/2)). "Roon Core unreachable"
  was shown whenever a connection failed, including when the Core was healthy
  and this machine simply had no path to it — a VPN capturing the local subnet,
  a link up but not routing. Observed on 2026-09-07: five hours pointing at the
  wrong end of the problem while the Core sat 1.9ms away. When a connection
  fails, tonearm now probes the default gateway (read from the main routing
  table, so it is found even while policy routing diverts traffic) and reports
  the new status `no_network` — "No route to your network" — when the gateway
  itself does not answer. A refused connection counts as reachable, since it
  proves a host answered; an inconclusive probe keeps the old wording rather
  than guess.

- **A Core that changes IP address no longer strands the daemon**
  ([#1](https://github.com/ssandys/tonearm/issues/1)). Discovery used to run
  only when no address was stored, so a new DHCP lease left tonearm retrying a
  dead address forever — recoverable only by editing `config.json` by hand.
  When the Core is unreachable, tonearm now asks the network where it went and
  moves only to a Core it can identify as the one it was paired with: matched
  on the SOOD `unique_id`, now persisted in `config.json`, falling back to the
  Core's name for configs written before this release. A Core answering at the
  address already stored is not a move, so one that is merely rebooting still
  recovers through roonapi with no restart, and an ambiguous or unmatched
  answer is refused rather than adopted.

### Security

- **The relocation check never sweeps the LAN.** `sood.discover()` falls back
  to probing every host on the /24 when multicast goes unanswered — 254 TCP
  connects on a typical network. That is a fair price once, on first run, with
  someone waiting for it; it is not something to repeat. Because the daemon
  retries by exiting and letting systemd restart it, a Core left switched off
  would otherwise have produced a full-subnet scan roughly every 40 seconds for
  as long as it stayed off. The relocation check is multicast-only.
- **An address is written to disk only once a Core has answered on it.**
  Discovery is unauthenticated UDP. A stale or forged reply can no longer
  overwrite an address that works: a candidate is held in memory, and persisted
  only after a successful connection. The connection watcher persists nothing
  at all.
- **Fields from a SOOD response are bounded** (`MAX_SOOD_FIELD`). The wire
  format allows 64KB per value; a Core's name reaches the bar and MPRIS, and is
  persisted. Oversized values are dropped rather than truncated, and the rest of
  the response still parses.

- **The zone arbiter no longer remembers every zone id forever**
  ([#5](https://github.com/ssandys/tonearm/issues/5)). Two dicts keyed on zone
  ids from the Core were never pruned. Not only an abuse case: grouping
  speakers creates a zone of its own, which exists only while the group does —
  measured on a real Core, where grouping two Sonos speakers produced a
  transient `"Sonos Move + 1"` zone that vanished on ungroup. A zone that leaves and later
  returns playing now counts as a fresh start, which is what it is from the
  listener's point of view.
- **Text from the Core is bounded** (`MAX_TEXT`, `MAX_ZONES`,
  [#6](https://github.com/ssandys/tonearm/issues/6)). Every bound in the daemon
  constrained the socket client or files on disk; nothing constrained the Core,
  whose zone names and track metadata are rendered inside `omarchy-shell` — the
  shared, always-loaded process — and published onto the session bus. Clipped
  rather than refused, because these are labels and a truncated title beats no
  title.

### Documentation

- `docs/FOLLOWUPS.md` listed four items as open that were fixed
  ([#7](https://github.com/ssandys/tonearm/issues/7)); they are now in
  **Closed** with the commit that closed each.
- `CONTRIBUTING.md` recommended the `os.lstat` pattern the security review
  rejected ([#8](https://github.com/ssandys/tonearm/issues/8)). Rewritten
  around what was learned: answer from a descriptor you hold, not from a path
  you will re-resolve.
- `docs/marketplace-submission.md` was a second, older copy of a document whose
  canonical version is the submission issue
  ([#9](https://github.com/ssandys/tonearm/issues/9)). Reduced to the standing
  claims that belong to the repository.

Two recurring sources of staleness were removed rather than refreshed: quoted
test counts, and the enumerated list of retired follow-up numbers. Both had
gone wrong twice. Code is now cited by symbol rather than by line.

**Trust model, stated plainly:** SOOD is unauthenticated, and a Core broadcasts
its `unique_id` in the clear, so identity matching cannot authenticate a reply —
an attacker already on your LAN can forge one. What it prevents is *accidental*
capture: a second Core in the house, a neighbour's on a shared network, one that
answers while yours is down. This is the same trust assumption Roon's own
discovery makes, and it is why nothing is persisted until a Core answers.

## 0.10.1 — 2026-09-14

### Security

- **The plugin no longer installs instructions for coding agents.** Installing
  a plugin clones the repository into `~/.config/omarchy/plugins/<id>/`, so a
  root `AGENTS.md` landed in a directory coding agents routinely work in or
  below, where it is discovered and applied automatically. That is an
  instruction channel into the user's agent that the user never agreed to, and
  it is a problem regardless of what the text happens to say. The contributor
  guide is now `CONTRIBUTING.md`, which carries no such meaning. Raised in the
  marketplace security review of 2026-09-14.

  A test (`tests/python/test_installable_tree.py`) now asserts that no tracked
  file is named `AGENTS.md`, `CLAUDE.md`, `.cursorrules` or any sibling,
  asking `git ls-files` because that is exactly what a clone delivers. An
  untracked file in a local worktree is unaffected; nothing about how the
  repository is developed needs to change.
||||||| 36b4fa4


## 0.10.0 — 2026-09-02

A security-hardening release. No new features.

**Upgrading:** re-run `./setup.sh`. It installs a sandboxed systemd unit and
creates `~/.config/tonearm`, which that unit now requires to exist before it
will start. Skipping this leaves the old unit in place; the daemon still runs,
without the sandbox.

### Fixed — marketplace security review

Four findings from the review of `d4a3513` on the Omarchy plugin marketplace
([#3414](https://github.com/HANCORE-linux/omarchy-plugin-marketplace/issues/3414)).

- **State I/O is descriptor-relative, bounded and no-follow.** `~/.config/tonearm`
  is opened once `O_DIRECTORY|O_NOFOLLOW` and every leaf is opened against that
  descriptor, so the path walk happens once. Reads add `O_NOFOLLOW`, an
  `S_ISREG` check and a size cap (64 KiB config, 4 KiB token); writes land on an
  unguessable name created `O_EXCL|O_NOFOLLOW`. The predictable `<path>.tmp`,
  opened `O_CREAT|O_TRUNC`, had let a planted symlink redirect the write of the
  Roon pairing token. The state directory is also `fchmod`ed back to 0700, which
  `makedirs(mode=…)` never did for an existing one.
- **Every socket resource is bounded.** Handler threads (32), time to send a
  request (10s), registered subscribers (16), one write to a subscriber (5s),
  and in-memory browse sessions (8, LRU). A client that connected and never
  sent a newline previously parked a handler thread forever.
- **No I/O runs under the subscriber lock.** `broadcast()` copies the list under
  the lock and writes outside it, with a per-connection write lock preserving
  ordering. One peer that had stopped reading used to stall every other
  subscriber and every new subscribe.
- **The album-art fetch is pinned to the Core's origin and validated.**
  Redirects off `(scheme, host, port)` are refused; bodies must be a PNG or
  JPEG within 2048px per side. A 1 MiB byte cap cannot see a decode bomb, and
  the file is read by `ColorQuantizer` inside the shared shell process.
- **`is_publishable()` answers from the descriptor it opened**, not an
  `os.lstat` of a path it never opened.
- **The discovery scan has a deduplicated total budget** of 512 addresses
  across all interfaces, replacing 254 per qualifying interface with no ceiling.

### Fixed — found while auditing for the same shapes

- **The systemd unit is sandboxed.** `ProtectSystem=strict`,
  `ProtectHome=read-only` with a single `ReadWritePaths`, a syscall filter and a
  restricted address-family set. Verified against a running daemon: writes to
  `~/.bashrc` and to the source tree both fail `EROFS` while Roon, MPRIS and
  album art continue to work. `AF_NETLINK` is required and documented in the
  unit — glibc's `if_nameindex(3)` needs it, and without it discovery silently
  finds nothing.
- **A dead socket server is now a dead process.** It ran on a daemon thread, so
  an exception killed the thread and nothing else: systemd reported the unit
  active with 0 restarts and no socket on disk, and nothing ever retried.
  `Restart=on-failure` can now do its job. A clean `systemctl --user stop` still
  exits 0.
- **`shutdown()` reaches a blocked `accept()`.** Closing the listening socket
  does not interrupt a thread already in `accept()`; the loop now polls.
- **Browse sessions are bounded and their key is validated.** `session` came
  off the wire into a dict nothing evicted, and was not required to be a string.
- **The zone pin argument is bounded.** It is the only wire argument written to
  `config.json`; a large enough value would push that file past its own read cap,
  so the next start would refuse the whole config and re-run discovery.
- **`search` terms are bounded** at 512 characters.
- **A non-object JSON request is refused** instead of raising `AttributeError`
  in a handler thread, which left the socket open and the client waiting.
- **`Model.js` percent-encodes `image_key`.** The daemon already treated it as
  untrusted; the widget built the same URL by concatenation, so a key carrying
  `&` or `?` rewrote the query and one carrying `/` walked the path.

### Changed

- `setup.sh` creates `~/.config/tonearm` at 0700. Under `ProtectHome=read-only`
  the daemon can no longer create it, and systemd refuses to start a unit whose
  `ReadWritePaths` names a missing directory. A test parses the shipped unit and
  asserts every entry is a directory the installer creates.
- The socket answers `bad_request` for a malformed `session` or `term`.
- `README.md` and `docs/marketplace-submission.md` state the scan budget and the
  full set of socket bounds.

## 0.9.0 — 2026-08-29

First submission to the Omarchy plugin marketplace. Roon now-playing,
transport, library search, zone switching and transfer.
