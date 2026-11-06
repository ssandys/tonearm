"""Capability facts and the player envelope contract. Pure; no Core, no socket.

Every fact here comes from a RAW zone payload, and a malformed payload must
yield NO fact rather than a default one: a default that reads as a fact is
how a widget ends up offering a control the Core does not have.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import player   # noqa: E402


def zone(**overrides) -> dict:
    """A well-formed raw zone, so each test changes exactly one thing."""
    raw = {
        "zone_id": "zone-a",
        "display_name": "Kitchen",
        "state": "playing",
        "is_seek_allowed": True,
        "now_playing": {"length": 240, "three_line": {"line1": "Track"}},
        "outputs": [{"output_id": "output-1",
                     "volume": {"type": "number", "value": 20, "min": 0,
                                "max": 100, "is_muted": False}}],
    }
    raw.update(overrides)
    return raw


def request(**overrides) -> dict:
    """A well-formed player envelope, so each test changes exactly one thing."""
    envelope = {"cmd": "player_targeted", "v": 1, "request_id": "req-1",
                "zone_id": "zone-a", "verb": "play", "arg": None,
                "output_id": None, "output_ids": None}
    envelope.update(overrides)
    return envelope


class TestCapabilityFacts(unittest.TestCase):
    def test_a_well_formed_zone_advertises_exactly_what_it_has(self):
        facts = player.controls(zone())
        self.assertEqual(facts, {
            "transport": True, "seek": True, "volume": True,
            "volume_output_id": "output-1",
            "mute_output_ids": ["output-1"], "muted": False})

    def test_transport_is_a_weak_fact_and_grants_nothing_by_itself(self):
        # The real gate for a transport command is the SELECTION and the
        # connection, both enforced at dispatch. The fact only says the zone
        # has a body to command.
        self.assertTrue(player.controls(zone())["transport"])
        self.assertFalse(player.controls({})["transport"])
        self.assertFalse(player.controls(None)["transport"])

    def test_seek_needs_permission_and_a_real_length(self):
        self.assertTrue(player.controls(zone())["seek"])
        for change in ({"is_seek_allowed": False},
                       {"is_seek_allowed": "true"},
                       {"is_seek_allowed": 1},
                       {"now_playing": {"length": 0}},
                       {"now_playing": {"length": -5}},
                       {"now_playing": {"length": float("nan")}},
                       {"now_playing": {"length": float("inf")}},
                       {"now_playing": {"length": "240"}},
                       {"now_playing": {}},
                       {"now_playing": None}):
            with self.subTest(change=change):
                self.assertFalse(player.controls(zone(**change))["seek"])

    def test_absolute_volume_needs_a_real_scale_on_the_first_output(self):
        self.assertTrue(player.controls(zone())["volume"])
        self.assertEqual(player.controls(zone())["volume_output_id"], "output-1")
        for volume in ({"type": "fixed", "value": 20, "min": 0, "max": 100,
                        "is_muted": False},
                       {"type": "incremental", "value": 20, "min": 0, "max": 100,
                        "is_muted": False},
                       {"type": "number", "min": 0, "max": 100},
                       {"type": "number", "value": 20, "max": 100},
                       {"type": "number", "value": 20, "min": 0},
                       {"type": "number", "value": True, "min": 0, "max": 100},
                       {"type": "number", "value": 20, "min": 100, "max": 0},
                       {"type": "number", "value": 20, "min": 50, "max": 50},
                       {"type": "number", "value": 200, "min": 0, "max": 100},
                       {"type": "number", "value": 20, "min": 0,
                        "max": float("nan")},
                       "not a dict"):
            with self.subTest(volume=volume):
                facts = player.controls(zone(outputs=[{"output_id": "output-1",
                                                        "volume": volume}]))
                self.assertFalse(facts["volume"])
                self.assertIsNone(facts["volume_output_id"])

    def test_a_volume_needs_a_usable_output_id(self):
        for output_id in (None, "", "x" * (player.MAX_ID + 1), 7):
            with self.subTest(output_id=output_id):
                facts = player.controls(zone(outputs=[
                    {"output_id": output_id,
                     "volume": {"type": "number", "value": 20, "min": 0,
                                "max": 100, "is_muted": False}}]))
                self.assertFalse(facts["volume"])

    def test_group_mute_needs_complete_unique_output_ids(self):
        two = [{"output_id": "output-1",
                "volume": {"type": "number", "value": 20, "min": 0, "max": 100,
                           "is_muted": False}},
               {"output_id": "output-2",
                "volume": {"type": "number", "value": 20, "min": 0, "max": 100,
                           "is_muted": False}}]
        facts = player.controls(zone(outputs=two))
        self.assertEqual(facts["mute_output_ids"], ["output-1", "output-2"])
        self.assertFalse(facts["muted"])

    def test_mute_metadata_must_be_complete_and_boolean(self):
        for volume in ({"type": "number", "value": 20, "min": 0, "max": 100},
                       {"type": "number", "value": 20, "min": 0, "max": 100,
                        "is_muted": "yes"},
                       {"type": "number", "value": 20, "min": 0, "max": 100,
                        "is_muted": 1},
                       {"type": "fixed", "is_muted": False},
                       "not a dict"):
            with self.subTest(volume=volume):
                facts = player.controls(zone(outputs=[
                    {"output_id": "output-1", "volume": volume}]))
                self.assertEqual(facts["mute_output_ids"], [])

    def test_duplicate_output_ids_grant_no_group_mute(self):
        # A group mute that named one id twice would mute one speaker twice
        # and leave the other audible.
        duplicate = [{"output_id": "output-1",
                      "volume": {"type": "number", "value": 20, "min": 0,
                                 "max": 100, "is_muted": False}},
                     {"output_id": "output-1",
                      "volume": {"type": "number", "value": 20, "min": 0,
                                 "max": 100, "is_muted": False}}]
        facts = player.controls(zone(outputs=duplicate))
        self.assertEqual(facts["mute_output_ids"], [])

    def test_more_outputs_than_the_bound_grant_no_group_mute(self):
        outputs = [{"output_id": "output-%d" % i,
                    "volume": {"type": "number", "value": 20, "min": 0,
                               "max": 100, "is_muted": False}}
                   for i in range(player.MAX_OUTPUTS + 1)]
        self.assertEqual(player.controls(zone(outputs=outputs))["mute_output_ids"], [])

    def test_muted_is_true_only_when_every_output_is_muted(self):
        outputs = [{"output_id": "output-1",
                    "volume": {"type": "number", "value": 20, "min": 0,
                               "max": 100, "is_muted": True}},
                   {"output_id": "output-2",
                    "volume": {"type": "number", "value": 20, "min": 0,
                               "max": 100, "is_muted": False}}]
        self.assertFalse(player.controls(zone(outputs=outputs))["muted"])
        outputs[1]["volume"]["is_muted"] = True
        self.assertTrue(player.controls(zone(outputs=outputs))["muted"])

    def test_an_incremental_output_can_still_be_muted(self):
        # Incremental volume has no absolute scale, but it does have a mute
        # flag -- so the group mute is available and the slider is not.
        facts = player.controls(zone(outputs=[
            {"output_id": "output-1",
             "volume": {"type": "incremental", "is_muted": True}}]))
        self.assertFalse(facts["volume"])
        self.assertIsNone(facts["volume_output_id"])
        self.assertEqual(facts["mute_output_ids"], ["output-1"])
        self.assertTrue(facts["muted"])

    def test_malformed_payloads_yield_no_facts_and_no_exception(self):
        for raw in (None, 7, "zone", [], {"outputs": "nope"},
                    {"outputs": [7]}, {"outputs": [{"output_id": "output-1"}]},
                    {"outputs": {}}, {"now_playing": 5}):
            with self.subTest(raw=raw):
                facts = player.controls(raw)
                self.assertIsInstance(facts, dict)
                self.assertFalse(facts["seek"])
                self.assertFalse(facts["volume"])
                self.assertEqual(facts["mute_output_ids"], [])

    def test_a_fractional_length_is_still_a_length(self):
        # The length is a number, not necessarily a whole number of seconds.
        # What makes a seek impossible is a length that is missing or not a
        # finite number, not one that is not an integer.
        self.assertTrue(player.controls(
            zone(now_playing={"length": 1.5}))["seek"])

    def test_the_fact_set_never_grants_a_default(self):
        # Every key is present in every answer, so a widget can read one
        # without asking whether the daemon understood it -- and a missing
        # fact is False/None/[], never a plausible-looking value.
        for raw in (None, {}, zone()):
            facts = player.controls(raw)
            self.assertEqual(set(facts), {"transport", "seek", "volume",
                                          "volume_output_id", "mute_output_ids",
                                          "muted"})


class TestRequestContract(unittest.TestCase):
    def test_a_well_formed_request_is_admissible(self):
        self.assertTrue(player.valid_request(request()))

    def test_the_key_set_is_exact(self):
        self.assertFalse(player.valid_request(request(extra="x")))
        for field in ("arg", "output_id", "output_ids"):
            broken = request()
            del broken[field]
            self.assertFalse(player.valid_request(broken))

    def test_the_version_must_be_the_integer_one(self):
        self.assertFalse(player.valid_request(request(v=2)))
        self.assertFalse(player.valid_request(request(v=True)))
        self.assertFalse(player.valid_request(request(v="1")))

    def test_identities_are_bounded_strings(self):
        self.assertFalse(player.valid_request(request(request_id="")))
        self.assertFalse(player.valid_request(request(request_id="r" * 65)))
        self.assertFalse(player.valid_request(request(request_id=None)))
        self.assertFalse(player.valid_request(request(zone_id="")))
        self.assertFalse(player.valid_request(request(zone_id="z" * 129)))
        self.assertFalse(player.valid_request(request(zone_id=7)))

    def test_numeric_arguments_are_integers_and_never_booleans(self):
        self.assertFalse(player.valid_request(request(verb="seek", arg=True)))
        self.assertFalse(player.valid_request(request(verb="seek", arg=1.5)))
        self.assertFalse(player.valid_request(request(verb="seek", arg="2")))
        self.assertFalse(player.valid_request(request(verb="seek", arg=None)))
        self.assertTrue(player.valid_request(request(verb="seek", arg=2)))

    def test_each_verb_takes_only_its_own_arguments(self):
        self.assertTrue(player.valid_request(request(verb="play")))
        self.assertFalse(player.valid_request(request(verb="play", arg=1)))
        self.assertFalse(player.valid_request(request(verb="play", output_id="o")))
        self.assertFalse(player.valid_request(request(verb="play", output_ids=["o"])))
        self.assertTrue(player.valid_request(request(
            verb="volume", arg=20, output_id="output-1")))
        self.assertFalse(player.valid_request(request(verb="volume", arg=20)))
        self.assertFalse(player.valid_request(request(
            verb="volume", arg=20, output_id="output-1", output_ids=["output-1"])))
        self.assertTrue(player.valid_request(request(
            verb="mute", output_ids=["output-1", "output-2"])))
        self.assertFalse(player.valid_request(request(verb="mute")))
        self.assertFalse(player.valid_request(request(verb="mute", output_ids=[])))
        self.assertFalse(player.valid_request(request(
            verb="mute", output_ids=["output-1"] * (player.MAX_OUTPUTS + 1))))
        self.assertFalse(player.valid_request(request(
            verb="mute", output_ids=["output-1", "output-1"])))
        self.assertFalse(player.valid_request(request(verb="transfer")))

    def test_the_group_is_bounded(self):
        self.assertTrue(player.valid_request(request(
            verb="mute", output_ids=["output-%d" % i
                                     for i in range(player.MAX_OUTPUTS)])))
        self.assertFalse(player.valid_request(request(
            verb="mute", output_ids=["output-%d" % i
                                     for i in range(player.MAX_OUTPUTS + 1)])))


class TestResultContract(unittest.TestCase):
    def test_a_refusal_is_correlated_by_request_zone_and_verb(self):
        reply = player.result(request(), "refused", "capability_unavailable")
        self.assertTrue(player.valid_result(reply, request()))
        for field, value in (("request_id", "other"), ("zone_id", "other"),
                             ("verb", "pause")):
            other = request(**{field: value})
            self.assertFalse(player.valid_result(reply, other))

    def test_every_refusal_code_in_the_vocabulary_is_valid(self):
        for error in player.REFUSAL_ERRORS:
            with self.subTest(error=error):
                reply = player.result(request(), "refused", error)
                self.assertTrue(player.valid_result(reply, request()))

    def test_every_unknown_code_in_the_vocabulary_is_valid(self):
        for error in player.UNKNOWN_ERRORS:
            with self.subTest(error=error):
                reply = player.result(request(), "unknown", error)
                self.assertTrue(player.valid_result(reply, request()))

    def test_an_attempt_carries_no_error(self):
        self.assertTrue(player.valid_result(player.result(request(), "attempted"),
                                             request()))
        self.assertFalse(player.valid_result(
            player.result(request(), "attempted", "dispatch_failed"), request()))

    def test_an_outcome_and_its_error_must_agree(self):
        for outcome, error in (("refused", None), ("refused", "dispatch_failed"),
                                ("unknown", "capability_unavailable"),
                                ("unknown", None), ("attempted", "unsupported"),
                                ("shrug", None)):
            with self.subTest(outcome=outcome, error=error):
                reply = player.result(request(), outcome, error)
                self.assertFalse(player.valid_result(reply, request()))

    def test_a_reply_must_be_a_v1_command_result(self):
        reply = player.result(request(), "attempted")
        for broken in (dict(reply, v=2), dict(reply, v=True),
                       dict(reply, type="something_else"), "not a dict", None):
            with self.subTest(broken=broken):
                self.assertFalse(player.valid_result(broken, request()))

    def test_unbounded_identities_are_not_echoed_back(self):
        # A reply is bounded output. An unbounded identity in the request is
        # answered with None rather than reflected.
        reply = player.result(request(request_id="r" * 500, zone_id="z" * 500),
                              "refused", "bad_request")
        self.assertIsNone(reply["request_id"])
        self.assertIsNone(reply["zone_id"])

    def test_a_refusal_to_an_unknown_verb_still_correlates(self):
        # The verb is echoed whenever it is a bounded string, so a client can
        # tell which request was refused.
        reply = player.result(request(verb="transfer"), "refused", "bad_request")
        self.assertEqual(reply["verb"], "transfer")
        self.assertTrue(player.valid_result(reply, request(verb="transfer")))


if __name__ == "__main__":
    unittest.main()
