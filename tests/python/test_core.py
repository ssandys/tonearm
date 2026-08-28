"""Regression test for core.py's one Core-independent, pure helper.

core.py is otherwise I/O and verified against yavin rather than by unit test
(see its module docstring). `_seeded_api()` is the exception: it does not
touch the network, so its one job -- giving every RoonApi instance its own
private `_zones`/`_outputs` instead of letting them alias the vendored
library's class-level shared defaults (roonapi.py:52-53) -- is directly
testable without a Core.

This is the fix for the finding that port fallback (core.py's `_connect`
trying `http_port` then falling back to `tcp_port`) could leave a
timed-out-and-abandoned RoonApi instance and a fresh one for the next port
both relying on the same shared dict, so a late subscription write from the
abandoned one could corrupt the state the new one reads. `_seeded_api()`
closes that deterministically -- see its docstring in core.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import core


class TestSeededApi(unittest.TestCase):
    def test_two_instances_do_not_share_zones_or_outputs(self):
        a = core._seeded_api()
        b = core._seeded_api()

        a._zones["zone1"] = {"display_name": "a"}
        a._outputs["output1"] = {"display_name": "a-out"}

        self.assertEqual(b._zones, {})
        self.assertEqual(b._outputs, {})
        self.assertIsNot(a._zones, b._zones)
        self.assertIsNot(a._outputs, b._outputs)

    def test_mutating_an_instance_does_not_leak_into_the_class_default(self):
        # This is the actual hazard being closed: roonapi.py's class body
        # declares `_zones = {}` / `_outputs = {}` as class attributes,
        # shared by every instance until each gets its own. A write to one
        # seeded instance must never reach that shared default, because any
        # instance created afterward without its own private dict yet
        # (mid-__init__, before RoonApi's own reassignment runs) would
        # otherwise see it.
        core._seeded_api()._zones["leaked"] = "should not happen"
        core._seeded_api()._outputs["leaked"] = "should not happen"

        self.assertEqual(core.RoonApi._zones, {})
        self.assertEqual(core.RoonApi._outputs, {})

    def test_seeded_dicts_are_fresh_instances_not_the_class_defaults(self):
        api = core._seeded_api()
        self.assertIsNot(api._zones, core.RoonApi._zones)
        self.assertIsNot(api._outputs, core.RoonApi._outputs)


if __name__ == "__main__":
    unittest.main()
