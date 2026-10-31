"""Read-only queue integration: synthetic sources, never a live Core."""
import json
import unittest
from unittest.mock import patch
from tests.python import fakes
from tonearm_lib import core, cli, queue_wire
from tests.python.test_queue_state import Api, item


class QueueRouteTests(unittest.TestCase):
    def setUp(self):
        with patch.object(core.config, 'load', return_value={}):
            self.session = core.RoonSession(lambda value: None)
        self.api = Api()
        self.api._roonsocket = type('Socket', (), {'connected': True})()
        self.api._zone_list_generation = 1
        self.session._api = self.api
        self.payload = {'status':'ok','zone':{'id':'fixture-a'},
                        'selection':{'availability':'available','zone_id':'fixture-a'}}
        self.session.snapshot = lambda: self.payload
    def test_reads_do_not_subscribe_watcher_does_and_query_is_correlated(self):
        self.assertEqual(self.session.queue_status('fixture-a','q1')['status'], 'loading')
        self.assertEqual(self.api.bindings, [])
        self.session._queue_tick()
        self.api.bindings[0][2].callback('Subscribed', {'items':[item(1)]})
        reply = self.session.queue_status('fixture-a','q1')
        self.assertEqual((reply['request_id'], reply['zone_id'],reply['status']), ('q1','fixture-a','ready'))
        self.assertTrue(queue_wire.valid_result(reply, {'request_id':'q1','zone_id':'fixture-a'}))
        self.assertEqual(len(self.api.bindings),1)
    def test_publish_invalidates_without_sdk_io(self):
        self.session._queue_tick();old=self.api.bindings[0][2]
        old.callback('Subscribed',{'items':[item(1)]})
        self.payload['zone']['id']='fixture-b';self.payload['selection']['zone_id']='fixture-b'
        self.session._publish()
        self.assertEqual(old.cancels,0)
        self.assertEqual(self.session.queue_status('fixture-a','q')['items'],[])
        self.session._queue_tick();self.assertEqual(old.cancels,1)
        self.assertEqual(self.api.bindings[-1][0],'fixture-b')
    def test_disconnect_without_publish_hides_rows_and_stop_cancels(self):
        self.session._queue_tick();h=self.api.bindings[0][2]
        h.callback('Subscribed',{'items':[item(1)]})
        self.api._roonsocket.connected=False
        self.assertEqual(self.session.queue_status('fixture-a','q')['status'],'disconnected')
        self.api.stop=lambda:None
        self.session.stop();self.assertEqual(h.cancels,1)
    def test_vendor_frames_through_core_cache_and_ipc_result(self):
        from tests.python import test_queue_feasibility as transport
        sock = transport.socket()
        self.session._api = transport.api(sock)
        self.session._api._zone_list_generation = 1
        self.session._queue_tick()
        transport.frame(sock, 10, 'Subscribed', {'items':[item(1)]})
        reply = self.session.queue_status('fixture-a', 'cross-layer')
        self.assertEqual(reply['items'][0]['id'], 1)
        self.assertTrue(queue_wire.valid_result(reply, {'zone_id':'fixture-a','request_id':'cross-layer'}))
        self.payload['zone']['id'] = 'fixture-b'
        self.payload['selection']['zone_id'] = 'fixture-b'
        self.session._publish()
        transport.frame(sock, 10, 'Changed', {'changes':[]})
        self.assertEqual(self.session.queue_status('fixture-b','new')['items'], [])
        self.session._queue_tick()
        self.assertEqual([entry[1].get('zone_or_output_id') for entry in sock._socket.sent],
                         ['fixture-a', None, 'fixture-b'])
        self.assertNotIn('queue', self.session.snapshot())

    def test_cli_and_exact_bounded_request(self):
        req={'cmd':'queue_status','v':1,'request_id':'q','zone_id':'fixture-a'}
        self.assertEqual(cli.to_request(['queue',json.dumps(req)]),req)
        for bad in (dict(req,v=True),dict(req,zone_id=''),dict(req,request_id='x'*65),dict(req,extra=1)):
            self.assertFalse(queue_wire.valid_request(bad))
            with self.assertRaises(ValueError):cli.to_request(['queue',json.dumps(bad)])
    def test_reply_validation_rejects_wrong_correlation_and_oversize(self):
        req={'request_id':'q','zone_id':'fixture-a'}
        result=queue_wire.result(req,'ready')
        self.assertTrue(queue_wire.valid_result(result,req))
        result['request_id']='different';self.assertFalse(queue_wire.valid_result(result,req))
        result=queue_wire.result(req,'ready');result['items']=[{'id':i,'title':'x'*256,'subtitle':'y'*256} for i in range(100)]
        self.assertTrue(queue_wire.valid_result(result,req))
        result['items'][0]['title']='x'*100000
        self.assertFalse(queue_wire.valid_result(result,req))


class QueueServerTests(unittest.TestCase):
    """Actual handler dispatch with an in-memory connection, no socket bind."""
    def call(self, session, request):
        import io
        from tonearm_lib import server
        class Connection:
            def __init__(self):self.sent=b'';self.closed=False
            def makefile(self, mode):return io.StringIO(json.dumps(request)+'\n')
            def sendall(self, data):self.sent+=data
            def close(self):self.closed=True
        conn=Connection();srv=server.Server(session)
        srv._handle(conn)
        self.assertTrue(conn.closed)
        return json.loads(conn.sent)
    def test_handler_correlates_and_refuses_unsupported_malformed_and_bad_results(self):
        req={'cmd':'queue_status','v':1,'request_id':'q','zone_id':'fixture-a'}
        class Session:
            def queue_status(self,zid,rid):return queue_wire.result(req,'loading')
        self.assertEqual(self.call(Session(),req)['status'],'loading')
        self.assertEqual(self.call(object(),req)['error'],'unsupported')
        self.assertEqual(self.call(Session(),dict(req,extra=1))['error'],'bad_request')
        class Wrong:
            def queue_status(self,zid,rid):return queue_wire.result(dict(req,request_id='wrong'),'ready')
        self.assertEqual(self.call(Wrong(),req)['error'],'internal_error')
    def test_artwork_wrapper_passes_query_without_holding_its_lock(self):
        from tonearm_lib import art
        class Session:
            def queue_status(self,zid,rid):
                acquired=wrapper._lock.acquire(blocking=False)
                if acquired:wrapper._lock.release()
                self_unittest.assertTrue(acquired)
                return (zid,rid)
        self_unittest=self
        wrapper=art.CachingSession(Session(),None)
        self.assertEqual(wrapper.queue_status('fixture-a','q'),('fixture-a','q'))
