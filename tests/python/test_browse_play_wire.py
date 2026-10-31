"""Focused opt-in browse wire contract; no real Roon."""
import unittest
from tests.python.fakes import FakeRoon
from tests.python.test_server_browse import roundtrip
from tonearm_lib import cli


def request():
    return dict(cmd='browse', op='play_selected', session='fixture', index=2,
                level_id=17, zone_id='fixture-zone', request_id='fixture-request',
                action='play_now')


class Session:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.corrupt = False
        self.force_action = None
    def browse_play_selected(self, key, index, level_id, zone_id, request_id, action):
        self.calls.append((key, index, level_id, zone_id, request_id, action))
        if self.fail: raise RuntimeError('synthetic failure')
        return dict(v=1, ok=True, request_id='wrong' if self.corrupt else request_id,
                    zone_id=zone_id, action=self.force_action or action,
                    outcome='attempted', error=None,
                    level_id=18, rows=[], count=0, offset=0, path=[])
    def browse(self, *args, **kwargs):
        raise AssertionError('must not use legacy browse route')


class TestBrowsePlayWire(unittest.TestCase):
    def test_cli_exact_arguments_and_bounds(self):
        args=['browse','--session','fixture','play_selected','2','17','fixture-zone','fixture-request','play_now']
        self.assertEqual(cli.to_request(args),request())
        for changes in (args+['extra'],args[:-1],args[:4]+['-1']+args[5:],
                        args[:-1]+['x'*65],args[:2]+['x'*65]+args[3:],
                        args[:-1]+['Queue'],args[:-1]+['Start Radio'],args[:-1]+['start_radio']):
            with self.subTest(args=changes), self.assertRaises(ValueError): cli.to_request(changes)

    def test_server_routes_once_and_preserves_correlation(self):
        s=Session();reply=roundtrip(s,request())
        self.assertEqual(s.calls,[('fixture',2,17,'fixture-zone','fixture-request','play_now')])
        self.assertEqual((reply['request_id'],reply['zone_id'],reply['action'],reply['outcome']),
                         ('fixture-request','fixture-zone','play_now','attempted'))
        self.assertNotIn('played',reply)

    def test_malformed_requests_never_call_session(self):
        for field,value in [('index',True),('level_id',-1),('zone_id',''),('request_id','x'*65),
                            ('session',''),('action','Queue'),('action','Start Radio'),
                            ('action','start_radio'),('action',''),('extra',42)]:
            req=request();req[field]=value;s=Session()
            with self.subTest(field=field):
                reply=roundtrip(s,req)
                self.assertEqual(reply['outcome'],'refused')
                self.assertEqual(reply['error'],'bad_request')
                self.assertEqual(s.calls,[])
        missing=request(); missing.pop('action'); s=Session(); reply=roundtrip(s,missing)
        self.assertEqual((reply['outcome'],reply['error'],reply['action']),( 'refused','bad_request',None))
        self.assertEqual(s.calls,[])

    def test_unsupported_exception_and_mismatched_result_never_fall_back(self):
        for variant in ('unsupported','exception','corrupt'):
            s=Session()
            if variant=='unsupported':s.browse_play_selected=None
            if variant=='exception':s.fail=True
            if variant=='corrupt':s.corrupt=True
            reply=roundtrip(s,request())
            self.assertEqual(reply['outcome'],'refused' if variant=='unsupported' else 'unknown')
            self.assertEqual(reply['request_id'],'fixture-request')
            self.assertEqual(len(s.calls),0 if variant=='unsupported' else 1)

    def test_a_different_returned_action_is_unknown_and_not_replayed(self):
        s=Session(); s.force_action='play_now'
        req=request(); req['action']='add_to_queue'
        reply=roundtrip(s,req)
        self.assertEqual((reply['outcome'],reply['action'],reply['error']),
                         ('unknown','add_to_queue','internal_error'))
        self.assertEqual(s.calls,[('fixture',2,17,'fixture-zone','fixture-request','add_to_queue')])
        self.assertNotIn('played',reply)

    def test_cli_through_real_server_core_and_synthetic_sdk(self):
        from tests.python import test_core_browse_selected as fixtures
        fixture=fixtures.TestCoreSelectedBrowse();fixture.setUp()
        args=['browse','--session','fixture','play_selected','0',str(fixture.fixture.page['level_id']),
              'fixture-a','integration-request','play_now']
        from tonearm_lib import art
        wrapped=art.CachingSession(fixture.core,None)
        reply=roundtrip(wrapped,cli.to_request(args))
        self.assertEqual((reply['outcome'],reply['request_id']),('attempted','integration-request'))
        self.assertEqual(len(fixture.fixture.actions()),1)
        self.assertEqual(reply['rows'],[])
