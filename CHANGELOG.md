# Changelog

Notable changes to tonearm. Versions follow [semantic versioning](https://semver.org);
while the major version is 0, the minor version carries changes that would
otherwise be breaking.

## 0.10.0 — 2026-09-02

A security-hardening release. No new features.

**Upgrading:** re-run `./setup.sh`. It installs a sandboxed systemd unit and
creates `~/.config/tonearm`, which that unit now requires to exist before it
will start. Skipping this leaves the old unit in place; the daemon still runs,
without the sandbox.

### Fixed — marketplace security review

Four findings from the review of `d4a3513` on the Omarchy plugin marketplace
([#3414](https://github.com/HANCORE-linux/omarchy-plugin-marketplace/issues/3414)).

- **State I/O is descriptor-relative, bounded and no-follow.** `~/.config/tonearm`
  is opened once `O_DIRECTORY|O_NOFOLLOW` and every leaf is opened against that
  descriptor, so the path walk happens once. Reads add `O_NOFOLLOW`, an
  `S_ISREG` check and a size cap (64 KiB config, 4 KiB token); writes land on an
  unguessable name created `O_EXCL|O_NOFOLLOW`. The predictable `<path>.tmp`,
  opened `O_CREAT|O_TRUNC`, had let a planted symlink redirect the write of the
  Roon pairing token. The state directory is also `fchmod`ed back to 0700, which
  `makedirs(mode=…)` never did for an existing one.
- **Every socket resource is bounded.** Handler threads (32), time to send a
  request (10s), registered subscribers (16), one write to a subscriber (5s),
  and in-memory browse sessions (8, LRU). A client that connected and never
  sent a newline previously parked a handler thread forever.
- **No I/O runs under the subscriber lock.** `broadcast()` copies the list under
  the lock and writes outside it, with a per-connection write lock preserving
  ordering. One peer that had stopped reading used to stall every other
  subscriber and every new subscribe.
- **The album-art fetch is pinned to the Core's origin and validated.**
  Redirects off `(scheme, host, port)` are refused; bodies must be a PNG or
  JPEG within 2048px per side. A 1 MiB byte cap cannot see a decode bomb, and
  the file is read by `ColorQuantizer` inside the shared shell process.
- **`is_publishable()` answers from the descriptor it opened**, not an
  `os.lstat` of a path it never opened.
- **The discovery scan has a deduplicated total budget** of 512 addresses
  across all interfaces, replacing 254 per qualifying interface with no ceiling.

### Fixed — found while auditing for the same shapes

- **The systemd unit is sandboxed.** `ProtectSystem=strict`,
  `ProtectHome=read-only` with a single `ReadWritePaths`, a syscall filter and a
  restricted address-family set. Verified against a running daemon: writes to
  `~/.bashrc` and to the source tree both fail `EROFS` while Roon, MPRIS and
  album art continue to work. `AF_NETLINK` is required and documented in the
  unit — glibc's `if_nameindex(3)` needs it, and without it discovery silently
  finds nothing.
- **A dead socket server is now a dead process.** It ran on a daemon thread, so
  an exception killed the thread and nothing else: systemd reported the unit
  active with 0 restarts and no socket on disk, and nothing ever retried.
  `Restart=on-failure` can now do its job. A clean `systemctl --user stop` still
  exits 0.
- **`shutdown()` reaches a blocked `accept()`.** Closing the listening socket
  does not interrupt a thread already in `accept()`; the loop now polls.
- **Browse sessions are bounded and their key is validated.** `session` came
  off the wire into a dict nothing evicted, and was not required to be a string.
- **The zone pin argument is bounded.** It is the only wire argument written to
  `config.json`; a large enough value would push that file past its own read cap,
  so the next start would refuse the whole config and re-run discovery.
- **`search` terms are bounded** at 512 characters.
- **A non-object JSON request is refused** instead of raising `AttributeError`
  in a handler thread, which left the socket open and the client waiting.
- **`Model.js` percent-encodes `image_key`.** The daemon already treated it as
  untrusted; the widget built the same URL by concatenation, so a key carrying
  `&` or `?` rewrote the query and one carrying `/` walked the path.

### Changed

- `setup.sh` creates `~/.config/tonearm` at 0700. Under `ProtectHome=read-only`
  the daemon can no longer create it, and systemd refuses to start a unit whose
  `ReadWritePaths` names a missing directory. A test parses the shipped unit and
  asserts every entry is a directory the installer creates.
- The socket answers `bad_request` for a malformed `session` or `term`.
- `README.md` and `docs/marketplace-submission.md` state the scan budget and the
  full set of socket bounds.

## 0.9.0 — 2026-08-29

First submission to the Omarchy plugin marketplace. Roon now-playing,
transport, library search, zone switching and transfer.
