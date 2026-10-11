# Vendored dependencies

## roonapi 0.1.6

Source: https://github.com/pavoni/pyroon (PyPI `roonapi`)
License: Apache-2.0 — see `roonapi/LICENSE`. Retained per §4 of the licence.

Vendored rather than packaged because Arch ships no `python-roonapi`, and the
AUR package is 0.1.4 — behind upstream, zero votes, untouched since May 2023.
Vendoring pins the version and needs no network at install time.

Its declared dependencies (`requests`, `six`, `ifaddr`) are over-declared: the
source files import `websocket` as a hard dependency, and conditionally
`simplejson` (which falls back to stdlib `json` when absent, adding no
installation requirement). System Python already has both `websocket` and `json`.

To refresh: bump the version, repeat the copy, re-run `./bin/test`, check
`RoonApi.__init__`'s signature has not changed, and **re-apply the local
patches below** — a plain copy drops them. **Also delete the SOOD discovery
files** (`roonapi/discovery.py`, `roonapi/soodmessage.py`,
`roonapi/.soodmsg`) and strip the `RoonDiscovery` import from
`roonapi/__init__.py`; they are dead code (see tonearm#39) and a refresh
would otherwise bring them back.

## Local patches

Marked in the source with `LOCAL PATCH (tonearm #13)` and pinned by
`tests/python/test_vendor_patches.py`, so a refresh that loses them fails the
suite rather than failing a user. Each is worth re-checking against upstream
on a bump: if 0.1.7 has fixed it, drop the divergence and the test with it.

`roonapi/roonapi.py`, `_on_state_change` ([#13](https://github.com/ssandys/tonearm/issues/13)):

- The `zones` and `outputs` subscription payloads are FULL snapshots, but
  upstream handles them in the same branch as the incremental `*_changed` and
  `*_added` events: it updates or inserts every id received and never removes
  one that is absent. A zone that disappeared while the socket was down
  therefore survived the reconnect and stayed in the bar's picker until the
  daemon restarted, while Roon itself no longer listed it. Snapshots now drop
  what they omit; increments still only merge.
- `zones_removed` and `outputs_removed` deleted without appending an event, so
  no state callback fired and a removal reached consumers only when some later,
  unrelated event happened to be published. They now report the change.
- Those same branches used a bare `del`, which raises `KeyError` inside a
  websocket callback when a removal races a snapshot that already dropped the
  entry. They now `pop`.

**SOOD discovery files removed** ([#39](https://github.com/ssandys/tonearm/issues/39)):

- `roonapi/discovery.py`, `roonapi/soodmessage.py`, and `roonapi/.soodmsg` were
  deleted; `roonapi/__init__.py` no longer imports `RoonDiscovery`.  The SOOD
  multicast discovery in these files was never instantiated — `tonearm_lib/sood.py`
  replaced it with a unicast-probing approach.  The deleted files had unfixed
  bugs: `FormatException.__init__` calls `Exception.__init__()` without `self`
  (always raises `TypeError`), `_parse_type` raises `IndexError` on a bare
  `SOOD\x02` datagram, and `_parse_property` misaligns on multi-byte UTF-8.
  `tests/python/test_vendor.py:test_no_sood_implementation` asserts the files
  stay removed after a vendor refresh.
