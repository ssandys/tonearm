"""SOOD discovery for Roon Cores.

Measured on this network 2026-08-27: multicast to 239.255.90.90:9003 and
broadcast to 255.255.255.255:9003 both draw NO reply -- the AP filters
multicast -- while a unicast query to a known host answers immediately. So
`discover` falls back to scanning the local /24 for an open image port and then
unicast-probing each hit. pyroon's own RoonDiscovery is multicast-only and will
not find a Core here.

Also measured 2026-08-27: this host runs Tailscale as an exit-node client, and
its policy routes capture the classic "UDP-connect to a scratch address" trick
for finding the local IP, returning a Tailscale CGNAT address instead of the
LAN one -- which would point the /24 scan at the wrong subnet entirely. So the
scan target is built from each network interface's own address (SIOCGIFADDR)
rather than from a routing decision; see `_local_networks`.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import fcntl  # POSIX-only; guarded so importing this module (and
                  # therefore core.py/tonearmd) never hard-fails on a
                  # platform without it. Only `_iface_ipv4` needs it.
except ImportError:  # pragma: no cover - not exercised on Linux, the only
                      # platform this daemon ships on
    fcntl = None

SOOD_PORT = 9003
SOOD_MULTICAST = "239.255.90.90"
SERVICE_ID = "00720724-5143-4a9b-abac-0e50cba674bb"
DEFAULT_TCP_PORT = 9150
DEFAULT_HTTP_PORT = 9330


def _tlv(key: str, value: str) -> bytes:
    kb, vb = key.encode(), value.encode()
    return bytes([len(kb)]) + kb + struct.pack(">H", len(vb)) + vb


def build_query() -> bytes:
    return b"SOOD" + b"\x02" + b"Q" + _tlv("query_service_id", SERVICE_ID)


def parse(buf: bytes) -> dict | None:
    """Decode a SOOD frame. Returns None for anything that is not one.

    Never raises on a malformed body: callers treat an exception as "no Core",
    so a single bad byte would otherwise discard a real response.
    """
    if len(buf) < 6 or buf[:4] != b"SOOD":
        return None
    out: dict[str, str] = {}
    i = 6  # "SOOD" + version byte + type byte
    while i < len(buf):
        klen = buf[i]
        i += 1
        if i + klen > len(buf):
            break
        key = buf[i:i + klen].decode("utf-8", "replace")
        i += klen
        if i + 2 > len(buf):
            break
        (vlen,) = struct.unpack(">H", buf[i:i + 2])
        i += 2
        if i + vlen > len(buf):
            break
        out[key] = buf[i:i + vlen].decode("utf-8", "replace")
        i += vlen
    return out


def _int(raw: dict, key: str, default: int) -> int:
    try:
        return int(raw.get(key, default))
    except (TypeError, ValueError):
        return default


def to_core(host: str, raw: dict, via: str) -> dict:
    return {
        "host": host,
        "name": raw.get("name", host),
        "tcp_port": _int(raw, "tcp_port", DEFAULT_TCP_PORT),
        "http_port": _int(raw, "http_port", DEFAULT_HTTP_PORT),
        "unique_id": raw.get("unique_id", ""),
        "display_version": raw.get("display_version", ""),
        "via": via,
    }


def _probe_unicast(host: str, timeout: float = 1.5) -> dict | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(build_query(), (host, SOOD_PORT))
        buf, _ = sock.recvfrom(4096)
    except OSError:
        return None
    finally:
        sock.close()
    raw = parse(buf)
    return to_core(host, raw, via="scan") if raw else None


def _local_ipv4() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1; no packet is sent
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


_SIOCGIFADDR = 0x8915
_IGNORED_IFACE_PREFIXES = ("lo", "tailscale", "docker", "br-", "veth")


def _iface_ipv4(name: str) -> str | None:
    """The IPv4 address bound to network interface `name`, via SIOCGIFADDR.

    Deliberately not a routing decision (unlike `_local_ipv4`'s connect
    trick): this machine runs Tailscale as an exit node, whose policy routes
    (a higher-priority `ip rule` pointing non-tailnet destinations at
    tailscale0) hijack the connect trick and report a Tailscale CGNAT address
    instead of the real LAN address. Asking each interface directly for its
    own address sidesteps routing-table policy entirely.
    """
    if fcntl is None:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", name[:15].encode())
        res = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, packed)
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def _local_networks() -> list[ipaddress.IPv4Network]:
    """Local /24s to scan, one per non-virtual interface with a PRIVATE IPv4 address.

    The private check is deliberate, not incidental: on a network handing
    out globally-routable addresses (a hotel, a campus, a bridged VM, a VPS)
    an interface can carry a public address, and scanning "whatever /24 the
    interface is on" would then port-scan 254 hosts on the public internet
    that never asked for it. Restricting to `is_private` keeps this to the
    address space it is actually meant for. It also structurally excludes
    100.64.0.0/10 (carrier-grade NAT, e.g. Tailscale), closing that hazard
    for any interface `_iface_ipv4` sees that the name-prefix ignore list
    above does not catch -- `is_private` does not depend on interface naming
    at all.
    """
    nets: list[ipaddress.IPv4Network] = []
    try:
        names = [name for _, name in socket.if_nameindex()]
    except (AttributeError, OSError):
        names = []
    for name in names:
        if name.startswith(_IGNORED_IFACE_PREFIXES):
            continue
        addr = _iface_ipv4(name)
        if addr:
            net = ipaddress.ip_network(addr + "/24", strict=False)
            if net.is_private:
                nets.append(net)
    return nets


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def discover(timeout: float = 6.0) -> list[dict]:
    """Multicast first; on silence, scan the /24 and unicast-probe the hits."""
    found: dict[str, dict] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(1.0)
    query = build_query()
    for dest in (SOOD_MULTICAST, "255.255.255.255"):
        for _ in range(3):
            try:
                sock.sendto(query, (dest, SOOD_PORT))
            except OSError:
                pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            buf, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        raw = parse(buf)
        if raw and addr[0] not in found:
            found[addr[0]] = to_core(addr[0], raw, via="multicast")
    sock.close()

    if found:
        return list(found.values())

    nets = _local_networks()
    if not nets:
        local = _local_ipv4()
        if local:
            # Same private-space restriction as _local_networks(), and for
            # the same reason: `_local_ipv4`'s connect-trick fallback can
            # return a globally-routable address (or, on this machine, the
            # Tailscale CGNAT one -- see the module docstring), and either
            # would otherwise send an unsolicited /24 scan outside the LAN.
            net = ipaddress.ip_network(local + "/24", strict=False)
            if net.is_private:
                nets = [net]
    hosts = [str(h) for net in nets for h in net.hosts()]
    with ThreadPoolExecutor(max_workers=128) as pool:
        candidates = [
            host for host, is_open
            in zip(hosts, pool.map(lambda h: _port_open(h, DEFAULT_HTTP_PORT), hosts))
            if is_open
        ]
    with ThreadPoolExecutor(max_workers=16) as pool:
        for core in pool.map(_probe_unicast, candidates):
            if core:
                found[core["host"]] = core
    return list(found.values())
