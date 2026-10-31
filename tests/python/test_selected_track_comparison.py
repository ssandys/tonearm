"""Offline selected-song comparison; characterization is not live bug proof."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.python.fakes import yavin_levels
from tests.python.test_search_action_baseline import HookedRoon
from tests.python.test_server_browse import roundtrip
from tonearm_lib import art, browse, cli, core

ZONE = 'track-comparison-output-73'


class ContextRoon(HookedRoon):
    def __init__(self, levels):
        super().__init__(levels)
        self.recognized_actions = []

    def browse_browse(self, opts):
        # Distinguish sending an action-looking key from it being valid at the
        # fake's current remote cursor. No playback effect is simulated/claimed.
        key = opts.get('item_key')
        if any(i.get('item_key') == key and i.get('hint') == 'action'
               for i in self.levels[self.current]['items']):
            self.recognized_actions.append(key)
        return super().browse_browse(opts)


def fixture(kind='tracks'):
    levels = yavin_levels()
    levels['library']['items'][0]['_goes_to'] = lambda _: 'results'
    if kind == 'top_track':
        levels['results']['items'] = [dict(levels['tracks']['items'][0])]
        levels['results']['list']['count'] = 1
    api = ContextRoon(levels)
    api._roonsocket = SimpleNamespace(connected=True)
    api._zone_list_generation = 1
    api.zones = {ZONE: {'zone_id': ZONE, 'display_name': 'Synthetic output', 'state': 'paused'}}
    with patch.object(core.config, 'load', return_value={'pinned_zone_id': ZONE, 'strict_pins': True}):
        session = core.RoonSession(lambda _: None)
    session._api = api; session._status = 'ok'
    cursor = session.browse_session('track-comparison')
    page = cursor.search('synthetic query')
    if kind != 'top_track':
        page = cursor.enter(2 if kind == 'tracks' else 1, page['level_id'])
    if kind == 'album_track':
        page = cursor.enter(0, page['level_id'])
    index = 1 if kind == 'album_track' else 0
    action_level = 'album_actions' if kind == 'album' else 'track_actions'
    api.action_key = levels[action_level]['items'][0]['item_key']
    api.calls.clear(); api.load_calls.clear()
    return session, cursor, api, page, index


def invoke(session, page, index):
    return session.browse_play_selected('track-comparison', index, page['level_id'], ZONE, 'selected-track-request')


class SelectedTrackComparison(unittest.TestCase):
    def test_top_track_tracks_category_album_track_and_album_resolve_their_own_action_once(self):
        for kind in ('top_track', 'tracks', 'album_track', 'album'):
            with self.subTest(kind=kind):
                s, _, api, page, index = fixture(kind)
                if kind != 'album':
                    self.assertTrue(page['rows'][index]['can_play'])
                    self.assertFalse(page['rows'][index]['can_navigate'])
                reply = invoke(s, page, index)
                self.assertEqual((reply['outcome'], reply['error']), ('attempted', None))
                self.assertEqual(api.recognized_actions, [api.action_key])
                self.assertEqual(sum(c.get('item_key') == api.action_key for c in api.calls), 1)
                self.assertTrue(all(c['zone_or_output_id'] == ZONE for c in api.calls + api.load_calls))
                self.assertEqual(reply['rows'], [])
                self.assertNotIn('played', reply)

    def test_non_navigable_track_through_actual_model_cli_server_and_core(self):
        s, _, api, page, index = fixture()
        script = ("const M=require('./Model.js'), fs=require('node:fs');"
                  "const d=JSON.parse(fs.readFileSync(0,'utf8'));"
                  "console.log(JSON.stringify(M.browsePlayRequest(d.state,d.row,d.index,d.level,'selected-track-request','play_now')))")
        p = subprocess.run(['node', '-e', script],
                           input=json.dumps(dict(state=s.snapshot(), row=page['rows'][index], index=index, level=page['level_id'])),
                           capture_output=True, text=True, timeout=5, cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(p.returncode, 0, p.stderr); req = json.loads(p.stdout)
        self.assertEqual((req['zone_id'], req['action']), (ZONE, 'play_now'))
        args = ['browse', '--session', 'track-comparison', 'play_selected', str(req['index']),
                str(req['level_id']), req['zone_id'], req['request_id'], req['action']]
        reply = roundtrip(art.CachingSession(s, None), cli.to_request(args))
        self.assertEqual((reply['outcome'], reply['request_id']), ('attempted', 'selected-track-request'))
        self.assertEqual(api.recognized_actions, [api.action_key])

    def test_track_terminal_no_response_or_message_is_unknown_and_never_retried(self):
        for response in (None, {'action': 'message', 'message': 'Synthetic failure'}):
            s, _, api, page, index = fixture()
            api.override_action = True; api.action_reply = response
            reply = invoke(s, page, index)
            self.assertEqual(reply['outcome'], 'unknown')
            self.assertEqual(sum(c.get('item_key') == api.action_key for c in api.calls), 1)

    def test_track_repin_before_terminal_action_refuses_without_action(self):
        s, _, api, page, index = fixture()
        api.after_load = lambda _: s._arbiter.pin('another-explicit-output')
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'selection_changed'))
        self.assertEqual(api.recognized_actions, [])

    def test_direct_terminal_hint_is_advertised_playable_but_refused_before_io(self):
        s, cursor, api, page, index = fixture()
        api.levels['tracks']['items'][0]['hint'] = 'action'
        page = cursor.page(0); api.calls.clear(); api.load_calls.clear()
        self.assertTrue(page['rows'][0]['can_play'])
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'unsupported_action'))
        self.assertEqual(api.calls + api.load_calls, [])

    def test_replacing_api_during_track_resolution_refuses(self):
        s, _, api, page, index = fixture()
        api.after_load = lambda _: setattr(s, '_api', object())
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(api.recognized_actions, [])

    def test_same_api_disconnected_socket_refuses_track_action(self):
        s, _, api, page, index = fixture()
        api.after_load = lambda _: setattr(api._roonsocket, 'connected', False)
        reply = invoke(s, page, index)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'disconnected'))
        self.assertEqual(api.recognized_actions, [])

    def test_same_api_new_socket_and_reset_remote_cursor_refuses_old_track_key(self):
        # The connection identity must change context even if the API object,
        # ready zone generation and selected zone remain otherwise acceptable.
        s, _, api, page, index = fixture()
        old_socket = api._roonsocket
        def reconnect(level):
            if level == 'track_actions':
                api.after_load = None
                api._roonsocket = SimpleNamespace(connected=True)
                api._zone_list_generation += 1
                api.current = 'root'; api.stack = []
        api.after_load = reconnect
        reply = invoke(s, page, index)
        self.assertIsNot(api._roonsocket, old_socket)
        self.assertEqual((reply['outcome'], reply['error']), ('refused', 'core_changed'))
        self.assertEqual(sum(c.get('item_key') == api.action_key for c in api.calls), 0)
        self.assertEqual(api.recognized_actions, [])
        self.assertEqual(reply['rows'], [])
