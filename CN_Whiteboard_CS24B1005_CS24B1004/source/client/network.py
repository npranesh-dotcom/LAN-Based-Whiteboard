"""
network.py
----------
All socket work for the client, kept off the GUI thread.

Threads
=======
* Connection thread (_run): connects, performs the join handshake, then
  reads from the socket. Every decoded server message is put on
  `self.events`, a thread-safe queue that the GUI drains with root.after().
  Tkinter is not thread-safe, so no other thread ever touches a widget.
* Writer thread (_writer): sends frames from an outbound queue, so a slow
  network can never freeze the GUI while the user is drawing.

Reliability
===========
* Heartbeat: if the line is idle for HEARTBEAT_INTERVAL s, a "ping" is
  sent. The "pong" reply gives a round-trip latency for the status bar.
* Dead-server detection: if NO bytes arrive for DEAD_AFTER s (pongs count),
  the connection is treated as dead even though the OS hasn't noticed.
* Auto-reconnect: after an unexpected drop the client retries with back-off
  and re-joins under the same name. If the server still holds the old
  session ("Username already taken" until it detects the dead socket),
  the client keeps retrying instead of giving up.

Events placed on the queue (kind, payload):
    ("status",  {"state": connecting|connected|reconnecting|disconnected, "detail": str})
    ("message", dict)          a server message
    ("join_failed", str)       wrong room code / password / name; no retry
    ("latency", float_ms)
"""

import errno
import queue
import select
import socket
import threading
import time

from client import config, protocol


def describe_error(exc, host, port):
    """Plain-English reason for a failed connection, shown on the connect screen."""
    if isinstance(exc, socket.gaierror):
        return f"'{host}' is not a valid address"
    if isinstance(exc, ConnectionRefusedError):
        return f"nothing is listening on port {port} - is the server running?"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ("no reply - check the IP address, that both computers are on the same "
                "network, and the firewall")
    if getattr(exc, "errno", None) in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "no route to that address - are both VMs on the same (bridged) network?"
    return str(exc) or exc.__class__.__name__


class NetworkClient:

    def __init__(self, events):
        self.events = events               # queue.Queue shared with the GUI
        self._outbox = None
        self._sock = None
        self._stop = threading.Event()    # set when the USER disconnects
        self._thread = None
        self.connected = False
        self._ping_sent_at = None

    # ----- public API (called from the GUI thread) ------------------------------

    def connect(self, host, port, username, room_code, password=""):
        self.params = (host, int(port), username, room_code, password)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def send(self, message):
        """Queue a message. Returns False if not connected (caller decides)."""
        outbox = self._outbox
        if not self.connected or outbox is None:
            return False
        # Under congestion, drop transient cursor updates first
        if message.get("type") == "cursor" and outbox.qsize() > 200:
            return True
        outbox.put(protocol.encode(message))
        return True

    def disconnect(self):
        """User-initiated: close and do NOT reconnect."""
        self._stop.set()
        self._close_socket()

    # ----- internals -----------------------------------------------------------

    def _post(self, kind, payload=None):
        self.events.put((kind, payload))

    def _status(self, state, detail=""):
        self._post("status", {"state": state, "detail": detail})

    def _close_socket(self):
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _run(self):
        """Connection thread: connect -> session -> (reconnect)* -> exit."""
        host, port, username, room, password = self.params
        had_session = False           # joined successfully at least once?
        lost_at = None
        attempt = 0

        while not self._stop.is_set():
            self._status("reconnecting" if had_session else "connecting",
                         f"{host}:{port}" + (f" (attempt {attempt})" if attempt else ""))
            self._joined = False
            try:
                kind, detail = self._session(host, port, username, room, password)
            except (OSError, protocol.FramingError) as e:
                kind, detail = "lost", describe_error(e, host, port)

            if self._stop.is_set():
                break
            if self._joined:                       # a session ran, then dropped
                had_session, lost_at, attempt = True, time.monotonic(), 0

            if kind == "rejected":
                # Only retry if the server still holds OUR previous session
                if not (had_session and detail == "Username already taken"):
                    self._post("join_failed", detail)
                    break
            elif not had_session:
                # Never got in: wrong address, server down, firewall...
                self._post("join_failed", f"Cannot reach server: {detail}")
                break

            if time.monotonic() - lost_at > config.RECONNECT_GIVE_UP:
                break
            delay = config.RECONNECT_DELAYS[min(attempt, len(config.RECONNECT_DELAYS) - 1)]
            attempt += 1
            self._status("reconnecting", f"connection lost ({detail}); retrying in {delay}s")
            if self._stop.wait(delay):
                break

        self.connected = False
        self._status("disconnected", "")

    def _session(self, host, port, username, room, password):
        """One TCP connection, from connect() to its end. Returns (kind, detail)."""
        sock = socket.create_connection((host, port), timeout=config.CONNECT_TIMEOUT)
        self._sock = sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # low latency
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(None)            # blocking; select() provides the ticks

        decoder = protocol.LineDecoder(config.MAX_LINE_BYTES)
        sock.sendall(protocol.encode(protocol.join(username, room, password)))

        # --- handshake: wait for welcome or error -------------------------------
        deadline = time.monotonic() + config.CONNECT_TIMEOUT
        backlog = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ("lost", "no reply to join")
            ready, _, _ = select.select([sock], [], [], remaining)
            if not ready:
                continue
            data = sock.recv(65536)
            if not data:
                return ("lost", "server closed the connection")
            backlog += decoder.feed(data)
            if any(m.get("type") in ("welcome", "error") for m in backlog):
                break
        first = next(m for m in backlog if m.get("type") in ("welcome", "error"))
        if first["type"] == "error":
            self._close_socket()
            return ("rejected", first.get("message", "join refused"))

        # --- joined: start the writer and report -------------------------------------
        self._joined = True
        self._outbox = queue.Queue()
        writer = threading.Thread(target=self._writer, args=(sock, self._outbox), daemon=True)
        writer.start()
        self.connected = True
        self._status("connected", f"{host}:{port}")
        for m in backlog:
            self._post("message", m)

        last_rx = last_ping = time.monotonic()
        self._ping_sent_at = None
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([sock], [], [], 1.0)
                now = time.monotonic()
                if ready:
                    data = sock.recv(65536)
                    if not data:
                        return ("lost", "server closed the connection")
                    last_rx = now
                    for m in decoder.feed(data):
                        if m.get("type") == "pong" and self._ping_sent_at:
                            self._post("latency", (now - self._ping_sent_at) * 1000)
                            self._ping_sent_at = None
                        else:
                            self._post("message", m)
                elif now - last_rx > config.DEAD_AFTER:
                    # The server answers every ping, so silence means it's gone
                    return ("lost", f"no data from server for {config.DEAD_AFTER:.0f}s")
                # Heartbeat: one ping in flight at a time
                if self._ping_sent_at is None and now - last_ping >= config.HEARTBEAT_INTERVAL:
                    self._ping_sent_at = last_ping = now
                    self._outbox.put(protocol.encode(protocol.simple("ping")))
            return ("lost", "closed by user")
        finally:
            self.connected = False
            self._outbox.put(None)       # stop the writer
            self._close_socket()

    def _writer(self, sock, outbox):
        """Writer thread: drain the outbound queue onto the socket in order."""
        while True:
            frame = outbox.get()
            if frame is None:
                return
            try:
                sock.sendall(frame)
            except OSError:
                self._close_socket()     # reader thread will notice and reconnect
                return
