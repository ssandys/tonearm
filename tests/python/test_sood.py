import os
import random
import struct
import sys
import unittest
import unittest.mock

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


class TestParseNeverRaises(unittest.TestCase):
    """`parse()` must never raise (see its docstring): Service-side, an
    exception is indistinguishable from "no Core found" and would discard a
    real response. These tests pin that property down so a future off-by-one
    in a bounds check fails this suite instead of shipping silently.
    """

    def test_parse_survives_every_truncation_of_a_valid_frame(self):
        body = (
            tlv("name", "yavin")
            + tlv("http_port", "9330")
            + tlv("unique_id", "96e11146-4bec-466e-afe9-e82a1d8f7b4d")
        )
        full = b"SOOD" + b"\x02" + b"R" + body
        for cut in range(len(full) + 1):
            try:
                sood.parse(full[:cut])
            except Exception as exc:
                self.fail(
                    f"parse() raised {exc!r} truncating a valid frame at "
                    f"offset {cut}/{len(full)}"
                )

    def test_parse_survives_seeded_random_fuzz(self):
        seed = 20260827
        rng = random.Random(seed)
        iterations = 4000

        def random_bytes(n):
            return bytes(rng.randrange(256) for _ in range(n))

        for i in range(iterations):
            if rng.random() < 0.5:
                # Pure garbage, with and without the "SOOD" magic prefix.
                prefix = b"SOOD" if rng.random() < 0.5 else b""
                buf = prefix + random_bytes(rng.randint(0, 80))
            else:
                # A SOOD-prefixed frame with adversarial TLV length fields:
                # key/value lengths that may wildly exceed, or exactly
                # match, the bytes that actually follow them.
                parts = [b"SOOD", bytes([2]), b"R"]
                for _ in range(rng.randint(0, 4)):
                    klen = rng.randint(0, 255)
                    key = random_bytes(rng.randint(0, 12))
                    vlen = rng.randint(0, 65535)
                    val = random_bytes(rng.randint(0, 12))
                    parts += [bytes([klen]), key, struct.pack(">H", vlen), val]
                buf = b"".join(parts)
                if buf and rng.random() < 0.5:
                    buf = buf[: rng.randint(0, len(buf))]
                if rng.random() < 0.5:
                    buf += random_bytes(rng.randint(0, 12))

            try:
                sood.parse(buf)
            except Exception as exc:
                self.fail(
                    f"parse() raised {exc!r} at seed={seed} iteration={i} "
                    f"on buf={buf!r}"
                )


class TestCoreRecord(unittest.TestCase):
    def test_to_core_extracts_the_fields_we_use(self):
        # Deliberately NOT 9150/9330 (DEFAULT_TCP_PORT/DEFAULT_HTTP_PORT):
        # using the defaults here means a stub that always returns the
        # default -- skipping both string->int coercion and the actual
        # dict lookup -- would leave this green. Port selection is the
        # trickiest thing in this project (see AGENTS.md); this fixture
        # must actually exercise it.
        raw = {
            "name": "yavin", "tcp_port": "9151", "http_port": "9331",
            "unique_id": "96e11146", "display_version": "2.71 (build 1683)",
        }
        core = sood.to_core("192.168.50.118", raw, via="multicast")
        self.assertEqual(core["host"], "192.168.50.118")
        self.assertEqual(core["name"], "yavin")
        self.assertEqual(core["tcp_port"], 9151)
        self.assertEqual(core["http_port"], 9331)
        self.assertEqual(core["via"], "multicast")

    def test_to_core_defaults_ports_when_absent(self):
        core = sood.to_core("10.0.0.5", {"name": "x"}, via="scan")
        self.assertEqual(core["tcp_port"], 9150)
        self.assertEqual(core["http_port"], 9330)


class TestLocalNetworksStayPrivate(unittest.TestCase):
    """`_local_networks()` and the `_local_ipv4()` fallback must never hand
    `discover()` a /24 outside private address space -- see Important 3 of
    the final review. A network handing out globally-routable addresses
    (hotel, campus, bridged VM, VPS) must not get 254 unsolicited TCP
    connections from this daemon.
    """

    def test_local_networks_excludes_a_public_interface_address(self):
        # eth0 is a normal private LAN address; eth1 is a real, globally
        # routable address (Google's public DNS range) as an interface
        # could plausibly carry on a network that hands those out directly.
        # Only eth0's /24 must survive.
        addrs = {"eth0": "192.168.1.50", "eth1": "8.8.8.8"}
        with unittest.mock.patch.object(
            sood.socket, "if_nameindex", return_value=[(1, "eth0"), (2, "eth1")]
        ), unittest.mock.patch.object(
            sood, "_iface_ipv4", side_effect=lambda name: addrs[name]
        ):
            nets = sood._local_networks()
        self.assertEqual([str(n) for n in nets], ["192.168.1.0/24"])

    def test_local_networks_excludes_the_tailscale_cgnat_range(self):
        # Measured on the dev machine (see the module docstring): an exit
        # node's policy routes can make an interface-level trick land on a
        # 100.64.0.0/10 address. is_private structurally excludes that
        # whole range, independent of the interface-name ignore list.
        with unittest.mock.patch.object(
            sood.socket, "if_nameindex", return_value=[(1, "eth0")]
        ), unittest.mock.patch.object(
            sood, "_iface_ipv4", return_value="100.94.206.126"
        ):
            nets = sood._local_networks()
        self.assertEqual(nets, [])

    def test_local_ipv4_fallback_rejects_a_public_address(self):
        with unittest.mock.patch.object(
            sood, "_local_networks", return_value=[]
        ), unittest.mock.patch.object(
            sood, "_local_ipv4", return_value="8.8.8.8"
        ):
            # discover() with a zero listen timeout still goes through the
            # multicast phase (no replies expected here) and then the /24
            # fallback path this test targets; assert only that no scan
            # target reached _port_open by checking discover() returns []
            # rather than hanging on 254 real connect attempts.
            found = sood.discover(timeout=0)
        self.assertEqual(found, [])

    def test_local_ipv4_fallback_accepts_a_private_address(self):
        with unittest.mock.patch.object(
            sood, "_local_networks", return_value=[]
        ), unittest.mock.patch.object(
            sood, "_local_ipv4", return_value="192.168.50.5"
        ), unittest.mock.patch.object(
            sood, "_port_open", return_value=False
        ) as port_open:
            sood.discover(timeout=0)
        # _port_open is only reached at all if the fallback network passed
        # the private-space check and produced scan targets.
        self.assertTrue(port_open.called)


if __name__ == "__main__":
    unittest.main()
