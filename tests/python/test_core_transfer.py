"""Moving the current stream to another zone.

`transfer_zone` moves Roon's queue between rooms. The source is never chosen
by the caller -- it is always the zone the daemon already follows, the same one
every transport verb targets -- so the only argument is the destination.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import config, core   # noqa: E402

KITCHEN = "16018f1b-kitchen"
DEN = "16018f1b-den"


class RecordingApi:
    """Only the surface `_command_locked` touches, and it records the calls.

    Local rather than added to fakes.py: six modules share that file, and
    nothing else needs a transport-command fake.
    """

    def __init__(self, playing=KITCHEN):
        self.transfers = []
        self.zones = {
            KITCHEN: {"zone_id": KITCHEN, "display_name": "Kitchen",
                      "state": "playing" if playing == KITCHEN else "paused",
                      "outputs": [{"output_id": "o-kitchen"}]},
            DEN: {"zone_id": DEN, "display_name": "Den",
                  "state": "playing" if playing == DEN else "paused",
                  "outputs": [{"output_id": "o-den"}]},
        }

    def transfer_zone(self, from_id, to_id):
        self.transfers.append((from_id, to_id))


class TransferCase(unittest.TestCase):
    def setUp(self):
        # config.save() writes a real file. Redirect it, or running the suite
        # rewrites the developer's own pinned zone.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        config.reset_paths()
        self.addCleanup(config.reset_paths)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev

    def session(self, api, status="ok"):
        s = core.RoonSession(lambda *a: None)
        s._api = api
        s._status = status
        return s


class TestTransfer(TransferCase):
    def test_moves_from_the_followed_zone_to_the_named_one(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s.command("transfer", DEN)
        # Argument ORDER is the whole risk here: reversed, this silently moves
        # the destination's queue into the room you are already listening in.
        self.assertEqual(api.transfers, [(KITCHEN, DEN)])

    def test_the_source_follows_a_repin_rather_than_being_fixed(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s._arbiter.pin(DEN)
        s.command("transfer", KITCHEN)
        self.assertEqual(api.transfers, [(DEN, KITCHEN)])

    def test_refuses_a_transfer_to_the_zone_already_playing(self):
        # Roon would accept it. The control would then be an inert button that
        # looks live, which is worse than one that is absent.
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s.command("transfer", KITCHEN)
        self.assertEqual(api.transfers, [])

    def test_refuses_an_unknown_destination(self):
        # A widget holding a zone list from before a zone disappeared would
        # otherwise send Roon an id for a room that no longer exists.
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s.command("transfer", "no-such-zone")
        self.assertEqual(api.transfers, [])

    def test_drops_the_command_when_not_connected(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api, status="unreachable")
        s._api = None
        s.command("transfer", DEN)
        self.assertEqual(api.transfers, [])


class TestTransferFollowsTheMusic(TransferCase):
    """A pinned widget must not be left watching the room it just emptied."""

    def test_a_pinned_session_repins_to_the_destination(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s._arbiter.pin(KITCHEN)
        s.command("transfer", DEN)
        self.assertEqual(s._arbiter.pinned_id, DEN)
        self.assertEqual(config.load()["pinned_zone_id"], DEN)

    def test_an_unpinned_session_stays_unpinned(self):
        # Auto-follow arbitration already moves to whichever zone is playing.
        # Pinning here would silently convert a user who chose to follow the
        # music into one locked to a single room.
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s.command("transfer", DEN)
        self.assertIsNone(s._arbiter.pinned_id)

    def test_a_refused_transfer_does_not_repin(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s._arbiter.pin(KITCHEN)
        s.command("transfer", "no-such-zone")
        self.assertEqual(s._arbiter.pinned_id, KITCHEN)


if __name__ == "__main__":
    unittest.main()


class TestPinArgumentIsConstrained(TransferCase):
    """`zone` is the one verb whose argument reaches persistent state.

    server.py passes `request.get("arg")` through untouched, and
    _pin_locked() writes it into ~/.config/tonearm/config.json. Unvalidated,
    any local process able to open the socket could persist an arbitrary
    JSON value there -- and one large enough would push the file past
    config.MAX_CONFIG_BYTES, so the NEXT start would refuse the whole file
    and fall back to defaults, losing the Core address and re-running
    discovery.
    """

    def test_an_over_long_zone_id_is_refused_and_nothing_is_persisted(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s.command("zone", "z" * (core.MAX_ZONE_ID + 1))
        self.assertIsNone(config.load()["pinned_zone_id"])
        self.assertIsNone(s._arbiter.pinned_id)

    def test_a_zone_id_that_is_not_a_string_is_refused(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s.command("zone", {"not": "a string"})
        self.assertIsNone(config.load()["pinned_zone_id"])
        self.assertIsNone(s._arbiter.pinned_id)

    def test_an_ordinary_zone_id_still_pins_and_persists(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s.command("zone", DEN)
        self.assertEqual(s._arbiter.pinned_id, DEN)
        self.assertEqual(config.load()["pinned_zone_id"], DEN)

    def test_unpin_still_clears_the_pin(self):
        api = RecordingApi(playing=KITCHEN)
        s = self.session(api)
        s.command("zone", DEN)
        s.command("zone", "unpin")
        self.assertIsNone(s._arbiter.pinned_id)
        self.assertIsNone(config.load()["pinned_zone_id"])
