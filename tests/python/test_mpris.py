import copy
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


class FakeSession:
    """Publish snapshots offline; no bus, Core or command dispatch."""

    def __init__(self):
        self.payload = copy.deepcopy(PLAYING)
        self.payload["zone"]["controls"] = {
            "play": True, "pause": False, "next": False, "previous": True,
        }

    def snapshot(self):
        return copy.deepcopy(self.payload)


class TestTransportCapabilities(unittest.TestCase):
    PROPERTIES = {
        "CanPlay": "play", "CanPause": "pause",
        "CanGoNext": "next", "CanGoPrevious": "previous",
    }

    def setUp(self):
        from unittest import mock
        self.session = FakeSession()
        self.adapter = mpris.MprisAdapter(self.session)
        self.player = self.adapter._player
        # Only enable the adapter's notification branch. Never start a bus.
        self.adapter._bus = object()
        self.emitted = mock.Mock()
        self.player.emit_properties_changed = self.emitted

    def update(self):
        self.adapter.update(self.session.snapshot())

    def assert_capabilities(self, expected):
        tracked = self.player._tracked_values()
        for name, verb in self.PROPERTIES.items():
            with self.subTest(property=name):
                self.assertIn(name, self.player._TRACKED)
                self.assertIs(getattr(self.player, name), expected[verb])
                self.assertIs(tracked[name], expected[verb])

    def test_permissions_are_independent_of_each_other_and_playback_state(self):
        for state in ("playing", "paused", "stopped", "loading"):
            for allowed_verb in self.PROPERTIES.values():
                with self.subTest(state=state, allowed_verb=allowed_verb):
                    controls = {verb: verb == allowed_verb
                                for verb in self.PROPERTIES.values()}
                    self.session.payload["zone"].update(state=state, controls=controls)
                    self.update()
                    self.assert_capabilities(controls)

    def test_each_permission_requires_literal_true(self):
        for verb in self.PROPERTIES.values():
            for value in (False, None, 0, 1, 1.0, "true", "false", [], {}, [True]):
                with self.subTest(verb=verb, value=value):
                    controls = dict.fromkeys(self.PROPERTIES.values(), True)
                    controls[verb] = value
                    self.session.payload["zone"]["controls"] = controls
                    self.update()
                    self.assert_capabilities({v: v != verb for v in controls})

    def test_missing_permission_does_not_borrow_another_verbs_fact(self):
        for verb in self.PROPERTIES.values():
            with self.subTest(verb=verb):
                controls = dict.fromkeys(self.PROPERTIES.values(), True)
                del controls[verb]
                self.session.payload["zone"]["controls"] = controls
                self.update()
                self.assert_capabilities({v: v != verb for v in self.PROPERTIES.values()})

    def test_missing_or_malformed_controls_deny_all_four_verbs(self):
        zone = self.session.payload["zone"]
        del zone["controls"]
        self.update()
        self.assert_capabilities(dict.fromkeys(self.PROPERTIES.values(), False))
        for controls in (None, False, 1, "true", [], [True], {}):
            with self.subTest(controls=controls):
                zone["controls"] = controls
                self.update()
                self.assert_capabilities(dict.fromkeys(self.PROPERTIES.values(), False))

    def test_disconnected_status_cannot_advertise_cached_permissions(self):
        for status in ("connecting", "unpaired", "unreachable", "no_network", None):
            with self.subTest(status=status):
                self.session.payload["status"] = status
                self.session.payload["zone"]["controls"] = dict.fromkeys(
                    self.PROPERTIES.values(), True)
                self.update()
                self.assert_capabilities(dict.fromkeys(self.PROPERTIES.values(), False))

    def test_no_zone_or_initial_payload_advertises_no_permissions(self):
        self.assert_capabilities(dict.fromkeys(self.PROPERTIES.values(), False))
        for payload in ({}, {"status": "ok"}, {"status": "ok", "zone": None},
                        {"status": "ok", "zone": {}}):
            with self.subTest(payload=payload):
                self.session.payload = payload
                self.update()
                self.assert_capabilities(dict.fromkeys(self.PROPERTIES.values(), False))

    def test_capability_only_flips_emit_exactly_the_changed_property(self):
        self.update()
        for name, verb in self.PROPERTIES.items():
            for _ in range(2):  # Both enabling and disabling each permission.
                with self.subTest(property=name):
                    controls = self.session.payload["zone"]["controls"]
                    controls[verb] = not controls[verb]
                    self.emitted.reset_mock()
                    self.update()
                    self.emitted.assert_called_once_with({name: controls[verb]})
                    self.assert_capabilities(controls)

    def test_unchanged_payload_and_position_ticks_emit_nothing(self):
        self.update()
        self.emitted.reset_mock()
        self.update()
        self.session.payload["zone"]["position"] += 1
        self.update()
        self.emitted.assert_not_called()

    def test_zone_loss_and_recovery_notify_permissions(self):
        zone = self.session.payload["zone"]
        zone["controls"] = dict.fromkeys(self.PROPERTIES.values(), True)
        self.update()
        self.emitted.reset_mock()
        self.session.payload["zone"] = None
        self.update()
        changes = self.emitted.call_args.args[0]
        for name in self.PROPERTIES:
            self.assertIs(changes[name], False)
        self.assertEqual(changes["PlaybackStatus"], "Stopped")
        self.assertEqual(changes["Metadata"], {})
        self.emitted.reset_mock()
        self.session.payload["zone"] = zone
        self.update()
        changes = self.emitted.call_args.args[0]
        for name in self.PROPERTIES:
            self.assertIs(changes[name], True)

    def test_metadata_status_and_seek_behavior_are_preserved(self):
        self.update()
        self.emitted.reset_mock()
        zone = self.session.payload["zone"]
        zone["state"] = "paused"
        zone["now_playing"]["title"] = "Next track"
        self.update()
        self.emitted.assert_called_once_with({
            "PlaybackStatus": "Paused", "Metadata": self.player.Metadata,
        })
        self.assertEqual(self.player.Metadata["xesam:title"].value, "Next track")
        self.assertEqual(self.player.Metadata["mpris:length"].value, 585_000_000)
        self.assertEqual(self.player.Position, 271_000_000)
        self.assertIs(self.player.CanSeek, True)
        self.assert_capabilities(zone["controls"])


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
