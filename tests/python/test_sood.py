import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import sood


def tlv(key, value):
    kb, vb = key.encode(), value.encode()
    return bytes([len(kb)]) + kb + struct.pack(">H", len(vb)) + vb


class TestSoodFraming(unittest.TestCase):
    def test_query_is_a_well_formed_sood_frame(self):
        q = sood.build_query()
        self.assertTrue(q.startswith(b"SOOD"))
        self.assertEqual(q[4], 2)          # version
        self.assertEqual(q[5:6], b"Q")     # query
        self.assertIn(sood.SERVICE_ID.encode(), q)

    def test_parse_round_trips_a_response(self):
        body = tlv("name", "yavin") + tlv("http_port", "9330")
        buf = b"SOOD" + b"\x02" + b"R" + body
        self.assertEqual(sood.parse(buf), {"name": "yavin", "http_port": "9330"})

    def test_parse_rejects_a_foreign_frame(self):
        self.assertIsNone(sood.parse(b"NOPE\x02R"))
        self.assertIsNone(sood.parse(b""))

    def test_parse_survives_a_truncated_frame(self):
        # A short read must not raise: Service-side, any throw is indistinguish-
        # able from "no core found" and would discard a real response.
        buf = b"SOOD" + b"\x02" + b"R" + b"\x04name\x00"
        self.assertIsNotNone(sood.parse(buf))

    def test_parse_ignores_trailing_garbage_after_a_valid_pair(self):
        buf = b"SOOD" + b"\x02" + b"R" + tlv("name", "yavin") + b"\xff"
        self.assertEqual(sood.parse(buf).get("name"), "yavin")


class TestCoreRecord(unittest.TestCase):
    def test_to_core_extracts_the_fields_we_use(self):
        raw = {
            "name": "yavin", "tcp_port": "9150", "http_port": "9330",
            "unique_id": "96e11146", "display_version": "2.71 (build 1683)",
        }
        core = sood.to_core("192.168.50.118", raw, via="multicast")
        self.assertEqual(core["host"], "192.168.50.118")
        self.assertEqual(core["name"], "yavin")
        self.assertEqual(core["tcp_port"], 9150)
        self.assertEqual(core["http_port"], 9330)
        self.assertEqual(core["via"], "multicast")

    def test_to_core_defaults_ports_when_absent(self):
        core = sood.to_core("10.0.0.5", {"name": "x"}, via="scan")
        self.assertEqual(core["tcp_port"], 9150)
        self.assertEqual(core["http_port"], 9330)


if __name__ == "__main__":
    unittest.main()
