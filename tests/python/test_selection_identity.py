"""Sanitized selection snapshots; no Core, credentials, or audio hardware."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import core, state


def zone(zid, playback="paused"):
    return {"zone_id": zid, "display_name": zid, "state": playback}


class FakeApi:
    def __init__(self, zones, generation=0):
        self.zones = {z["zone_id"]: z for z in zones}
        self._zone_list_generation = generation
        self._roonsocket = type("Socket", (), {"connected": True})()
        self.calls = []

    def playback_control(self, *args):
        self.calls.append(args)


def session(pin=None, strict=True, zones=(), generation=0, status="ok"):
    with patch.object(core.config, "load", return_value={
            "pinned_zone_id": pin, "strict_pins": strict}):
        result = core.RoonSession(lambda _: None)
    result._api = FakeApi(zones, generation)
    result._status = status
    return result


class SelectionIdentityTest(unittest.TestCase):
    def test_paused_pin_wins_over_other_playing_zone(self):
        s = session("pin", zones=[zone("pin"), zone("other", "playing")], generation=1)
        snap = s.snapshot()
        self.assertEqual(snap["selection"], {"mode": "pinned", "zone_id": "pin", "availability": "available"})
        self.assertEqual(snap["zone"]["id"], "pin")

    def test_missing_pin_waits_for_authoritative_listing_and_never_falls_back(self):
        s = session("pin", zones=[zone("other", "playing")])
        self.assertEqual(s.snapshot()["selection"]["availability"], "unknown")
        s._api._zone_list_generation = 1
        snap = s.snapshot()
        self.assertEqual(snap["selection"], {"mode": "pinned", "zone_id": "pin", "availability": "missing"})
        self.assertIsNone(snap["zone"])
        self.assertEqual([z["id"] for z in snap["zones"]], ["other"])
        s.command("pause")
        self.assertEqual(s._api.calls, [])

    def test_auto_idle_and_active(self):
        s = session(zones=[zone("idle")], generation=1)
        self.assertEqual(s.snapshot()["selection"], {"mode": "auto", "zone_id": None, "availability": "none"})
        s._api.zones["active"] = zone("active", "playing")
        self.assertEqual(s.snapshot()["selection"], {"mode": "auto", "zone_id": "active", "availability": "available"})

    def test_auto_last_followed_paused_room_is_available_not_none(self):
        s = session(zones=[zone("followed", "playing")], generation=1)
        self.assertEqual(s.snapshot()["selection"]["zone_id"], "followed")
        s._api.zones["followed"]["state"] = "paused"
        snap = s.snapshot()
        self.assertEqual(snap["selection"], {"mode": "auto", "zone_id": "followed", "availability": "available"})
        self.assertEqual(snap["zone"]["state"], "paused")
        self.assertEqual(s._api.calls, [])

    def test_vendor_events_drive_session_authority_and_publish(self):
        published = []
        with patch.object(core.config, "load", return_value={
                "pinned_zone_id": "pin", "strict_pins": True}):
            s = core.RoonSession(published.append)
        api = core._seeded_api()
        api._state_callbacks = []
        api._request = lambda *_: None
        s._api, s._status = api, "ok"
        api.register_state_callback(s._publish)

        # A failed initial read and an incremental update cannot prove absence.
        self.assertEqual(api._get_zones(), {})
        api._on_state_change({"zones_changed": [zone("other", "playing")]})
        self.assertEqual(published[-1]["selection"]["availability"], "unknown")
        self.assertEqual(published[-1]["zones"], [])

        # The full event must flow through the vendor callback into the
        # session snapshot, retaining the other room without selecting it.
        api._on_state_change({"zones": [zone("other", "playing")]})
        self.assertEqual(published[-1]["selection"], {
            "mode": "pinned", "zone_id": "pin", "availability": "missing"})
        self.assertIsNone(published[-1]["zone"])
        self.assertEqual([z["id"] for z in published[-1]["zones"]], ["other"])

    def test_successful_initial_vendor_query_reaches_session_snapshot(self):
        s = session("pin", generation=0)
        api = core._seeded_api()
        api._request = lambda *_: {"zones": [zone("other", "playing")]}
        api._zones = api._get_zones()
        s._api = api
        self.assertEqual(s.snapshot()["selection"]["availability"], "missing")
        self.assertEqual([z["id"] for z in s.snapshot()["zones"]], ["other"])

    def test_disconnected_and_connected_before_listing_are_unknown(self):
        for status in ("connecting", "unreachable"):
            s = session("pin", zones=[zone("pin")], generation=1, status=status)
            snap = s.snapshot()
            self.assertEqual(snap["selection"], {"mode": "pinned", "zone_id": "pin", "availability": "unknown"})
            self.assertIsNone(snap["zone"])
            self.assertEqual(snap["zones"], [])
        s = session("pin", zones=[], generation=0)
        self.assertEqual(s.snapshot()["selection"]["availability"], "unknown")
        s = session("pin", zones=[zone("pin", "playing")], generation=0)
        snap = s.snapshot()
        self.assertEqual(snap["selection"]["availability"], "unknown")
        self.assertIsNone(snap["zone"])
        self.assertEqual(snap["zones"], [])

    def test_return_unpin_and_restart_have_no_audio_side_effect(self):
        s = session("pin", zones=[zone("other", "playing")], generation=1)
        self.assertEqual(s.snapshot()["selection"]["availability"], "missing")
        s._api.zones["pin"] = zone("pin")
        self.assertEqual(s.snapshot()["selection"]["availability"], "available")
        s._arbiter.unpin()
        self.assertEqual(s.snapshot()["selection"], {"mode": "auto", "zone_id": "other", "availability": "available"})
        restarted = session("pin", zones=[zone("pin")], generation=1)
        self.assertEqual(restarted.snapshot()["selection"]["zone_id"], "pin")
        self.assertEqual(s._api.calls + restarted._api.calls, [])

    def test_legacy_fallback_and_v1_fields_remain(self):
        s = session("pin", strict=False, zones=[zone("other", "playing")], generation=1)
        snap = s.snapshot()
        self.assertEqual(snap["v"], 1)
        self.assertEqual(snap["zone"]["id"], "other")
        self.assertEqual(snap["selection"], {"mode": "pinned", "zone_id": "pin", "availability": "missing"})
        self.assertEqual(set(snap) - {"selection"}, {"v", "status", "core", "zone", "zones"})

    def test_reconnect_requires_new_full_list_before_missing(self):
        s = session("pin", zones=[zone("other", "playing")], generation=1)
        self.assertEqual(s.snapshot()["selection"]["availability"], "missing")
        s._api._roonsocket.connected = False
        with patch.object(core, "_unreachable_status", return_value="unreachable"):
            s._check_connection()
            s._check_connection()
        self.assertEqual(s.snapshot()["selection"]["availability"], "unknown")
        s._api._roonsocket.connected = True
        s._check_connection()
        recovering = s.snapshot()
        self.assertEqual(recovering["selection"]["availability"], "unknown")
        self.assertIsNone(recovering["zone"])
        self.assertEqual(recovering["zones"], [])
        s._api._zone_list_generation = 2
        self.assertEqual(s.snapshot()["selection"]["availability"], "missing")

    def test_legacy_v1_projection_clears_cached_room_until_new_full_list(self):
        s = session("pin", zones=[zone("pin", "playing")], generation=1)
        old_keys = ("v", "status", "core", "zone", "zones")
        old_consumer = lambda snap: {key: snap[key] for key in old_keys}
        before = old_consumer(s.snapshot())
        self.assertEqual(before["zone"]["id"], "pin")
        self.assertEqual([z["id"] for z in before["zones"]], ["pin"])

        s._api._roonsocket.connected = False
        with patch.object(core, "_unreachable_status", return_value="unreachable"):
            s._check_connection()
            s._check_connection()
        s._api._roonsocket.connected = True
        s._check_connection()
        waiting = old_consumer(s.snapshot())
        self.assertEqual(waiting["status"], "ok")
        self.assertIsNone(waiting["zone"])
        self.assertEqual(waiting["zones"], [])

        s._api.zones = {"other": zone("other", "playing")}
        s._api._zone_list_generation = 2
        after = old_consumer(s.snapshot())
        self.assertIsNone(after["zone"])
        self.assertEqual([z["id"] for z in after["zones"]], ["other"])
        self.assertEqual(set(after), set(old_keys))

    def test_invalid_and_oversize_identity_is_bounded(self):
        for pin in (["bad"], "p" * (state.MAX_ID + 1)):
            with self.subTest(pin_type=type(pin).__name__):
                snap = session(pin, zones=[], generation=1).snapshot()
                self.assertEqual(snap["selection"], {"mode": "auto", "zone_id": None, "availability": "none"})
                self.assertIsNone(snap["zone"])


if __name__ == "__main__":
    unittest.main()
