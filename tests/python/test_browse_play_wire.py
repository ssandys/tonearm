"""Focused opt-in browse wire contract; no real Roon."""
import unittest
from tests.python.test_server_browse import roundtrip
from tonearm_lib import cli, browse_play


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
    def browse(self, key, op, **kwargs):
        if op != 'play_selected': raise AssertionError('must not use legacy browse route')
        if not callable(self.browse_play_selected): raise RuntimeError('unsupported')
        return self.browse_play_selected(key, **kwargs)


class TestBrowsePlayWire(unittest.TestCase):
    def test_synthetic_result_requires_every_mandatory_field(self):
        req=request()
        good=browse_play.result(req,'attempted',None)
        for field in ('v','ok','request_id','zone_id','action','outcome','error'):
            bad=dict(good);del bad[field]
            with self.subTest(missing=field):
                self.assertFalse(browse_play.valid_result(bad,req))

    def test_synthetic_result_requires_strict_consistent_types(self):
        req=request();good=browse_play.result(req,'attempted',None)
        for field,value in [('v',True),('v','1'),('v',1.0),('ok',False),('ok',1),('ok','true'),
                            ('request_id',None),('zone_id',[]),('action',{}),('outcome',True),
                            ('error',''),('error',False)]:
            with self.subTest(field=field,value=value):
                self.assertFalse(browse_play.valid_result(dict(good,**{field:value}),req))
        for bad in (None,[],True,'result',42):
            with self.subTest(reply=bad):self.assertFalse(browse_play.valid_result(bad,req))
        for outcome in ('refused','unknown'):
            good=browse_play.result(req,outcome,'no_action')
            for field,value in [('ok',True),('ok',0),('ok',None),('error',None),('error',''),
                                ('error','x'*65),('error',{}),('outcome',[]),('outcome','success')]:
                with self.subTest(outcome=outcome,field=field,value=value):
                    self.assertFalse(browse_play.valid_result(dict(good,**{field:value}),req))

    def test_synthetic_valid_outcomes_preserve_optional_current_page_fields(self):
        req=request()
        for outcome,error in [('attempted',None),('refused','no_action'),('unknown','roon_error')]:
            reply=dict(browse_play.result(req,outcome,error),rows=[],path=['Library'],level_id=18,count=0,offset=0)
            with self.subTest(outcome=outcome):
                self.assertTrue(browse_play.valid_result(reply,req))
                s=Session();s.browse_play_selected=lambda *args,**kwargs: s.calls.append(args) or reply
                self.assertEqual(roundtrip(s,req),reply)
                self.assertEqual(len(s.calls),1)

    def test_synthetic_malformed_returned_result_becomes_unknown_after_one_dispatch(self):
        req=request();good=browse_play.result(req,'attempted',None)
        missing_error=dict(good);del missing_error['error']
        missing_ok=dict(good);del missing_ok['ok']
        for bad in (missing_error,missing_ok,dict(good,ok=False),dict(good,ok=1)):
            with self.subTest(result=bad):
                s=Session();s.browse_play_selected=lambda *args,**kwargs: s.calls.append(args) or bad
                reply=roundtrip(s,req)
                self.assertEqual(reply,browse_play.result(req,'unknown','internal_error'))
                self.assertEqual(len(s.calls),1)

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
        for field,value in [('index',True),('index',100),('level_id',-1),('level_id',True),
                            ('level_id',9007199254740992),('zone_id',''),('request_id','x'*65),
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
            self.assertEqual(reply['outcome'],'unknown')
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
