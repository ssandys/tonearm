"""Candidate queue body contract is synthetic until an approved live probe."""
import unittest
from tests.python import fakes
from tonearm_lib import queue_state as q


def item(i,title=None):return {'queue_item_id':i,'three_line':{'line1':title or 'Synthetic '+str(i),'line2':'Fixture artist'}}
class Handle:
    def __init__(self,callback):self.callback=callback;self.active=True;self.cancels=0
    def cancel(self):self.cancels+=1;self.active=False
class Api:
    def __init__(self):self.bindings=[];self.immediate=None
    def subscribe_queue(self,cb,zid,limit):
        h=Handle(cb);self.bindings.append((zid,limit,h))
        if self.immediate:cb(*self.immediate)
        return h


class TestQueueState(unittest.TestCase):
    def setUp(self):
        self.api=Api();self.sock=object();self.context=q.Context('available',self.api,self.sock,'fixture-a',1)
        self.cache=q.QueueCache(lambda:self.context)
    def start(self):
        self.cache.observe(self.context);self.cache.drain();return self.api.bindings[-1][2]
    def read(self,zone='fixture-a'):return self.cache.snapshot(self.context,zone)
    def test_initial_callback_during_bind_and_detached_snapshot(self):
        self.api.immediate=('Subscribed',{'items':[item(7)]});self.start()
        self.assertEqual(self.api.bindings[0][:2],('fixture-a',100))
        a=self.read();self.assertEqual(a['status'],'ready');self.assertEqual(a['items'][0]['title'],'Synthetic 7')
        a['items'][0]['title']='mutated';self.assertEqual(self.read()['items'][0]['title'],'Synthetic 7')
    def test_read_never_subscribes_or_retargets(self):
        self.assertEqual(self.read()['items'],[]);self.assertEqual(self.api.bindings,[])
        self.start();before=len(self.api.bindings);self.read();self.read('fixture-b')
        self.assertEqual(len(self.api.bindings),before);self.assertEqual(self.read('fixture-b')['items'],[])
    def test_full_empty_and_insert_remove_order(self):
        h=self.start();h.callback('Subscribed',{'items':[item(1),item(2),item(3)]})
        h.callback('Changed',{'changes':[{'operation':'remove','index':1,'count':1},{'operation':'insert','index':1,'items':[item(9)]}]})
        self.assertEqual([r['id'] for r in self.read()['items']],[1,9,3])
        h.callback('Subscribed',{'items':[]});self.assertEqual(self.read()['status'],'ready');self.assertEqual(self.read()['items'],[])
    def test_unknown_update_and_delta_before_snapshot_fail_closed(self):
        h=self.start();h.callback('Changed',{'changes':[]});self.assertEqual(self.read()['status'],'error')
        h.callback('Subscribed',{'items':[item(1)]});h.callback('Changed',{'changes':[{'operation':'update','index':0,'items':[item(1,'new')]}]})
        self.assertEqual((self.read()['status'],self.read()['items']),('error',[]))
        self.cache.drain();self.assertEqual(len(self.api.bindings),1)
    def test_malformed_and_out_of_range_delta_are_atomic(self):
        h=self.start();h.callback('Subscribed',{'items':[item(1)]})
        h.callback('Changed',{'changes':[{'operation':'insert','index':1,'items':[item(2)]},{'operation':'remove','index':99,'count':1}]})
        self.assertEqual(self.read()['items'],[]);self.assertEqual(self.read()['status'],'error')
    def test_item_and_encoded_byte_limits(self):
        h=self.start();h.callback('Subscribed',{'items':[item(i) for i in range(101)]})
        self.assertEqual(self.read()['status'],'error');self.assertEqual(self.read()['items'],[])
        h.callback('Subscribed',{'items':[item(i,'\U0001f642'*500) for i in range(100)]})
        self.assertEqual(self.read()['status'],'error');self.assertEqual(self.read()['items'],[])
    def test_duplicate_ids_and_bad_items_refuse(self):
        h=self.start()
        for rows in ([item(1),item(1)],[{'queue_item_id':True,'three_line':{'line1':'bad'}}],[{'queue_item_id':1}]):
            h.callback('Subscribed',{'items':rows});self.assertEqual(self.read()['status'],'error')
    def test_selection_change_invalidates_before_cancel_and_discards_old_events(self):
        h=self.start();h.callback('Subscribed',{'items':[item(1)]})
        self.context=q.Context('available',self.api,self.sock,'fixture-b',1);self.cache.observe(self.context)
        self.assertEqual(h.cancels,0);h.callback('Subscribed',{'items':[item(99)]})
        self.assertEqual(self.read('fixture-b')['items'],[])
        self.cache.drain();self.assertEqual(h.cancels,1);self.assertEqual(len(self.api.bindings),2)
    def test_same_api_new_socket_requires_new_full_zone_generation(self):
        h=self.start();self.context=q.Context('available',self.api,object(),'fixture-a',1)
        self.cache.observe(self.context);self.cache.drain();self.assertEqual(self.read()['status'],'unknown');self.assertEqual(len(self.api.bindings),1)
        self.context=q.Context('available',self.api,self.context.socket,'fixture-a',2)
        self.cache.observe(self.context);self.cache.drain();self.assertEqual(len(self.api.bindings),2);self.assertEqual(self.read()['status'],'loading')
    def test_api_replacement_and_same_zone_old_callback_do_not_leak(self):
        h=self.start();new=Api();self.context=q.Context('available',new,object(),'fixture-a',1)
        self.cache.observe(self.context);self.cache.drain();h.callback('Subscribed',{'items':[item(99)]})
        self.assertEqual(self.read()['items'],[]);self.assertEqual(len(new.bindings),1)
    def test_unavailable_unknown_disconnected_never_subscribe(self):
        for status in ('unavailable','unknown','disconnected'):
            self.context=q.Context(status,self.api,self.sock,'fixture-a',0)
            self.cache.observe(self.context);self.cache.drain();self.assertEqual(self.read()['status'],status)
        self.assertEqual(self.api.bindings,[])
    def test_stop_cancels_once_and_cannot_restart(self):
        h=self.start();self.cache.stop();self.cache.stop();self.cache.observe(self.context);self.cache.drain()
        h.callback('Subscribed',{'items':[item(99)]})
        self.assertEqual(h.cancels,1);self.assertEqual(self.read()['status'],'disconnected');self.assertEqual(len(self.api.bindings),1)
    def test_changed_live_context_rejects_event_before_observe(self):
        h=self.start();self.context=q.Context('unavailable',self.api,self.sock,'fixture-a',1)
        h.callback('Subscribed',{'items':[item(99)]});self.assertEqual(self.read()['items'],[])
    def test_completion_and_unknown_schema_error_not_empty_success(self):
        h=self.start();h.callback('Subscribed',{});self.assertEqual(self.read()['status'],'error')
        h.callback('Subscribed',{'items':[]});h.callback('Unsubscribed',{})
        self.assertEqual(self.read()['status'],'error')

    def test_nested_schema_snapshot_is_detached_and_hidden_for_other_selection(self):
        h=self.start();h.callback('Subscribed',{'items':[item(1)]})
        value=self.read();value['schema']['fields']['items']='mutated'
        self.assertEqual(self.read()['schema']['fields']['items'],'list')
        self.assertEqual(self.read('fixture-b')['schema'],{})
    def test_stop_during_bind_cancels_returned_handle(self):
        original=self.api.subscribe_queue
        def stop_during_bind(*args):
            h=original(*args);self.cache.stop();return h
        self.api.subscribe_queue=stop_during_bind
        self.start();h=self.api.bindings[0][2]
        self.assertEqual(h.cancels,1);self.assertEqual(self.read()['status'],'disconnected')
    def test_selection_change_during_bind_cancels_stale_return_then_binds_latest(self):
        original=self.api.subscribe_queue
        def change_during_bind(*args):
            h=original(*args)
            self.context=q.Context('available',self.api,self.sock,'fixture-b',1)
            self.cache.observe(self.context)
            return h
        self.api.subscribe_queue=change_during_bind
        self.start();self.assertEqual(self.api.bindings[0][2].cancels,1)
        self.api.subscribe_queue=original;self.cache.drain()
        self.assertEqual(self.api.bindings[-1][0],'fixture-b')
    def test_observed_same_socket_disconnect_requires_fresh_zones(self):
        self.start();self.context=q.Context('disconnected',self.api,self.sock,'fixture-a',1)
        self.cache.observe(self.context);self.cache.drain()
        self.context=q.Context('available',self.api,self.sock,'fixture-a',1)
        self.cache.observe(self.context);self.cache.drain()
        self.assertEqual(self.read()['status'],'unknown');self.assertEqual(len(self.api.bindings),1)
