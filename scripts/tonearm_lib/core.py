"""The Roon side of tonearmd: connect, pair, subscribe, issue transport.

This module is mostly I/O. Everything decidable without a Core lives in
state.py and zones.py, which is why most of this file has no unit tests --
verify it against yavin. `_seeded_api()` and `_connect_timeout()` are the
exceptions: pure, Core-independent decisions with no I/O of their own, and
both have regression tests in tests/python/test_core.py.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor"))

from roonapi import RoonApi              # noqa: E402  (vendored, path-inserted)

from . import browse, config, sood, state, zones  # noqa: E402

LOG = logging.getLogger("tonearmd.core")

APPINFO = {
    "extension_id": "com.onemanposse.tonearm",
    "display_name": "tonearm",
    "display_version": "0.1.0",
    "publisher": "Sean Sandys",
    "email": "sean@onemanposse.com",
}

# Seconds to wait for one port to answer the MOO/WS handshake before giving
# up on it, when a token is ALREADY known (a paired reconnect). Measured
# against yavin (Roon 2.71 build 1683): the SOOD-advertised tcp_port (9150)
# accepts a TCP connection and then never answers the WebSocket upgrade -- a
# silent hang, not an error. RoonApi's own blocking_init has no ceiling on
# that wait, so without a bound here RoonApi.__init__ blocks forever and the
# daemon looks "healthy" while doing nothing. See
# docs/superpowers/specs/2026-08-27-tonearm-design.md S2.1.
#
# This bound is deliberately short: a paired Core that does not answer
# quickly is either down or not yet reachable (a boot-ordering race), and
# `start()` exits so systemd's `Restart=on-failure` can retry rather than
# blocking this thread for minutes on a Core that is genuinely absent. See
# PAIRING_TIMEOUT for the other case this constant used to (incorrectly)
# also cover.
CONNECT_TIMEOUT = 10.0

# Seconds to wait for one port to answer, when NO token is known yet (first
# run, unpaired). `RoonApi.__init__(blocking_init=True)` does not return
# until the Core answers `registry/register`, and Roon does not answer that
# until a human opens Roon Remote -> Settings -> Extensions and clicks
# Enable by hand -- an action the README's own install steps put AFTER
# starting the service. Reusing CONNECT_TIMEOUT here was the bug: a 10s
# budget guards the silent-hang hazard above, but it also silently ate the
# entire pairing window, so `_connect` gave up long before any human could
# plausibly have clicked anything. This budget only needs to be "long enough
# for a person to switch apps and tap a button", not unbounded -- it still
# protects against the same 9150 silent-hang hazard, just on a timescale a
# first-run pairing actually needs.
PAIRING_TIMEOUT = 240.0

# Seconds to wait, after signalling a timed-out attempt to give up, for it to
# actually finish before moving on to the next candidate port regardless.
#
# This is NOT what makes cross-instance state sharing safe -- that is
# `_seeded_api()`, below, which gives every RoonApi instance its own private
# `_zones`/`_outputs` from the moment it is created, before anything else
# can touch it. That closes the actual correctness risk deterministically,
# with no dependence on timing. See `_seeded_api()`'s docstring.
#
# What STOP_GRACE still does: narrow how long a timed-out attempt's thread
# and open socket can go on existing in the background before `_connect`
# moves to the next port. For an abandoned attempt that eventually reaches
# registration late (a genuinely slow-but-working port, not a dead one),
# this reliably lets it finish and exit -- its zones/outputs prefetch is
# bounded purely by retry count (roonapi.py:983-996, ~2.5s per call,
# independent of `_exit` or socket state) once `ready` is True. Two such
# calls bound to ~5.0s, the same as this constant -- an exact tie, not a
# comfortable margin, once the ready-wait loop's own 50ms poll granularity,
# `send_request()`'s round trip, and scheduling jitter are accounted for.
# That imprecision no longer matters for correctness now that `_seeded_api`
# isolates instance state regardless of timing; it only means a slow port
# occasionally still gets logged as "did not unwind" when it was in fact
# about to.
#
# For an abandoned attempt that never gets past the initial handshake read
# at all -- port 9150's actual measured behavior against yavin (TCP
# connects, then silence, forever) -- this is a THREAD AND SOCKET LEAK, not
# a data risk: `on_open` never fires, so `_socket_connected()` never sends
# `registry/register`, so `_server_registered()` never runs, so `.subscribe`
# is never called and the "zones"/"outputs" item-mutation at roonapi.py:
# 900-903 is never reached for that instance at all -- there is nothing left
# for it to corrupt. Measured directly: a local test server reproducing that
# exact behavior (accept the connection, send nothing) left the abandoned
# thread still blocked in that read even after stop()'s
# self._socket.close() and this full grace period elapsed -- it only
# unblocked once the test closed the server socket and a RST arrived.
# Nothing on our side can manufacture that RST; only the remote Core (or a
# firewall) can, and by definition it never does for this specific failure.
# No finite STOP_GRACE closes this leak, so on timeout this code proceeds to
# the next port anyway (logged loudly) rather than letting one permanently
# dead port block discovery of a working one forever. In practice this leak
# is rarely exercised at all: `_candidate_ports()` tries `http_port` first,
# and `http_port` has answered reliably every time it has been observed --
# `tcp_port` is the one that leaks, and it is only reached as a fallback.
STOP_GRACE = 5.0


def _seeded_api() -> RoonApi:
    """A bare RoonApi instance with its own private `_zones`/`_outputs`.

    `_zones` and `_outputs` are mutable CLASS-level defaults in the vendored
    library (roonapi.py:52-53), not instance attributes. Until an instance
    reassigns them itself -- which RoonApi.__init__ does as soon as its
    blocking wait loop exits (whether because `ready` became True, the
    normal case, or because `stop()` set `_exit` on a timed-out attempt) and
    `self.token` is truthy (roonapi.py:809-813) -- any write to
    `self._zones[...]` from anywhere lands in the ONE dict every instance
    shares. That gate is on `self.token` alone, not on `ready`, so this
    reassignment also runs for an abandoned, timed-out instance whenever a
    token was already known (a paired reconnect) -- it is not limited to
    instances that actually finished registering. With a single connection
    attempt per process that was invisible; with port fallback, a
    timed-out-and-abandoned attempt and a fresh attempt for the next port
    can briefly coexist, and if the abandoned one ever reaches registration
    late, its subscription could write into the dict the new instance is
    also relying on.

    Assigning fresh instance dicts here -- via `RoonApi.__new__`, before
    `__init__` (and therefore `_server_setup()`, which starts the
    background socket thread) ever runs -- closes that deterministically
    rather than merely narrowing the window: at the moment these lines run,
    the instance exists but nothing else does yet, so there is no other
    thread that could be racing this assignment. Every instance this
    function returns, whether it goes on to win or get abandoned, is
    isolated from the shared class-level dict for its entire life. See
    STOP_GRACE's comment for what is left for it to still guard (thread and
    socket cleanup, not correctness) once instances no longer share state.
    """
    api = RoonApi.__new__(RoonApi)
    api._zones = {}
    api._outputs = {}
    return api


def _connect_timeout(token) -> float:
    """How long one candidate port's connect attempt is allowed to run.

    A pure decision, Core-independent and deliberately factored out so it
    has its own regression test (test_core.py) rather than being buried
    inline in `_connect`, where only a live-yavin check would exercise it.

    No token yet means first run: RoonApi blocks until a human clicks
    Enable in Roon Remote, which needs a realistic window (PAIRING_TIMEOUT).
    A token already on disk means this Core has answered before: fail fast
    (CONNECT_TIMEOUT) and let `start()` exit so systemd's `Restart=on-failure`
    retries, rather than blocking this thread for minutes on a Core that is
    genuinely down or not yet reachable (boot ordering).
    """
    return PAIRING_TIMEOUT if token is None else CONNECT_TIMEOUT


class RoonSession:
    """Owns the Roon connection and publishes normalized state on change."""

    def __init__(self, on_change) -> None:
        self._on_change = on_change
        self._api: RoonApi | None = None
        self._cfg = config.load()
        self._arbiter = zones.Arbiter(self._cfg.get("pinned_zone_id"))
        self._status = "connecting"
        self._lock = threading.Lock()
        # Separate from every other lock in this class. A browse round-trip is
        # far slower than a snapshot; sharing a lock with the publish path
        # would stall subscribers (spec 7.5).
        self._browse_lock = threading.Lock()
        self._browse_sessions: dict = {}

    @property
    def status(self) -> str:
        return self._status

    def snapshot(self) -> dict:
        core = None
        if self._cfg.get("host"):
            core = {"host": self._cfg["host"],
                    "http_port": self._cfg.get("http_port", 9330),
                    "name": self._cfg.get("name") or self._cfg["host"]}
        listing, selected = self._zones()
        return state.build(self._status, core, selected, listing)

    def _zones(self):
        if not self._api:
            return [], None
        raw = self._raw_zones()
        listing = [z for z in (state.normalize_zone(r) for r in raw) if z]
        self._arbiter.observe(listing)
        return listing, self._arbiter.select(listing)

    def _raw_zones(self) -> list:
        """A defensive read of `self._api.zones.values()`.

        `self._api.zones` is a plain dict, mutated IN PLACE by roonapi's own
        websocket thread (`RoonApiWebSocket.run_forever()` ->
        `_on_state_change()`, roonapisocket.py/roonapi.py) as zones are
        added, updated or removed -- entirely outside anything this class
        (or `CachingSession`'s lock, in server.py's caller) controls. A zone
        being added or removed changes the dict's size, so IN PRINCIPLE a
        caller here could be mid-iteration when that happens: `list(...
        values())` would then raise `RuntimeError: dictionary changed size
        during iteration`, which nothing downstream catches -- it would
        escape through `snapshot()` and kill whichever connection thread in
        server.py was reading state at that instant.

        Nuance worth recording so nobody "proves" this guard unnecessary and
        removes it: on a normal GIL-enabled CPython, `list(d.values())` is
        one uninterrupted C call and cannot actually raise mid-materialisation
        -- measured at 4,548+ reads under sustained size-changing mutation
        from a real background thread, zero errors, while a Python-level loop
        over the same dict raised after 3 iterations. This is NOT a bug
        reproducible today; the retry below is forward-looking insurance for
        a free-threaded (PEP 703) build, or any future refactor that splits
        this read across a bytecode boundary (e.g. a generator or explicit
        loop instead of `list(...values())`), either of which would let the
        window this guards against actually open up.

        One retry closes it if it ever does: the window would be a handful
        of microseconds around a single dict mutation, and landing in it
        twice in a row would not happen. If it somehow did anyway, an empty
        listing is what `self._api` being unset already produces elsewhere
        in this class -- a graceful "nothing to report" rather than a crash.
        """
        for _ in range(2):
            try:
                return list((self._api.zones or {}).values())
            except RuntimeError:
                continue
        return []

    def _publish(self, *_args) -> None:
        try:
            self._on_change(self.snapshot())
        except Exception:
            LOG.exception("publish failed")

    def start(self) -> None:
        """Discover (if needed), pair or reconnect, and subscribe.

        `start()` is called exactly once per process (scripts/tonearmd). It
        does not loop or retry internally: on either failure path below it
        publishes the honest terminal status and then calls `sys.exit(1)`,
        letting the process die and the systemd unit's own
        `Restart=on-failure` / `RestartSec=3` (systemd/tonearmd.service)
        restart it from scratch a few seconds later.

        This is the cheapest correct option, and preferred here over an
        internal retry loop for two reasons: systemd already implements
        backoff/restart-accounting correctly and is going to be watching
        this unit regardless, so a second implementation inside the process
        would be pure duplication; and a hung connect attempt that could
        never be cleanly cancelled (see STOP_GRACE) is safest resolved by
        the OS tearing down the whole process rather than by this code
        trying to loop around it forever in place. The cost is a ~3s gap
        with no daemon at all between attempts, which is immaterial next to
        the multi-minute pairing window this is largely in service of.

        Both failure points below ("no Core found" and "no Core answered")
        are instances of the same underlying problem -- the Core was not
        reachable *yet*, whether because the network/Core is still booting
        or because a human has not clicked Enable in Roon Remote yet -- so
        both retry the same way.
        """
        if not self._cfg.get("host"):
            cores = sood.discover()
            if not cores:
                self._status = "unreachable"
                self._publish()
                sys.exit(1)
            core = cores[0]
            LOG.info("discovered %s at %s via %s",
                     core["name"], core["host"], core["via"])
            self._cfg.update({k: core[k] for k in ("host", "tcp_port", "http_port", "name")})
            config.save(self._cfg)

        token = config.load_token()
        if token is None:
            # First run: RoonApi blocks until the extension is enabled by hand
            # in Roon Remote -> Settings -> Extensions. See PAIRING_TIMEOUT.
            self._status = "unpaired"
            self._publish()

        self._api = self._connect(token)
        if self._api is None:
            self._status = "unreachable"
            self._publish()
            sys.exit(1)

        if self._api.token:
            config.save_token(self._api.token)

        self._status = "ok"
        self._api.register_state_callback(self._publish)
        self._publish()

    def _candidate_ports(self) -> list[int]:
        """MOO/WS ports to try, in order.

        SOOD advertises `tcp_port`, but on Roon 2.71 that port accepts the
        TCP connection and then never answers the WebSocket handshake -- MOO
        actually lives on `http_port` on this Core. Try `http_port` first,
        falling back to `tcp_port` for an older Core where the advertised
        port really is the live one. Never hardcode or swap the two: both
        stay in config exactly as discovered/reported.
        """
        http_port = self._cfg.get("http_port") or 9330
        tcp_port = self._cfg.get("tcp_port") or 9150
        ports = [http_port]
        if tcp_port != http_port:
            ports.append(tcp_port)
        return ports

    def _connect(self, token) -> RoonApi | None:
        """Try each candidate port in turn, each bounded by `_connect_timeout(token)`.
        Returns a ready RoonApi, or None if nothing answered on any port.

        The per-port budget depends on whether we already hold a token: see
        `_connect_timeout()`. Ports are tried strictly one at a time.
        `_try_port` waits (bounded by STOP_GRACE) for a timed-out attempt to
        unwind before returning, so a second RoonApi instance is rarely even
        alive at the same time as the first -- but that is no longer what
        keeps this safe. Every instance `_try_port` creates gets its own
        private `_zones`/`_outputs` from `_seeded_api()` before anything
        else can touch them, so even on the rare occasion two do overlap,
        neither can corrupt the other's state. See `_seeded_api()`'s and
        STOP_GRACE's comments.
        """
        host = self._cfg["host"]
        ports = self._candidate_ports()
        timeout = _connect_timeout(token)
        for port in ports:
            api = self._try_port(host, port, token, timeout)
            if api is not None:
                return api
        LOG.error("no MOO response from %s on any of %s (%.0fs each)",
                  host, ports, timeout)
        return None

    @staticmethod
    def _try_port(host: str, port: int, token, timeout: float) -> RoonApi | None:
        """Connect on one port, bounded by `timeout` (see `_connect_timeout`).

        This deliberately does NOT use RoonApi(..., blocking_init=False) to
        get a bound. That looked like the obvious approach, but when a token
        is already known it backfires: blocking_init only skips the
        ready-wait loop, not the unconditional `if self.token: self._zones =
        self._get_zones()` step right after it -- and that step fires before
        the socket has even connected. It always loses that race, times out
        after its own ~2.5s retry budget, and then *overwrites* self._zones
        with {} -- discarding the real zone list the async "zones"
        subscription delivers correctly a few milliseconds after
        registration (self._zones is also a mutable class-level default,
        which is how the subscription callback can populate it before the
        instance even has its own copy). Confirmed against yavin by
        instrumenting _on_state_change: the two real zones land, then get
        clobbered by the trailing empty-dict assignment. Zones would then
        stay empty forever for a zone that never changes state again.

        blocking_init=True does not have that race -- its zones/outputs
        prefetch only runs once `ready` is confirmed, so it reliably
        succeeds. Its own wait loop is unbounded, though (nothing ever sets
        `ready` or `_exit` if the port never answers), which is the original
        problem. So: keep blocking_init=True's safe ordering, but run it in
        our own thread and bound it with an external join timeout. __new__
        is called separately from __init__ (via `_seeded_api()`) so there is
        a handle to `api` to call .stop() on even if __init__ itself never
        returns -- and so `_zones`/`_outputs` can be seeded as private
        instance dicts before __init__ starts the background socket thread
        that could otherwise race that assignment. See `_seeded_api()`.

        A timed-out attempt is also joined again (bounded by STOP_GRACE)
        after stop() before this returns. That is not what prevents state
        corruption -- `_seeded_api()` already guarantees this instance can
        never mutate another's `_zones`/`_outputs`, regardless of timing --
        it just narrows how long the abandoned attempt's thread and open
        socket can go on existing before `_connect` moves to the next port.
        """
        api = _seeded_api()
        errors: list[Exception] = []

        def init() -> None:
            try:
                RoonApi.__init__(api, APPINFO, token, host, port, blocking_init=True)
            except Exception as exc:  # reported via `errors`, not swallowed
                errors.append(exc)

        thread = threading.Thread(target=init, daemon=True)
        thread.start()
        thread.join(timeout)

        if thread.is_alive():
            # Silent hang: TCP connected but the WebSocket handshake never
            # answered -- exactly what the SOOD-advertised tcp_port does on
            # this Roon version -- OR (when `timeout` is PAIRING_TIMEOUT) a
            # human simply has not clicked Enable in Roon Remote yet. Signal
            # the stuck init to give up, then wait (bounded) to actually
            # confirm it has, before returning -- this is thread/socket-leak
            # hygiene, not a correctness guard (that is `_seeded_api()`'s
            # job). See STOP_GRACE's comment for what this join does and
            # does not clean up.
            LOG.warning("no response from %s:%s within %.0fs", host, port, timeout)
            try:
                api.stop()
            except Exception:
                LOG.exception("error tearing down timed-out connection to %s:%s", host, port)
            thread.join(STOP_GRACE)
            if thread.is_alive():
                LOG.error(
                    "connection attempt to %s:%s did not unwind within %.0fs of "
                    "stop(); abandoning it and moving on. It may still be running "
                    "and could briefly overlap with the next connection attempt.",
                    host, port, STOP_GRACE)
            return None

        if errors:
            LOG.exception("connect to %s:%s failed", host, port, exc_info=errors[0])
            return None

        LOG.info("connected to %s:%s", host, port)
        return api

    def stop(self) -> None:
        if self._api:
            self._api.stop()
            self._api = None

    # -- browse -----------------------------------------------------------
    def selected_zone_id(self):
        """The followed/pinned zone's id, or None. Read fresh on every call.

        This is the SAME arbitration `_command_locked` uses to route
        playpause/seek/volume, deliberately: a browse action must play into
        the zone the bar is showing and the transport controls already drive,
        never into a second, separately-chosen one. The widget does not get a
        say -- spec 3 keeps the pinned zone the single target, and the popup
        already has a zone switcher for changing it.

        Passed to BrowseSession as a callable rather than a value so a repin
        between two browses is picked up (see BrowseSession.__init__). It is
        therefore called several times per browse op; that is a dict
        comprehension over a handful of zones next to a network round-trip.

        Takes no lock. `_zones()` is exactly what `snapshot()` already calls
        off the publish path with no lock, and taking `self._lock` here would
        put a browse round-trip's worth of Roon latency behind the same lock
        every transport command uses (spec 7.5).
        """
        _, selected = self._zones()
        return selected["id"] if selected else None

    def browse_session(self, key: str):
        """One BrowseSession per multi_session_key, created on first use.

        Sessions are in-memory and lost on restart (spec 7.4); the widget's
        next request rebuilds from root. Not bounded today because only the
        widget uses one -- add an LRU cap if consumers multiply (spec 11).
        """
        with self._browse_lock:
            existing = self._browse_sessions.get(key)
            if existing is None:
                existing = browse.BrowseSession(
                    self._api, key, self.selected_zone_id)
                self._browse_sessions[key] = existing
            return existing

    def browse(self, key: str, op: str, **kwargs) -> dict:
        """Dispatch one browse op. Never takes Server._lock (spec 7.5)."""
        if self._status != "ok":
            raise browse.BrowseError("unreachable", "Roon Core unreachable")
        session = self.browse_session(key)
        if op == "search":
            return session.search(kwargs.get("term") or "")
        if op == "enter":
            return session.enter(kwargs.get("index"), kwargs.get("level_id"))
        if op == "activate":
            return session.activate(kwargs.get("index"), kwargs.get("level_id"))
        if op == "play":
            return session.play(kwargs.get("index"),
                                kwargs.get("action") or "play_now",
                                kwargs.get("level_id"))
        if op == "back":
            return session.back()
        if op == "page":
            return session.page(kwargs.get("offset") or 0)
        if op == "reset":
            return session.reset()
        raise browse.BrowseError("bad_index", "unknown browse op %r" % (op,))

    # -- commands -------------------------------------------------------
    def command(self, verb: str, arg=None) -> None:
        with self._lock:
            self._command_locked(verb, arg)

    def _pin_locked(self, zone_id: str | None) -> None:
        """Follow `zone_id` (or resume auto-follow when None), and persist it.

        Shared by the `zone` verb and by `transfer`, which re-pins to the
        destination so a pinned widget is not left watching the room it just
        emptied. Factored out rather than duplicated because the arbiter
        update and the config write have to stay in step -- a caller that did
        one without the other would follow the right zone until the next
        restart and then silently revert.
        """
        if zone_id is None:
            self._arbiter.unpin()
        else:
            self._arbiter.pin(zone_id)
        self._cfg["pinned_zone_id"] = zone_id
        config.save(self._cfg)
        self._publish()

    def _command_locked(self, verb: str, arg) -> None:
        if verb == "zone":
            self._pin_locked(None if arg == "unpin" else arg)
            return

        if not self._api:
            LOG.warning("dropping %s: not connected", verb)
            return
        _, selected = self._zones()
        if not selected:
            LOG.warning("dropping %s: no followed zone", verb)
            return
        zid = selected["id"]

        if verb in ("playpause", "play", "pause", "next", "previous"):
            self._api.playback_control(zid, verb)
        elif verb == "seek":
            self._api.seek(zid, int(arg), "absolute")
        elif verb == "volume":
            # state.py's _volume_of reports outputs[0]'s raw min/max/value
            # scale (not a 0-100 percent), and only outputs[0]'s -- so the
            # widget's slider is always outputs[0]'s scale, never an
            # average or any other output's. The write must target the same
            # single output the read came from: in a grouped zone, outputs
            # can have different volume ranges (e.g. a -80..0 dB streamer
            # and a 0..100 amp in one group), and applying one output's
            # absolute value to another's scale would be wrong on its face,
            # not just imprecise. It would also silently discard the user's
            # deliberate per-output balance across the group. change_volume_raw
            # (not change_volume_percent) matches what the widget's slider shows.
            outputs = self._api.zones.get(zid, {}).get("outputs") or []
            if outputs:
                self._api.change_volume_raw(outputs[0]["output_id"], int(arg), "absolute")
        elif verb == "transfer":
            # `zid` is the followed zone, resolved above -- the source is never
            # the caller's to choose, so the only argument is the destination.
            #
            # Both guards keep a click from becoming a request Roon would
            # accept and act on pointlessly: transferring a zone onto itself,
            # and a destination id from a widget whose zone list predates a
            # room disappearing.
            if arg == zid:
                LOG.warning("dropping transfer: %r is already the followed zone", arg)
                return
            if arg not in self._api.zones:
                LOG.warning("dropping transfer: unknown destination %r", arg)
                return
            self._api.transfer_zone(zid, arg)
            # Follow the music, but only for a user who had already chosen a
            # room. Unpinned, the arbiter's auto-follow lands on the
            # destination by itself once audio starts there; pinning here would
            # convert someone who deliberately follows the music into someone
            # locked to one zone.
            if self._arbiter.pinned_id is not None:
                self._pin_locked(arg)
        elif verb in ("mute", "unmute"):
            # Unlike volume, mute/unmute deliberately applies to every output
            # in the zone, not just outputs[0]: muting the whole zone (not
            # just the output whose volume happens to be displayed) is the
            # reading a user expects from one mute control for a grouped zone.
            for output in (self._api.zones.get(zid, {}).get("outputs") or []):
                self._api.mute(output["output_id"], verb == "mute")
        else:
            LOG.warning("unknown verb %r", verb)
