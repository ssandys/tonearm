# Marketplace submission

**The canonical submission text is the issue itself**, not this file:
[omacom/omarchy-plugin-marketplace#3414](https://github.com/omacom/omarchy-plugin-marketplace/issues/3414).
It is edited in place as the review proceeds, and editing it is what
re-runs validation and the security baseline against the current commit.

This file used to hold a copy of that text, field by field, including the
issue form's own scaffolding. The copy went stale — it described the
pre-0.10.0 design, contradicted the shipped code on a point a reviewer had
specifically raised, and quoted test counts that were two releases old.
Keeping two versions of one document in step was never going to work, so it
no longer tries.

What remains is the part that belongs to the repository rather than to the
submission: the standing claims about what tonearm does on your network and
on your disk. These are also stated in `README.md` under "What it does on
your network", for users rather than reviewers.

## Network

Discovery runs on first run only, if no Core is configured: SOOD multicast
first, then — because many networks filter multicast — a TCP connection to
one port (9330) on each address in the local `/24`. The scan is restricted
to interfaces whose own IPv4 address is `ipaddress.is_private`, and the
subnet comes from the interfaces themselves rather than a routing lookup,
which a VPN would otherwise poison. A total budget of `MAX_SCAN_HOSTS`
addresses is enforced across every interface, deduplicated, walking networks
in address order so a truncated scan is deterministic.

If the Core stops answering, tonearm re-runs discovery to find it at a new
address — multicast only, never the `/24` sweep, because that is a first-run
cost and not something to repeat while a Core is switched off. It moves only
to a Core it can identify as the one it paired with, and writes an address
to disk only after a Core has answered on it.

After that the only traffic is to the Core: one WebSocket for Roon's MOO
protocol, and HTTP GETs for album art through an opener whose redirect
handler compares `(scheme, host, port)` and refuses anything that leaves the
Core. No telemetry, no third-party endpoints.

## Files written

`~/.config/tonearm/` (0700) holds the Core address, the Roon pairing token
and the pinned zone. All state I/O is descriptor-relative: the directory is
opened once `O_DIRECTORY|O_NOFOLLOW` and every leaf is opened against that
descriptor, so the path walk happens once and a component swapped afterwards
cannot redirect a later open. Reads add `O_NOFOLLOW`, an `S_ISREG` check and
a size cap; writes go to an unguessable name created `O_EXCL|O_NOFOLLOW` and
are renamed with both ends resolved against the same descriptor.

`$XDG_RUNTIME_DIR/tonearm/` (0700) holds the socket and the art cache,
capped at `MAX_CACHED` files. No user configuration outside tonearm's own
directories is modified.

## Privilege and sandboxing

There is no `sudo` and no `pkexec` anywhere in the repository — code, scripts
or documentation. `setup.sh` uses `systemctl --user` and writes only under
`~/.config/systemd/user/`, refusing to install through a symlink, over a
non-regular file, or over a regular file that is not tonearm's own unit.

The unit runs under `ProtectSystem=strict` and `ProtectHome=read-only` with
a single `ReadWritePaths`, `NoNewPrivileges`, `ProtectProc=invisible`, a
syscall filter and a restricted address-family set. `AF_NETLINK` is required
and documented in the unit: `if_nameindex(3)` needs it, and without it
discovery silently finds nothing.

## What the shared shell process consumes

The widget runs inside `omarchy-shell`, so everything it reads is bounded
producer-side. `art.is_publishable()` opens the cached thumbnail
`O_NOFOLLOW|O_NONBLOCK` and answers every question from that descriptor;
fetched art is refused unless it is a PNG or JPEG within `MAX_ART_BYTES` and
`MAX_ART_DIMENSION` per side, read from the PNG `IHDR` or by walking JPEG
segments — decoding nothing. Text from the Core is bounded by `MAX_TEXT`
and the zone listing by `MAX_ZONES`. The socket bounds every resource a
client can reach: request line, connection deadline, handler threads,
subscribers, per-write deadline, browse sessions, session key and search
term.

One residual is stated rather than papered over: the shell re-opens
`art_path` by name, so a same-user race between the check and Qt's open
survives. Closing it needs an open-time guarantee from the reader, and the
reader is `ColorQuantizer`, which exposes none.

## Dependencies

Two Arch packages the user installs themselves, `python-dbus-next` and
`python-websocket-client`; neither is bundled. `scripts/vendor/roonapi/` is
a vendored, unmodified copy of `roonapi` 0.1.6 (Apache-2.0) with its licence
retained in place and the reason for vendoring in `scripts/vendor/README.md`.
tonearm's own code is MIT; `LICENSE` carries the dependency breakdown below
the grant.

## Tests

`./bin/test` runs the Python and JS suites; CI runs it on every push.
Counts are deliberately not quoted here — they were wrong in this file twice.

## Agent instructions

The repository ships no `AGENTS.md`, `CLAUDE.md`, `.cursorrules` or any
sibling. Installing a plugin clones the repository into
`~/.config/omarchy/plugins/<id>/`, so such a file would be discovered and
applied by coding agents working there — an instruction channel the user
never opted into, independent of what it says. The contributor guide is
`CONTRIBUTING.md`, and `tests/python/test_installable_tree.py` asserts the
old names cannot return, asking `git ls-files` because that is exactly what
a clone delivers.
