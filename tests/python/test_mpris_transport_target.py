"""Offline MPRIS method/held-executor tests. No bus is connected."""
import asyncio
import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,os.path.abspath(os.path.join(os.path.dirname(__file__),'..','..','scripts')))
from tonearm_lib import mpris,art,core
from tests.python.test_player_targeted import session

METHODS=(('PlayPause','playpause'),('Play','play'),('Pause','pause'),
         ('Stop','pause'),('Next','next'),('Previous','previous'))

def payload(zid='zone-a'):
    return {'v':1,'status':'ok','zone':{'id':zid,'state':'paused','position':19,'length':87},
            'selection':{'mode':'pinned','zone_id':zid,'availability':'available'}}

async def call(player,method,*args):
    # dbus-next's public wrapper invokes the coroutine without awaiting it;
    # __wrapped__ is the same body its async bus dispatcher invokes.
    return await getattr(type(player),method).__wrapped__(player,*args)

class HeldExecutor:
    def __init__(self,loop):
        self.loop=loop;self.entered=asyncio.Event();self.jobs=[]
    def submit(self,executor,fn,*args):
        future=self.loop.create_future();self.jobs.append((fn,args,future));self.entered.set();return future
    def release(self):
        fn,args,future=self.jobs.pop(0)
        try:future.set_result(fn(*args))
        except Exception as exc:future.set_exception(exc)

class FakeSession:
    def __init__(self,result=None,exception=None):
        self.targeted=[];self.legacy=[];self.result=result or {'outcome':'attempted','error':None};self.exception=exception
    def player_targeted(self,zid,verb):
        self.targeted.append((zid,verb))
        if self.exception:raise self.exception
        return self.result
    def command(self,verb,arg=None):self.legacy.append((verb,arg))

class MprisTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Keep all generic cases off the real thread pool. HeldExecutor below
        # independently tests that command work waits beyond the handoff.
        loop=asyncio.get_running_loop()
        def immediate(executor,fn,*args):
            future=loop.create_future()
            try:future.set_result(fn(*args))
            except Exception as exc:future.set_exception(exc)
            return future
        patcher=patch.object(loop,'run_in_executor',side_effect=immediate)
        patcher.start();self.addCleanup(patcher.stop)

    async def test_all_methods_dispatch_once_to_captured_target_off_loop(self):
        for method,verb in METHODS:
            with self.subTest(method=method):
                s=FakeSession();p=mpris._Player(s);p.apply(payload());loop=asyncio.get_running_loop();held=HeldExecutor(loop)
                with patch.object(loop,'run_in_executor',side_effect=held.submit):
                    task=asyncio.create_task(call(p,method));await held.entered.wait()
                    self.assertFalse(task.done());self.assertEqual(s.targeted,[]);self.assertEqual(s.legacy,[])
                    p.apply(payload('zone-b'));held.release();self.assertIsNone(await task)
                self.assertEqual(s.targeted,[('zone-a',verb)]);self.assertEqual(s.legacy,[])

    async def test_repinned_real_core_refuses_original_target_after_handoff(self):
        s=session();p=mpris._Player(art.CachingSession(s,None));p.apply(s.snapshot())
        loop=asyncio.get_running_loop();held=HeldExecutor(loop)
        with patch.object(s,'command',side_effect=AssertionError('legacy fallback')),patch.object(loop,'run_in_executor',side_effect=held.submit):
            task=asyncio.create_task(call(p,'Play'));await held.entered.wait()
            s._arbiter.pin('zone-b');p.apply(s.snapshot());held.release();await task
        self.assertEqual(s._api.calls,[])

    async def test_missing_unknown_malformed_and_disconnected_identity_schedule_nothing(self):
        bad=[{},dict(payload(),zone=None),dict(payload(),status='unreachable'),
             dict(payload(),selection={'zone_id':'zone-a','availability':'unknown'}),
             dict(payload(),selection={'zone_id':'zone-a','availability':'unavailable'}),
             dict(payload(),selection={'zone_id':'zone-b','availability':'available'}),
             dict(payload(),selection=None),dict(payload(),zone={'id':''}),
             dict(payload(),zone={'id':True}),dict(payload(),zone={'id':'x'*129})]
        for value in bad:
            with self.subTest(value=value):
                s=FakeSession();p=mpris._Player(s);p.apply(value)
                loop=asyncio.get_running_loop()
                with patch.object(loop,'run_in_executor',side_effect=AssertionError('must not schedule')):
                    await call(p,'Play')
                self.assertEqual(s.targeted,[]);self.assertEqual(s.legacy,[])

    async def test_absent_session_or_target_api_never_uses_legacy(self):
        for s in (None,type('Legacy',(),{'command':lambda *a: self.fail('legacy fallback')})()):
            p=mpris._Player(s);p.apply(payload())
            with patch.object(asyncio.get_running_loop(),'run_in_executor',side_effect=AssertionError('must not schedule')):
                await call(p,'Pause')

    async def test_legacy_v1_zone_without_additive_selection_still_targets_explicitly(self):
        s=FakeSession();p=mpris._Player(s);v=payload();del v['selection'];p.apply(v)
        await call(p,'Pause');self.assertEqual(s.targeted,[('zone-a','pause')]);self.assertEqual(s.legacy,[])

    async def test_real_core_unknown_down_and_disappeared_after_handoff_refuse(self):
        for variant in ('unknown','down','missing'):
            with self.subTest(variant=variant):
                s=session();p=mpris._Player(art.CachingSession(s,None));p.apply(s.snapshot())
                loop=asyncio.get_running_loop();held=HeldExecutor(loop)
                with patch.object(s,'command',side_effect=AssertionError('legacy fallback')),patch.object(loop,'run_in_executor',side_effect=held.submit):
                    task=asyncio.create_task(call(p,'Next'));await held.entered.wait()
                    if variant=='unknown':s._api._zone_list_generation=0
                    elif variant=='down':s._status='unreachable'
                    else:del s._api.zones['zone-a']
                    held.release();await task
                self.assertEqual(s._api.calls,[])

    async def test_legacy_missing_pin_fallback_cannot_play_another_room(self):
        s=session();s._arbiter.strict_pins=False;del s._api.zones['zone-a'];v=s.snapshot()
        self.assertEqual(v['zone']['id'],'zone-b');self.assertEqual(v['selection']['availability'],'missing')
        p=mpris._Player(art.CachingSession(s,None));p.apply(v)
        with patch.object(s,'command',side_effect=AssertionError('legacy fallback')):
            await call(p,'PlayPause')
        self.assertEqual(s._api.calls,[])

    async def test_refused_unknown_or_exception_never_retries(self):
        for result,error in (({'outcome':'refused','error':'selection_changed'},None),
                             ({'outcome':'unknown','error':'dispatch_failed'},None),
                             (None,RuntimeError('synthetic failure'))):
            s=FakeSession(result,error);p=mpris._Player(s);p.apply(payload())
            if error:
                with self.assertRaises(RuntimeError):await call(p,'Previous')
            else:await call(p,'Previous')
            self.assertEqual(s.targeted,[('zone-a','previous')]);self.assertEqual(s.legacy,[])

    async def test_seek_and_set_position_keep_existing_legacy_behavior(self):
        s=FakeSession();p=mpris._Player(s);p.apply(payload())
        await call(p,'SetPosition','/synthetic/track',8_000_000)
        await call(p,'Seek',-4_000_000)
        self.assertEqual(s.legacy,[('seek',8),('seek',15)]);self.assertEqual(s.targeted,[])

    async def test_payload_and_existing_properties_not_mutated(self):
        s=FakeSession();p=mpris._Player(s);v=payload();before=copy.deepcopy(v);p.apply(v)
        values=(p.PlaybackStatus,p.CanPlay,p.CanPause,p.CanGoNext,p.CanGoPrevious,p.CanSeek)
        await call(p,'Pause')
        self.assertEqual(v,before);self.assertEqual(values,(p.PlaybackStatus,p.CanPlay,p.CanPause,p.CanGoNext,p.CanGoPrevious,p.CanSeek))

if __name__=='__main__':unittest.main()
