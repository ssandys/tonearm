"""Expose the followed Roon zone as an MPRIS player.

This is why media keys work without touching hyprland config: omarchy already
routes XF86Audio* through its own MPRIS arbitrator, so binding those keys
directly to tonearm would hijack them from every other player.

Not load-bearing. If the bus is unavailable the daemon logs it and keeps serving
the socket -- the widget works, only the media keys do not.
"""

from __future__ import annotations

import asyncio
import logging
import re

from dbus_next import PropertyAccess, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, dbus_property, method

LOG = logging.getLogger("tonearmd.mpris")

BUS_NAME = "org.mpris.MediaPlayer2.tonearm"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
TRACK_PREFIX = "/com/onemanposse/tonearm/"

_STATUS = {"playing": "Playing", "loading": "Playing",
           "paused": "Paused", "stopped": "Stopped"}


def playback_status(zone_state) -> str:
    if not isinstance(zone_state, str):
        return "Stopped"
    # hasOwnProperty equivalent: a plain lookup on a dict is already safe from
    # prototype walking, but be explicit about the unknown case.
    return _STATUS.get(zone_state, "Stopped")


def metadata_for(payload: dict) -> dict:
    zone = (payload or {}).get("zone")
    if not zone:
        return {}
    np = zone.get("now_playing")
    if not np:
        return {}
    core = payload.get("core") or {}

    # Object paths accept only [A-Za-z0-9_]; Roon's keys contain hyphens, and a
    # malformed path makes some clients drop off the bus entirely.
    safe = re.sub(r"[^A-Za-z0-9_]", "_", str(np.get("image_key") or "notrack"))
    md = {
        "mpris:trackid": TRACK_PREFIX + safe,
        "xesam:title": np.get("title", ""),
        # `as` in the spec. A bare string makes strict clients discard the dict.
        "xesam:artist": [np.get("artist", "")] if np.get("artist") else [],
        "xesam:album": np.get("album", ""),
        "mpris:length": int((zone.get("length") or 0) * 1_000_000),
    }
    image_key = np.get("image_key")
    if image_key and core.get("host"):
        md["mpris:artUrl"] = (
            "http://%s:%s/api/image/%s?scale=fit&width=512&height=512"
            % (core["host"], core.get("http_port", 9330), image_key)
        )
    return md


def _variants(md: dict) -> dict:
    out = {}
    for key, value in md.items():
        if isinstance(value, list):
            out[key] = Variant("as", value)
        elif isinstance(value, int):
            out[key] = Variant("x", value)
        elif key == "mpris:trackid":
            out[key] = Variant("o", value)
        else:
            out[key] = Variant("s", value)
    return out


class _Root(ServiceInterface):
    def __init__(self):
        super().__init__("org.mpris.MediaPlayer2")

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":            # noqa: F821
        return "Tonearm (Roon)"

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> "s":        # noqa: F821
        return "tonearm"

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":             # noqa: F821
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":            # noqa: F821
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":        # noqa: F821
        return False

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":   # noqa: F821
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":    # noqa: F821
        return []


class _Player(ServiceInterface):
    def __init__(self, session):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._session = session
        self._payload: dict = {}

    # Properties whose values genuinely change as the followed zone changes,
    # and therefore need a PropertiesChanged signal when they do. Position is
    # deliberately excluded: the spec has clients poll it (or listen for
    # Seeked), not watch it via PropertiesChanged, and it moves every second.
    _TRACKED = ("PlaybackStatus", "Metadata", "CanPlay", "CanPause",
                "CanGoNext", "CanGoPrevious", "CanSeek")

    def _tracked_values(self) -> dict:
        zone = self._payload.get("zone") or {}
        has_zone = bool(self._payload.get("zone"))
        return {
            "PlaybackStatus": playback_status(zone.get("state")),
            "Metadata": metadata_for(self._payload),
            "CanPlay": has_zone,
            "CanPause": has_zone,
            "CanGoNext": has_zone,
            "CanGoPrevious": has_zone,
            "CanSeek": bool(zone.get("length")),
        }

    def apply(self, payload: dict) -> list[str]:
        """Store new state; return the property names that actually changed.

        Every tracked property is diffed, not just PlaybackStatus/Metadata:
        an MPRIS client that caches CanPlay/CanPause from the initial GetAll
        and only refreshes on a PropertiesChanged signal for that name would
        otherwise keep serving a stale "no zone yet" False forever once a
        zone actually appears -- silently breaking Play/Pause from a media
        key while every other property looks perfectly correct. Confirmed
        live: omarchy's media IpcHandler returned "unhandled" for playPause
        against a real paused zone until this diff included CanPlay/CanPause.
        """
        before = self._tracked_values()
        self._payload = payload or {}
        after = self._tracked_values()
        return [name for name in self._TRACKED if before[name] != after[name]]

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":      # noqa: F821
        return playback_status((self._payload.get("zone") or {}).get("state"))

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":        # noqa: F821
        return _variants(metadata_for(self._payload))

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":            # noqa: F821
        zone = self._payload.get("zone") or {}
        return int((zone.get("position") or 0) * 1_000_000)

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":             # noqa: F821
        return bool(self._payload.get("zone"))

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":            # noqa: F821
        return bool(self._payload.get("zone"))

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":           # noqa: F821
        return bool(self._payload.get("zone"))

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":       # noqa: F821
        return bool(self._payload.get("zone"))

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":             # noqa: F821
        return bool((self._payload.get("zone") or {}).get("length"))

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":          # noqa: F821
        return True

    async def _run_command(self, verb: str, arg=None) -> None:
        """Run session.command() off the asyncio loop thread.

        dbus-next dispatches D-Bus method calls (this method included) on the
        loop thread. RoonSession.command() blocks synchronously -- it walks
        into the vendored roonapi's request/response wait, which polls with
        time.sleep(0.05) up to 50 times (~2.5s) before giving up. Calling it
        directly here would stall the whole loop for that long on a slow or
        briefly unreachable Core, freezing every other bus consumer's
        PropertiesChanged traffic along with it. Running it in the default
        executor keeps the loop free; RoonSession.command() already takes
        its own lock, so concurrent calls from the executor's worker threads
        are safe.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._session.command, verb, arg)

    @method()
    async def PlayPause(self):
        await self._run_command("playpause")

    @method()
    async def Play(self):
        await self._run_command("play")

    @method()
    async def Pause(self):
        await self._run_command("pause")

    @method()
    async def Stop(self):
        await self._run_command("pause")

    @method()
    async def Next(self):
        await self._run_command("next")

    @method()
    async def Previous(self):
        await self._run_command("previous")

    @method()
    async def SetPosition(self, track_id: "o", position: "x"):   # noqa: F821
        await self._run_command("seek", int(position // 1_000_000))

    @method()
    async def Seek(self, offset: "x"):    # noqa: F821
        zone = self._payload.get("zone") or {}
        target = int((zone.get("position") or 0) + offset / 1_000_000)
        await self._run_command("seek", max(0, target))


class MprisAdapter:
    def __init__(self, session) -> None:
        self._session = session
        self._bus: MessageBus | None = None
        self._player = _Player(session)
        self._root = _Root()

    async def start(self) -> None:
        self._bus = await MessageBus().connect()
        self._bus.export(OBJECT_PATH, self._root)
        self._bus.export(OBJECT_PATH, self._player)
        await self._bus.request_name(BUS_NAME)
        LOG.info("published %s", BUS_NAME)

    def update(self, payload: dict) -> None:
        changed = self._player.apply(payload)
        if changed and self._bus:
            # emit_properties_changed is what drives omarchy's media widget and
            # the OSD; without it they show the first track forever.
            self._player.emit_properties_changed(
                {name: getattr(self._player, name) for name in changed}
            )

    async def stop(self) -> None:
        if self._bus:
            # Withdraw the name rather than leave it advertising a stale track.
            self._bus.disconnect()
            self._bus = None
