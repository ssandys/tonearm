"""Unix-socket fan-out. One line of JSON per state change; one line in per command."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading

LOG = logging.getLogger("tonearmd.server")


def runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or ("/run/user/%d" % os.getuid())
    return os.path.join(base, "tonearm")


def socket_path() -> str:
    return os.path.join(runtime_dir(), "sock")


class Server:
    def __init__(self, session) -> None:
        self._session = session
        self._subscribers: list[socket.socket] = []
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._running = False

    def serve_forever(self) -> None:
        path = socket_path()
        os.makedirs(runtime_dir(), mode=0o700, exist_ok=True)
        # A SIGKILL leaves the node behind; bind() would then fail forever.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        os.chmod(path, 0o600)
        self._sock.listen(16)
        self._running = True
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def shutdown(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            os.unlink(socket_path())
        except OSError:
            pass
        with self._lock:
            for sub in self._subscribers:
                try:
                    sub.close()
                except OSError:
                    pass
            self._subscribers = []

    def _handle(self, conn: socket.socket) -> None:
        try:
            line = conn.makefile("r").readline()
            request = json.loads(line)
        except (ValueError, OSError):
            # Malformed input closes that one connection and nothing else.
            LOG.warning("dropping malformed request")
            conn.close()
            return

        cmd = request.get("cmd")
        if cmd == "subscribe":
            # Send current state at once: for a paused zone the next Roon event
            # may never come, and the widget would render nothing until it did.
            try:
                conn.sendall((json.dumps(self._session.snapshot()) + "\n").encode())
            except OSError:
                conn.close()
                return
            with self._lock:
                self._subscribers.append(conn)
            return

        try:
            self._session.command(cmd, request.get("arg"))
        except Exception:
            LOG.exception("command %r failed", cmd)
        conn.close()

    def broadcast(self, payload: dict) -> None:
        blob = (json.dumps(payload) + "\n").encode()
        with self._lock:
            alive = []
            for sub in self._subscribers:
                try:
                    sub.sendall(blob)
                    alive.append(sub)
                except OSError:
                    # A hot-reload closes the relay mid-broadcast. Drop it and
                    # keep going; one dead peer must not stall the rest.
                    try:
                        sub.close()
                    except OSError:
                        pass
            self._subscribers = alive
