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

The discriminator is the default gateway. It is the one LAN address that is
always present, and it is read from /proc/net/route, which lists the MAIN
routing table only -- so it still names the physical gateway even while
policy routing diverts traffic elsewhere. The probe that follows goes
through the live rules, so it fails exactly when real traffic would.

A connection REFUSED counts as reachable: it proves a host answered. Only
silence and explicit unreachability count against the network.

Known limit: a gateway that DROPs rather than rejects on its LAN side is
indistinguishable from an unreachable one, and a Core switched off behind
it would be reported as a network fault. That is why an unknown answer
stays unknown -- callers fall back to the old wording rather than guess.

Verified inside the unit's own sandbox on 2026-09-07: /proc/net/route is
readable under ProtectProc=invisible, and the probe runs under
SystemCallFilter=~@privileged @resources.
"""

import errno
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


def lan_reachable() -> bool | None:
    """True if the gateway answered, False if it did not, None if unknown.

    Never raises: every caller is already handling a failure and must not
    acquire a second one from the diagnosis of the first.
    """
    gateway = default_gateway()
    if gateway is None:
        return None

    try:
        _tcp_probe((gateway, PROBE_PORT), PROBE_TIMEOUT)
        return True
    except ConnectionRefusedError:
        return True
    except (socket.timeout, TimeoutError):
        LOG.warning("gateway %s did not answer within %.0fs",
                    gateway, PROBE_TIMEOUT)
        return False
    except OSError as exc:
        if exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            LOG.warning("no route to gateway %s: %s", gateway, exc)
            return False
        LOG.warning("gateway %s probe was inconclusive: %s", gateway, exc)
        return None
    except Exception:
        LOG.exception("gateway probe failed unexpectedly")
        return None
