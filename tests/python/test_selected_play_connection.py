"""Connection-bound selected playback, with no real SDK/daemon/audio."""
from types import SimpleNamespace
import unittest

from tests.python.test_selected_track_comparison import fixture, invoke


def reconnect(api):
    api._roonsocket = SimpleNamespace(connected=True)
    api._zone_list_generation += 1


class SelectedPlayConnectionTest(unittest.TestCase):
    def test_preexisting_cursor_from_old_connection_refuses_before_any_io(self):
        s, _, api, page, index = fixture()
        reconnect(api)
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(api.calls + api.load_calls, [])
        self.assertEqual(reply['rows'], [])
        self.assertGreater(reply['level_id'], page['level_id'])

    def test_explicit_fresh_navigation_adopts_new_connection_and_can_play(self):
        s, cursor, api, page, index = fixture()
        reconnect(api); invoke(s, page, index)
        page = cursor.search('fresh synthetic query')
        page = cursor.enter(2, page['level_id'])
        api.calls.clear(); api.load_calls.clear()
        reply = invoke(s, page, 0)
        self.assertEqual((reply['outcome'], reply['error']), ('attempted', None))
        self.assertEqual(api.recognized_actions, [api.action_key])

    def test_connection_change_while_loading_navigation_rows_does_not_rebind_old_reply(self):
        s, cursor, api, _, index = fixture()
        def change(_):
            api.after_load = None
            reconnect(api)
        api.after_load = change
        page = cursor.page(0)
        api.calls.clear(); api.load_calls.clear()
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(api.calls + api.load_calls, [])

    def test_failed_adoption_after_reconnect_does_not_relabel_retained_old_keys(self):
        s, cursor, api, page, index = fixture()
        reconnect(api)
        api.levels['tracks']['items'] = [object()]  # normalization fails before keys are replaced
        with self.assertRaises(AttributeError):
            cursor.page(0)
        api.calls.clear(); api.load_calls.clear()
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(api.calls + api.load_calls, [])

    def test_connection_change_during_first_resolution_reply_refuses_before_load_or_action(self):
        s, _, api, page, index = fixture()
        original = api.browse_browse
        def change(opts):
            reply = original(opts)
            reconnect(api)
            return reply
        api.browse_browse = change
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.load_calls, [])
        self.assertEqual(api.recognized_actions, [])

    def test_reconnect_during_terminal_reply_is_unknown_no_cleanup_on_new_connection_or_retry(self):
        s, _, api, page, index = fixture()
        original = api.browse_browse
        count_at_reconnect = []
        def change(opts):
            reply = original(opts)
            if opts.get('item_key') == api.action_key:
                reconnect(api)
                count_at_reconnect.append((len(api.calls), len(api.load_calls)))
            return reply
        api.browse_browse = change
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('unknown', 'core_changed'))
        self.assertEqual(api.recognized_actions, [api.action_key])
        self.assertEqual((len(api.calls), len(api.load_calls)), count_at_reconnect[0])
        self.assertEqual(reply['rows'], [])
        calls = (len(api.calls), len(api.load_calls))
        stale = invoke(s, page, index)
        self.assertEqual(stale['outcome'], 'refused')
        self.assertEqual((len(api.calls), len(api.load_calls)), calls)

    def test_reconnect_only_during_cleanup_keeps_already_accepted_attempt(self):
        s, _, api, page, index = fixture()
        def change(_):
            if api.recognized_actions:
                api.after_load = None
                reconnect(api)
        api.after_load = change
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('attempted', None))
        self.assertEqual(api.recognized_actions, [api.action_key])
        self.assertEqual(reply['rows'], [])
