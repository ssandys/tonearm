"""Batch6B inspection probes, not acceptance of safe targeted browse playback.

These characterize preserved LEGACY behavior so a new opt-in path can be tested
against the same deterministic seams. No real SDK, socket or Core is used.
"""
import unittest
from tests.python.fakes import FakeRoon, yavin_levels
from tonearm_lib import browse


class HookedRoon(FakeRoon):
    def __init__(self, levels):
        super().__init__(levels)
        self.after_load = None
        self.action_reply = None
        self.override_action = False
        self.action_key = next(item['item_key'] for item in levels['album_actions']['items']
                               if item['title'] == 'Play Now')

    def browse_load(self, opts):
        reply = super().browse_load(opts)
        if self.after_load:
            self.after_load(self.current)
        return reply

    def browse_browse(self, opts):
        if self.override_action and opts.get('item_key') == self.action_key:
            self.calls.append(dict(opts))
            return self.action_reply
        return super().browse_browse(opts)


class TestLegacySearchActionBaseline(unittest.TestCase):
    def setUp(self):
        self.selected = 'fixture-output-a'
        levels = yavin_levels()
        levels['library']['items'][0]['_goes_to'] = lambda term: 'results'
        self.api = HookedRoon(levels)
        self.session = browse.BrowseSession(self.api, 'fixture-search', lambda: self.selected)
        reply = self.session.search('fixture query')
        self.page = self.session.enter(1, reply['level_id'])
        self.api.calls.clear()
        self.api.load_calls.clear()

    def actions(self):
        return [call for call in self.api.calls
                if call.get('item_key') == self.api.action_key]

    def test_legacy_resolution_can_switch_targets_before_final_action(self):
        def repin(level):
            if level == 'album_actions':
                self.selected = 'fixture-output-b'
        self.api.after_load = repin
        reply = self.session.play(0, self.page['level_id'])
        self.assertTrue(reply['played'])
        self.assertEqual(self.api.calls[0]['zone_or_output_id'], 'fixture-output-a')
        self.assertEqual(len(self.actions()), 1)
        self.assertEqual(self.actions()[0]['zone_or_output_id'], 'fixture-output-b')
        # This deliberately proves the legacy gap, not the desired new contract.

    def test_disappearance_before_terminal_action_prevents_dispatch(self):
        def disappear(level):
            if level == 'album_actions':
                self.selected = None
        self.api.after_load = disappear
        with self.assertRaises(browse.BrowseError) as caught:
            self.session.play(0, self.page['level_id'])
        self.assertEqual(caught.exception.token, 'no_zone')
        self.assertEqual(self.actions(), [])

    def test_legacy_message_response_is_still_reported_as_played(self):
        self.api.override_action = True
        self.api.action_reply = {'action': 'message', 'message': 'Synthetic confirmation required'}
        reply = self.session.play(0, self.page['level_id'])
        self.assertTrue(reply['played'])
        self.assertEqual(len(self.actions()), 1)
        # A non-None response does not prove the intended remote effect.

    def test_legacy_play_does_not_resolve_a_terminal_input_prompt(self):
        action = next(item for item in self.api.levels['album_actions']['items']
                      if item['item_key'] == self.api.action_key)
        action['input_prompt'] = {'prompt': 'Synthetic confirmation', 'action': 'Confirm'}
        reply = self.session.play(0, self.page['level_id'])
        self.assertTrue(reply['played'])
        self.assertEqual(len(self.actions()), 1)
        self.assertNotIn('input', self.actions()[0])
        # The new path must refuse unsupported prompts, never guess a response.


if __name__ == '__main__':
    unittest.main()
