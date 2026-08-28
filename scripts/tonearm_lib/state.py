"""Normalize Roon's zone objects into tonearm's v1 wire payload.

The daemon emits what is TRUE. Nothing here formats for display: no time
strings, no severity, no URLs. Those are Model.js's job, so that display can be
iterated without restarting the service.
"""

from __future__ import annotations

SCHEMA_VERSION = 1
VALID_STATUS = ("connecting", "unpaired", "unreachable", "ok")


def _volume_of(roon_zone: dict) -> dict | None:
    outputs = roon_zone.get("outputs") or []
    if not outputs:
        return None
    raw = outputs[0].get("volume")
    if not raw or raw.get("type") == "incremental" or raw.get("value") is None:
        # Many streamers expose no volume object at all (a fixed-volume
        # output) -- `not raw` -- or type "incremental", which has no
        # absolute value/min/max at all, only relative up/down steps `raw
        # can neither read nor set a slider position for. Previously only
        # the first case was handled, and an incremental output fell
        # through to the fabricated {"value": 0, "min": 0, "max": 100}
        # below -- a slider parked at zero for a zone whose real volume
        # this can neither read nor set. Rendering a slider for either case
        # would be a lie, so the widget is told there is nothing to show.
        return None
    return {
        "value": raw.get("value", 0),
        "min": raw.get("min", 0),
        "max": raw.get("max", 100),
        "step": raw.get("step", 1),
        "muted": bool(raw.get("is_muted", False)),
    }


def _now_playing_of(roon_zone: dict) -> dict | None:
    np = roon_zone.get("now_playing")
    if not np:
        return None
    lines = np.get("three_line") or {}
    return {
        "title": lines.get("line1", ""),
        "artist": lines.get("line2", ""),
        "album": lines.get("line3", ""),
        "image_key": np.get("image_key", ""),
        # Nullable placeholder: this module does no I/O (see the module
        # docstring), so it cannot know whether a local cached copy exists.
        # The daemon's art cache (art.py) fills this in, or leaves it null.
        "art_path": None,
    }


def normalize_zone(roon_zone: dict | None) -> dict | None:
    if not roon_zone:
        return None
    np = roon_zone.get("now_playing") or {}
    return {
        "id": roon_zone.get("zone_id", ""),
        "name": roon_zone.get("display_name", ""),
        "state": roon_zone.get("state", "stopped"),
        "pinned": False,          # the daemon overwrites this; see zones.py
        "volume": _volume_of(roon_zone),
        "position": np.get("seek_position") or 0,
        "length": np.get("length") or 0,
        "now_playing": _now_playing_of(roon_zone),
    }


def build(status: str, core: dict | None, zone: dict | None,
          zones: list[dict]) -> dict:
    if status not in VALID_STATUS:
        raise ValueError("status must be one of %r, got %r" % (VALID_STATUS, status))
    trimmed_core = None
    if core:
        trimmed_core = {
            "host": core.get("host", ""),
            "http_port": core.get("http_port", 9330),
            "name": core.get("name", ""),
        }
    return {
        "v": SCHEMA_VERSION,
        "status": status,
        "core": trimmed_core,
        "zone": zone,
        # Sent in full on every push. It is small, and always sending it removes
        # a class of staleness bug rather than trading it for bytes.
        "zones": [
            {"id": z.get("id", ""), "name": z.get("name", ""),
             "state": z.get("state", "stopped")}
            for z in zones
        ],
    }
