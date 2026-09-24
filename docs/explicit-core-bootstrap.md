# Explicit Core bootstrap design

## Problem

Tonearm's first start depends on SOOD discovery. Some same-LAN systems can
reach the Core's MOO ports but do not receive a discovery answer, so the daemon
exits before Roon can show its extension authorization request.

## Interface

`setup.sh` remains the public setup entry point and accepts:

```text
setup.sh --core HOST [--http-port PORT] [--tcp-port PORT]
```

`HOST` may be an IPv4 literal or DNS hostname. Raw IPv6 literals are refused
because Tonearm's current Core and artwork URL builders do not bracket them.
The advertised defaults remain HTTP/MOO `9330` and TCP `9150`. Port options
are valid only with `--core`. The shell parses option structure, then delegates
host/port validation and persistence to `tonearm_lib.config`; it does not write
JSON itself.

The config helper updates only `host`, `http_port` and `tcp_port` through the
existing descriptor-relative atomic writer. It preserves the pairing token,
pin and learned Core identity. Repeating bootstrap for the same host is safe,
including a correction to its ports. A different already-configured host is
refused because Core switching and token ownership need a separate design.
Consequently, `--core` cannot repair a configured Core that moved to a new
address, and it deliberately preserves a stale or incorrect `unique_id`. Those
cases require the separate re-pair/reset path; users should not edit the private
config to force bootstrap past either guard.

## Runtime behavior

`RoonSession.start()` already skips initial discovery whenever config has a
host, so no daemon change is required. Connection success, relocation lookup,
pairing and persistence keep their existing behavior.

## Non-goals

This change does not alter discovery, retry cadence, relocation/re-pair
behavior, service restart policy, uninstall, firewall/routes, Roon Bridge, zone
selection or playback.
