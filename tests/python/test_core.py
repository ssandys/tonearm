"""Regression tests for core.py's Core-independent, pure/isolable pieces.

core.py is otherwise I/O and verified against yavin rather than by unit test
(see its module docstring). Two pieces are the exception, because neither
touches the network:

- `_seeded_api()`: giving every RoonApi instance its own private
  `_zones`/`_outputs` instead of letting them alias the vendored library's
  class-level shared defaults (roonapi.py:52-53). This is the fix for the
  finding that port fallback (core.py's `_connect` trying `http_port` then
  falling back to `tcp_port`) could leave a timed-out-and-abandoned RoonApi
  instance and a fresh one for the next port both relying on the same
  shared dict, so a late subscription write from the abandoned one could
  corrupt the state the new one reads. `_seeded_api()` closes that
  deterministically -- see its docstring in core.py.

- `RoonSession._raw_zones()`: defending `self._api.zones` (a dict mutated
  in place by roonapi's own websocket thread, entirely outside anything
  this module or Task 10's `CachingSession` lock controls) against
  `RuntimeError: dictionary changed size during iteration` when a zone is
  added or removed mid-read. See its docstring in core.py.
"""

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import config, core


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


class _RaisesOnceThenSucceeds:
    """Stands in for `self._api.zones`: `.values()` raises RuntimeError the
    first time it is called -- exactly what a real dict looks like to a
    reader caught mid-mutation by another thread -- then returns the real
    values on every call after. This proves `_raw_zones()`'s retry actually
    recovers, without needing to win a real race to exercise it.
    """

    def __init__(self, real: dict):
        self._real = real
        self._raised = False

    def values(self):
        if not self._raised:
            self._raised = True
            raise RuntimeError("dictionary changed size during iteration")
        return self._real.values()


class TestRawZones(unittest.TestCase):
    """`RoonSession._raw_zones()` guards `self._api.zones` iteration against
    a concurrent mutation from roonapi's own websocket thread. See its
    docstring in core.py and Important 2 of the Task 10 review.
    """

    def setUp(self):
        # RoonSession.__init__ calls config.load(); isolate it from the
        # real user config the same way test_config.py does.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev_config_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        config.reset_paths()
        self.addCleanup(self._restore_config_home)

    def _restore_config_home(self):
        if self._prev_config_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev_config_home
        config.reset_paths()

    def _session(self) -> core.RoonSession:
        return core.RoonSession(lambda _payload: None)

    def test_a_single_runtime_error_is_retried_not_raised(self):
        zone = {"zone_id": "z1", "display_name": "Living Room", "state": "playing"}
        session = self._session()
        session._api = types.SimpleNamespace(zones=_RaisesOnceThenSucceeds({"z1": zone}))

        raw = session._raw_zones()

        self.assertEqual(raw, [zone])

    def test_two_consecutive_runtime_errors_degrade_to_an_empty_listing(self):
        # The pathological case: if the retry itself is unlucky twice in a
        # row, _raw_zones() must still not let the exception escape --
        # empty is the same shape self._api being unset already produces.
        class _AlwaysRaises:
            def values(self):
                raise RuntimeError("dictionary changed size during iteration")

        session = self._session()
        session._api = types.SimpleNamespace(zones=_AlwaysRaises())

        self.assertEqual(session._raw_zones(), [])

    # No test here exercises a REAL cross-thread dict mutation racing
    # list(self._api.zones.values()) -- see the "on the real race" section
    # of the fix-round-2 report for why: it was tried (a genuine dict, a
    # genuine background thread mutating it continuously, at sizes from
    # 200 up to 3,000,000 entries, for up to 8 seconds and 1000+ attempts
    # per run, including single-call durations up to ~26ms), and it never
    # once reproduced the RuntimeError, in this CPython build or any
    # tested size. That is because materializing list(dict.values())
    # executes as a single non-preemptible C-level loop under the GIL --
    # CPython's cooperative thread-switch check only happens in the
    # bytecode eval loop, which this call never re-enters until it is
    # done -- so a second Python thread genuinely cannot interleave a
    # mutation into the middle of it here, regardless of dict size. A test
    # built on that premise would pass identically whether or not the fix
    # above exists, which is worse than no test at all. The two tests
    # above instead prove the fix's own logic directly: a RuntimeError,
    # however it arises, is retried and never escapes.


if __name__ == "__main__":
    unittest.main()
