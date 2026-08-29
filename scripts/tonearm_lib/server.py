"""Unix-socket fan-out. One line of JSON per state change; one line in per command."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading

from . import browse

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
            #
            # The reply and the subscriber registration happen under the SAME
            # lock broadcast() takes, and as one atomic unit -- not "send,
            # then separately register". Splitting them left a gap: a
            # broadcast landing between "reply sent" and "conn appended"
            # would iterate self._subscribers without this conn in it yet,
            # silently dropping the new subscriber's first update (and, since
            # broadcast()'s own sendall()s would otherwise be unsynchronized
            # with this one, risking two threads writing to the same socket
            # at once). Locking across both closes both: broadcast() cannot
            # run at all until this conn is either fully registered or the
            # handshake has failed and closed it.
            #
            # `sendall()` under this lock (here and in broadcast()) is an
            # accepted, deliberate trade-off -- see this class's module
            # docstring -- not an oversight. Worth noting separately: this
            # handshake is the LONGER of the two holds on self._lock, not the
            # shorter one. broadcast()'s sendall() writes bytes that are
            # already fully serialized before the lock is taken. Here,
            # `self._session.snapshot()` runs INSIDE the lock: zone
            # arbitration and (via CachingSession) an art-cache lookup, THEN
            # serialization, THEN the sendall(). A slow snapshot() -- or a
            # slow peer's sendall() -- blocks every other subscribe and every
            # broadcast for its entire duration, not just the write.
            with self._lock:
                try:
                    conn.sendall((json.dumps(self._session.snapshot()) + "\n").encode())
                except OSError:
                    conn.close()
                    return
                self._subscribers.append(conn)
            return

        if cmd == "status":
            try:
                conn.sendall((json.dumps(self._session.snapshot()) + "\n").encode())
            except OSError:
                pass
            conn.close()
            return

        if cmd == "browse":
            # Deliberately NOT under self._lock. A browse round-trip is far
            # slower than a snapshot, and holding the broadcast lock here
            # would stall every subscriber for its duration (spec 7.5, and
            # docs/FOLLOWUPS.md item 3). Nothing here touches the subscriber
            # list, so there is nothing for that lock to protect.
            payload = dict(request)
            payload.pop("cmd", None)
            key = payload.pop("session", None) or "widget"
            op = payload.pop("op", None) or ""
            # Serialization is deliberately INSIDE this guarded region, not
            # a separate step after it. `json.dumps` on a reply that
            # contains something non-serializable raises TypeError -- if
            # that happened after the try/except below, it would kill this
            # thread before conn.close() ran, and the client's readline()
            # would block forever rather than see EOF. That is exactly the
            # permanent-freeze failure this branch exists to prevent, so
            # the only thing left outside the guard is the write itself,
            # where OSError genuinely is the only expected failure.
            try:
                reply = self._session.browse(key, op, **payload)
                reply = dict(reply)
                reply["v"] = 1
                blob = (json.dumps(reply) + "\n").encode()
            except browse.BrowseError as exc:
                reply = {"v": 1, "ok": False, "error": exc.token,
                         "message": exc.message}
                if exc.level:
                    # Spec 5.1.1/5.2: a `stale` reply carries the current
                    # level so the widget can re-render, and BrowsePane._apply
                    # is already written expecting it. Merged AFTER the error
                    # fields are built and with `ok` stripped, so the level
                    # payload's own `ok: true` can never turn an error reply
                    # into a success one.
                    reply.update({k: v for k, v in exc.level.items()
                                  if k != "ok"})
                blob = (json.dumps(reply) + "\n").encode()
            except Exception:
                # A browse failure -- including a reply that fails to
                # serialize -- must never take the daemon down or leave the
                # widget waiting on a line that never arrives.
                LOG.exception("browse %r failed", op)
                reply = {"v": 1, "ok": False, "error": "roon_error",
                         "message": "browse failed"}
                blob = (json.dumps(reply) + "\n").encode()
            try:
                conn.sendall(blob)
            except OSError:
                pass
            conn.close()
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
