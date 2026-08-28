"""The Roon side of tonearmd: connect, pair, subscribe, issue transport.

This module is I/O. Everything decidable without a Core lives in state.py and
zones.py, which is why this file has no unit tests -- verify it against yavin.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor"))

from roonapi import RoonApi              # noqa: E402  (vendored, path-inserted)

from . import config, sood, state, zones  # noqa: E402

LOG = logging.getLogger("tonearmd.core")

APPINFO = {
    "extension_id": "com.onemanposse.tonearm",
    "display_name": "tonearm",
    "display_version": "0.1.0",
    "publisher": "Sean Sandys",
    "email": "sean@onemanposse.com",
}

# Seconds to wait for one port to answer the MOO/WS handshake before giving
# up on it. Measured against yavin (Roon 2.71 build 1683): the SOOD-advertised
# tcp_port (9150) accepts a TCP connection and then never answers the
# WebSocket upgrade -- a silent hang, not an error. RoonApi's own
# blocking_init has no ceiling on that wait, so without a bound here
# RoonApi.__init__ blocks forever and the daemon looks "healthy" while doing
# nothing. See docs/superpowers/specs/2026-08-27-tonearm-design.md S2.1.
CONNECT_TIMEOUT = 10.0


class RoonSession:
    """Owns the Roon connection and publishes normalized state on change."""

    def __init__(self, on_change) -> None:
        self._on_change = on_change
        self._api: RoonApi | None = None
        self._cfg = config.load()
        self._arbiter = zones.Arbiter(self._cfg.get("pinned_zone_id"))
        self._status = "connecting"
        self._lock = threading.Lock()

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
        raw = list((self._api.zones or {}).values())
        listing = [z for z in (state.normalize_zone(r) for r in raw) if z]
        self._arbiter.observe(listing)
        return listing, self._arbiter.select(listing)

    def _publish(self, *_args) -> None:
        try:
            self._on_change(self.snapshot())
        except Exception:
            LOG.exception("publish failed")

    def start(self) -> None:
        if not self._cfg.get("host"):
            cores = sood.discover()
            if not cores:
                self._status = "unreachable"
                self._publish()
                return
            core = cores[0]
            LOG.info("discovered %s at %s via %s",
                     core["name"], core["host"], core["via"])
            self._cfg.update({k: core[k] for k in ("host", "tcp_port", "http_port", "name")})
            config.save(self._cfg)

        token = config.load_token()
        if token is None:
            # First run: RoonApi blocks until the extension is enabled by hand
            # in Roon Remote -> Settings -> Extensions.
            self._status = "unpaired"
            self._publish()

        self._api = self._connect(token)
        if self._api is None:
            self._status = "unreachable"
            self._publish()
            return

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
        """Try each candidate port in turn, each bounded by CONNECT_TIMEOUT.
        Returns a ready RoonApi, or None if nothing answered on any port.
        """
        host = self._cfg["host"]
        ports = self._candidate_ports()
        for port in ports:
            api = self._try_port(host, port, token)
            if api is not None:
                return api
        LOG.error("no MOO response from %s on any of %s (%.0fs each)",
                  host, ports, CONNECT_TIMEOUT)
        return None

    @staticmethod
    def _try_port(host: str, port: int, token) -> RoonApi | None:
        """Connect on one port, bounded by CONNECT_TIMEOUT.

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
        is called separately from __init__ so there is a handle to `api`
        to call .stop() on even if __init__ itself never returns.
        """
        api = RoonApi.__new__(RoonApi)
        errors: list[Exception] = []

        def init() -> None:
            try:
                RoonApi.__init__(api, APPINFO, token, host, port, blocking_init=True)
            except Exception as exc:  # reported via `errors`, not swallowed
                errors.append(exc)

        thread = threading.Thread(target=init, daemon=True)
        thread.start()
        thread.join(CONNECT_TIMEOUT)

        if thread.is_alive():
            # Silent hang: TCP connected but the WebSocket handshake never
            # answered -- exactly what the SOOD-advertised tcp_port does on
            # this Roon version. Signal the stuck init to give up; the
            # abandoned thread unwinds on its own once _exit is seen.
            LOG.warning("no response from %s:%s within %.0fs", host, port, CONNECT_TIMEOUT)
            try:
                api.stop()
            except Exception:
                LOG.exception("error tearing down timed-out connection to %s:%s", host, port)
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

    # -- commands -------------------------------------------------------
    def command(self, verb: str, arg=None) -> None:
        with self._lock:
            self._command_locked(verb, arg)

    def _command_locked(self, verb: str, arg) -> None:
        if verb == "zone":
            if arg == "unpin":
                self._arbiter.unpin()
                self._cfg["pinned_zone_id"] = None
            else:
                self._arbiter.pin(arg)
                self._cfg["pinned_zone_id"] = arg
            config.save(self._cfg)
            self._publish()
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
            # state.py exposes the output's raw min/max/value scale (not a
            # 0-100 percent), so the raw setter -- not change_volume_percent
            # -- is the one that matches what the widget's slider shows.
            for output in (self._api.zones.get(zid, {}).get("outputs") or []):
                self._api.change_volume_raw(output["output_id"], int(arg), "absolute")
        elif verb in ("mute", "unmute"):
            for output in (self._api.zones.get(zid, {}).get("outputs") or []):
                self._api.mute(output["output_id"], verb == "mute")
        else:
            LOG.warning("unknown verb %r", verb)
