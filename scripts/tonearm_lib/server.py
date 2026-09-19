"""Unix-socket fan-out. One line of JSON per state change; one line in per command.

Every resource this exposes is bounded: the request line (MAX_REQUEST_BYTES),
the time a client gets to send it (REQUEST_TIMEOUT), the handler threads alive
at once (MAX_CONNECTIONS), the registered subscribers (MAX_SUBSCRIBERS), and
the time one write to one of them may take (SEND_TIMEOUT). An unbounded one is
a denial of service reachable by any local process that can open the socket.

`self._lock` guards the subscriber LIST and nothing else. No I/O -- no
`sendall()`, no `snapshot()` -- runs while it is held. Ordering between a
subscriber's initial snapshot and a broadcast racing it is kept by that
subscriber's own per-socket lock instead, so one slow peer stalls only itself.
"""

from __future__ import annotations

import json
import select
import logging
import os
import socket
import threading

from . import browse

LOG = logging.getLogger("tonearmd.server")


# Longest request line accepted. The largest real request is a browse search,
# whose term is whatever the user typed; 64 KiB is orders of magnitude past
# that. Without a bound, `readline()` reads until a newline arrives or memory
# runs out, so a client that simply never sends one is enough.
MAX_REQUEST_BYTES = 64 * 1024

# Seconds a connection gets to deliver its request line. Without a deadline a
# client that connects and never sends a newline parks its handler thread
# forever -- indistinguishable from a slow one, and free to repeat.
REQUEST_TIMEOUT = 10.0

# Seconds one write to one subscriber may take before that subscriber is
# dropped. A peer that has stopped reading fills its socket buffer and then
# blocks the writer; the daemon must give up on it, not on the broadcast.
SEND_TIMEOUT = 5.0

# Handler threads alive at once. The accept loop spawned one per connection
# with no ceiling, so N connections cost N threads whether or not any of them
# ever sent anything.
MAX_CONNECTIONS = 32

# Registered subscribers. The widget holds one; tonearmd-mcp and `tonearm
# watch` hold one each. Past this the daemon refuses rather than fanning out
# to an unbounded list under lock.
MAX_SUBSCRIBERS = 16

# Longest `session` key accepted on a browse request. The key names a
# long-lived per-consumer browse state in the daemon, so its shape is settled
# here rather than wherever it lands. Real keys are "widget", "mcp", "cli".
MAX_SESSION_KEY = 64

# Longest search term forwarded to the Core. The widget sends what the user
# typed; a term measured in kilobytes is not a search, and every one of them
# was a round trip the daemon made a Core do on a client's say-so.
MAX_SEARCH_TERM = 512

# How often the accept loop wakes to re-check whether it should still be
# running. Closing the listening socket does NOT interrupt another thread
# already blocked in accept(), so without this shutdown() sets a flag nobody
# ever reads again and the thread stays parked. It was masked in production
# only because the thread is daemon=True and the process was exiting anyway.
ACCEPT_POLL = 0.25


class _Subscriber:
    """One registered subscriber, with its own write lock.

    The lock is per-socket, not global: two threads must never interleave
    bytes on one connection, but a slow peer must not stall writes to any
    other -- which is exactly what a single global lock held across
    `sendall()` did.
    """

    def __init__(self, sock) -> None:
        self.sock = sock
        self.send_lock = threading.Lock()

    def send(self, blob: bytes) -> bool:
        """Write `blob`, returning whether the subscriber is still usable."""
        with self.send_lock:
            try:
                self.sock.sendall(blob)
                return True
            except OSError:
                return False

    def is_dead(self) -> bool:
        """True when this subscriber's peer has closed.

        A unix stream socket whose peer is gone becomes readable at EOF, so
        liveness needs no traffic, no timer on the client and no protocol
        support. MSG_PEEK leaves anything real in the buffer: a subscriber
        that happens to have sent bytes is chatty, not dead, and a later
        reader must still find them.

        Errs towards alive. A false positive drops a working widget; a false
        negative costs one slot until the next sweep.
        """
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
            if not readable:
                return False
            return self.sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except (OSError, ValueError):
            # A descriptor that is closed or invalid answers neither way, and
            # is not one to keep. ValueError is the already-closed case:
            # select() rejects a socket whose fileno() is -1.
            return True
        except Exception:
            # Anything else means the question could not be asked, not that
            # the answer was "dead". Erring the other way here would drop a
            # working subscriber, which is strictly worse than holding a slot
            # until the next sweep.
            LOG.warning("could not assess subscriber liveness", exc_info=True)
            return False

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or ("/run/user/%d" % os.getuid())
    return os.path.join(base, "tonearm")


def socket_path() -> str:
    return os.path.join(runtime_dir(), "sock")


def _bad_field(name: str, value, limit: int) -> str | None:
    """Why `value` is not an acceptable `name`, or None if it is fine."""
    if not isinstance(value, str):
        return "%s must be a string" % name
    if len(value) > limit:
        return "%s must be at most %d characters" % (name, limit)
    return None


def supervise(srv, on_failure) -> None:
    """Run `srv.serve_forever()`, calling `on_failure()` if it ever stops unbidden.

    The server runs on a daemon thread, so without this its death is
    invisible: the entrypoint's asyncio loop keeps running, systemd still
    sees an active unit, and Restart=on-failure never fires. Measured under
    a hardened unit -- an OSError on the bind path left the process
    `active`, NRestarts 0, and no socket on disk. The bar sits on a stale
    track forever with nothing retrying, which is exactly the outcome the
    unit file's own StartLimitIntervalSec comment calls worse than a visible
    failure.

    A deliberate shutdown() closes the listener, so accept() raises and
    serve_forever returns on the ordinary SIGTERM path -- told apart by
    `srv.running`, which shutdown() clears first. Reporting that as a
    failure would make every clean stop exit non-zero and have systemd
    restart a daemon the user just stopped.

    MPRIS is deliberately not load-bearing (see scripts/tonearmd). The
    socket is the daemon's entire purpose, so the opposite rule applies.
    """
    try:
        srv.serve_forever()
    except BaseException:
        LOG.exception("socket server crashed")
    if not srv.running:
        return
    LOG.error("socket server stopped unexpectedly")
    on_failure()


class Server:
    def __init__(self, session) -> None:
        self._session = session
        self._subscribers: list[_Subscriber] = []
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._running = False
        # Set once the socket is LISTENING, not merely bound; see
        # serve_forever. Anything waiting for the daemon to be connectable
        # should wait on this rather than on the socket file appearing.
        self.ready = threading.Event()

    @property
    def running(self) -> bool:
        """False once shutdown() has been called. See supervise()."""
        return self._running

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
        # See ACCEPT_POLL: this is what makes shutdown() actually reach a
        # thread already blocked in accept().
        self._sock.settimeout(ACCEPT_POLL)
        self._running = True
        # Bound is not the same as listening. bind() creates the socket file,
        # so anything watching for the path to appear can connect in the gap
        # before listen() and get ECONNREFUSED. Rare on a quiet machine and
        # reproducible on a loaded CI runner, which is where it was found.
        self.ready.set()
        # Read at start, not at import, so the bound is one number to change
        # and tests can lower it. BoundedSemaphore, not Semaphore: a release
        # without a matching acquire is a bug worth raising on, not one to
        # paper over by silently growing the ceiling.
        slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue          # nothing waiting; re-check self._running
            except OSError:
                break
            if not slots.acquire(blocking=False):
                # Refused immediately rather than queued. A queued connection
                # is indistinguishable to the client from a hung daemon, and
                # the queue would itself be the unbounded thing this bounds.
                LOG.warning("refusing connection: %d already in flight",
                            MAX_CONNECTIONS)
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            # Every subsequent recv/send on this connection is deadlined. A
            # client that connects and never sends a newline previously
            # parked its handler thread in readline() forever.
            conn.settimeout(REQUEST_TIMEOUT)
            threading.Thread(target=self._serve_one, args=(conn, slots),
                             daemon=True).start()

    def _serve_one(self, conn: socket.socket, slots) -> None:
        """Run one handler and give the connection slot back, whatever happens.

        The slot covers the HANDLER, not the connection: a subscriber's socket
        outlives its handler by design, and is bounded by MAX_SUBSCRIBERS
        instead. Releasing here is what keeps a long-lived subscriber from
        permanently consuming one of the MAX_CONNECTIONS handler slots.
        """
        try:
            self._handle(conn)
        finally:
            slots.release()

    def shutdown(self) -> None:
        self._running = False
        self.ready.clear()
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
            subs, self._subscribers = self._subscribers, []
        for sub in subs:
            sub.close()

    def _handle(self, conn: socket.socket) -> None:
        try:
            # readline(SIZE), never a bare readline(): the bare form reads
            # until a newline arrives or memory runs out, so a client that
            # never sends one is a denial of service against the daemon with
            # no packet larger than any other. A line that hits the cap comes
            # back WITHOUT its terminator, which is how an over-long request
            # is told apart from an ordinary one -- json.loads then rejects
            # the truncated fragment and the connection closes below.
            # Closed explicitly rather than left to the collector: this
            # wrapper holds a buffer and a duplicate reference to the socket.
            with conn.makefile("r") as reader:
                line = reader.readline(MAX_REQUEST_BYTES)
            if not line.endswith("\n"):
                raise ValueError("request line exceeds %d bytes" % MAX_REQUEST_BYTES)
            request = json.loads(line)
            if not isinstance(request, dict):
                # `3` and `"subscribe"` are valid JSON but not requests.
                # Without this, request.get() raised AttributeError, which
                # nothing caught: the handler thread died leaving the socket
                # open and the client waiting on a reply that never came.
                raise ValueError("request is not a JSON object")
        except (ValueError, OSError):
            # Malformed input closes that one connection and nothing else.
            LOG.warning("dropping malformed request")
            conn.close()
            return

        cmd = request.get("cmd")
        if cmd == "subscribe":
            self._subscribe(conn)
            return

        if cmd == "status":
            try:
                conn.sendall((json.dumps(self._session.snapshot()) + "\n").encode())
            except Exception:
                # Not only OSError. json.dumps raises TypeError on a value it
                # cannot encode, and snapshot() raises on its own for a status
                # outside VALID_STATUS; both used to escape this guard and skip
                # the close below, so the handler thread died with the
                # descriptor still open and the client waiting on a reply that
                # never came. _subscribe has caught the same case since it was
                # written -- this is that decision applied to the branch that
                # did not get it.
                LOG.warning("status reply failed", exc_info=True)
            finally:
                # In a finally so no later branch can be added that skips it.
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
            # Refused rather than coerced. The daemon keys long-lived browse
            # state on `session` and forwards `term` to the Core, so a client
            # that sent the wrong shape has a bug worth reporting back --
            # and str() of a dict makes a perfectly good cache key.
            complaint = _bad_field("session", key, MAX_SESSION_KEY)
            if complaint is None and "term" in payload:
                complaint = _bad_field("term", payload["term"], MAX_SEARCH_TERM)
            if complaint is not None:
                LOG.warning("refusing browse: %s", complaint)
                self._reply_once(conn, {"v": 1, "ok": False,
                                        "error": "bad_request",
                                        "message": complaint})
                return
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

    def _subscribe(self, conn: socket.socket) -> None:
        """Register `conn` and send it the current state, in that order.

        Send-then-register left a gap: a broadcast landing in it iterated a
        subscriber list this connection was not in yet, and its first update
        vanished with no error anywhere. The previous fix closed that by
        holding `self._lock` across BOTH the snapshot and the write -- which
        made one slow peer, or one slow `snapshot()`, stall every other
        subscribe and every broadcast for its full duration.

        Registering first and writing under this subscriber's OWN lock keeps
        the ordering guarantee without that cost: a broadcast racing this
        handshake finds the connection registered, blocks on its per-socket
        lock only, and lands after the snapshot. Nothing waits on the global
        lock for longer than a list append.
        """
        # Reclaim slots held by peers that have gone before deciding this
        # connection cannot have one. Without it the cap counts corpses, and
        # sixteen widget restarts lock the seventeenth out permanently.
        self.reap_dead_subscribers()

        sub = _Subscriber(conn)
        # Claimed before the subscriber is visible, so a broadcast cannot
        # overtake the snapshot below.
        with sub.send_lock:
            with self._lock:
                if len(self._subscribers) >= MAX_SUBSCRIBERS:
                    LOG.warning("refusing subscriber: %d already registered",
                                MAX_SUBSCRIBERS)
                    conn.close()
                    return
                self._subscribers.append(sub)
            # Outside self._lock on purpose. snapshot() does zone arbitration
            # and an art-cache lookup, and the write can block on a slow peer;
            # neither may stall the subscriber list.
            try:
                blob = (json.dumps(self._session.snapshot()) + "\n").encode()
                conn.settimeout(SEND_TIMEOUT)
                conn.sendall(blob)
            except Exception:
                # Includes a snapshot that fails to serialize: this connection
                # is already registered, so it must be unregistered rather
                # than left in the list writing to a socket nobody closed.
                LOG.warning("subscriber handshake failed", exc_info=True)
                self._drop(sub)

    def _reply_once(self, conn: socket.socket, payload: dict) -> None:
        """Send one JSON line and close. Used by the request-refusal paths."""
        try:
            conn.sendall((json.dumps(payload) + "\n").encode())
        except OSError:
            pass
        conn.close()

    def reap_dead_subscribers(self) -> int:
        """Drop subscribers whose peer has closed; return how many went.

        MAX_SUBSCRIBERS bounds the list, but until this existed a subscriber
        was only removed when a write to it failed -- and writes only happen
        on state changes. With nothing playing there are no writes, so every
        widget restart leaked a slot and sixteen of them locked the real
        widget out while the daemon still reported healthy. Measured on a
        live install: 856 refusals over four days, every slot an orphan.

        The liveness checks run OUTSIDE the lock, like every other piece of
        I/O here: the lock is taken twice, briefly, to copy and to remove.
        """
        with self._lock:
            current = list(self._subscribers)
        dead = [sub for sub in current if sub.is_dead()]
        if not dead:
            return 0
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s not in dead]
        for sub in dead:
            sub.close()
        LOG.info("reaped %d subscriber(s) whose peer had closed", len(dead))
        return len(dead)

    def _drop(self, sub: "_Subscriber") -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not sub]
        sub.close()

    def broadcast(self, payload: dict) -> None:
        """Fan `payload` out to every subscriber.

        The lock is held just long enough to copy the list. Writing inside it
        -- as this used to -- meant one peer that had stopped reading blocked
        every other subscriber, and every new subscribe, until its write gave
        up. Each write is deadlined by SEND_TIMEOUT and serialized by that
        subscriber's own lock, so a stalled peer costs only itself.
        """
        # Sweep here too, so the list stays clean during normal operation
        # rather than only when someone new arrives. A closed peer is often
        # noticed by the failing write below, but only if there IS a write:
        # a paused zone can emit nothing for hours.
        self.reap_dead_subscribers()

        blob = (json.dumps(payload) + "\n").encode()
        with self._lock:
            targets = list(self._subscribers)
        dead = [sub for sub in targets if not sub.send(blob)]
        if not dead:
            return
        # A hot-reload closes the relay mid-broadcast; a peer that blew its
        # send deadline is equally gone. Prune both, and keep the rest.
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s not in dead]
        for sub in dead:
            sub.close()
