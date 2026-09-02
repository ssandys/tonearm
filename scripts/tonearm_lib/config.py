"""Daemon-side configuration: Core host, ports, pairing token, pinned zone.

The daemon owns the pin. The widget sends `zone pin <id>` and renders whatever
comes back; it never stores the pin. Splitting that across shell.json would put
arbitration in two processes, and MPRIS -- which cannot read plugin settings --
would then follow a different zone than the bar.

All I/O here is DESCRIPTOR-RELATIVE. The state directory is opened once and
every leaf below it is opened against that descriptor (`dir_fd=`), so the path
walk happens exactly once and a component swapped afterwards cannot redirect a
later open. Reads are bounded and refuse a symlink or a non-regular file;
writes land on an unguessable temp name created O_EXCL|O_NOFOLLOW. The pairing
token lives here, which is why this file is stricter than its size suggests.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import stat

LOG = logging.getLogger("tonearmd.config")

CONFIG_NAME = "config.json"
TOKEN_NAME = "token"

CONFIG_ROOT = ""
CONFIG_PATH = ""
TOKEN_PATH = ""

# Ceilings on what the daemon will read back out of its own state directory.
# Neither file is user-authored: config.json is five scalars this module
# writes, and the token is a Roon pairing string. Anything past these is not
# a tonearm file, and buffering it to find that out is the bug.
MAX_CONFIG_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4 * 1024

DEFAULTS = {
    "host": None,
    "tcp_port": 9150,
    "http_port": 9330,
    "name": None,
    "pinned_zone_id": None,
}


def reset_paths() -> None:
    """Recompute paths from the environment. Called at import and by tests."""
    global CONFIG_ROOT, CONFIG_PATH, TOKEN_PATH
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    CONFIG_ROOT = os.path.join(base, "tonearm")
    CONFIG_PATH = os.path.join(CONFIG_ROOT, CONFIG_NAME)
    TOKEN_PATH = os.path.join(CONFIG_ROOT, TOKEN_NAME)


reset_paths()


def _open_root(create: bool = False) -> int:
    """Open the state directory itself, returning a descriptor to work through.

    `O_NOFOLLOW` refuses a symlinked `~/.config/tonearm` outright rather than
    following it somewhere else, and every leaf open below passes this
    descriptor as `dir_fd=` -- an openat(2) resolved against the directory
    already held open, not a fresh walk from `/` that could resolve
    differently the second time.

    The caller closes the descriptor.
    """
    if create:
        os.makedirs(CONFIG_ROOT, mode=0o700, exist_ok=True)
    fd = os.open(CONFIG_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if create:
        # makedirs' mode applies only when it CREATES the directory. A
        # pre-existing ~/.config/tonearm at 0755 -- restored from a backup,
        # or made by hand -- leaves the pairing token readable by every
        # account on the machine no matter what mode the file itself carries.
        # fchmod on the descriptor already held, so this can never be aimed
        # at a directory other than the one being written.
        try:
            os.fchmod(fd, 0o700)
        except OSError:
            LOG.warning("could not tighten %s to 0700", CONFIG_ROOT)
    return fd


def _close(*fds: int | None) -> None:
    for fd in fds:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _read_private(name: str, limit: int) -> str | None:
    """Read `name` out of the state directory, or None if it cannot be trusted.

    None covers every refusal -- absent, symlinked, not a regular file, or
    larger than `limit` -- because every caller's answer is the same: fall
    back to the default. Raising instead would make systemd restart the
    daemon into the same failure indefinitely.
    """
    root = None
    fd = None
    try:
        root = _open_root()
        # O_NONBLOCK so a FIFO planted at this name fails the S_ISREG check
        # below rather than blocking the open until someone writes to it --
        # which, on the startup config read, is the daemon hanging forever.
        # It is a no-op for the regular file this expects to find.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=root)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            LOG.warning("refusing %s: not a regular file", name)
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = None                       # the file object owns it now
            # One byte past the cap, so "at the limit" and "over it" are
            # distinguishable without reading the rest of a huge file.
            data = handle.read(limit + 1)
        if len(data) > limit:
            LOG.warning("refusing %s: larger than %d bytes", name, limit)
            return None
        return data.decode("utf-8", "replace")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            LOG.warning("refusing %s: it is a symlink", name)
        return None
    finally:
        _close(fd, root)


def _write_private(name: str, text: str) -> None:
    """Atomically replace `name` in the state directory with `text`, at 0600.

    The temp name carries 64 bits of entropy and is created O_EXCL|O_NOFOLLOW.
    The predictable `<name>.tmp` this replaced was opened O_CREAT|O_TRUNC, so
    anything able to plant a symlink at that one name redirected the daemon's
    write -- of the pairing token, among other things -- to its target.
    """
    root = _open_root(create=True)
    tmp = ".%s.%s.tmp" % (name, secrets.token_hex(8))
    placed = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=root)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        # Atomic within a filesystem, and both ends resolved against the same
        # descriptor, so neither can be redirected between create and rename.
        os.replace(tmp, name, src_dir_fd=root, dst_dir_fd=root)
        placed = True
    finally:
        if not placed:
            try:
                os.unlink(tmp, dir_fd=root)
            except OSError:
                pass
        _close(root)


def load() -> dict:
    cfg = dict(DEFAULTS)
    raw = _read_private(CONFIG_NAME, MAX_CONFIG_BYTES)
    if raw is None:
        return cfg
    try:
        stored = json.loads(raw)
    except ValueError:
        # Corrupt: defaults, for the same reason absent means defaults.
        return cfg
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return cfg


def save(cfg: dict) -> None:
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    _write_private(CONFIG_NAME, json.dumps(merged, indent=2) + "\n")


def load_token() -> str | None:
    raw = _read_private(TOKEN_NAME, MAX_TOKEN_BYTES)
    if raw is None:
        return None
    return raw.strip() or None


def save_token(token: str) -> None:
    _write_private(TOKEN_NAME, token)
