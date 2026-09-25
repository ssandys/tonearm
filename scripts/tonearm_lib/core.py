"""The Roon side of tonearmd: connect, pair, subscribe, issue transport.

This module is mostly I/O. Everything decidable without a Core lives in
state.py and zones.py, which is why most of this file has no unit tests --
verify it against yavin. `_seeded_api()` and `_connect_timeout()` are the
exceptions: pure, Core-independent decisions with no I/O of their own, and
both have regression tests in tests/python/test_core.py.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor"))

from roonapi import RoonApi              # noqa: E402  (vendored, path-inserted)

from . import browse, config, net, sood, state, zones  # noqa: E402

LOG = logging.getLogger("tonearmd.core")

VERSION_FALLBACK = "0.0.0"


def manifest_path() -> str:
    """The plugin manifest, two levels up from this module.

    A function rather than a constant so a test can point the reader below at
    a file that is missing or malformed and actually exercise the fallback,
    instead of only asserting it is not being used.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "manifest.json")


def plugin_version(path: str | None = None) -> str:
    """The plugin's own version, read from the manifest rather than restated.

    Roon shows this in Settings -> Extensions. It drifted for exactly the
    reason two copies of a fact always do: the manifest reached 0.9.0 while
    this stayed at the 0.1.0 it was first written with, because nothing
    connected them and nothing could notice. The manifest ships in the same
    directory tree as this file, so there is no reason for a second copy.

    Falls back rather than raising. A daemon that refuses to start because it
    cannot read its own version number is a worse failure than one reporting
    the wrong one -- and the version reaches nothing but a label in Roon's
    extension list.
    """
    try:
        with open(path or manifest_path()) as handle:
            return json.load(handle).get("version") or VERSION_FALLBACK
    except (OSError, ValueError):
        return VERSION_FALLBACK


APPINFO = {
    "extension_id": "com.onemanposse.tonearm",
    "display_name": "tonearm",
    "display_version": plugin_version(),
    "publisher": "Sean Sandys",
    "email": "sean@onemanposse.com",
}

# Concurrent browse sessions kept in memory. The key is a wire field, so this
# is a ceiling on what a client can make the daemon hold, not a tuning knob:
# the widget uses one PER BAR SURFACE (one per monitor -- a browse cursor
# shared between two monitors means navigating on one re-renders the other),
# tonearmd-mcp uses one, and `tonearmctl browse` uses one. Eight therefore
# covers a six-monitor desk alongside both other consumers; past that the LRU
# eviction below is the answer rather than a larger number, since an evicted
# consumer rebuilds from root on its next request.
MAX_BROWSE_SESSIONS = 8

# Longest zone id accepted from a client. Roon's are UUID-shaped, around 36
# characters. This is the only wire argument that reaches persistent state
# (the pin, written to config.json), so it is bounded where it is used rather
# than trusted to be reasonable.
MAX_ZONE_ID = 128

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


# How often the connection watcher samples, and how many consecutive down
# samples it takes to report a fault. 2s x 2 = ~4s to surface a drop, well
# inside roonapi's own ~21s reconnect backoff, while still riding out a socket
# that closes and reopens between polls.
POLL_INTERVAL = 2.0
DOWN_SAMPLES = 2

# How long an outage must last before the daemon suspects the Core has MOVED
# rather than merely gone away for a while, and asks the network where it went.
# roonapi's own reconnect (a fresh socket ~21s after each failure, forever) gets
# several clean attempts inside this window: a Core that comes back at the same
# address recovers with no restart at all, which is strictly cheaper than the
# exit/rediscover/reconnect cycle a relocation costs. Re-checked once per window
# for as long as the outage lasts, since a Core can move while already down.
RELOCATE_AFTER = 120.0
RELOCATE_SAMPLES = int(RELOCATE_AFTER / POLL_INTERVAL)

# Relocation windows that may fall back to the /24 sweep before backing off,
# and how often to retry after that.
#
# Multicast SOOD never arrives on some networks -- measured 0/7 on the LAN this
# was written for, including four 12s windows, while the sweep found the Core
# 3/6 (#17). Without a sweep, relocation cannot work there at all.
#
# The sweep is 254 connects and ~8s, and relocation retries every
# RELOCATE_AFTER for as long as an outage lasts, so sweeping every window would
# hunt a switched-off Core all day. The first few sweep regardless, because at
# roughly 50% a single sweep is a coin flip on whether the bar ever heals; 4
# windows is ~94% cumulative inside about eight minutes. After that, every
# eighth window (~16 minutes) still catches a Core that returns at a new
# address overnight.
#
# NOT gated on the Core's last-known subnet, which was the first design and
# could lock itself out: a router swap puts everything on a new /24, the
# stored address is then in no local subnet, no sweep is permitted, the stored
# address is never refreshed, and the gate never reopens -- the same shape as
# a stale unique_id disabling relocation for good. _local_networks() already
# bounds this in space by scanning private /24s only; these bound it in time.
# Distinct relocation conclusions remembered per outage, so the same one is
# not reported twice while discovery flaps. A ceiling rather than a tuning
# knob: the entries are derived from what discovery returns, which a changing
# LAN can vary without limit.
MAX_REPORTED_NOTES = 32

SWEEP_EAGER_WINDOWS = 4
SWEEP_BACKOFF_WINDOWS = 8


def _unreachable_status(host=None) -> str:
    """Name the fault behind a connect that never answered.

    The Core not answering and this machine having no path to the LAN are
    the same symptom, and the difference is the whole of what the person
    reading the bar needs. An inconclusive probe keeps the old wording --
    telling someone their network is down when it is not would be a worse
    error than the one this fixes.
    """
    if host:
        fault = net.routed_off_lan(host, net.source_address_for(host),
                                   net.local_networks())
        if fault is True:
            return "no_network"
    return "unreachable"


def _should_sweep(window: int) -> bool:
    """May this relocation window pay for the /24 sweep?

    `window` counts consecutive relocation attempts that did not RESOLVE --
    not ones where discovery came back empty. The difference matters: a config
    carrying an identity no Core can match makes every sweep succeed at
    discovery and every adoption fail, so counting empty results would reset
    forever and sweep every window against a fault no sweep can fix.
    """
    if window < 1:
        return False
    if window <= SWEEP_EAGER_WINDOWS:
        return True
    return (window - SWEEP_EAGER_WINDOWS) % SWEEP_BACKOFF_WINDOWS == 0


def _relocation_candidates(cfg: dict, cores: list[dict]) -> tuple[list[dict], str]:
    """Discovered Cores that could be ours, and what was used to identify them.

    Shared by the decision (`_relocated_core`) and the explanation
    (`_relocation_refusal`) so there is exactly one matching rule. Two copies
    would drift, and an explanation that contradicts the decision it is
    explaining is worse than no explanation at all.
    """
    unique_id = cfg.get("unique_id")
    name = cfg.get("name")
    if unique_id:
        return [c for c in cores if c.get("unique_id") == unique_id], "unique_id"
    if name and name != cfg.get("host"):
        return [c for c in cores if c.get("name") == name], "name"
    return list(cores), "sole-Core-on-the-LAN rule"


def _describe_cores(cores: list[dict]) -> str:
    """Cores as a reader needs them: the name to recognise, the address to try."""
    return ", ".join("%s at %s" % (c.get("name") or "?", c.get("host"))
                     for c in cores)


def _relocation_note(cfg: dict, cores: list[dict]) -> str | None:
    """What this relocation attempt is worth saying, or None to stay quiet.

    The daemon could see a Core on the LAN, decline to adopt it, and say
    nothing -- leaving "your Core is switched off" and "your Core is right
    there and I will not touch it" identical in the journal, when they are
    opposite problems with opposite remedies (#15). Nothing here softens the
    refusal itself, which is correct; this only makes it audible.

    Empty `cores` is reported, not passed over. The first version of this
    stayed silent on the grounds that nothing discovered means the Core is
    off and roonapi already says so -- and that premise was measured false on
    the network this was written for: multicast SOOD never arrives over its
    Wi-Fi, so discovery comes back empty while the Core is up and answering a
    unicast probe. That silence is exactly what left the original incident
    with no signal anywhere. The note names both readings because this cannot
    distinguish them, and naming only the likelier one misdirects the reader
    half the time.

    Silent in two cases, both on purpose:

      * exactly one candidate that IS a move -- `_find_relocated` logs that
        itself, and reporting it twice reads as two separate events.
      * exactly one candidate already at the configured address -- a Core
        rebooting rather than moving, which needs no comment.
    """
    if not cores:
        return ("discovery found no Cores on the LAN: the Core is switched "
                "off, or its SOOD replies are not reaching this host")
    matches, by = _relocation_candidates(cfg, cores)
    if len(matches) == 1:
        return None
    if not matches:
        return ("discovery found %s, but none matched the stored %s; "
                "not relocating" % (_describe_cores(cores), by))
    return ("discovery found %d Cores matching the stored %s (%s); "
            "refusing to choose between them"
            % (len(matches), by, _describe_cores(matches)))


def _relocated_core(cfg: dict, cores: list[dict]) -> dict | None:
    """Which discovered Core, if any, is ours at a NEW address? None if none is.

    A pure decision, factored out of its two callers (`start()` and the
    connection watcher) so the "is this really our Core?" rule has its own
    regression test rather than being reachable only with a live LAN.

    What this rule does and does not buy is worth being exact about. SOOD is
    unauthenticated UDP and a Core broadcasts its `unique_id` in the clear, so
    an attacker on the same LAN can forge a reply carrying the right id and
    name; matching cannot authenticate anything. What it does prevent is
    ACCIDENTAL capture -- the second Core in the house, a neighbour's on a
    shared network, a Core that answers while ours is down -- which is the
    realistic failure. The daemon inherits Roon's own LAN trust assumption
    here, and `_find_relocated`'s caller keeps the blast radius small by never
    persisting an address until a Core has answered on it.

    The rule is deliberately conservative, because adopting the wrong answer
    points a paired daemon -- and the user's widget -- at a stranger's Roon:

      * a stored `unique_id` must match exactly, and nothing else counts. Two
        Cores can share a display name; only the id is identity.
      * without one (every config written before 0.11.0), the Core's name
        identifies it -- unless `name` is just the address, which is
        config.DEFAULTS' fallback label rather than anything the Core said.
      * with no identity at all (a hand-written config: an address and nothing
        else), a LAN holding exactly one Core is unambiguous -- that is the
        Core a fresh install would have discovered. Two or more, and we refuse.

    An answer at the address already in config is not a relocation: that is a
    Core rebooting, and roonapi's reconnect handles it without a restart.
    """
    matches, _by = _relocation_candidates(cfg, cores)
    if len(matches) != 1:
        return None
    found = matches[0]
    if all(found.get(k) == cfg.get(k) for k in ("host", "tcp_port", "http_port")):
        return None
    return found


class RoonSession:
    """Owns the Roon connection and publishes normalized state on change."""

    def __init__(self, on_change, on_restart_needed=None) -> None:
        self._on_change = on_change
        # Called when the Core has demonstrably moved and only a fresh process
        # can follow it. Optional: the CLI and most tests build a session with
        # no hook, and a watcher that cannot request a restart must degrade to
        # logging rather than raise on a daemon thread. scripts/tonearmd wires
        # it to the same exit path as a dead socket server.
        self._on_restart_needed = on_restart_needed
        self._api: RoonApi | None = None
        self._cfg = config.load()
        self._arbiter = zones.Arbiter(self._cfg.get("pinned_zone_id"))
        self._status = "connecting"
        self._lock = threading.Lock()
        # Separate from every other lock in this class. A browse round-trip is
        # far slower than a snapshot; sharing a lock with the publish path
        # would stall subscribers (spec 7.5).
        self._browse_lock = threading.Lock()
        self._browse_sessions: OrderedDict = OrderedDict()
        # Consecutive polls that found the socket down. A drop is only
        # reported once this reaches DOWN_SAMPLES, so a socket that closes and
        # reopens between two polls never reaches the bar.
        self._down_samples = 0
        # Relocation conclusions already reported during the current outage.
        # A set rather than "the last one": #17's sweep is about 50% reliable
        # on some networks, so consecutive windows alternate between finding
        # the Core and finding nothing, and comparing against only the last
        # note logged every flip -- the repetition #15 set out to avoid,
        # reintroduced. Each distinct conclusion is worth saying once.
        #
        # Bounded like every other resource here: an outage lasting days on a
        # changing LAN could otherwise accumulate one entry per address seen.
        # Clearing costs at most one repeated line.
        self._reported_notes: set[str] = set()
        # Consecutive relocation windows that did not resolve, which is what
        # the sweep backoff is measured in.
        self._unresolved_windows = 0
        # Set by `_apply`, cleared by `_save_cfg`: an address taken in memory
        # that no Core has answered on yet.
        self._cfg_dirty = False

    @property
    def status(self) -> str:
        return self._status

    def snapshot(self) -> dict:
        core = None
        if self._cfg.get("host"):
            core = {"host": self._cfg["host"],
                    "http_port": self._cfg.get("http_port", 9330),
                    "name": self._cfg.get("name") or self._cfg["host"]}
        # A snapshot that is not "ok" carries no zone. roonapi keeps its last
        # zone dict through a disconnect, so without this the payload still
        # describes a track -- and the popup renders it in the card while the
        # header directly above reads "Roon Core unreachable". Two halves of
        # one popup contradicting each other is worse than an empty card.
        if self._status != "ok":
            return state.build(self._status, core, None, [])
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

    def _check_connection(self) -> None:
        """One poll of the live socket. Flips `_status` on a transition only.

        Called on a timer by `_watch_connection`, but kept separate from it so
        the decision is testable without threads or sleeps.

        `getattr` every call, never a captured reference: roonapi's reconnect
        does NOT revive the old socket -- `_server_setup` builds a brand new
        `RoonApiWebSocket` and rebinds `_roonsocket`. Code holding the original
        object would read the dead one's `connected = False` forever and report
        unreachable for the rest of the process's life, starting from the first
        successful reconnect.

        Statuses other than "ok"/"unreachable" belong to `start()`: a daemon
        still waiting to be enabled in Roon Remote is "unpaired", and a socket
        existing does not make it paired.
        """
        if self._status not in ("ok", "unreachable", "no_network"):
            return

        sock = getattr(self._api, "_roonsocket", None) if self._api else None
        up = bool(sock is not None and getattr(sock, "connected", False))

        if up:
            self._down_samples = 0
            if self._status != "ok":
                LOG.info("Roon connection restored")
                self._status = "ok"
                self._publish()
            return

        self._down_samples += 1
        if self._status == "ok" and self._down_samples >= DOWN_SAMPLES:
            self._status = _unreachable_status(self._cfg.get('host'))
            LOG.warning("Roon connection lost after %d polls: %s",
                        self._down_samples, self._status)
            self._publish()
        if self._down_samples % RELOCATE_SAMPLES:
            return

        # Which fault this is can change while we are down: a network that
        # comes back with the Core still switched off must stop claiming
        # there is no route to it. Only re-checked on this slow cadence --
        # it is a blocking probe, and the poll loop runs every 2s.
        fault = _unreachable_status(self._cfg.get('host'))
        if fault != self._status:
            LOG.info("fault changed: %s -> %s", self._status, fault)
            self._status = fault
            self._publish()
        # An outage this long is no longer roonapi's to recover: it rebuilds
        # the socket against the address it was given, forever, so a Core that
        # took a new DHCP lease is invisible to it. Discovery is the only way
        # to tell that case apart from a Core that is merely switched off --
        # and only the first costs a restart.
        #
        # Nothing is applied or written here, deliberately. The restarted
        # process runs `start()`, fails against the address still on disk, and
        # relocates through the path that persists only what a Core answered.
        # Handing the new address over by writing it first would just be a way
        # to persist an address nothing has connected to yet.
        if self._find_relocated() is not None and self._on_restart_needed is not None:
            self._on_restart_needed()

    def _apply(self, found: dict) -> None:
        """Take a Core's address in memory. Deliberately does NOT write it.

        Persistence is `_save_cfg`, and `start()` calls it only once a Core has
        actually answered. An address that is applied and then never proven
        dies with the process, leaving the last known-good one on disk -- so a
        stale reply, or a forged one, cannot cost the daemon an address that
        works.

        Under `_lock` for the same reason `_pin_locked` writes under it: the
        watcher thread is not the only writer of `_cfg`.
        """
        with self._lock:
            self._cfg.update({k: found[k] for k in
                              ("host", "tcp_port", "http_port", "name",
                               "unique_id") if k in found})
            self._cfg_dirty = True

    def _save_cfg(self) -> None:
        """Write config, if `_apply` changed anything since the last write."""
        with self._lock:
            if not self._cfg_dirty:
                return
            config.save(self._cfg)
            self._cfg_dirty = False

    def _find_relocated(self) -> dict | None:
        """Is this Core answering at a NEW address? Returns it, or None.

        Multicast only (`sood.discover(scan=False)`): the /24 sweep is 254 TCP
        connects on a typical LAN and this runs on every restart for as long as
        a Core stays switched off, which is not a thing to point at someone
        else's network. A Core that has MOVED is up and answering SOOD anyway;
        one that answers nothing is off, and no amount of scanning finds it.

        Reads nothing and writes nothing beyond the log: the callers decide.
        """
        # scan=True means "multicast, and sweep only if it is silent" -- the
        # sweep is never paid when multicast answers.
        window = self._unresolved_windows + 1
        try:
            cores = sood.discover(scan=_should_sweep(window))
        except Exception:
            # Best effort, and broad on purpose. On the watcher thread an
            # escape would kill the poll loop; in `start()` it would turn a
            # recoverable outage into a crash. The next window tries again.
            LOG.exception("discovery failed while looking for a moved Core")
            return None
        with self._lock:
            cfg = dict(self._cfg)
        found = _relocated_core(cfg, cores)
        if found is not None:
            LOG.warning("Core %s is answering at %s, not %s",
                        found.get("name"), found["host"], cfg.get("host"))
            # Cleared so a refusal AFTER a successful move is reported afresh
            # rather than suppressed as a repeat of one from before it.
            self._reported_notes.clear()
            self._unresolved_windows = 0
            return found
        # Logged on CHANGE, not per call. This runs on every restart and on
        # every watcher poll, so an unconditional warning would repeat for the
        # whole length of an outage -- which is how a message that matters gets
        # tuned out. The conclusion is what is worth an entry; a conclusion
        # that has not changed is not news.
        note = _relocation_note(cfg, cores)
        if note is not None and note not in self._reported_notes:
            LOG.warning("%s", note)
            if len(self._reported_notes) >= MAX_REPORTED_NOTES:
                self._reported_notes.clear()
            self._reported_notes.add(note)
        self._unresolved_windows = window
        return None

    def _watch_connection(self) -> None:
        """Timing only; every decision lives in `_check_connection`.

        This observes and reports -- it deliberately does NOT exit the way
        `start()` does on a failed initial connect. roonapi's own
        `_socket_watcher` is already a reconnect loop (poll every 2s, rebuild
        the socket ~21s after a failure, forever), so exiting here would throw
        away a recovery path that works and churn the whole process every
        ~23 seconds for the length of an outage.
        """
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                self._check_connection()
            except Exception:
                LOG.exception("connection check failed")

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
                self._status = _unreachable_status(self._cfg.get('host'))
                self._publish()
                sys.exit(1)
            core = cores[0]
            LOG.info("discovered %s at %s via %s",
                     core["name"], core["host"], core["via"])
            self._apply(core)

        token = config.load_token()
        if token is None:
            # First run: RoonApi blocks until the extension is enabled by hand
            # in Roon Remote -> Settings -> Extensions. See PAIRING_TIMEOUT.
            self._status = "unpaired"
            self._publish()

        self._api = self._connect(token)
        if self._api is None:
            # The stored address answered nothing. This Core may simply be on
            # the LAN at a new one -- a DHCP lease change, measured 2026-09-06.
            # Without this the daemon would exit, restart, and retry the same
            # dead address forever: discovery above runs only when `host` is
            # ABSENT, so an address once stored was never revisited.
            found = self._find_relocated()
            if found is not None:
                self._apply(found)
                self._api = self._connect(token)
        if self._api is None:
            self._status = _unreachable_status(self._cfg.get('host'))
            self._publish()
            sys.exit(1)

        # A Core answered here, so this address is finally worth keeping. One
        # applied above but never proven dies with the process instead, leaving
        # the last known-good address on disk for the next start to try.
        self._save_cfg()
        if self._api.token:
            config.save_token(self._api.token)

        self._status = "ok"
        self._api.register_state_callback(self._publish)
        self._publish()

        # daemon=True: this must never hold the process open. There is no stop
        # flag because there is nothing to stop it for -- `stop()` tears the
        # process down, and a watcher that outlives its own session by one
        # poll is harmless.
        threading.Thread(target=self._watch_connection,
                         name="conn-watch", daemon=True).start()

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
        next request rebuilds from root.

        Bounded at MAX_BROWSE_SESSIONS, least-recently-used evicted first.
        `key` is a wire field -- server.py takes it from the request -- so an
        unbounded store meant any local process able to open the socket could
        allocate Roon browse state without limit, one entry per distinct
        string it sent. Eviction is by USE rather than insertion so the
        widget's long-lived session survives a burst of new keys; an evicted
        consumer rebuilds from root on its next request, which is the same
        thing a daemon restart already does to it.
        """
        with self._browse_lock:
            existing = self._browse_sessions.get(key)
            if existing is None:
                existing = browse.BrowseSession(
                    self._api, key, self.selected_zone_id)
            else:
                self._browse_sessions.pop(key)
            self._browse_sessions[key] = existing      # newest at the end
            while len(self._browse_sessions) > MAX_BROWSE_SESSIONS:
                evicted, _ = self._browse_sessions.popitem(last=False)
                LOG.info("evicting least recently used browse session %r",
                         evicted)
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
            return session.play(kwargs.get("index"), kwargs.get("level_id"))
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
            if not isinstance(zone_id, str) or len(zone_id) > MAX_ZONE_ID:
                # Refused, not truncated. This value is persisted, and one
                # large enough would push config.json past
                # config.MAX_CONFIG_BYTES -- so the next start would refuse
                # the whole file, lose the Core address and re-run discovery.
                LOG.warning("dropping zone pin: bad zone id")
                return
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
