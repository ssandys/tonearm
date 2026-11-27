"""Stateful synthetic cursor regressions; no live SDK or Core operations."""
import unittest
from types import SimpleNamespace

from tests.python.fakes import FakeRoon
from tonearm_lib import browse


def level(depth, items):
    return {'list': {'level': depth, 'count': len(items)}, 'items': items}


class StatefulRoon(FakeRoon):
    """Every accepted action pushes a remote level BEFORE its canned response.

    In particular, terminal dispatch never clears the stack to root. A wrong
    cleanup count leaves an observable remote cursor/stack mismatch.
    """
    def __init__(self, kind='album'):
        row_hint = 'action_list' if kind == 'track' else 'list'
        row_target = 'menu' if kind == 'track' else 'wrapper' if kind == 'wrapped' else 'detail'
        levels = {
            'root': level(0, [dict(title='Library', hint='list', item_key='library', _goes_to='library'),
                              dict(title='Settings', hint='list', item_key='settings', _goes_to='settings')]),
            'settings': level(1, []),
            'library': level(1, [dict(title='Synthetic selection', hint=row_hint, item_key='selected', _goes_to=row_target)]),
            'wrapper': level(2, [dict(title='Synthetic album', hint='list', item_key='wrapper', _goes_to='detail')]),
            'detail': level(3 if kind == 'wrapped' else 2,
                            [dict(title='Play Album', hint='action_list', item_key='menu', _goes_to='menu'),
                             dict(title='Synthetic track', hint='action_list', item_key='track', _goes_to='menu')]),
            'menu': level(4 if kind == 'wrapped' else 2 if kind == 'track' else 3,
                          [dict(title=title, hint='action', item_key=token, _goes_to='terminal')
                           for token, title in browse.SELECTED_ACTIONS.items()]),
            'terminal': level(5, []),
        }
        super().__init__(levels)
        self._roonsocket = SimpleNamespace(connected=True)
        self.terminal_calls = []
        self.response = 'normal'
        self.fail_at = 'terminal'
        self.after_dispatch = None
        self.after_load = None
        self.stale_library = False

    def _target_for(self, key, text):
        if self.stale_library and key == 'library':
            return None
        return super()._target_for(key, text)

    def browse_browse(self, opts):
        terminal = self.current == 'menu' and 'item_key' in opts
        if terminal:
            self.terminal_calls.append(dict(opts))
        result = super().browse_browse(opts)
        if self.after_dispatch:
            self.after_dispatch(opts)
        fail = terminal if self.fail_at == 'terminal' else opts.get('item_key') == self.fail_at
        if fail and self.response != 'normal':
            if self.response == 'exception':
                raise RuntimeError('synthetic failure after cursor moved')
            return self.response
        return result

    def browse_load(self, opts):
        result = super().browse_load(opts)
        if self.after_load:
            self.after_load(self.current)
        return result


class StatefulSelectedTest(unittest.TestCase):
    def fixture(self, kind='album'):
        self.api = StatefulRoon(kind)
        self.session = browse.BrowseSession(self.api, 'stateful', lambda: 'fixture-zone')
        self.page = self.session.library()
        self.origin = (self.api.current, list(self.api.stack))
        self.context_error = None
        self.cleanup_depths = []
        original = self.session._unwind
        def unwind(depth):
            self.cleanup_depths.append(depth)
            return original(depth)
        self.session._unwind = unwind
        self.api.calls.clear(); self.api.load_calls.clear()

    def play(self, action='play_now'):
        return self.session.play_selected(0, self.page['level_id'], 'fixture-zone',
                                          'stateful-request', lambda: self.context_error, action)

    def pops(self):
        return [call['pop_levels'] for call in self.api.calls if 'pop_levels' in call]

    def assert_invalidated(self, reply):
        self.assertEqual(reply['rows'], [])
        self.assertGreater(reply['level_id'], self.page['level_id'])
        self.api.calls.clear(); self.api.load_calls.clear()
        stale = self.play()
        self.assertEqual((stale['outcome'], stale['error']), ('refused', 'stale'))
        self.assertEqual(self.api.calls + self.api.load_calls, [])

    def test_terminal_moves_before_bad_reply_or_exception_and_exactly_unwinds(self):
        for kind, depth in [('track', 2), ('album', 3), ('wrapped', 4)]:
            for action in browse.SELECTED_ACTIONS:
                for response, error in [(None, 'roon_error'), ({'action':'message','message':'Synthetic'}, 'action_response'),
                                        ({'action':'error'}, 'action_response'), ('exception', 'dispatch_failed')]:
                    with self.subTest(kind=kind, action=action, response=response):
                        self.fixture(kind); self.api.response = response
                        reply = self.play(action)
                        self.assertEqual((reply['outcome'], reply['error']), ('unknown', error))
                        self.assertEqual([c['item_key'] for c in self.api.terminal_calls], [action])
                        self.assertEqual(self.cleanup_depths, [depth])
                        self.assertEqual(self.pops(), [depth])
                        self.assertEqual((self.api.current, self.api.stack), self.origin)
                        self.assert_invalidated(reply)

    def test_nonterminal_bad_responses_count_every_entered_step_without_attempt(self):
        for key, depth in [('selected', 1), ('menu', 2)]:
            with self.subTest(key=key):
                self.fixture(); self.api.fail_at = key
                self.api.response = {'action':'message','message':'Synthetic'}
                reply = self.play()
                self.assertEqual((reply['outcome'], reply['error']), ('refused', 'action_response'))
                self.assertEqual(self.api.terminal_calls, [])
                self.assertEqual(self.cleanup_depths, [depth]); self.assertEqual(self.pops(), [depth])
                self.assertEqual((self.api.current, self.api.stack), self.origin)
                self.assert_invalidated(reply)

    def test_changed_context_explicitly_refuses_cleanup_without_pop_or_replay(self):
        for change in ('selection', 'socket'):
            with self.subTest(change=change):
                self.fixture(); self.api.response = {'action':'message','message':'Synthetic'}
                def changed(opts):
                    if opts.get('item_key') == 'play_now':
                        if change == 'socket': self.api._roonsocket = SimpleNamespace(connected=True)
                        else: self.context_error = 'selection_changed'
                self.api.after_dispatch = changed
                reply = self.play()
                self.assertEqual((reply['outcome'],reply['error']),
                                 ('unknown','core_changed' if change=='socket' else 'action_response'))
                self.assertEqual(self.cleanup_depths, [3]); self.assertEqual(self.pops(), [])
                self.assertEqual(self.api.current, 'terminal')
                self.assertEqual(len(self.api.terminal_calls), 1)
                self.assert_invalidated(reply)

    def test_guard_refusal_before_sdk_entry_is_not_counted(self):
        for refused_key, depth in [('selected', 0), ('menu', 1), ('play_now', 2)]:
            with self.subTest(key=refused_key):
                self.fixture(); original = self.session._opts
                def opts(**extra):
                    if extra.get('item_key') == refused_key: self.context_error = 'selection_changed'
                    return original(**extra)
                self.session._opts = opts
                reply = self.play()
                self.assertEqual((reply['outcome'], reply['error']), ('refused', 'selection_changed'))
                self.assertEqual(self.cleanup_depths, [depth]); self.assertEqual(self.pops(), [])
                self.assertFalse(any(c.get('item_key') == refused_key for c in self.api.calls))
                self.assertEqual(self.api.terminal_calls, [])

    def test_invalid_terminal_key_never_latches_or_sends_for_any_token(self):
        missing = object()
        for action in browse.SELECTED_ACTIONS:
            for key in [missing, None, '', 42, [], {}]:
                with self.subTest(action=action, key=key):
                    self.fixture()
                    item = next(i for i in self.api.levels['menu']['items'] if i['item_key'] == action)
                    if key is missing: del item['item_key']
                    else: item['item_key'] = key
                    reply = self.play(action)
                    self.assertEqual(reply['outcome'], 'refused')
                    self.assertIn(reply['error'], ('no_action', 'unsupported_action'))
                    self.assertEqual(self.api.terminal_calls, [])
                    self.assertTrue(all(isinstance(c['item_key'],str) and c['item_key'] for c in self.api.calls if 'item_key' in c))
                    self.assertEqual(self.cleanup_depths, [2]); self.assertEqual(self.pops(), [2])
                    self.assertEqual((self.api.current, self.api.stack), self.origin)
                    self.assert_invalidated(reply)

    def test_success_and_legacy_keep_exact_depth_and_remote_cursor(self):
        for kind, depth in [('track',2),('album',3),('wrapped',4)]:
            for legacy in (False, True):
                with self.subTest(kind=kind, legacy=legacy):
                    self.fixture(kind)
                    reply = self.session.play(0,self.page['level_id']) if legacy else self.play()
                    self.assertTrue(reply['played'] if legacy else reply['outcome'] == 'attempted')
                    self.assertEqual(self.pops(), [depth])
                    self.assertEqual((self.api.current,self.api.stack), self.origin)
                    self.assertEqual(len(self.api.terminal_calls), 1)


class StatefulLibraryTest(unittest.TestCase):
    def test_same_socket_failed_walk_revokes_prior_keys_without_retry(self):
        for phase in ('root_browse','root_load','child_browse','child_load','adopt'):
            for failure in ('none','browse_error','exception'):
                with self.subTest(phase=phase,failure=failure):
                    api=StatefulRoon();session=browse.BrowseSession(api,'library-failed-walk',lambda:'fixture-zone')
                    previous=session.library();self.assertTrue(previous['rows'])
                    socket=api._roonsocket
                    api.calls.clear();api.load_calls.clear()
                    original_browse=api.browse_browse;original_load=api.browse_load
                    original_adopt=session._adopt
                    error=(browse.BrowseError('synthetic_failure','Synthetic failure after movement',previous)
                           if failure=='browse_error' else RuntimeError('Synthetic failure after movement'))
                    def fail():
                        if failure=='none':return None
                        raise error
                    def browse_call(opts):
                        result=original_browse(opts)
                        if ((phase=='root_browse' and opts.get('pop_all')) or
                                (phase=='child_browse' and opts.get('item_key')=='library')):return fail()
                        return result
                    def load_call(opts):
                        result=original_load(opts)
                        if ((phase=='root_load' and api.current=='root') or
                                (phase=='child_load' and api.current=='library')):return fail()
                        return result
                    def adopt_call(**kwargs):
                        if phase=='adopt':return fail()
                        return original_adopt(**kwargs)
                    api.browse_browse=browse_call;api.browse_load=load_call;session._adopt=adopt_call
                    expected=RuntimeError if failure=='exception' else browse.BrowseError
                    with self.assertRaises(expected) as caught:session.library()
                    if failure!='none':self.assertIs(caught.exception,error)
                    if isinstance(caught.exception,browse.BrowseError):
                        self.assertEqual(caught.exception.token,'roon_error' if failure=='none' else 'synthetic_failure')
                        if caught.exception.level is not None:
                            self.assertEqual(caught.exception.level['rows'],[])
                            self.assertEqual(caught.exception.level['path'],[])
                    self.assertIs(api._roonsocket,socket)
                    self.assertEqual(api.current,'root' if phase.startswith('root') else 'library')
                    self.assertEqual(session.current()['rows'],[]);self.assertEqual(session.current()['path'],[])
                    self.assertEqual(session._keys,[]);self.assertIsNone(session._cursor_connection)
                    self.assertEqual(session.level_id,previous['level_id']+1)
                    self.assertEqual(sum(bool(c.get('pop_all')) for c in api.calls),1)
                    self.assertEqual(sum(c.get('item_key')=='library' for c in api.calls),0 if phase.startswith('root') else 1)
                    self.assertFalse(any('pop_levels' in c for c in api.calls))
                    api.calls.clear();api.load_calls.clear()
                    stale=session.play_selected(0,previous['level_id'],'fixture-zone','old-library',lambda:None,'play_now')
                    self.assertEqual((stale['outcome'],stale['error']),('refused','stale'))
                    self.assertEqual(api.calls+api.load_calls,[])
                    self.assertEqual(api.terminal_calls,[])

    def test_failed_walk_does_not_reset_an_already_invalidated_view_twice(self):
        for failure in ('socket','stale_root'):
            with self.subTest(failure=failure):
                api=StatefulRoon();session=browse.BrowseSession(api,'library-once')
                previous=session.library()
                if failure=='socket':api.after_dispatch=lambda opts:setattr(api,'_roonsocket',SimpleNamespace(connected=True))
                else:api.stale_library=True
                with self.assertRaises(browse.BrowseError):session.library()
                self.assertEqual(session.level_id,previous['level_id']+1)
                self.assertEqual(session.current()['rows'],[]);self.assertEqual(session.current()['path'],[])

    def test_stale_library_key_returns_root_settings_but_never_publishes_library(self):
        api=StatefulRoon(); api.stale_library=True
        session=browse.BrowseSession(api,'library-stale')
        with self.assertRaises(browse.BrowseError) as caught: session.library()
        self.assertEqual(caught.exception.token,'roon_error')
        self.assertEqual(session.current()['rows'],[]);self.assertEqual(session.current()['path'],[])
        self.assertEqual([c.get('item_key') for c in api.calls if 'item_key' in c],['library'])
        self.assertEqual([i['title'] for i in api.levels[api.current]['items']],['Library','Settings'])

    def test_normal_child_and_missing_level_metadata_stay_usable(self):
        for missing in (False,True):
            with self.subTest(missing=missing):
                api=StatefulRoon()
                if missing:
                    for value in api.levels.values():value['list'].pop('level',None)
                session=browse.BrowseSession(api,'library-normal')
                reply=session.library()
                self.assertEqual(reply['path'],['Library'])
                self.assertEqual([row['title'] for row in reply['rows']],['Synthetic selection'])
                self.assertIs(session._cursor_connection,api._roonsocket)

    def test_socket_replacement_during_any_walk_io_discards_old_keys_without_rewalk(self):
        for phase in ('root_browse','root_load','child_browse','child_load'):
            with self.subTest(phase=phase):
                api=StatefulRoon(); old=api._roonsocket
                session=browse.BrowseSession(api,'library-socket')
                def swap():api._roonsocket=SimpleNamespace(connected=True)
                if phase.endswith('browse'):
                    api.after_dispatch=lambda opts: swap() if (opts.get('pop_all') if phase=='root_browse' else opts.get('item_key')=='library') else None
                else:
                    api.after_load=lambda current: swap() if current==('root' if phase=='root_load' else 'library') else None
                with self.assertRaises(browse.BrowseError) as caught:session.library()
                self.assertEqual(caught.exception.token,'stale')
                self.assertEqual(session.current()['rows'],[]);self.assertEqual(session.current()['path'],[])
                self.assertIsNone(session._cursor_connection)
                self.assertIsNot(api._roonsocket,old)
                self.assertEqual(sum(bool(c.get('pop_all')) for c in api.calls),1)
                self.assertEqual(sum(c.get('item_key')=='library' for c in api.calls),0 if phase.startswith('root') else 1)

    def test_socket_replacement_with_failed_io_clears_a_previously_loaded_view(self):
        for phase in ('root_browse','root_load','child_browse','child_load'):
            for failure in ('none','exception'):
                with self.subTest(phase=phase,failure=failure):
                    api=StatefulRoon();session=browse.BrowseSession(api,'library-failure')
                    previous=session.library();self.assertTrue(previous['rows'])
                    api.calls.clear();api.load_calls.clear()
                    original_browse=api.browse_browse;original_load=api.browse_load
                    def fail():
                        api._roonsocket=SimpleNamespace(connected=True)
                        if failure=='exception':raise RuntimeError('Synthetic socket failure after move')
                        return None
                    def browse_call(opts):
                        result=original_browse(opts)
                        if ((phase=='root_browse' and opts.get('pop_all')) or
                                (phase=='child_browse' and opts.get('item_key')=='library')):return fail()
                        return result
                    def load_call(opts):
                        result=original_load(opts)
                        if ((phase=='root_load' and api.current=='root') or
                                (phase=='child_load' and api.current=='library')):return fail()
                        return result
                    api.browse_browse=browse_call;api.browse_load=load_call
                    with self.assertRaises(browse.BrowseError) as caught:session.library()
                    self.assertEqual(caught.exception.token,'stale')
                    self.assertEqual(session.current()['rows'],[])
                    self.assertEqual(session.current()['path'],[])
                    self.assertGreater(session.level_id,previous['level_id'])
                    self.assertIsNone(session._cursor_connection)
                    self.assertEqual(sum(bool(c.get('pop_all')) for c in api.calls),1)
