"""Offline Play Next and Add to Queue. No Core, daemon, socket, or queue write."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeRoon
from test_browse_play import ZONE as WRAP_ZONE, deep_wrapper_levels
from test_selected_track_comparison import ZONE, fixture
from test_server_browse import roundtrip
from tonearm_lib import art, browse


def invoke(session, page, index, action):
    return session.browse_play_selected(
        'track-comparison', index, page['level_id'], ZONE, 'queue-action-request', action)


def menu(kind):
    return 'album_actions' if kind == 'album' else 'track_actions'


def action_key(api, kind, action):
    title = browse.SELECTED_ACTIONS[action]
    return next(item['item_key'] for item in api.levels[menu(kind)]['items']
                if item['title'] == title)


class TestQueueActions(unittest.TestCase):
    def test_track_and_album_actions_resolve_their_own_key_once(self):
        for action in ('play_now', 'play_next', 'add_to_queue'):
            for kind in ('top_track', 'tracks', 'album_track', 'album'):
                with self.subTest(action=action, kind=kind):
                    s, _, api, page, index = fixture(kind)
                    key = action_key(api, kind, action)
                    reply = invoke(s, page, index, action)
                    sent = [call.get('item_key') for call in api.calls]
                    self.assertEqual((reply['outcome'], reply['error'], reply['action']),
                                     ('attempted', None, action))
                    self.assertEqual(api.recognized_actions, [key])
                    self.assertEqual(sent.count(key), 1)
                    self.assertTrue(all(call['zone_or_output_id'] == ZONE
                                        for call in api.calls + api.load_calls))
                    self.assertEqual(reply['rows'], [])
                    self.assertNotIn('played', reply)
                    if kind == 'album':
                        self.assertFalse(any(item and str(item).startswith('72:') for item in sent))

    def test_in_process_default_remains_play_now(self):
        s, _, api, page, index = fixture('tracks')
        reply = s.browse_play_selected(
            'track-comparison', index, page['level_id'], ZONE, 'default-request')
        self.assertEqual((reply['action'], api.recognized_actions),
                         ('play_now', [action_key(api, 'tracks', 'play_now')]))

    def test_legacy_play_still_invokes_only_play_now(self):
        _, cursor, api, page, _ = fixture('album')
        reply = cursor.play(0, page['level_id'])
        self.assertIs(reply['played'], True)
        self.assertEqual(api.recognized_actions, [action_key(api, 'album', 'play_now')])

    def test_menu_order_does_not_change_the_selected_action(self):
        s, _, api, page, index = fixture('album')
        api.levels['album_actions']['items'].reverse()
        reply = invoke(s, page, index, 'play_now')
        self.assertEqual(api.recognized_actions, ['71:0'])
        self.assertEqual(reply['action'], 'play_now')
        self.assertNotIn('71:3', api.recognized_actions)

    def test_missing_action_does_not_fall_back_to_play_now_or_another_action(self):
        for action in ('play_next', 'add_to_queue'):
            for kind in ('tracks', 'album'):
                with self.subTest(action=action, kind=kind):
                    s, _, api, page, index = fixture(kind)
                    title = browse.SELECTED_ACTIONS[action]
                    level = api.levels[menu(kind)]
                    level['items'] = [item for item in level['items'] if item['title'] != title]
                    reply = invoke(s, page, index, action)
                    sent = [call.get('item_key') for call in api.calls]
                    self.assertEqual((reply['outcome'], reply['error'], reply['action']),
                                     ('refused', 'no_action', action))
                    self.assertEqual(api.recognized_actions, [])
                    for item in level['items']:
                        if item.get('hint') == 'action':
                            self.assertNotIn(item['item_key'], sent)

    def test_wrapped_album_uses_one_album_action_and_no_track_keys(self):
        for action, key in (('play_next', '8:21'), ('add_to_queue', '8:22')):
            with self.subTest(action=action):
                api = FakeRoon(deep_wrapper_levels())
                session = browse.BrowseSession(api, 'wrapper', lambda: WRAP_ZONE)
                page = session.search('oingo boingo')
                api.calls.clear()
                api.load_calls.clear()
                reply = session.play_selected(
                    0, page['level_id'], WRAP_ZONE, 'wrap-request', lambda: None, action)
                sent = [call.get('item_key') for call in api.calls]
                self.assertEqual((reply['outcome'], reply['error'], reply['action']),
                                 ('attempted', None, action))
                self.assertEqual(sent.count(key), 1)
                self.assertNotIn('8:20', sent)
                self.assertNotIn('8:23', sent)
                for track in ('8:%d' % n for n in range(11, 20)):
                    self.assertNotIn(track, sent)
                self.assertTrue(all(call['zone_or_output_id'] == WRAP_ZONE
                                    for call in api.calls + api.load_calls))
                self.assertEqual(reply['rows'], [])
                self.assertNotIn('played', reply)

    def test_context_loss_refuses_before_sending_the_queue_action(self):
        for action in ('play_next', 'add_to_queue'):
            for kind in ('album_track', 'album'):
                for variant, code in (('pin', 'selection_changed'),
                                      ('down', 'disconnected'),
                                      ('core', 'core_changed')):
                    with self.subTest(action=action, kind=kind, variant=variant):
                        s, _, api, page, index = fixture(kind)
                        if variant == 'pin':
                            api.after_load = lambda _: s._arbiter.pin('another-explicit-output')
                        elif variant == 'down':
                            api.after_load = lambda _: setattr(api._roonsocket, 'connected', False)
                        else:
                            api.after_load = lambda _: setattr(s, '_api', object())
                        reply = invoke(s, page, index, action)
                        self.assertEqual((reply['outcome'], reply['error'], reply['action']),
                                         ('refused', code, action))
                        self.assertEqual(api.recognized_actions, [])

    def test_stale_prompt_and_direct_rows_refuse_before_queue_io(self):
        for action in ('play_next', 'add_to_queue'):
            with self.subTest(action=action, case='stale'):
                s, _, api, page, index = fixture('tracks')
                reply = s.browse_play_selected(
                    'track-comparison', index, page['level_id'] + 1, ZONE, 'stale-request', action)
                self.assertEqual((reply['outcome'], reply['error']), ('refused', 'stale'))
                self.assertEqual(api.calls + api.load_calls, [])
            with self.subTest(action=action, case='prompt'):
                s, _, api, page, index = fixture('album')
                item = next(item for item in api.levels['album_actions']['items']
                            if item['item_key'] == action_key(api, 'album', action))
                item['input_prompt'] = {'prompt': 'Synthetic confirmation'}
                reply = invoke(s, page, index, action)
                self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unsupported_prompt'))
                self.assertEqual(api.recognized_actions, [])
            with self.subTest(action=action, case='direct'):
                s, cursor, api, page, index = fixture('tracks')
                api.levels['tracks']['items'][0]['hint'] = 'action'
                page = cursor.page(0)
                api.calls.clear()
                api.load_calls.clear()
                reply = s.browse_play_selected(
                    'track-comparison', 0, page['level_id'], ZONE, 'direct-request', action)
                self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unsupported_action'))
                self.assertEqual(api.calls + api.load_calls, [])

    def test_terminal_silence_is_unknown_once_and_is_not_retried(self):
        for action in ('play_next', 'add_to_queue'):
            for response in (None, {'action': 'message', 'message': 'Synthetic failure'}):
                with self.subTest(action=action, response=response):
                    s, _, api, page, index = fixture('album_track')
                    key = action_key(api, 'album_track', action)
                    api.action_key = key
                    api.override_action = True
                    api.action_reply = response
                    reply = invoke(s, page, index, action)
                    self.assertEqual(reply['outcome'], 'unknown')
                    self.assertEqual(reply['action'], action)
                    self.assertEqual(api.recognized_actions, [key])
                    self.assertNotIn('played', reply)

    def test_unknown_tokens_are_refused_before_a_session_or_cursor_change(self):
        s, cursor, api, page, index = fixture('tracks')
        before = cursor.current()['level_id']
        rows = list(cursor.current()['rows'])
        for bad in ('Queue', 'Add Next', 'Play Next', 'Start Radio', 'start_radio', '', None):
            with self.subTest(bad=bad):
                reply = s.browse_play_selected(
                    'track-comparison', index, page['level_id'], ZONE, 'bad-request', bad)
                self.assertEqual((reply['outcome'], reply['error'], reply['action']),
                                 ('refused', 'bad_request', None))
        self.assertEqual(api.calls + api.load_calls, [])
        self.assertEqual(cursor.current()['level_id'], before)
        self.assertEqual(cursor.current()['rows'], rows)
        wire = dict(cmd='browse', op='play_selected', session='track-comparison', index=index,
                    level_id=page['level_id'], zone_id=ZONE, request_id='wire-bad', action='Queue')
        reply = roundtrip(art.CachingSession(s, None), wire)
        self.assertEqual((reply['outcome'], reply['error'], reply['action']),
                         ('refused', 'bad_request', None))
        self.assertEqual(cursor.current()['level_id'], before)
        self.assertEqual(api.calls + api.load_calls, [])


if __name__ == '__main__':
    unittest.main()
