"""Offline queue feasibility characterization with 07a corrections; original unchanged-vendor proof is at 2dcf163.

Construct via __new__, never initialize a websocket, SDK, Core or service.
Synthetic bodies deliberately do not assert a live Roon event schema.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from tests.python import fakes
from tonearm_lib import core
from roonapi import roonapi
from roonapi.roonapisocket import RoonApiWebSocket
from roonapi.constants import SERVICE_TRANSPORT

FIXTURES=json.loads((Path(__file__).parents[1]/'fixtures/queue/events.json').read_text())


class Transport:
    def __init__(self):self.sent=[];self.closed=False
    def send(self, data, opcode):
        header,body=data.decode().split('\n\n',1)
        self.sent.append((header.splitlines()[0],json.loads(body) if body else None))
    def close(self):self.closed=True


def socket():
    s=RoonApiWebSocket.__new__(RoonApiWebSocket)
    s._subscriptions={};s._results={};s._requestid=10;s._subkey=0
    s._exit=False;s.connected=True;s.failed_state=False;s._socket=Transport()
    return s


def frame(s,rid,name,body):
    encoded=json.dumps(body).encode()
    kind='COMPLETE' if name in ('Unsubscribed','Error') else 'CONTINUE'
    s.on_message(None,(f'MOO/1 {kind} {name}\nRequest-Id: {rid}\nContent-Type: application/json\nContent-Length: {len(encoded)}\n\n'.encode()+encoded))


def api(s):
    a=roonapi.RoonApi.__new__(roonapi.RoonApi);a._roonsocket=s;return a


class TestExistingQueueHook(unittest.TestCase):
    def test_existing_wrapper_targets_zone_but_has_no_bound_or_handle(self):
        s=socket();cb=Mock();handle=api(s).register_queue_callback(cb,'fixture-a')
        self.assertIsNone(handle)
        endpoint,body=s._socket.sent[0]
        self.assertEqual(endpoint,'MOO/1 REQUEST '+SERVICE_TRANSPORT+'/subscribe_queue')
        self.assertEqual(body,{'subscription_key':0,'zone_or_output_id':'fixture-a'})
        self.assertNotIn('max_item_count',body)
        self.assertEqual(len(s._subscriptions),1)

    def test_legacy_omitted_target_is_possible_but_not_suitable_for_selected_queue(self):
        s=socket();api(s).register_queue_callback(Mock())
        self.assertNotIn('zone_or_output_id',s._socket.sent[0][1])

    def test_low_level_can_send_explicit_target_and_server_side_bound(self):
        s=socket();handle=s.subscribe(SERVICE_TRANSPORT,'queue',Mock(),{'zone_or_output_id':'fixture-a','max_item_count':100})
        self.assertIsNone(handle)
        self.assertEqual(s._socket.sent[0][1],{'subscription_key':0,'zone_or_output_id':'fixture-a','max_item_count':100})

    def test_full_incremental_update_empty_and_completion_bodies_arrive_in_order(self):
        s=socket();seen=[];api(s).register_queue_callback(seen.append,'fixture-a')
        for event in FIXTURES['frames']:frame(s,10,event['name'],event['body'])
        self.assertEqual(seen,[event['body'] for event in FIXTURES['frames']])
        # This proves delivery, NOT insert/remove/update semantics or reconstruction.
        self.assertEqual(seen[1]['changes'][0]['operation'],'remove')
        self.assertEqual(seen[1]['changes'][1]['operation'],'insert')
        self.assertEqual(seen[2]['changes'][0]['operation'],'update')

    def test_status_name_is_lost_and_empty_is_not_subscription_confirmation(self):
        s=socket();seen=[];api(s).register_queue_callback(seen.append,'fixture-a')
        for name in ('Subscribed','Changed','Unsubscribed','Error'):frame(s,10,name,{})
        self.assertEqual(seen,[{},{},{},{}])
        self.assertIn(10,s._subscriptions)  # receipt doesn't retire local entry

    def test_legacy_callback_does_not_enforce_retained_or_incoming_item_bound(self):
        s=socket();seen=[];api(s).register_queue_callback(seen.append,'fixture-a')
        body={'items':[{'queue_item_id':i,'title':'Synthetic row'} for i in range(137)]}
        frame(s,10,'Subscribed',body)
        self.assertEqual(len(seen[0]['items']),137)

    def test_endpoint_unsubscribe_removes_queue_but_preserves_zone_subscription(self):
        s=socket();q=Mock();zones=Mock();s.subscribe(SERVICE_TRANSPORT,'zones',zones)
        api(s).register_queue_callback(q,'fixture-a');s.unsubscribe(SERVICE_TRANSPORT,'queue')
        self.assertEqual(list(s._subscriptions),[10])
        self.assertEqual(s._socket.sent[-1],('MOO/1 REQUEST '+SERVICE_TRANSPORT+'/unsubscribe_queue',{'subscription_key':1}))
        s.unsubscribe(SERVICE_TRANSPORT,'queue');self.assertEqual(len(s._socket.sent),3)

    def test_endpoint_unsubscribe_cancels_all_queue_consumers_not_one_owner(self):
        s=socket();api(s).register_queue_callback(Mock(),'fixture-a');api(s).register_queue_callback(Mock(),'fixture-b')
        s.unsubscribe(SERVICE_TRANSPORT,'queue')
        self.assertEqual(s._subscriptions,{})
        self.assertEqual([v[1]['subscription_key'] for v in s._socket.sent[2:]],[0,1])

    def test_late_reply_after_unsubscribe_does_not_callback_or_enter_results(self):
        s=socket();cb=Mock();api(s).register_queue_callback(cb,'fixture-a');s.unsubscribe(SERVICE_TRANSPORT,'queue')
        frame(s,10,'Changed',{'items':[]})
        cb.assert_not_called();self.assertNotIn(10,s._results)
        # 07a narrowly drops unknown/late replies; the larger issue30 remains.

    def test_stop_unsubscribes_before_closing_socket(self):
        s=socket();api(s).register_queue_callback(Mock(),'fixture-a');s.stop()
        self.assertTrue(s._socket.closed)
        self.assertEqual(len(s._socket.sent),2)
        self.assertEqual(s._subscriptions,{})

    def test_close_resets_ids_so_old_endpoint_cancellation_can_cancel_rebound_queue(self):
        s=socket();old=Mock();new=Mock();api(s).register_queue_callback(old,'fixture-a')
        s.on_close(None,None,None);self.assertEqual(s._subscriptions,{})
        s.connected=True;api(s).register_queue_callback(new,'fixture-b')
        self.assertIn(10,s._subscriptions)
        s.unsubscribe(SERVICE_TRANSPORT,'queue')
        self.assertEqual(s._subscriptions,{})  # no binding identity in endpoint API
        old.assert_not_called();new.assert_not_called()

    def test_disconnected_subscribe_does_not_register_false_request_id(self):
        s=socket();s.connected=False;api(s).register_queue_callback(Mock(),'fixture-a')
        self.assertEqual(s._socket.sent,[])
        self.assertEqual(s._subscriptions,{})

    def test_initial_reply_before_subscribe_returns_reaches_callback(self):
        s=socket();cb=Mock();original=s._socket.send
        def reply_during_send(data,opcode):
            original(data,opcode)
            frame(s,10,'Subscribed',{'items':[]})
        s._socket.send=reply_during_send
        api(s).register_queue_callback(cb,'fixture-a')
        cb.assert_called_once_with({'items':[]})
        self.assertNotIn(10,s._results)
        self.assertIn(10,s._subscriptions)

    def test_unsubscribe_invalidates_before_send(self):
        s=socket();cb=Mock();api(s).register_queue_callback(cb,'fixture-a')
        original=s._socket.send
        def reply_during_send(data,opcode):
            original(data,opcode)
            frame(s,10,'Changed',{'changes':[]})
        s._socket.send=reply_during_send
        s.unsubscribe(SERVICE_TRANSPORT,'queue')
        cb.assert_not_called()
        self.assertEqual(s._subscriptions,{})
        # Invalidate the owner binding before cancellation; do not trust removal timing.


class TestExistingSelectionAuthority(unittest.TestCase):
    def session(self):
        with patch.object(core.config,'load',return_value={'pinned_zone_id':'fixture-a','strict_pins':True}):
            session=core.RoonSession(lambda *args:None)
        a=Mock();a.zones={z:{'zone_id':z,'display_name':z,'state':'paused'} for z in ('fixture-a','fixture-b')}
        a._zone_list_generation=1;session._api=a;session._status='ok';return session

    def test_connected_before_full_zones_is_unknown(self):
        s=self.session();s._api._zone_list_generation=0
        snap=s.snapshot();self.assertEqual(snap['selection']['availability'],'unknown');self.assertIsNone(snap['zone'])

    def test_missing_pin_does_not_return_another_rooms_identity(self):
        s=self.session();del s._api.zones['fixture-a']
        snap=s.snapshot();self.assertEqual(snap['selection']['availability'],'missing');self.assertIsNone(snap['zone'])

    def test_selection_and_core_replacement_provide_a_guardable_binding(self):
        s=self.session();original=s._api;generation=object();active=generation;accepted=[]
        def callback(body):
            # Minimal test-only guard sketch, not a shipped subscription owner.
            snap=s.snapshot()
            if active is generation and s._api is original and snap['selection']['availability']=='available' and snap['zone']['id']=='fixture-a':accepted.append(body)
        callback({'items':[]});s._arbiter.pin('fixture-b');callback({'items':['old-zone']})
        s._arbiter.pin('fixture-a');s._api=Mock();s._api.zones=original.zones;s._api._zone_list_generation=1;callback({'items':['old-core']})
        s._api=original;active=object();callback({'items':['old-binding']})
        self.assertEqual(accepted,[{'items':[]}])
