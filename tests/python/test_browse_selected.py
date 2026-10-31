"""Opt-in browse-play safety: synthetic SDK calls only."""
import unittest
from tests.python.test_search_action_baseline import HookedRoon
from tests.python.fakes import yavin_levels
from tonearm_lib import browse


class TestSelectedBrowse(unittest.TestCase):
    def setUp(self):
        self.selected = 'fixture-a'
        self.context_error = None
        levels = yavin_levels()
        levels['library']['items'][0]['_goes_to'] = lambda term: 'results'
        self.api = HookedRoon(levels)
        self.session = browse.BrowseSession(self.api, 'fixture', lambda: self.selected)
        reply = self.session.search('synthetic')
        self.page = self.session.enter(1, reply['level_id'])
        self.api.calls.clear()
        self.api.load_calls.clear()

    def guard(self):
        return self.context_error or (None if self.selected == 'fixture-a' else 'selection_changed')

    def play(self, **changes):
        args = dict(index=0, level_id=self.page['level_id'], zone_id='fixture-a',
                    request_id='fixture-request', validate=self.guard)
        args.update(changes)
        return self.session.play_selected(**args)

    def actions(self):
        return [c for c in self.api.calls if c.get('item_key') == self.api.action_key]

    def test_attempt_correlates_and_never_claims_played(self):
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('attempted', None))
        self.assertEqual((reply['request_id'], reply['zone_id']), ('fixture-request', 'fixture-a'))
        self.assertNotIn('played', reply)
        self.assertEqual(len(self.actions()), 1)
        for call in self.api.calls + self.api.load_calls:
            self.assertEqual(call['zone_or_output_id'], 'fixture-a')

    def test_pre_dispatch_changed_context_refuses_without_io(self):
        self.context_error = 'core_changed'
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(self.api.calls + self.api.load_calls, [])

    def test_mid_resolution_repin_refuses_without_action_or_retargeted_cleanup(self):
        def repin(level):
            if level == 'album_actions':
                self.selected = 'fixture-b'
        self.api.after_load = repin
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'selection_changed'))
        self.assertEqual(self.actions(), [])
        self.assertTrue(all(c['zone_or_output_id'] == 'fixture-a' for c in self.api.calls))
        self.assertEqual(reply['rows'], [])
        self.assertGreater(reply['level_id'], self.page['level_id'])

    def test_missing_context_mid_resolution_refuses(self):
        self.api.after_load = lambda level: setattr(self, 'context_error', 'unavailable')
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unavailable'))
        self.assertEqual(self.actions(), [])

    def test_stale_and_boolean_addresses_refuse_before_io(self):
        for changes in ({'level_id': self.page['level_id']+1}, {'index': True}, {'level_id': True}):
            reply = self.play(**changes)
            self.assertEqual(reply['outcome'], 'refused')
        self.assertEqual(self.api.calls + self.api.load_calls, [])

    def test_terminal_prompt_refused_before_action(self):
        for item in self.api.levels['album_actions']['items']:
            if item['item_key'] == self.api.action_key:
                item['input_prompt'] = {'prompt': 'Synthetic confirmation'}
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unsupported_prompt'))
        self.assertEqual(self.actions(), [])

    def test_selected_row_prompt_refused_before_any_io(self):
        # Re-adopt an input-bearing result before attempting its address.
        item = self.api.levels['albums']['items'][0]
        item['input_prompt'] = {'prompt': 'Synthetic input'}
        self.page = self.session.page(0)
        self.api.calls.clear()
        self.api.load_calls.clear()
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unsupported_prompt'))
        self.assertEqual(self.api.calls + self.api.load_calls, [])

    def test_terminal_message_and_no_reply_are_unknown_without_retry(self):
        for response in ({'action': 'message', 'message': 'Synthetic response'},
                         {'input_prompt': {'prompt': 'Synthetic confirmation'}}, None):
            with self.subTest(response=response):
                self.setUp()
                self.api.override_action = True
                self.api.action_reply = response
                reply = self.play()
                self.assertEqual(reply['outcome'], 'unknown')
                self.assertIsInstance(reply['error'], str)
                self.assertEqual(len(self.actions()), 1)
                self.assertNotIn('played', reply)

    def test_terminal_exception_is_unknown_without_retry(self):
        original = self.api.browse_browse
        def fail(opts):
            if opts.get('item_key') == self.api.action_key:
                self.api.calls.append(dict(opts))
                raise RuntimeError('synthetic transport failure')
            return original(opts)
        self.api.browse_browse = fail
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('unknown', 'dispatch_failed'))
        self.assertEqual(len(self.actions()), 1)

    def test_lost_context_after_action_keeps_attempt_and_invalidates_cursor(self):
        original = self.api.browse_browse
        def lose(opts):
            result = original(opts)
            if opts.get('item_key') == self.api.action_key:
                self.context_error = 'core_changed'
            return result
        self.api.browse_browse = lose
        reply = self.play()
        self.assertEqual(reply['outcome'], 'attempted')
        self.assertEqual(reply['rows'], [])
        self.assertEqual(len(self.actions()), 1)

    def test_guard_failure_at_terminal_opts_is_refused_before_sdk_entry(self):
        original = self.session._opts
        def opts(**extra):
            if extra.get('item_key') == self.api.action_key:
                self.context_error = 'selection_changed'
            return original(**extra)
        self.session._opts = opts
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'selection_changed'))
        self.assertEqual(self.actions(), [])

    def test_failed_unwind_invalidates_local_keys_after_attempt(self):
        original = self.api.browse_load
        def fail_cleanup(opts):
            if self.actions():
                return None
            return original(opts)
        self.api.browse_load = fail_cleanup
        reply = self.play()
        self.assertEqual(reply['outcome'], 'attempted')
        self.assertEqual(reply['rows'], [])
        self.assertEqual(len(self.actions()), 1)

    def test_direct_terminal_row_is_refused_without_implicit_action(self):
        # Direct action rows need a separate, measured confirmation flow.
        self.api.levels['albums']['items'][0]['hint'] = 'action'
        self.page = self.session.page(0)
        self.api.calls.clear()
        self.api.load_calls.clear()
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unsupported_action'))
        self.assertEqual(self.api.calls + self.api.load_calls, [])

    def test_wrapper_input_prompt_is_not_followed_automatically(self):
        self.api.levels['album_detail']['items'][0]['input_prompt'] = {'prompt': 'Synthetic input'}
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unsupported_prompt'))
        self.assertEqual(self.actions(), [])

    def test_terminal_attempt_invalidates_old_item_addresses(self):
        reply = self.play()
        self.assertEqual(reply['outcome'], 'attempted')
        self.assertEqual(reply['rows'], [])
        self.assertGreater(reply['level_id'], self.page['level_id'])
        self.api.calls.clear()
        self.api.load_calls.clear()
        stale = self.play()
        self.assertEqual((stale['outcome'], stale['error']), ('refused', 'stale'))
        self.assertEqual(self.api.calls + self.api.load_calls, [])

    def test_unadvertised_row_is_refused_before_resolution(self):
        self.api.levels['albums']['items'][0]['hint'] = 'unknown'
        self.page = self.session.page(0)
        self.api.calls.clear()
        self.api.load_calls.clear()
        reply = self.play()
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unsupported_action'))
        self.assertEqual(self.api.calls + self.api.load_calls, [])
