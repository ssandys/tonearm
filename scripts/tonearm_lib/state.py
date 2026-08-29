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


def _seek_of(roon_zone: dict, np: dict) -> int:
    """Current playback position, preferring the value Roon actually refreshes.

    Roon emits `zones_seek_changed` about once a second carrying only
    {zone_id, seek_position, queue_time_remaining}, and roonapi merges it with
    `self._zones[zone_id].update(zone)` -- so that value lands at the TOP LEVEL
    of the zone dict. The nested `now_playing.seek_position` is refreshed only
    by a full `zones_changed`, which Roon sends on state transitions, not on a
    timer.

    Reading only the nested one froze the position at whatever it was during
    the last full update: measured live, 13 pushes in 12 seconds every one of
    them `position: 0` while the track advanced past its end into the next.
    The widget could not compensate either -- Model.position() extrapolates
    from `receivedAt`, and those same 1 Hz pushes reset it every second.

    `is None` rather than `or`, because 0 is a real position (track start, or
    a seek back to the beginning) and must not fall through to the stale
    nested value.
    """
    pos = roon_zone.get("seek_position")
    if pos is None:
        pos = np.get("seek_position")
    return pos or 0


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
        "position": _seek_of(roon_zone, np),
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
