"""Search input waits for a slow response without retrying the request."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'vendor')))
from roonapi import roonapi


class SearchReplyBudgetTests(unittest.TestCase):
    def request(self, command, data, response_at=None):
        class Socket:
            results = {}
            sent = 0
            def send_request(self, command, data):
                self.sent += 1
                self.results[17] = None
                return 17
        sock = Socket()
        api = roonapi.RoonApi.__new__(roonapi.RoonApi)
        api._roonsocket = sock
        elapsed = [0]
        response = {'action': 'list', 'list': {'count': 1}}
        def sleep(n):
            elapsed[0] += n
            if response_at is not None and elapsed[0] >= response_at:
                sock.results[17] = response
        with patch.object(roonapi.time, 'sleep', side_effect=sleep):
            result = api._request(command, data)
        self.assertEqual(sock.sent, 1)
        self.assertNotIn(17, sock.results)
        return result, elapsed[0]

    def test_search_reply_after_old_deadline_is_received_without_resend(self):
        result, elapsed = self.request(roonapi.SERVICE_BROWSE + '/browse',
                                       {'input': 'Synthetic artist'}, response_at=3)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(elapsed, 3)

    def test_search_wait_is_bounded_to_ten_seconds(self):
        result, elapsed = self.request(roonapi.SERVICE_BROWSE + '/browse', {'input': 'Synthetic artist'})
        self.assertIsNone(result)
        self.assertAlmostEqual(elapsed, 10)

    def test_ordinary_browse_keeps_original_budget(self):
        result, elapsed = self.request(roonapi.SERVICE_BROWSE + '/browse', {'pop_all': True}, response_at=3)
        self.assertIsNone(result)
        self.assertAlmostEqual(elapsed, 2.5)

    def test_load_and_transport_do_not_receive_search_budget(self):
        for command in (roonapi.SERVICE_BROWSE + '/load', roonapi.SERVICE_TRANSPORT + '/control'):
            with self.subTest(command=command):
                result, elapsed = self.request(command, {'input': 'Synthetic artist'}, response_at=3)
                self.assertIsNone(result)
                self.assertAlmostEqual(elapsed, 2.5)

    def test_empty_or_invalid_input_does_not_extend_budget(self):
        for value in ('', None, 1):
            with self.subTest(value=value):
                _, elapsed = self.request(roonapi.SERVICE_BROWSE + '/browse', {'input': value})
                self.assertAlmostEqual(elapsed, 2.5)


if __name__ == '__main__': unittest.main()
