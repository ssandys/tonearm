"""SYNTHETIC queue contracts, never captured Core events or live acceptance."""
import json
import inspect
import os
import socket as unix_socket
import tempfile
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from tonearm_lib import core, queue_state as q, server, state
from tests.python.test_vendor_subscription_lifecycle import socket, frame
from roonapi.roonapi import RoonApi


def item(key=1, title='Synthetic title', subtitle='Synthetic artist'):
    return {'queue_item_id': key, 'three_line': {'line1': title, 'line2': subtitle}}


class FakeApi:
    def __init__(self):
        self._roonsocket = SimpleNamespace(connected=True)
        self._zone_list_generation = 1
        self.zones = {'z': {'zone_id': 'z', 'display_name': 'Synthetic', 'state': 'paused'}}
        self.bindings = []
        self.on_bind = None

    def subscribe_queue(self, cb, zid, limit):
        handle = Mock()
        self.bindings.append((cb, zid, limit, handle))
        if self.on_bind:
            self.on_bind(cb)
        return handle


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.ctx = q.Context('available', self.api, self.api._roonsocket, 'z', 1)
        self.pushed = []
        self.cache = q.QueueCache(lambda: self.ctx, lambda: self.pushed.append(self.cache.snapshot()))

    def bind(self):
        self.cache.drain()
        cb = self.api.bindings[-1][0]
        return lambda name='Subscribed', body=None, verb='CONTINUE': cb(name, body if body is not None else {'items': [item()]}, verb)

    def test_initial_replace_empty_insert_remove_are_synthetic(self):
        cb = self.bind()
        cb()
        self.assertEqual(self.pushed[-1]['items'][0]['id'], 1)
        cb(body={'items': []})
        self.assertEqual(self.cache.snapshot()['status'], 'ready')
        cb('Changed', {'changes': [{'operation': 'insert', 'index': 0, 'items': [item('s'), item(2)]}]})
        cb('Changed', {'changes': [{'operation': 'remove', 'index': 0, 'count': 1}]})
        self.assertEqual([x['id'] for x in self.cache.snapshot()['items']], [2])
        self.assertNotIn('schema', self.pushed[-1])

    def test_invalid_contracts_clear_and_allow_only_one_fresh_bind(self):
        bad = [('Changed', {'changes': []}), ('Subscribed', {'items': [item(), item()]}),
               ('Subscribed', {'items': [item(i) for i in range(101)]}),
               ('Subscribed', {'items': [item(True)]}),
               ('Subscribed', {'items': [item('x' * 129)]}),
               ('Subscribed', {'items': [item(subtitle=False)]}),
               ('Subscribed', {'items': [item()], 'extra': 'x' * q.MAX_CACHE_BYTES}),
               ('Subscribed', {'items': [item(i, '界' * 256, '界' * 256) for i in range(100)]})]
        for name, body in bad:
            with self.subTest(body_type=name):
                self.setUp()
                cb = self.bind()
                cb(name, body)
                self.assertEqual(self.cache.snapshot()['status'], 'error')
                self.assertEqual(self.cache.snapshot()['items'], [])
                self.cache.drain()
                self.api.bindings[0][3].cancel.assert_called_once()
                self.assertEqual(len(self.api.bindings), 2)
                cb()  # old token cannot repair the cache
                self.assertEqual(self.cache.snapshot()['status'], 'error')
                self.api.bindings[1][0](name, body, 'CONTINUE')
                for _ in range(4):
                    self.cache.drain()
                self.api.bindings[1][3].cancel.assert_called_once()
                self.assertEqual(len(self.api.bindings), 2)

    def test_ready_invalid_delta_is_atomic_and_recovery_can_succeed(self):
        for change in [{'operation': 'update', 'index': 0},
                       {'operation': 'remove', 'index': 1, 'count': 1},
                       {'operation': 'insert', 'index': True, 'items': []}]:
            self.setUp(); cb = self.bind(); cb()
            cb('Changed', {'changes': [change]})
            self.assertEqual(self.cache.snapshot()['items'], [])
            self.cache.drain()
            self.api.bindings[1][0]('Subscribed', {'items': [item(2)]}, 'CONTINUE')
            self.cache.drain()
            self.assertEqual(self.cache.snapshot()['items'][0]['id'], 2)
            self.api.bindings[1][3].cancel.assert_not_called()

    def test_terminal_verb_independent_of_event_name(self):
        for name in ['Subscribed', 'Unsubscribed', 'Error']:
            self.setUp(); cb = self.bind(); cb(name, {'items': []}, 'COMPLETE')
            self.assertEqual(self.cache.snapshot()['status'], 'error')

    def test_retarget_cancel_before_bind_and_ignore_late_callback(self):
        cb = self.bind(); cb()
        handle = self.api.bindings[0][3]
        self.ctx = replace(self.ctx, zone_id='other')
        self.api.on_bind = lambda _: handle.cancel.assert_called_once()
        self.cache.drain(); cb()
        self.assertEqual(self.cache.snapshot()['status'], 'loading')
        self.assertEqual(self.api.bindings[-1][1], 'other')

    def test_selection_change_reentrant_in_cancel_refuses_captured_replacement(self):
        self.bind()()
        handle = self.api.bindings[0][3]
        self.ctx = replace(self.ctx, zone_id='captured')
        handle.cancel.side_effect = lambda: setattr(self, 'ctx', replace(self.ctx, zone_id='latest'))
        self.cache.drain()
        self.assertEqual(len(self.api.bindings), 1)
        self.cache.drain()
        self.assertEqual(self.api.bindings[-1][1], 'latest')
        handle.cancel.assert_called_once()

    def test_disconnected_unknown_and_generation_floors(self):
        cb = self.bind(); cb()
        self.ctx = replace(self.ctx, socket=SimpleNamespace(connected=True))
        self.cache.drain()
        self.assertEqual(len(self.api.bindings), 1)
        self.assertEqual(self.cache.snapshot()['status'], 'unknown')
        self.ctx = replace(self.ctx, generation=2)
        self.cache.drain()
        self.assertEqual(len(self.api.bindings), 2)
        cb()
        self.assertEqual(self.cache.snapshot()['items'], [])
        self.ctx = replace(self.ctx, required=3)
        self.cache.drain()
        self.assertEqual(self.cache.snapshot()['status'], 'unknown')
        for word in ['unknown', 'unavailable', 'disconnected']:
            self.ctx = replace(self.ctx, status=word, zone_id=None)
            self.cache.drain()
            self.assertEqual(self.cache.snapshot()['status'], word)
        self.assertEqual(len(self.api.bindings), 2)

    def test_same_socket_reconnect_requires_fresh_authority(self):
        self.bind()()
        self.ctx = replace(self.ctx, status='disconnected')
        self.cache.drain()
        self.ctx = replace(self.ctx, status='available')
        self.cache.drain()
        self.assertEqual(self.cache.snapshot()['status'], 'unknown')
        self.assertEqual(len(self.api.bindings), 1)

    def test_new_api_identity_resets_floor_and_cancels_previous_owner_first(self):
        old = self.bind(); old()
        handle = self.api.bindings[0][3]
        fresh = FakeApi()
        fresh.on_bind = lambda _: handle.cancel.assert_called_once()
        self.ctx = q.Context('available', fresh, fresh._roonsocket, 'z', 1)
        self.cache.drain()
        self.assertEqual(len(fresh.bindings), 1)
        old()
        self.assertEqual(self.cache.snapshot()['items'], [])

    def test_snapshot_observed_change_is_still_notified_by_watcher(self):
        self.bind()()
        self.pushed.clear()
        self.ctx = replace(self.ctx, status='disconnected', zone_id=None)
        self.cache.snapshot()  # read must never consume the next push
        self.cache.drain()
        self.assertEqual(self.pushed[-1]['status'], 'disconnected')
        revision = self.pushed[-1]['revision']
        self.cache.drain()
        self.assertEqual(self.pushed[-1]['revision'], revision)

    def test_stop_or_selection_change_during_synchronous_bind(self):
        for stop in [True, False]:
            self.setUp()
            def during(cb):
                if stop:
                    self.cache.stop()
                else:
                    self.ctx = replace(self.ctx, zone_id='new')
                cb('Subscribed', {'items': [item()]}, 'CONTINUE')
            self.api.on_bind = during
            self.cache.drain()
            self.api.bindings[0][3].cancel.assert_called_once()
            self.assertEqual(self.cache.snapshot()['items'], [])
            if stop:
                self.cache.stop(); self.cache.drain()
                self.assertEqual(len(self.api.bindings), 1)

    def test_bind_failure_does_not_retry_and_synchronous_bad_event_recovers_once(self):
        self.api.on_bind = Mock(side_effect=RuntimeError('synthetic'))
        self.cache.drain(); self.cache.drain()
        self.assertEqual(len(self.api.bindings), 1)
        self.assertEqual(self.cache.snapshot()['error'], 'subscription_failed')
        self.setUp()
        self.api.on_bind = lambda cb: cb('Changed', {}, 'CONTINUE')
        for _ in range(5): self.cache.drain()
        self.assertEqual(len(self.api.bindings), 2)
        for binding in self.api.bindings: binding[3].cancel.assert_called_once()

    def test_io_and_publish_release_cache_lock_and_busy_prevents_duplicate_owner(self):
        entered, release = threading.Event(), threading.Event()
        def during(_):
            self.assertTrue(self.cache._lock.acquire(blocking=False))
            self.cache._lock.release()
            entered.set(); self.assertTrue(release.wait(2))
        self.api.on_bind = during
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.cache.drain)
            self.assertTrue(entered.wait(2))
            self.cache.drain()
            release.set(); future.result(2)
        self.assertEqual(len(self.api.bindings), 1)
        # Use a separate thread to snapshot during publication: would time out under lock.
        def publish():
            with ThreadPoolExecutor(1) as pool:
                self.pushed.append(pool.submit(self.cache.snapshot).result(1))
        self.cache._changed = publish
        self.api.bindings[0][0]('Subscribed', {'items': [item()]}, 'CONTINUE')

    def test_clipping_cache_copy_and_envelope_budget(self):
        cb = self.bind(); cb(body={'items': [item(i, 'x' * 300) for i in range(100)]})
        snap = self.cache.snapshot()
        self.assertEqual(len(snap['items']), 100)
        self.assertEqual(len(snap['items'][0]['title']), 256)
        snap['items'][0]['title'] = 'changed'
        self.assertNotEqual(self.cache.snapshot()['items'][0]['title'], 'changed')
        payload = state.build('ok', None, state.normalize_zone(self.api.zones['z']), [])
        payload['queue'] = self.cache.snapshot()
        self.assertLess(len(json.dumps(payload).encode()), server.MAX_PAYLOAD_BYTES)
        self.assertEqual(json.loads(server._bounded_blob(payload))['queue']['status'], 'ready')


class SessionTests(unittest.TestCase):
    def setUp(self):
        with patch.object(core.config, 'load', return_value={}):
            self.pushed = []
            self.session = core.RoonSession(self.pushed.append)
        self.api = FakeApi()
        self.session._api, self.session._status = self.api, 'ok'
        self.session._arbiter.pin('z')

    def test_push_uses_existing_snapshot_and_read_never_binds(self):
        for _ in range(3): self.session.snapshot()
        self.assertEqual(self.api.bindings, [])
        self.session._queue.drain()
        self.api.bindings[0][0]('Subscribed', {'items': [item()]}, 'CONTINUE')
        self.assertEqual(self.pushed[-1]['queue']['items'][0]['id'], 1)
        self.assertNotIn('schema', self.pushed[-1]['queue'])

    def test_existing_subscribe_stream_gets_queue_callback_without_polling(self):
        with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, {'XDG_RUNTIME_DIR': runtime}):
            srv = server.Server(self.session)
            self.session._on_change = srv.broadcast
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            self.assertTrue(srv.ready.wait(2))
            try:
                with unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(server.socket_path())
                    client.sendall(b'{"cmd":"subscribe"}\n')
                    with client.makefile('r') as lines:
                        self.assertEqual(json.loads(lines.readline())['queue']['status'], 'loading')
                        self.session._queue.drain()
                        # Watcher may first publish loading; callbacks then publish ready.
                        self.api.bindings[0][0]('Subscribed', {'items': [item()]}, 'CONTINUE')
                        payload = json.loads(lines.readline())
                        if payload['queue']['status'] != 'ready':
                            payload = json.loads(lines.readline())
                        self.assertEqual(payload['queue']['items'][0]['id'], 1)
                        self.assertNotIn('schema', payload['queue'])
            finally:
                srv.shutdown(); thread.join(2)

    def test_snapshot_context_race_clears_rows(self):
        self.session._queue.drain()
        self.api.bindings[0][0]('Subscribed', {'items': [item()]}, 'CONTINUE')
        original = self.session._state_snapshot
        def stale_payload():
            payload = original()
            self.session._arbiter.pin('missing')
            return payload
        with patch.object(self.session, '_state_snapshot', side_effect=stale_payload):
            snap = self.session.snapshot()
        self.assertEqual(snap['queue']['items'], [])
        self.assertNotEqual(snap['queue']['status'], 'ready')

    def test_no_stale_map_or_unavailable_pin_or_missing_generation_subscriptions(self):
        for generation, pinned, connected in [(0, None, True), (1, 'absent', True), (1, None, False)]:
            with self.subTest(generation=generation, pinned=pinned, connected=connected):
                self.api._zone_list_generation = generation
                self.api._roonsocket.connected = connected
                if pinned: self.session._arbiter.pin(pinned)
                self.session.snapshot(); self.session._queue.drain()
        del self.api._zone_list_generation
        self.session._queue.drain()
        self.assertEqual(self.api.bindings, [])

    def test_time_scalar_is_strict_and_zone_list_unchanged(self):
        for raw in [None, True, float('nan'), float('inf'), -1, state.MAX_NUMBER + 1, '42']:
            self.assertIsNone(state.normalize_zone({'zone_id': 'z', 'queue_time_remaining': raw})['queue_time_remaining'])
        for raw in [0, 3723.5]:
            self.assertEqual(state.normalize_zone({'zone_id': 'z', 'queue_time_remaining': raw})['queue_time_remaining'], raw)
        self.assertEqual(set(self.session.snapshot()['zones'][0]), {'id', 'name', 'state'})


class OwnedVendorTests(unittest.TestCase):
    def test_numbered_queue_patch_markers_survive_vendor_refresh(self):
        from roonapi import roonapisocket
        self.assertIn('LOCAL PATCH (tonearm queue 1)', inspect.getsource(roonapisocket))
        self.assertIn('LOCAL PATCH (tonearm queue 1)', inspect.getsource(sys.modules[RoonApi.__module__]))

    def test_malformed_and_oversized_owned_frame_fail_closed_without_raw_logs(self):
        sock = socket(); seen = Mock()
        sock.subscribe_owned_queue(seen, 'z', 100)
        with patch('roonapi.roonapisocket.LOGGER') as logger:
            sock.on_message(None, b'MOO/1 CONTINUE Changed\nRequest-Id: 10\nContent-Type: application/json\n\n{"private": broken}')
            seen.assert_called_with('Invalid', None, 'INVALID')
            logger.exception.assert_not_called()
            seen.reset_mock()
            frame(sock, 10, {'items': [], 'extra': 'x' * (64 * 1024)})
            seen.assert_called_once_with('Invalid', None, 'INVALID')
            logger.exception.assert_not_called()

    def test_owned_frame_does_not_reach_reused_route_while_parsing(self):
        sock = socket(); old, new = Mock(), Mock()
        sock.subscribe_owned_queue(old, 'z', 100)
        from roonapi import roonapisocket
        loads = roonapisocket.json.loads
        def reused(text):
            sock.on_close(None, None, None); sock.connected = True
            with patch.object(sock._socket, 'send'):
                sock.subscribe_owned_queue(new, 'new', 100)
            return loads(text)
        with patch.object(roonapisocket.json, 'loads', side_effect=reused):
            frame(sock, 10, {'items': []})
        old.assert_not_called(); new.assert_not_called()
        self.assertEqual(len(sock._subscriptions), 1)

    def test_cancel_epoch_cannot_unsubscribe_reused_key(self):
        sock = socket(); h = sock.subscribe_owned_queue(Mock(), 'z', 100)
        cancel = sock._cancel_entry
        def close_then_cancel(entry):
            sock.on_close(None, None, None); sock.connected = True
            sock.subscribe_owned_queue(Mock(), 'new', 100)
            cancel(entry)
        sock._cancel_entry = close_then_cancel
        h.cancel(); h.cancel()
        self.assertEqual(len(sock._socket.sent), 2)
        self.assertIn(10, sock._subscriptions)

    def test_register_before_send_and_terminal_removes_exact_route(self):
        sock = socket(); seen = []
        original = sock._socket.send
        def send(data, opcode):
            self.assertFalse(sock._request_lock.locked())
            original(data, opcode)
            frame(sock, 10, {'items': []}, 'Subscribed')
        sock._socket.send = send
        h = sock.subscribe_owned_queue(lambda *args: seen.append(args), 'z', 100)
        self.assertEqual(seen, [('Subscribed', {'items': []}, 'CONTINUE')])
        sock._socket.send = original
        sock.subscribe('synthetic', 'zones', Mock())
        sock.on_message(None, b'MOO/1 COMPLETE Subscribed\nRequest-Id: 10\nContent-Type: application/json\n\n{"items": []}')
        self.assertEqual(seen[-1][2], 'COMPLETE')
        self.assertEqual(list(sock._subscriptions), [11])
        count = len(sock._socket.sent); h.cancel(); h.cancel()
        self.assertEqual(len(sock._socket.sent), count)
        self.assertEqual(sock.results, {})

    def test_owned_cancel_exact_entry_reentrant_close_and_failed_send(self):
        sock = socket(); seen = Mock()
        h = sock.subscribe_owned_queue(seen, 'z', 1)
        other = sock.subscribe_owned_queue(Mock(), 'z', 1)
        original = sock._socket.send
        def send(data, opcode):
            self.assertNotIn(10, sock._subscriptions)
            self.assertFalse(sock._request_lock.locked())
            original(data, opcode)
            frame(sock, 10, {'late': True})
            if b'unsubscribe_queue' in data:
                sock.on_close(None, None, None); sock.connected = True
                sock._socket.send = original
                sock.send_request('synthetic/read')
        sock._socket.send = send
        h.cancel(); h.cancel(); other.cancel()
        seen.assert_not_called()
        self.assertEqual(set(sock.results), {10})
        sock._socket.send = Mock(side_effect=RuntimeError('synthetic'))
        with self.assertRaises(RuntimeError): sock.subscribe_owned_queue(Mock(), 'z', 1)
        self.assertEqual(sock._subscriptions, {})

    def test_strict_identifiers_counts_and_missing_disconnected_socket(self):
        sock = socket()
        for zid, count in [('', 1), ('x' * 129, 1), ('bad\n', 1), ('z', True), ('z', 0), ('z', 101), ('z', 1.0)]:
            with self.assertRaises(ValueError): sock.subscribe_owned_queue(Mock(), zid, count)
        self.assertEqual(sock._socket.sent, [])
        sock.connected = False
        with self.assertRaises(ConnectionError): sock.subscribe_owned_queue(Mock(), 'z', 1)
        api = RoonApi.__new__(RoonApi)
        with self.assertRaises(ConnectionError): api.subscribe_queue(Mock(), 'z')
