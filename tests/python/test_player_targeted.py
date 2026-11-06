"""Player dispatch against a fake Core: refusals, targets, and uncertainty.

No Core, no socket, no audio. The fake records every call it is handed, so
a refusal can be proved to have sent nothing at all rather than merely to
have returned the wrong answer.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import config, core   # noqa: E402


def zone_payload(zid="zone-a", outputs=None, length=240, seek=True) -> dict:
    """A raw zone payload, as the Core would deliver it."""
    return {
        "zone_id": zid,
        "display_name": "Kitchen",
        "state": "playing",
        "is_seek_allowed": seek,
        "now_playing": {"length": length,
                        "three_line": {"line1": "Synthetic track"}},
        "outputs": outputs if outputs is not None else [
            {"output_id": "output-1",
             "volume": {"type": "number", "value": 20, "min": 0, "max": 100,
                        "is_muted": False}}],
    }


def output(oid="output-1", value=20, muted=False, kind="number") -> dict:
    return {"output_id": oid,
            "volume": {"type": kind, "value": value, "min": 0, "max": 100,
                       "is_muted": muted}}


class RecordingApi:
    """A fake Core surface that records every call and answers explicitly.

    The ANSWER matters as much as the call. The vendored API returns None
    when the Core does not reply inside its own budget, and None after it
    swallows a send failure -- so a fake that answered None by default
    would make every outcome "unknown" and hide the difference between a
    call that was made and one that was not. This one answers {} (an
    explicit acknowledgement) unless a test says otherwise.
    """

    def __init__(self, zones=None, generation=1):
        self._zone_list_generation = generation
        self._roonsocket = type("Socket", (), {"connected": True})()
        self.zones = {"zone-a": zone_payload()} if zones is None else zones
        self.calls = []
        self.answers = {}
        self.raises = {}

    def _result(self, name):
        if name in self.raises:
            raise self.raises[name]
        return self.answers.get(name, {})

    def playback_control(self, zone_id, verb):
        self.calls.append(("playback", zone_id, verb))
        return self._result("playback_control")

    def seek(self, zone_id, seconds, method):
        self.calls.append(("seek", zone_id, seconds, method))
        return self._result("seek")

    def change_volume_raw(self, output_id, value, method):
        self.calls.append(("volume", output_id, value, method))
        return self._result("change_volume_raw")

    def mute(self, output_id, flag):
        self.calls.append(("mute", output_id, flag))
        return self._result("mute")


class PlayerDispatchTest(unittest.TestCase):
    def setUp(self):
        # `command("zone", ...)` persists a pin; keep that out of the
        # developer's own config.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        config.reset_paths()
        self.addCleanup(config.reset_paths)
        self.addCleanup(self._restore, previous)

    def _restore(self, previous):
        if previous is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = previous

    def session(self, api=None, pin="zone-a", strict=True, status="ok"):
        with patch.object(core.config, "load", return_value={
                "pinned_zone_id": pin, "strict_pins": strict}):
            s = core.RoonSession(lambda _: None)
        s._api = api if api is not None else RecordingApi()
        s._status = status
        return s

    def invoke(self, s, verb="play", arg=None, output_id=None,
               output_ids=None, zone_id="zone-a"):
        return s.player_targeted(zone_id, verb, arg, output_id, output_ids)

    # -- refusals ------------------------------------------------------
    def test_a_disconnected_core_is_refused_before_anything_is_sent(self):
        for status in ("connecting", "unpaired", "unreachable", "no_network"):
            with self.subTest(status=status):
                s = self.session(status=status)
                self.assertEqual(self.invoke(s),
                                 {"outcome": "refused", "error": "disconnected"})
                self.assertEqual(s._api.calls, [])

    def test_a_socket_that_is_not_up_is_refused_before_anything_is_sent(self):
        # The status poll decides "up" as: a socket exists AND its connected
        # flag is set. A missing socket, a None socket, or a socket with no
        # flag is DOWN by that rule, and the player route has to agree --
        # otherwise it walks into the SDK, waits out the API's own ~4s
        # timeout with the session lock held, and reports a disconnection as
        # an unknown dispatch.
        for broken in ("absent", "none", "no_flag", "false", "null"):
            with self.subTest(broken=broken):
                s = self.session()
                if broken == "absent":
                    del s._api._roonsocket
                elif broken == "none":
                    s._api._roonsocket = None
                elif broken == "no_flag":
                    s._api._roonsocket = type("Socket", (), {})()
                elif broken == "false":
                    s._api._roonsocket.connected = False
                else:
                    s._api._roonsocket.connected = None
                self.assertEqual(self.invoke(s),
                                 {"outcome": "refused", "error": "disconnected"})
                self.assertEqual(s._api.calls, [])

    def test_a_socket_that_is_up_still_dispatches(self):
        s = self.session()
        self.assertTrue(getattr(s._api._roonsocket, "connected", False))
        self.assertEqual(self.invoke(s),
                         {"outcome": "attempted", "error": None})
        self.assertEqual(s._api.calls, [("playback", "zone-a", "play")])

    def test_a_listing_that_is_not_authoritative_is_refused(self):
        for generation in (0, None):
            with self.subTest(generation=generation):
                s = self.session()
                if generation is None:
                    # A missing marker is not a fresh listing. Failing open
                    # here would make the whole readiness gate decoration.
                    del s._api._zone_list_generation
                else:
                    s._api._zone_list_generation = generation
                self.assertEqual(self.invoke(s),
                                 {"outcome": "refused", "error": "zones_unknown"})
                self.assertEqual(s._api.calls, [])

    def test_a_reconnect_without_a_new_listing_is_refused(self):
        # The socket came back, so the daemon says "ok" again -- but the
        # zone map is still the previous session's, and it must not be
        # treated as a fresh list of rooms.
        s = self.session()
        s._api._roonsocket.connected = False
        with patch.object(core, "_unreachable_status", return_value="unreachable"):
            s._check_connection()
            s._check_connection()
        s._api._roonsocket.connected = True
        s._check_connection()
        self.assertEqual(s.status, "ok")
        self.assertEqual(self.invoke(s),
                         {"outcome": "refused", "error": "zones_unknown"})
        self.assertEqual(s._api.calls, [])

    def test_a_room_that_is_gone_is_refused(self):
        s = self.session()
        del s._api.zones["zone-a"]
        self.assertEqual(self.invoke(s),
                         {"outcome": "refused", "error": "unavailable"})
        self.assertEqual(s._api.calls, [])

    def test_a_room_that_is_no_longer_followed_is_refused(self):
        # The room is still there, but it is not the one the bar shows.
        s = self.session(api=RecordingApi(zones={
            "zone-a": zone_payload(),
            "zone-b": zone_payload("zone-b")}))
        self.assertEqual(self.invoke(s, zone_id="zone-b"),
                         {"outcome": "refused", "error": "selection_changed"})
        self.assertEqual(s._api.calls, [])

    def test_a_moved_pin_is_refused(self):
        s = self.session()
        s._arbiter.pin("zone-b")
        self.assertEqual(self.invoke(s),
                         {"outcome": "refused", "error": "selection_changed"})
        self.assertEqual(s._api.calls, [])

    def test_an_unsupported_control_is_refused(self):
        s = self.session(api=RecordingApi(zones={
            "zone-a": zone_payload(seek=False)}))
        self.assertEqual(self.invoke(s, "seek", 10),
                         {"outcome": "refused", "error": "capability_unavailable"})
        self.assertEqual(s._api.calls, [])

    def test_a_fixed_volume_output_refuses_an_absolute_volume(self):
        s = self.session(api=RecordingApi(zones={
            "zone-a": zone_payload(outputs=[output(kind="fixed")])}))
        self.assertEqual(self.invoke(s, "volume", 20, output_id="output-1"),
                         {"outcome": "refused", "error": "capability_unavailable"})
        self.assertEqual(s._api.calls, [])

    def test_an_argument_outside_the_zone_bounds_is_refused(self):
        s = self.session()
        for verb, arg, output_id in (("seek", 241, None),
                                     ("seek", -1, None),
                                     ("volume", 101, "output-1"),
                                     ("volume", -1, "output-1")):
            with self.subTest(verb=verb, arg=arg):
                self.assertEqual(self.invoke(s, verb, arg, output_id),
                                 {"outcome": "refused", "error": "out_of_range"})
        self.assertEqual(s._api.calls, [])

    def test_a_changed_output_is_refused(self):
        s = self.session()
        self.assertEqual(self.invoke(s, "volume", 20, output_id="output-9"),
                         {"outcome": "refused", "error": "output_changed"})
        self.assertEqual(s._api.calls, [])

    def test_a_changed_output_set_is_refused(self):
        s = self.session(api=RecordingApi(zones={
            "zone-a": zone_payload(outputs=[output(), output("output-2")])}))
        # The client still remembers one output; the zone now has two.
        self.assertEqual(self.invoke(s, "mute", output_ids=["output-1"]),
                         {"outcome": "refused", "error": "output_changed"})
        self.assertEqual(s._api.calls, [])

    def test_a_refusal_leaves_the_selection_alone(self):
        s = self.session()
        self.invoke(s, "volume", 500, output_id="output-1")
        self.assertEqual(s._arbiter.pinned_id, "zone-a")
        self.assertEqual(s.snapshot()["selection"],
                         {"mode": "pinned", "zone_id": "zone-a",
                          "availability": "available"})

    # -- dispatch ------------------------------------------------------
    def test_each_verb_binds_its_own_target_and_never_the_legacy_route(self):
        cases = (
            ("play", None, None, None, ("playback", "zone-a", "play")),
            ("pause", None, None, None, ("playback", "zone-a", "pause")),
            ("playpause", None, None, None, ("playback", "zone-a", "playpause")),
            ("next", None, None, None, ("playback", "zone-a", "next")),
            ("previous", None, None, None, ("playback", "zone-a", "previous")),
            ("seek", 42, None, None, ("seek", "zone-a", 42, "absolute")),
            ("volume", 25, "output-1", None,
             ("volume", "output-1", 25, "absolute")),
            ("mute", None, None, ["output-1"], ("mute", "output-1", True)),
            ("unmute", None, None, ["output-1"], ("mute", "output-1", False)),
        )
        for verb, arg, output_id, output_ids, expected in cases:
            with self.subTest(verb=verb):
                s = self.session()
                with patch.object(s, "command",
                                  side_effect=AssertionError("legacy route")):
                    self.assertEqual(
                        self.invoke(s, verb, arg, output_id, output_ids),
                        {"outcome": "attempted", "error": None})
                self.assertEqual(s._api.calls, [expected])

    def test_volume_writes_to_the_first_output_only(self):
        s = self.session(api=RecordingApi(zones={
            "zone-a": zone_payload(outputs=[output("output-1"),
                                           output("output-2")])}))
        self.assertEqual(self.invoke(s, "volume", 25, output_id="output-1"),
                         {"outcome": "attempted", "error": None})
        self.assertEqual(s._api.calls,
                         [("volume", "output-1", 25, "absolute")])

    def test_a_group_mute_names_every_output_in_order(self):
        s = self.session(api=RecordingApi(zones={
            "zone-a": zone_payload(outputs=[output("output-1"),
                                           output("output-2")])}))
        self.assertEqual(self.invoke(s, "mute",
                                      output_ids=["output-1", "output-2"]),
                         {"outcome": "attempted", "error": None})
        self.assertEqual(s._api.calls,
                         [("mute", "output-1", True),
                          ("mute", "output-2", True)])

    def test_a_partial_group_failure_is_unknown_and_is_not_retried(self):
        s = self.session(api=RecordingApi(zones={
            "zone-a": zone_payload(outputs=[output("output-1"),
                                           output("output-2"),
                                           output("output-3")])}))
        calls = []

        def mute(output_id, flag):
            calls.append(("mute", output_id, flag))
            if output_id == "output-2":
                raise RuntimeError("the Core said no")
            return {}

        s._api.mute = mute
        self.assertEqual(self.invoke(s, "mute",
                                      output_ids=["output-1", "output-2",
                                                  "output-3"]),
                         {"outcome": "unknown", "error": "dispatch_failed"})
        # The third output was never touched, and nothing was un-muted to
        # "compensate": there is no undo for a group mute.
        self.assertEqual(calls, [("mute", "output-1", True),
                                 ("mute", "output-2", True)])

    def test_an_unanswered_group_mute_stops_the_fan_out(self):
        s = self.session(api=RecordingApi(zones={
            "zone-a": zone_payload(outputs=[output("output-1"),
                                           output("output-2"),
                                           output("output-3")])}))
        calls = []

        def mute(output_id, flag):
            calls.append(("mute", output_id, flag))
            if output_id == "output-2":
                return None        # the Core did not answer
            return {}

        s._api.mute = mute
        self.assertEqual(self.invoke(s, "mute",
                                      output_ids=["output-1", "output-2",
                                                  "output-3"]),
                         {"outcome": "unknown", "error": "dispatch_failed"})
        self.assertEqual(calls, [("mute", "output-1", True),
                                 ("mute", "output-2", True)])

    def test_an_unproven_attempt_is_unknown_not_attempted(self):
        # The vendored API answers None when the Core does not reply in
        # time, and None after it swallows a send failure. Neither proves
        # that anything was attempted, so neither is "attempted".
        for method, verb, arg, output_id, output_ids in (
                ("playback_control", "play", None, None, None),
                ("seek", "seek", 10, None, None),
                ("change_volume_raw", "volume", 20, "output-1", None),
                ("mute", "mute", None, None, ["output-1"])):
            with self.subTest(method=method):
                s = self.session()
                s._api.answers[method] = None
                self.assertEqual(self.invoke(s, verb, arg, output_id, output_ids),
                                 {"outcome": "unknown",
                                  "error": "dispatch_failed"})

    def test_a_raised_call_is_unknown(self):
        s = self.session()
        s._api.raises["playback_control"] = RuntimeError("socket died")
        self.assertEqual(self.invoke(s),
                         {"outcome": "unknown", "error": "dispatch_failed"})
        self.assertEqual(s._api.calls, [("playback", "zone-a", "play")])

    def test_a_malformed_request_is_refused_without_a_call(self):
        s = self.session()
        for verb, arg, output_id, output_ids in (("transfer", None, None, None),
                                                  ("play", 1, None, None),
                                                  ("volume", 20, None, None)):
            with self.subTest(verb=verb):
                self.assertEqual(self.invoke(s, verb, arg, output_id, output_ids),
                                 {"outcome": "refused", "error": "bad_request"})
        self.assertEqual(s._api.calls, [])

    # -- the legacy command path ---------------------------------------
    def test_the_legacy_path_refuses_a_listing_that_is_not_authoritative(self):
        for generation in (0, None):
            with self.subTest(generation=generation):
                s = self.session()
                if generation is None:
                    del s._api._zone_list_generation
                else:
                    s._api._zone_list_generation = generation
                s.command("pause")
                self.assertEqual(s._api.calls, [])

    def test_the_legacy_path_still_works_with_a_fresh_listing(self):
        s = self.session()
        s.command("pause")
        self.assertEqual(s._api.calls, [("playback", "zone-a", "pause")])

    def test_the_pin_is_allowed_before_the_listing_is_fresh(self):
        # Remembering a room is allowed before the rooms are known: that is
        # how a pin survives a restart into a Core that is not up yet.
        s = self.session()
        s._api._zone_list_generation = 0
        s.command("zone", "zone-b")
        self.assertEqual(s._arbiter.pinned_id, "zone-b")
        self.assertEqual(s._api.calls, [])


if __name__ == "__main__":
    unittest.main()
