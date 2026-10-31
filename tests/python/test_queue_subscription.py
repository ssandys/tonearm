"""Owned queue subscription contract, entirely fake transport."""
import unittest
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from tests.python import test_queue_feasibility as f


class TestOwnedQueueSubscription(unittest.TestCase):
    def test_explicit_bound_status_and_cancel_handle(self):
        s=f.socket();seen=[];handle=f.api(s).subscribe_queue(lambda *x:seen.append(x),'fixture-a',100)
        self.assertTrue(handle.active)
        self.assertEqual(s._socket.sent[0][1],{'subscription_key':0,'zone_or_output_id':'fixture-a','max_item_count':100})
        f.frame(s,10,'Subscribed',{'items':[]})
        self.assertEqual(seen,[('Subscribed',{'items':[]})])
        handle.cancel();self.assertFalse(handle.active)
        self.assertEqual(s._socket.sent[-1][1],{'subscription_key':0})
        self.assertEqual(s._subscriptions,{})
        self.assertEqual(s._results,{})

    def test_bad_inputs_and_disconnect_never_send(self):
        for zone,limit in [('',100),('x'*129,100),('a',True),('a',0),('a',101)]:
            s=f.socket()
            with self.assertRaises(ValueError):f.api(s).subscribe_queue(Mock(),zone,limit)
            self.assertEqual(s._socket.sent,[])
        s=f.socket();s.connected=False
        with self.assertRaises(RuntimeError):f.api(s).subscribe_queue(Mock(),'a',100)
        self.assertEqual(s._subscriptions,{})

    def test_initial_response_during_send_is_not_lost(self):
        s=f.socket();seen=[];original=s._socket.send
        def immediate(data,opcode):
            original(data,opcode);f.frame(s,10,'Subscribed',{'items':[]})
        s._socket.send=immediate
        f.api(s).subscribe_queue(lambda *x:seen.append(x),'a',100)
        self.assertEqual(seen,[('Subscribed',{'items':[]})])
        self.assertEqual(s._results,{})

    def test_cancel_is_once_only_and_late_reply_dropped(self):
        s=f.socket();cb=Mock();handle=f.api(s).subscribe_queue(cb,'a',100)
        original=s._socket.send
        def while_cancelling(data,opcode):
            original(data,opcode);f.frame(s,10,'Changed',{'changes':[]})
        s._socket.send=while_cancelling
        handle.cancel();handle.cancel();f.frame(s,10,'Changed',{})
        cb.assert_not_called();self.assertEqual(len(s._socket.sent),2)
        self.assertEqual(s._subscriptions,{});self.assertEqual(s._results,{})

    def test_old_handle_cannot_cancel_new_binding_after_id_reuse(self):
        s=f.socket();old=f.api(s).subscribe_queue(Mock(),'a',100)
        s.on_close(None,None,None);s.connected=True
        new=f.api(s).subscribe_queue(Mock(),'b',100);count=len(s._socket.sent)
        old.cancel();self.assertTrue(new.active);self.assertFalse(old.active)
        self.assertEqual(len(s._socket.sent),count)
        new.cancel();self.assertFalse(new.active)

    def test_handle_stays_bound_to_original_socket_when_api_reconnects(self):
        old=f.socket();api=f.api(old);handle=api.subscribe_queue(Mock(),'a',100)
        new=f.socket();api._roonsocket=new;other=api.subscribe_queue(Mock(),'b',100)
        handle.cancel();self.assertTrue(other.active);self.assertEqual(len(new._socket.sent),1)

    def test_complete_is_delivered_once_and_retires_binding(self):
        s=f.socket();seen=[];handle=f.api(s).subscribe_queue(lambda *x:seen.append(x),'a',100)
        f.frame(s,10,'Unsubscribed',{})
        self.assertEqual(seen,[('Unsubscribed',{})]);self.assertFalse(handle.active)
        f.frame(s,10,'Changed',{});self.assertEqual(len(seen),1);self.assertEqual(s._results,{})

    def test_send_failure_does_not_leave_owned_bookkeeping(self):
        s=f.socket();s._socket.send=Mock(side_effect=RuntimeError('synthetic send failure'))
        with self.assertRaises(RuntimeError):f.api(s).subscribe_queue(Mock(),'a',100)
        self.assertEqual(s._subscriptions,{});self.assertEqual(s._results,{})

    def test_shared_request_allocation_remains_unique(self):
        s=f.socket()
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids=list(pool.map(lambda _:s.send_request('synthetic/read',{}),range(200)))
        self.assertEqual(len(set(ids)),200)

    def test_stop_still_closes_and_invalidates_all_handles_if_cancel_send_fails(self):
        s=f.socket();a=f.api(s);h=a.subscribe_queue(Mock(),'fixture-a',100)
        s._socket.send=Mock(side_effect=RuntimeError('synthetic failure'))
        s.stop();self.assertFalse(h.active);self.assertTrue(s._socket.closed)
        self.assertEqual(s._subscriptions,{});self.assertEqual(s._results,{})

    def test_malformed_owned_body_is_not_logged_verbatim(self):
        from unittest.mock import patch
        s=f.socket();f.api(s).subscribe_queue(Mock(),'fixture-a',100)
        message=b'MOO/1 CONTINUE Changed\nRequest-Id: 10\nContent-Type: application/json\n\n{"private_synthetic_title":broken}'
        with patch('roonapi.roonapisocket.LOGGER') as logger:
            s.on_message(None,message)
        self.assertEqual(logger.exception.call_args.args,('Error while parsing queue message',))
