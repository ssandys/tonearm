"""Local divergences from vendored roonapi 0.1.6, and why they must survive.

`scripts/vendor/README.md` says to refresh by bumping the version and
repeating the copy. A patch applied to the vendored tree is therefore one
`cp -r` away from being silently lost, and the symptom would not be a crash
-- it would be a zone the bar shows that Roon does not. These tests are what
makes that loss loud.

Each one names the upstream behaviour it corrects, so whoever refreshes can
tell whether upstream has since fixed it and the divergence can be dropped.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "vendor")))

from roonapi import roonapi   # noqa: E402


def api():
    """A RoonApi with only the state `_on_state_change` touches.

    Built without __init__ on purpose: the real one opens a websocket to a
    Core, and this is a message-handling test.
    """
    obj = roonapi.RoonApi.__new__(roonapi.RoonApi)
    obj._zones = {}
    obj._outputs = {}
    obj._state_callbacks = []
    return obj


def zone(zone_id, name):
    return {"zone_id": zone_id, "display_name": name, "state": "paused"}


class TestAFullSnapshotReplacesRatherThanMerges(unittest.TestCase):
    """Upstream handles the `zones` subscription payload in the same branch as
    `zones_added` and `zones_changed`: it updates or inserts every id it
    receives and never removes one that is absent.

    That is right for an incremental update and wrong for a full snapshot.
    Reported in #13 from a real session: a zone powered off while the laptop
    slept was still listed by `tonearmctl status` after reconnect, while the
    official Roon client no longer showed it, and it stayed until tonearmd
    was restarted.
    """

    def test_a_zone_missing_from_the_snapshot_is_dropped(self):
        a = api()
        a._on_state_change({"zones": [zone("z1", "iFi"), zone("z2", "Gustard")]})
        self.assertEqual(sorted(a._zones), ["z1", "z2"])
        # Reconnect: the Core's full picture no longer has Gustard in it.
        a._on_state_change({"zones": [zone("z1", "iFi")]})
        self.assertEqual(sorted(a._zones), ["z1"])

    def test_an_incremental_update_still_only_merges(self):
        # The distinction this rests on. `zones_changed` carries only what
        # changed, so treating IT as authoritative would delete every zone
        # that merely had nothing to report.
        a = api()
        a._on_state_change({"zones": [zone("z1", "iFi"), zone("z2", "Gustard")]})
        a._on_state_change({"zones_changed": [zone("z1", "iFi")]})
        self.assertEqual(sorted(a._zones), ["z1", "z2"])

    def test_the_snapshot_still_reports_a_change(self):
        a = api()
        seen = []
        a._state_callbacks.append((lambda e, ids: seen.append((e, ids)), None, None))
        a._on_state_change({"zones": [zone("z1", "iFi")]})
        self.assertEqual([e for e, _ in seen], ["zones_changed"])

    def test_outputs_behave_the_same_way(self):
        # Identical shape, identical bug, one branch below. tonearm reads
        # outputs nested inside zones rather than this dict, so nothing here
        # depends on it today -- but leaving half of a patched function
        # correct is a trap for whoever reads it next.
        a = api()
        out = lambda oid: {"output_id": oid, "display_name": oid, "zone_id": "z1"}
        a._on_state_change({"outputs": [out("o1"), out("o2")]})
        self.assertEqual(sorted(a._outputs), ["o1", "o2"])
        a._on_state_change({"outputs": [out("o1")]})
        self.assertEqual(sorted(a._outputs), ["o1"])


class TestARemovalIsPublished(unittest.TestCase):
    """Upstream deletes on `zones_removed` and appends no event, so no state
    callback fires. The removal reaches consumers only when some later,
    unrelated event happens to be published -- which on an idle system may be
    a long time, or never.
    """

    def test_removing_a_zone_notifies_callbacks(self):
        a = api()
        a._on_state_change({"zones": [zone("z1", "iFi"), zone("z2", "Gustard")]})
        seen = []
        a._state_callbacks.append((lambda e, ids: seen.append((e, ids)), None, None))
        a._on_state_change({"zones_removed": ["z2"]})
        self.assertEqual(sorted(a._zones), ["z1"])
        self.assertEqual(seen, [("zones_changed", ["z2"])])

    def test_removing_an_output_notifies_callbacks(self):
        a = api()
        a._outputs = {"o1": {"output_id": "o1"}}
        seen = []
        a._state_callbacks.append((lambda e, ids: seen.append((e, ids)), None, None))
        a._on_state_change({"outputs_removed": ["o1"]})
        self.assertEqual(a._outputs, {})
        self.assertEqual(seen, [("outputs_changed", ["o1"])])

    def test_removing_a_zone_that_was_never_there_is_not_an_error(self):
        # A removal racing a reconnect snapshot that already dropped it. The
        # upstream `del` raises KeyError here, inside a websocket callback.
        a = api()
        a._on_state_change({"zones_removed": ["gone"]})
        self.assertEqual(a._zones, {})
