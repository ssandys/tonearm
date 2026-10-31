"""Normal-player opt-in admission and IPC, with synthetic API only."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.python.test_reconnect_readiness import RecordingApi
from tonearm_lib import art, cli, core, server, targeted


def request(verb='pause', arg=None, zid='zone-a'):
    return dict(cmd='player_targeted', v=1, request_id='player-test', zone_id=zid,
                verb=verb, arg=arg, output_id='output-1' if verb=='volume' else None,
                output_ids=['output-1'] if verb in ('mute','unmute') else None)


def session():
    with patch.object(core.config,'load',return_value={'pinned_zone_id':'zone-a','strict_pins':True}):
        s=core.RoonSession(lambda _:None)
    s._status='ok';s._api=RecordingApi(generation=1)
    z=s._api.zones['zone-a'];z['now_playing']={'length':240,'three_line':{'line1':'Synthetic track'}}
    z['outputs'][0]['volume']['is_muted']=False
    return s


def invoke(s, req):
    return s.player_targeted(req['zone_id'],req['verb'],req['arg'],req['output_id'],req['output_ids'])


class PlayerTest(unittest.TestCase):
    def test_controls_snapshot_is_additive_and_not_presentation_default_authority(self):
        s=session();z=s.snapshot()['zone'];c=z['controls']
        self.assertTrue(c['transport']);self.assertTrue(c['seek']);self.assertTrue(c['volume'])
        self.assertEqual(c['volume_output_id'],'output-1');self.assertEqual(c['mute_output_ids'],['output-1'])
        self.assertEqual(z['now_playing']['title'],'Synthetic track')
        s._api.zones['zone-a']['outputs'][0]['volume']={'type':'number','value':20}
        self.assertFalse(s.snapshot()['zone']['controls']['volume'])
        self.assertEqual(invoke(s,request('volume',20))['error'],'capability_unavailable')
        self.assertEqual(s._api.calls,[])

    def test_each_normal_control_binds_expected_target_and_never_uses_legacy(self):
        for verb,arg,expected in [('play',None,('playback','zone-a','play')),
             ('pause',None,('playback','zone-a','pause')),('playpause',None,('playback','zone-a','playpause')),
             ('next',None,('playback','zone-a','next')),('previous',None,('playback','zone-a','previous')),
             ('seek',42,('seek','zone-a',42,'absolute')),('volume',25,('volume','output-1',25,'absolute')),
             ('mute',None,('mute','output-1',True)),('unmute',None,('mute','output-1',False))]:
            with self.subTest(verb=verb):
                s=session()
                with patch.object(s,'command',side_effect=AssertionError('legacy route')):
                    self.assertEqual(invoke(s,request(verb,arg)),{'outcome':'attempted','error':None})
                self.assertEqual(s._api.calls,[expected])

    def test_unavailable_unknown_disconnect_and_mismatched_selection_never_dispatch(self):
        for variant,code in [('missing','unavailable'),('unknown','zones_unknown'),('down','disconnected'),('changed','selection_changed')]:
            s=session()
            if variant=='missing':del s._api.zones['zone-a']
            if variant=='unknown':s._api._zone_list_generation=0
            if variant=='down':s._status='unreachable'
            if variant=='changed':s._arbiter.pin('zone-b')
            self.assertEqual(invoke(s,request())['error'],code)
            self.assertEqual(s._api.calls,[])

    def test_seek_permission_length_and_range(self):
        for change,arg,code in [({'is_seek_allowed':False},42,'capability_unavailable'),
             ({'now_playing':{'length':0}},0,'capability_unavailable'),
             ({'now_playing':{'length':float('nan')}},1,'capability_unavailable'),
             ({},-1,'out_of_range'),({},241,'out_of_range')]:
            s=session();s._api.zones['zone-a'].update(change)
            self.assertEqual(invoke(s,request('seek',arg))['error'],code)
            self.assertEqual(s._api.calls,[])

    def test_volume_and_group_output_changes_fail_closed(self):
        for variant,code in [('fixed','capability_unavailable'),('incremental','capability_unavailable'),
                             ('bad','capability_unavailable'),('different','output_changed'),('range','out_of_range')]:
            s=session();out=s._api.zones['zone-a']['outputs'][0];req=request('volume',25)
            if variant in ('fixed','incremental'):out['volume']['type']=variant
            if variant=='bad':out['volume']['max']=True
            if variant=='different':out['output_id']='changed-output'
            if variant=='range':req['arg']=101
            self.assertEqual(invoke(s,req)['error'],code)
            self.assertEqual(s._api.calls,[])

    def test_incremental_mute_does_not_grant_absolute_volume(self):
        s=session();raw=s._api.zones['zone-a'];raw['outputs'][0]['volume']={'type':'incremental','is_muted':True}
        z=s.snapshot()['zone'];self.assertIsNone(z['volume'])
        self.assertFalse(z['controls']['volume']);self.assertTrue(z['controls']['muted'])
        self.assertEqual(z['controls']['mute_output_ids'],['output-1'])
        self.assertEqual(invoke(s,request('unmute'))['outcome'],'attempted')
        self.assertEqual(s._api.calls,[('mute','output-1',False)])

    def test_group_mute_requires_the_displayed_complete_output_set(self):
        s=session();s._api.zones['zone-a']['outputs'].append({'output_id':'output-extra','volume':{'type':'number','value':1,'min':0,'max':100,'is_muted':False}})
        self.assertEqual(invoke(s,request('mute'))['error'],'output_changed')
        self.assertEqual(s._api.calls,[])
        req=request('mute');req['output_ids']=['output-1','output-extra']
        self.assertEqual(invoke(s,req)['outcome'],'attempted')
        self.assertEqual(s._api.calls,[('mute','output-1',True),('mute','output-extra',True)])

    def test_partial_group_failure_is_unknown_not_retried(self):
        s=session();s._api.zones['zone-a']['outputs'].append({'output_id':'extra','volume':{'type':'number','value':1,'min':0,'max':100,'is_muted':False}})
        def mute(output,flag):
            s._api.calls.append((output,flag))
            if output=='extra':raise RuntimeError('private SDK details')
        s._api.mute=mute;req=request('mute');req['output_ids']=['output-1','extra']
        self.assertEqual(invoke(s,req),{'outcome':'unknown','error':'dispatch_failed'})
        self.assertEqual(s._api.calls,[('output-1',True),('extra',True)])

    def test_malformed_player_payloads_rejected_before_core(self):
        for patcher in ({'verb':'transfer'},{'arg':True},{'arg':1.5},{'arg':float('nan')},{'output_id':'unexpected'},{'output_ids':['x']}):
            self.assertFalse(targeted.valid_player_request(dict(request(),**patcher)))
        for arg in (None,True,1.5,'2'):
            self.assertFalse(targeted.valid_player_request(request('seek',arg)))
        self.assertFalse(targeted.valid_player_request(dict(request('mute'),output_ids=['x','x'])))
        self.assertFalse(targeted.valid_player_request(dict(request('mute'),output_ids=['x']*33)))
        s=session();self.assertEqual(s.player_targeted('zone-a','transfer')['error'],'bad_request')
        self.assertEqual(s._api.calls,[])

    def test_snapshot_to_actual_model_request_to_backend_is_compatible(self):
        s=session()
        script="const M=require('./Model.js'); const fs=require('node:fs'); const st=JSON.parse(fs.readFileSync(0,'utf8')); console.log(JSON.stringify(M.playerRequest(st,'volume',25,'cross-layer')));"
        proc=subprocess.run(['node','-e',script],input=json.dumps(s.snapshot()),capture_output=True,text=True,
                            cwd=Path(__file__).resolve().parents[2],timeout=5)
        self.assertEqual(proc.returncode,0,proc.stderr)
        req=json.loads(proc.stdout)
        self.assertTrue(targeted.valid_player_request(req))
        self.assertEqual(invoke(s,req)['outcome'],'attempted')
        self.assertEqual(s._api.calls,[('volume','output-1',25,'absolute')])

    def test_external_metadata_seek_and_volume_changes_reach_same_snapshot_without_commands(self):
        s=session();raw=s._api.zones['zone-a'];raw['seek_position']=73
        raw['now_playing']['three_line']['line1']='Changed elsewhere'
        raw['outputs'][0]['volume']['value']=15
        z=s.snapshot()['zone'];self.assertEqual((z['position'],z['now_playing']['title'],z['volume']['value']),(73,'Changed elsewhere',15))
        self.assertEqual(s._api.calls,[])


class PlayerWireTest(unittest.TestCase):
    def test_actual_wrapper_server_cli_and_client_keep_correlated_target(self):
        from tonearm_lib import contract_client
        s=session()
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'XDG_RUNTIME_DIR':tmp}):
            srv=server.Server(art.CachingSession(s,art.Cache(tmp)));worker=threading.Thread(target=srv.serve_forever);worker.start()
            try:
                self.assertTrue(srv.ready.wait(3))
                req=request('seek',24)
                self.assertEqual(cli.to_request(['player',json.dumps(req)]),req)
                proc=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/tonearmctl'),'player',json.dumps(req)],capture_output=True,text=True,timeout=8)
                self.assertEqual(proc.returncode,0,proc.stderr)
                reply=json.loads(proc.stdout);self.assertEqual(reply['request_id'],req['request_id']);self.assertEqual(reply['verb'],'seek')
                self.assertEqual(reply['outcome'],'attempted');self.assertEqual(s._api.calls,[('seek','zone-a',24,'absolute')])
                s._arbiter.pin('zone-b')
                reply=contract_client.player(server.socket_path(),request('pause'))
                self.assertEqual(reply['error'],'selection_changed');self.assertEqual(len(s._api.calls),1)
                bad=dict(request(),arg=True)
                reply=contract_client.exchange(server.socket_path(),bad)
                self.assertEqual(reply['error'],'bad_request');self.assertEqual(len(s._api.calls),1)
            finally:
                srv.shutdown();worker.join(3);self.assertFalse(worker.is_alive())
