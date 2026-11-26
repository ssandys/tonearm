import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import mpris

CORE = {"host": "192.168.50.118", "http_port": 9330, "name": "yavin"}
PLAYING = {
    "v": 1, "status": "ok", "core": CORE,
    "zone": {"id": "z1", "name": "Living Room", "state": "playing", "pinned": False,
             "volume": {"value": 62, "min": 0, "max": 100, "step": 1, "muted": False},
             "position": 271, "length": 585,
             "now_playing": {"title": "Blue Train", "artist": "John Coltrane",
                             "album": "Blue Train", "image_key": "a1b2"}},
    "zones": [],
}


class TestMetadata(unittest.TestCase):
    def test_maps_title_artist_album(self):
        md = mpris.metadata_for(PLAYING)
        self.assertEqual(md["xesam:title"], "Blue Train")
        self.assertEqual(md["xesam:artist"], ["John Coltrane"])
        self.assertEqual(md["xesam:album"], "Blue Train")

    def test_artist_is_a_list_even_for_one_artist(self):
        # xesam:artist is `as` in the spec; a bare string makes strict clients
        # drop the whole metadata dict.
        self.assertIsInstance(mpris.metadata_for(PLAYING)["xesam:artist"], list)

    def test_length_is_microseconds(self):
        self.assertEqual(mpris.metadata_for(PLAYING)["mpris:length"], 585_000_000)

    def test_art_url_is_the_daemon_s_cached_file(self):
        # Not the Core's http:// URL. Whatever reads MPRIS metadata -- the
        # shell's own media widget included -- would fetch that itself, with
        # none of art.py's byte or dimension caps. The marketplace security
        # review (2026-10-31) raised exactly that against the widget's Image;
        # this is the same fetch by a different reader.
        import copy
        payload = copy.deepcopy(PLAYING)
        payload["zone"]["now_playing"]["art_path"] = "/run/user/1000/tonearm/art/a1b2.jpg"
        self.assertEqual(mpris.metadata_for(payload)["mpris:artUrl"],
                         "file:///run/user/1000/tonearm/art/a1b2.jpg")

    def test_art_url_is_omitted_until_the_daemon_has_a_local_copy(self):
        # PLAYING has a Core and an image_key -- everything 0.14.1 built an
        # http:// artUrl from -- but no art_path yet.
        self.assertNotIn("mpris:artUrl", mpris.metadata_for(PLAYING))

    def test_art_url_is_never_remote(self):
        import copy
        for art_path in ("http://evil/x.jpg", "relative/x.jpg", "/a/../b.jpg",
                         "/a/b.jpg?x", None, 7):
            payload = copy.deepcopy(PLAYING)
            payload["zone"]["now_playing"]["art_path"] = art_path
            self.assertNotIn("mpris:artUrl", mpris.metadata_for(payload),
                             repr(art_path))

    def test_track_id_is_a_valid_object_path(self):
        # A bad path makes some clients disconnect from the bus entirely.
        # PLAYING's own image_key ("a1b2") has no hyphen to strip, so this
        # would pass even if the sanitization in metadata_for() were deleted
        # outright -- use a hyphenated key, like Roon's real ones, so the
        # assertion can actually fail.
        import copy
        payload = copy.deepcopy(PLAYING)
        payload["zone"]["now_playing"]["image_key"] = "a1b2-c3d4-e5f6"
        track_id = mpris.metadata_for(payload)["mpris:trackid"]
        self.assertTrue(track_id.startswith("/com/onemanposse/tonearm/"))
        self.assertNotIn("-", track_id.rsplit("/", 1)[-1])

    def test_no_track_yields_empty_metadata_not_a_crash(self):
        idle = {"v": 1, "status": "ok", "core": CORE, "zone": None, "zones": []}
        self.assertEqual(mpris.metadata_for(idle), {})

    def test_art_url_is_omitted_when_there_is_no_image_key(self):
        import copy
        payload = copy.deepcopy(PLAYING)
        payload["zone"]["now_playing"]["image_key"] = ""
        self.assertNotIn("mpris:artUrl", mpris.metadata_for(payload))


class TestPlaybackStatus(unittest.TestCase):
    def test_maps_roon_states_to_mpris_names(self):
        self.assertEqual(mpris.playback_status("playing"), "Playing")
        self.assertEqual(mpris.playback_status("paused"), "Paused")
        self.assertEqual(mpris.playback_status("stopped"), "Stopped")
        self.assertEqual(mpris.playback_status("loading"), "Playing")

    def test_an_unknown_state_is_stopped_rather_than_an_exception(self):
        self.assertEqual(mpris.playback_status("constructor"), "Stopped")
        self.assertEqual(mpris.playback_status(None), "Stopped")


if __name__ == "__main__":
    unittest.main()


class TestConnectingThroughTheProxy(unittest.TestCase):
    """The bus is xdg-dbus-proxy's socket now, and it can lag or be absent.

    tonearmd-bus.service is Type=exec, so the daemon's After= is released once
    the proxy has exec'd -- its socket appears a moment later. And without
    xdg-dbus-proxy installed there is no socket at all. Neither may take the
    daemon down, and the second must say what is missing rather than dump a
    bare ENOENT traceback into the journal.
    """

    def setUp(self):
        import asyncio
        import tempfile
        from unittest import mock
        self.asyncio = asyncio
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.socket_path = os.path.join(self.tmp.name, "tonearm-bus", "bus")
        patcher = mock.patch.dict(os.environ, {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + self.socket_path})
        patcher.start()
        self.addCleanup(patcher.stop)
        delay = mock.patch.object(mpris, "CONNECT_DELAY", 0)
        delay.start()
        self.addCleanup(delay.stop)

    def test_a_missing_socket_is_a_clear_bus_unavailable(self):
        adapter = mpris.MprisAdapter(session=None)
        with self.assertRaises(mpris.BusUnavailable) as caught:
            self.asyncio.run(adapter.start())
        message = str(caught.exception)
        # The path the daemon actually tried, and where to look next.
        self.assertIn(self.socket_path, message)
        self.assertIn("tonearmd-bus", message)
        self.assertIn("setup.sh --check", message)

    def test_a_socket_that_appears_late_is_retried(self):
        from unittest import mock
        attempts = []

        class FakeBus:
            def __init__(self):
                attempts.append(1)
                # Two refusals, deliberately unlike CONNECT_ATTEMPTS, so an
                # implementation that never retries -- or retries forever --
                # cannot pass.
                if len(attempts) == 1:
                    raise FileNotFoundError(2, "No such file or directory")
                if len(attempts) == 2:
                    raise ConnectionRefusedError(111, "Connection refused")

            async def connect(self):
                return self

            def export(self, path, iface):
                pass

            async def request_name(self, name):
                self.requested = name

        with mock.patch.object(mpris, "MessageBus", FakeBus):
            adapter = mpris.MprisAdapter(session=None)
            self.asyncio.run(adapter.start())
        self.assertEqual(len(attempts), 3)
        self.assertEqual(adapter._bus.requested, mpris.BUS_NAME)

    def test_retries_are_bounded(self):
        from unittest import mock
        attempts = []

        class NeverBus:
            def __init__(self):
                attempts.append(1)
                raise FileNotFoundError(2, "No such file or directory")

        with mock.patch.object(mpris, "MessageBus", NeverBus):
            with self.assertRaises(mpris.BusUnavailable):
                self.asyncio.run(mpris.MprisAdapter(session=None).start())
        self.assertEqual(len(attempts), mpris.CONNECT_ATTEMPTS)
        self.assertGreater(mpris.CONNECT_ATTEMPTS, 1)

    def test_other_failures_are_not_disguised_as_a_missing_proxy(self):
        from unittest import mock

        class BrokenBus:
            def __init__(self):
                raise PermissionError(13, "Permission denied")

        with mock.patch.object(mpris, "MessageBus", BrokenBus):
            with self.assertRaises(PermissionError):
                self.asyncio.run(mpris.MprisAdapter(session=None).start())


def transport_payload(state="playing", **controls):
    """A snapshot whose zone carries the daemon's per-verb transport facts.

    Shaped as state.normalize_zone publishes it: `controls` comes from
    player.controls(), which is True only where Roon's is_*_allowed is True.
    """
    facts = {"play": False, "pause": False, "next": False, "previous": False}
    facts.update(controls)
    zone = dict(PLAYING["zone"], state=state, controls=facts)
    return dict(PLAYING, zone=zone)


class FakeSession:
    def __init__(self):
        self.calls = []

    def command(self, verb, arg=None):
        self.calls.append((verb, arg))


def call(player, name, *args):
    """Run a D-Bus method the way dbus_next dispatches it.

    The decorated attribute is a sync wrapper that drops the coroutine;
    dbus_next itself awaits the original function, kept on __DBUS_METHOD.
    """
    import asyncio
    fn = getattr(player, name).__dict__["__DBUS_METHOD"].fn
    asyncio.run(fn(player, *args))


TRANSPORT_PROPS = ("CanPlay", "CanPause", "CanGoNext", "CanGoPrevious")


class TestTransportCapabilities(unittest.TestCase):
    """Media keys follow Roon's per-verb permissions, like the popup.

    omarchy's media service asks the property matching the action it is about
    to take (CanPause while playing, CanPlay otherwise, CanGoNext/
    CanGoPrevious for skips) and hands the key to another player when it is
    false. Reporting True for any zone sent play to an empty queue, where
    Roon silently ignores it.
    """

    def player(self, payload):
        p = mpris._Player(FakeSession())
        p.apply(payload)
        return p

    def test_each_property_reads_its_own_roon_permission(self):
        for prop, fact in (("CanPlay", "play"), ("CanPause", "pause"),
                           ("CanGoNext", "next"), ("CanGoPrevious", "previous")):
            with self.subTest(prop=prop):
                self.assertTrue(getattr(self.player(transport_payload(**{fact: True})), prop))
                self.assertFalse(getattr(self.player(transport_payload(**{fact: False})), prop))

    def test_a_normal_zone_keeps_its_keys(self):
        # The regression that matters: what works today must keep working.
        playing = self.player(transport_payload("playing", pause=True, next=True, previous=True))
        self.assertTrue(playing.CanPause)
        self.assertTrue(playing.CanGoNext)
        self.assertTrue(playing.CanGoPrevious)
        paused = self.player(transport_payload("paused", play=True, next=True, previous=True))
        self.assertTrue(paused.CanPlay)

    def test_an_empty_queue_offers_nothing(self):
        p = self.player(transport_payload("stopped"))
        for prop in TRANSPORT_PROPS:
            self.assertFalse(getattr(p, prop), prop)

    def test_missing_or_malformed_controls_are_false(self):
        zone_without = {k: v for k, v in PLAYING["zone"].items() if k != "controls"}
        payloads = [None, {}, {"zone": None}, dict(PLAYING, zone=zone_without)]
        for bad in (None, "yes", [], {"play": 1}, {"play": "true"}):
            payloads.append(dict(PLAYING, zone=dict(PLAYING["zone"], controls=bad)))
        for payload in payloads:
            p = self.player(payload)
            for prop in TRANSPORT_PROPS:
                with self.subTest(payload=payload, prop=prop):
                    self.assertFalse(getattr(p, prop))

    def test_a_flip_is_announced(self):
        # Without a PropertiesChanged for the name, a client that cached
        # CanPlay keeps the old answer forever.
        p = self.player(transport_payload("paused", play=True, next=True))
        changed = p.apply(transport_payload("stopped", next=True))
        self.assertIn("CanPlay", changed)
        self.assertNotIn("CanGoNext", changed)

    def test_the_announced_value_is_the_value_read(self):
        # update() diffs _tracked_values() but emits the property getters;
        # the two must never disagree.
        for payload in (transport_payload("playing", pause=True, next=True),
                        transport_payload("paused", play=True, previous=True),
                        transport_payload("stopped"), {}):
            p = self.player(payload)
            for name, value in p._tracked_values().items():
                if name == "Metadata":
                    continue    # the getter wraps it in D-Bus Variants
                with self.subTest(name=name):
                    self.assertEqual(getattr(p, name), value)


class TestTransportMethodsHonourTheirPermission(unittest.TestCase):
    """The MPRIS spec: calling a method whose Can* is false has no effect.

    These go through session.command(), which has none of player_targeted's
    capability checks, so the gate is here.
    """

    def run_method(self, payload, name):
        session = FakeSession()
        p = mpris._Player(session)
        p.apply(payload)
        call(p, name)
        return session.calls

    def test_each_method_is_refused_without_its_permission(self):
        for name, verb, fact in (("Play", "play", "play"), ("Pause", "pause", "pause"),
                                 ("Stop", "pause", "pause"), ("Next", "next", "next"),
                                 ("Previous", "previous", "previous")):
            with self.subTest(method=name):
                self.assertEqual(self.run_method(transport_payload(), name), [])
                self.assertEqual(self.run_method(transport_payload(**{fact: True}), name),
                                 [(verb, None)])

    def test_playpause_asks_the_permission_for_the_current_state(self):
        cases = (("playing", "pause"), ("loading", "play"), ("paused", "play"),
                 ("stopped", "play"))
        for state, fact in cases:
            for allowed in (True, False):
                with self.subTest(state=state, allowed=allowed):
                    other = "play" if fact == "pause" else "pause"
                    payload = transport_payload(state, **{fact: allowed, other: not allowed})
                    self.assertEqual(self.run_method(payload, "PlayPause"),
                                     [("playpause", None)] if allowed else [])

    def test_no_zone_sends_nothing(self):
        for name in ("Play", "Pause", "Stop", "Next", "Previous", "PlayPause"):
            with self.subTest(method=name):
                self.assertEqual(self.run_method({}, name), [])
