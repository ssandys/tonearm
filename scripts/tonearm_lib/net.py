"""Is the local network reachable at all?

`start()` and the connection watcher both end up at the same place when the
Core does not answer: a connect that times out. That single symptom covers
two unrelated situations, and they need different things from the person
reading the bar:

  * the Core is switched off, asleep, or not running Roon -- "Roon Core
    unreachable" is the right thing to say, and turning it on fixes it;
  * this machine has no working path to the LAN -- a VPN swallowing the
    local subnet, wifi associated but not routing, a downed link. Nothing
    about the Core is wrong and turning it on changes nothing.

Measured on 2026-09-07: a Tailscale exit node captured 192.168.50.0/24 and
every connect to the Core timed out for five hours while the Core sat
healthy 1.9ms away. The daemon reported "Roon Core unreachable" the entire
time, which is what sent the search to the wrong end of the problem.

The discriminator was the default gateway: probe it, and call the network
down if it did not answer. That was wrong often enough to matter, and CI
proved it unprompted -- eleven tests failed on a GitHub runner because its
gateway ignores tcp/80, while its network was perfect. Plenty of healthy
gateways DROP rather than reject on their LAN side, and such a network
could only ever report a fault. Before, the daemon always blamed the Core;
after, it could always blame the network (#11).

What replaced it asks the kernel which source address it would use to
reach the Core -- a connected UDP socket, which sends nothing and only
consults the routing table. A fault is claimed ONLY when it is positively
established: the Core is on a subnet this machine is directly on, and the
kernel is nonetheless leaving by a different one. That is the 2026-09-07
condition observed as a routing fact rather than inferred from whether
some third party answers a port.

Everything else stays unknown, and callers keep the weaker wording.
Telling someone their network is down when it is not is a worse error than
the one this fixes, so the tempting wider rule -- "is the source outside
the Core's own /24?" -- is deliberately not used: a Core reached through a
router legitimately has one.

Verified inside the unit's own sandbox on 2026-09-07: /proc/net/route is
readable under ProtectProc=invisible, and the probe runs under
SystemCallFilter=~@privileged @resources.
"""

import errno
import ipaddress
import logging
import socket
import struct

LOG = logging.getLogger("tonearmd.net")

# Any port answers the question. Both a completed handshake and a refusal
# prove the gateway is there, so this is not chosen for what listens on it.
PROBE_PORT = 80

# The probe sits on the failure path, in front of a status the user is
# already waiting on. Long enough to cross a LAN many times over.
PROBE_TIMEOUT = 2.0

_RTF_GATEWAY = 0x0002


def _read_proc_route() -> str:
    with open("/proc/net/route", "r") as handle:
        return handle.read()


def default_gateway() -> str | None:
    """The next hop of the main table's default route, or None if there is none.

    None is a real answer -- an ethernet cable pulled out leaves no default
    route at all -- and is not the same as "unreachable".
    """
    try:
        text = _read_proc_route()
    except Exception:
        LOG.exception("could not read /proc/net/route")
        return None

    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            packed = int(fields[2], 16)
        except ValueError:
            continue
        # A default route with no next hop (a point-to-point tunnel) has no
        # address to probe; reading its zero gateway as 0.0.0.0 would.
        if not flags & _RTF_GATEWAY or not packed:
            continue
        return socket.inet_ntoa(struct.pack("<L", packed))
    return None


def _tcp_probe(address, timeout) -> None:
    """Connect and drop it. Raises exactly what the connect raised."""
    sock = socket.socket()
    try:
        sock.settimeout(timeout)
        sock.connect(address)
    finally:
        sock.close()


def local_networks() -> list:
    """The subnets this machine is directly on, from the main routing table.

    Read out of /proc/net/route rather than enumerated from interfaces: that
    file is already read here, is already verified readable inside the unit's
    sandbox, and carries the REAL prefix instead of an assumed /24.

    The default route is skipped. 0.0.0.0/0 contains every address, so letting
    it through would make every Core look on-link and the test vacuous.
    """
    try:
        text = _read_proc_route()
    except Exception:
        LOG.exception("could not read /proc/net/route")
        return []
    nets = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            dest = int(fields[1], 16)
            mask = int(fields[7], 16)
        except ValueError:
            continue
        if not mask:                      # the default route
            continue
        if mask == 0xFFFFFFFF:
            # A /32 host route says how to reach ONE address, not that this
            # machine shares a subnet with it. Counting it as a subnet is not
            # merely imprecise: a source address can never be inside a /32
            # naming something else, so a Core reachable only by a host route
            # would be reported as a routing fault every time. Seen on a live
            # table as a /32 for the default gateway.
            continue
        try:
            net_addr = socket.inet_ntoa(struct.pack("<L", dest))
            netmask = socket.inet_ntoa(struct.pack("<L", mask))
            entry = ipaddress.ip_network("%s/%s" % (net_addr, netmask),
                                         strict=False)
        except (OSError, ValueError):
            continue
        if entry not in nets:
            nets.append(entry)
    return nets


def source_address_for(host: str) -> str | None:
    """Which local address the kernel would send to `host` from.

    A connected UDP socket sends nothing -- it only asks the routing table to
    pick a source -- so this is instant, needs no privileges, and cannot be
    confused by whether anything is listening. Never raises: every caller is
    already handling a failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 9))
        return sock.getsockname()[0]
    except OSError as exc:
        LOG.warning("no source address for %s: %s", host, exc)
        return None
    except Exception:
        LOG.exception("source address lookup failed unexpectedly")
        return None
    finally:
        sock.close()


def routed_off_lan(host, source, networks) -> bool | None:
    """Is Core traffic leaving by an interface that cannot reach its LAN?

    True only when that is POSITIVELY established: the Core sits on a subnet
    this machine is directly on, and the kernel is nonetheless leaving by a
    different one. That is the 2026-09-07 condition -- a tunnel swallowing
    192.168.50.0/24 while the Core sat 1.9ms away -- observed as a routing
    fact rather than inferred from whether a gateway answers TCP.

    Everything else is None, and callers keep the older, weaker wording.
    The tempting wider rule -- "is the source outside the Core's /24?" -- is
    wrong: a Core reached through a router, on another VLAN or a wired
    segment, legitimately has a source address outside its own subnet, and
    calling that a network fault is the same false message this replaces,
    merely inverted. Saying "your network is down" when it is not is worse
    than saying nothing.
    """
    if source is None or not networks:
        return None
    try:
        core_ip = ipaddress.ip_address(str(host))
        src_ip = ipaddress.ip_address(str(source))
    except ValueError:
        return None                       # a hostname, or something stranger
    parsed = []
    for entry in networks:
        try:
            parsed.append(entry if hasattr(entry, "network_address")
                          else ipaddress.ip_network(str(entry), strict=False))
        except ValueError:
            continue
    if not parsed:
        return None
    owning = [n for n in parsed if core_ip in n]
    if not owning:
        return None                       # not on a subnet we hold: unknowable
    return not any(src_ip in n for n in owning)
