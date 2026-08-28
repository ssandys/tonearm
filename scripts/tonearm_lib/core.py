"""The Roon side of tonearmd: connect, pair, subscribe, issue transport.

This module is I/O. Everything decidable without a Core lives in state.py and
zones.py, which is why this file has no unit tests -- verify it against yavin.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

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

        try:
            self._api = RoonApi(APPINFO, token, self._cfg["host"], self._cfg["tcp_port"])
        except Exception:
            LOG.exception("connect failed")
            self._status = "unreachable"
            self._publish()
            return

        if self._api.token:
            config.save_token(self._api.token)

        self._status = "ok"
        self._api.register_state_callback(self._publish)
        self._publish()

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
