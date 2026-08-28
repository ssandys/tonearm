"""Daemon-side configuration: Core host, ports, pairing token, pinned zone.

The daemon owns the pin. The widget sends `zone pin <id>` and renders whatever
comes back; it never stores the pin. Splitting that across shell.json would put
arbitration in two processes, and MPRIS -- which cannot read plugin settings --
would then follow a different zone than the bar.
"""

from __future__ import annotations

import json
import os

CONFIG_PATH = ""
TOKEN_PATH = ""

DEFAULTS = {
    "host": None,
    "tcp_port": 9150,
    "http_port": 9330,
    "name": None,
    "pinned_zone_id": None,
}


def reset_paths() -> None:
    """Recompute paths from the environment. Called at import and by tests."""
    global CONFIG_PATH, TOKEN_PATH
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    root = os.path.join(base, "tonearm")
    CONFIG_PATH = os.path.join(root, "config.json")
    TOKEN_PATH = os.path.join(root, "token")


reset_paths()


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        # Absent or corrupt: defaults. Raising here would make systemd restart
        # the daemon into the same failure indefinitely.
        pass
    return cfg


def _write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)   # atomic within a filesystem


def save(cfg: dict) -> None:
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    _write_private(CONFIG_PATH, json.dumps(merged, indent=2) + "\n")


def load_token() -> str | None:
    try:
        with open(TOKEN_PATH) as handle:
            token = handle.read().strip()
        return token or None
    except OSError:
        return None


def save_token(token: str) -> None:
    _write_private(TOKEN_PATH, token)
