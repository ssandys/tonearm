"""General vendor lifecycle regressions: fake websocket, no owned queue APIs."""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/vendor'))
from roonapi.roonapi import RoonApi
from roonapi.roonapisocket import RoonApiWebSocket
from roonapi.constants import SERVICE_TRANSPORT


class Transport:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data, opcode):
        header, body = data.decode().split('\n\n', 1)
        self.sent.append((header.splitlines()[0], json.loads(body) if body else None))

    def close(self):
        self.closed = True


def socket():
    transport = Transport()
    with patch('roonapi.roonapisocket.websocket.WebSocketApp', return_value=transport):
        result = RoonApiWebSocket('ws://synthetic.invalid')
    result.connected = True
    return result


def frame(sock, request_id, body, name='Changed'):
    encoded = json.dumps(body).encode()
    sock.on_message(None, (f'MOO/1 CONTINUE {name}\nRequest-Id: {request_id}\n'
                          'Content-Type: application/json\n\n').encode() + encoded)


class SubscriptionLifecycleTests(unittest.TestCase):
    def test_timeout_cleanup_cannot_race_admitted_reply_and_reinsert_result(self):
        sock = socket()
        api = RoonApi.__new__(RoonApi)
        api._roonsocket = sock
        polling = threading.Event()
        finish_polling = threading.Event()
        admitted = threading.Event()
        finish_reply = threading.Event()
        cleanup_attempted = threading.Event()
        waiter = []
        lock = sock._request_lock

        class ObservedLock:
            def __enter__(self):
                if admitted.is_set() and threading.get_ident() == waiter[0]:
                    cleanup_attempted.set()
                lock.acquire()

            def __exit__(self, *args):
                lock.release()

        class PausedReplyResults(dict):
            def __contains__(self, key):
                present = super().__contains__(key)
                admitted.set()
                if not finish_reply.wait(3):
                    raise AssertionError('reply was not released')
                return present

            def __delitem__(self, key):
                # Observe the old unlocked cleanup too, so it fails on the
                # leaked entry rather than on a coordination timeout.
                super().__delitem__(key)
                cleanup_attempted.set()

        sock._request_lock = ObservedLock()
        sock._results = PausedReplyResults()

        def poll(_seconds):
            polling.set()
            self.assertTrue(finish_polling.wait(3))

        def request():
            waiter.append(threading.get_ident())
            return api._request('synthetic/read')

        with patch('roonapi.roonapi.time.sleep', side_effect=poll):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(request)
                reply = None
                try:
                    self.assertTrue(polling.wait(3))
                    reply = pool.submit(frame, sock, 10, {'late': True})
                    self.assertTrue(admitted.wait(3))
                    finish_polling.set()
                    self.assertTrue(cleanup_attempted.wait(3))
                finally:
                    finish_polling.set()
                    finish_reply.set()
                self.assertIsNone(pending.result(timeout=3))
                if reply is not None:
                    reply.result(timeout=3)
        self.assertEqual(sock.results, {})

    def test_completed_request_removes_result_and_drops_late_reply(self):
        sock = socket()
        api = RoonApi.__new__(RoonApi)
        api._roonsocket = sock
        with patch('roonapi.roonapi.time.sleep',
                   side_effect=lambda _: frame(sock, 10, {'received': True})):
            self.assertEqual(api._request('synthetic/read'), {'received': True})
        self.assertEqual(sock.results, {})
        frame(sock, 10, {'late': True})
        self.assertEqual(sock.results, {})

    def test_register_before_synchronous_initial_reply_and_preserve_none_return(self):
        sock = socket()
        seen = []
        original = sock._socket.send

        def immediate(data, opcode):
            original(data, opcode)
            frame(sock, 10, {'synthetic': True}, 'Subscribed')

        sock._socket.send = immediate
        result = sock.subscribe('synthetic', 'events', seen.append)
        self.assertIsNone(result)
        self.assertEqual(seen, [{'synthetic': True}])
        self.assertIn(10, sock._subscriptions)
        self.assertEqual(sock.results, {})

    def test_disconnected_subscribe_and_request_register_nothing(self):
        sock = socket()
        sock.connected = False
        self.assertIsNone(sock.subscribe('synthetic', 'events', Mock()))
        self.assertIs(sock.send_request('synthetic/read'), False)
        self.assertEqual(sock._subscriptions, {})
        self.assertEqual(sock.results, {})
        self.assertEqual(sock._socket.sent, [])

    def test_unknown_and_late_results_do_not_accumulate(self):
        sock = socket()
        for request_id in range(100):
            frame(sock, request_id, {'synthetic': True})
        self.assertEqual(sock.results, {})
        rid = sock.send_request('synthetic/read')
        frame(sock, rid, {'received': True})
        self.assertEqual(sock.results[rid], {'received': True})
        sock.results.pop(rid)
        frame(sock, rid, {'late': True})
        self.assertEqual(sock.results, {})

    def test_unsubscribe_invalidates_before_send_and_preserves_other_endpoint(self):
        sock = socket()
        queue, zones = Mock(), Mock()
        sock.subscribe('synthetic', 'events', queue)
        sock.subscribe('synthetic', 'zones', zones)
        original = sock._socket.send

        def late_during_cancel(data, opcode):
            original(data, opcode)
            frame(sock, 10, {'late': True})

        sock._socket.send = late_during_cancel
        sock.unsubscribe('synthetic', 'events')
        queue.assert_not_called()
        self.assertEqual(list(sock._subscriptions), [11])
        self.assertEqual(sock.results, {})
        sent = len(sock._socket.sent)
        sock.unsubscribe('synthetic', 'events')
        self.assertEqual(len(sock._socket.sent), sent)

    def test_cancel_does_not_remove_a_new_result_after_id_reuse(self):
        sock = socket()
        sock.subscribe('synthetic', 'events', Mock())
        sock.subscribe('synthetic', 'zones', Mock())
        original = sock._socket.send

        def reconnect_during_cancel(data, opcode):
            original(data, opcode)
            if b'/unsubscribe_events' in data:
                sock.on_close(None, None, None)
                sock.connected = True
                # Reuse the cancellation ID for a real ordinary request.
                for _ in range(3):
                    sock.send_request('synthetic/read')

        sock._socket.send = reconnect_during_cancel
        sock.unsubscribe('synthetic', 'events')
        self.assertEqual(set(sock.results), {10, 11, 12})

    def test_multiple_endpoint_bindings_cancel_once_each(self):
        sock = socket()
        sock.subscribe('synthetic', 'events', Mock())
        sock.subscribe('synthetic', 'events', Mock())
        sock.unsubscribe('synthetic', 'events')
        self.assertEqual(sock._subscriptions, {})
        self.assertEqual([row[1]['subscription_key'] for row in sock._socket.sent[2:]], [0, 1])
        self.assertEqual(sock.results, {})

    def test_stop_unsubscribes_all_before_close(self):
        sock = socket()
        sock.subscribe('synthetic', 'events', Mock())
        sock.subscribe('synthetic', 'zones', Mock())
        original = sock._socket.close

        def close_after_cancels():
            self.assertEqual(sock._subscriptions, {})
            self.assertEqual(len(sock._socket.sent), 4)
            original()

        sock._socket.close = close_after_cancels
        sock.stop()
        self.assertTrue(sock._socket.closed)
        self.assertEqual(sock.results, {})

    def test_stop_closes_and_attempts_other_cancellations_after_send_failure(self):
        sock = socket()
        sock.subscribe('synthetic', 'events', Mock())
        sock.subscribe('synthetic', 'zones', Mock())
        send = Mock(side_effect=RuntimeError('synthetic failure'))
        sock._socket.send = send
        sock.stop()
        self.assertEqual(send.call_count, 2)
        self.assertTrue(sock._socket.closed)
        self.assertEqual(sock._subscriptions, {})
        self.assertEqual(sock.results, {})

    def test_send_failure_removes_ordinary_and_subscription_bookkeeping(self):
        sock = socket()
        sock._socket.send = Mock(side_effect=RuntimeError('synthetic failure'))
        with self.assertRaises(RuntimeError):
            sock.subscribe('synthetic', 'events', Mock())
        with self.assertRaises(RuntimeError):
            sock.send_request('synthetic/read')
        self.assertEqual(sock._subscriptions, {})
        self.assertEqual(sock.results, {})

    def test_request_ids_are_unique_across_concurrent_operations(self):
        sock = socket()

        def operation(i):
            if i % 2:
                sock.subscribe('synthetic', 'events', Mock())
            else:
                sock.send_request('synthetic/read')

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(operation, range(200)))
        ids = set(sock._subscriptions) | set(sock.results)
        self.assertEqual(len(ids), 200)
        self.assertFalse(set(sock._subscriptions) & set(sock.results))

    def test_instances_do_not_share_lock_or_block_each_other(self):
        first, second = socket(), socket()
        self.assertIsNot(first._request_lock, second._request_lock)
        with first._request_lock, ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(second.send_request, 'synthetic/read').result(timeout=1), 10)

    def test_sends_and_callbacks_run_outside_bookkeeping_lock(self):
        sock = socket()

        def can_acquire():
            acquired = sock._request_lock.acquire(timeout=0.5)
            if acquired:
                sock._request_lock.release()
            return acquired

        def check_lock():
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(can_acquire).result(timeout=1))

        original = sock._socket.send

        def send(data, opcode):
            check_lock()
            original(data, opcode)

        sock._socket.send = send
        sock.subscribe('synthetic', 'events', lambda _: check_lock())
        frame(sock, 10, {'synthetic': True})
        sock.unsubscribe('synthetic', 'events')

    def test_disconnected_logging_runs_outside_bookkeeping_lock(self):
        sock = socket()
        sock.connected = False

        def log_without_lock(*args):
            def acquire():
                acquired = sock._request_lock.acquire(timeout=0.5)
                if acquired:
                    sock._request_lock.release()
                return acquired
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(acquire).result(timeout=1))

        with patch('roonapi.roonapisocket.LOGGER.error', side_effect=log_without_lock):
            self.assertIs(sock.send_request('synthetic/read'), False)

    def test_close_clears_routing_before_reuse(self):
        sock = socket()
        callback = Mock()
        sock.subscribe('synthetic', 'events', callback)
        sock.on_close(None, None, None)
        self.assertFalse(sock.connected)
        self.assertEqual(sock._subscriptions, {})
        self.assertEqual((sock._requestid, sock._subkey), (10, 0))
        frame(sock, 10, {'late': True})
        callback.assert_not_called()
        self.assertEqual(sock.results, {})

    def test_existing_queue_wrapper_remains_body_only_and_limit_is_not_enforced(self):
        sock = socket()
        api = RoonApi.__new__(RoonApi)
        api._roonsocket = sock
        seen = []
        self.assertIsNone(api.register_queue_callback(seen.append, 'synthetic-zone'))
        self.assertEqual(sock._socket.sent[0][0], 'MOO/1 REQUEST ' + SERVICE_TRANSPORT + '/subscribe_queue')
        self.assertFalse(hasattr(api, 'subscribe_queue'))
        self.assertFalse(hasattr(sock, 'subscribe_owned_queue'))
        sock.subscribe('synthetic', 'limited', seen.append, {'max_item_count': 100})
        self.assertEqual(sock._socket.sent[-1][1]['max_item_count'], 100)
        frame(sock, 11, {'items': list(range(137))})
        self.assertEqual(len(seen[0]['items']), 137)

    def test_malformed_message_log_omits_personal_body(self):
        sock = socket()
        with patch('roonapi.roonapisocket.LOGGER') as logger:
            sock.on_message(None, b'MOO/1 CONTINUE Changed\nRequest-Id: 99\nContent-Type: application/json\n\n{"private": broken}')
        self.assertEqual(logger.exception.call_args.args, ('Error while parsing message',))


if __name__ == '__main__':
    unittest.main()
