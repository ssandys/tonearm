"""Core guard for selected browse; never starts an SDK or daemon."""
import unittest
from unittest.mock import patch
from tests.python import test_browse_selected as fixtures
from tonearm_lib import core


class TestCoreSelectedBrowse(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.TestSelectedBrowse()
        fixture.setUp()
        self.fixture = fixture
        with patch.object(core.config, 'load', return_value={'pinned_zone_id': 'fixture-a', 'strict_pins': True}):
            self.core = core.RoonSession(lambda *args: None)
        api = fixture.api
        api.zones = {z: {'zone_id': z, 'display_name': z, 'state': 'paused'}
                     for z in ('fixture-a', 'fixture-b')}
        api._zone_list_generation = 1
        self.core._api = api
        self.core._status = 'ok'
        self.core._browse_sessions['fixture'] = fixture.session

    def invoke(self):
        return self.core.browse_play_selected('fixture', 0, self.fixture.page['level_id'],
                                             'fixture-a', 'synthetic-request')

    def test_attempt_uses_captured_core_and_target(self):
        reply = self.invoke()
        self.assertEqual(reply['outcome'], 'attempted')
        self.assertEqual(reply['request_id'], 'synthetic-request')
        self.assertEqual(len(self.fixture.actions()), 1)

    def test_disconnected_unknown_missing_changed_refuse_before_io(self):
        for variant, code in [('down', 'disconnected'), ('unknown', 'zones_unknown'),
                              ('missing', 'unavailable'), ('changed', 'selection_changed')]:
            with self.subTest(variant=variant):
                self.setUp()
                if variant == 'down': self.core._status = 'unreachable'
                if variant == 'unknown': self.core._api._zone_list_generation = 0
                if variant == 'missing': del self.core._api.zones['fixture-a']
                if variant == 'changed': self.core._arbiter.pin('fixture-b')
                reply = self.invoke()
                self.assertEqual((reply['outcome'], reply['error']), ('refused', code))
                self.assertEqual(self.fixture.api.calls + self.fixture.api.load_calls, [])

    def test_mid_resolution_core_replacement_never_dispatches_on_either_api(self):
        replacement = object()
        self.fixture.api.after_load = lambda level: setattr(self.core, '_api', replacement)
        reply = self.invoke()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(self.fixture.actions(), [])
        self.assertEqual(reply['rows'], [])

    def test_mid_resolution_pin_change_cannot_retarget(self):
        self.fixture.api.after_load = lambda level: self.core._arbiter.pin('fixture-b')
        reply = self.invoke()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'selection_changed'))
        self.assertEqual(self.fixture.actions(), [])

    def test_old_session_api_cannot_be_used_after_replacement(self):
        self.core._api = object()
        reply = self.invoke()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(self.fixture.api.calls + self.fixture.api.load_calls, [])
