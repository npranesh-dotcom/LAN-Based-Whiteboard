"""
backend_e2e_test.py
-------------------
End-to-end tests for the whiteboard server. Each test starts a REAL server
process on a free port, connects real TCP clients, and checks the replies.

Run from the project root (venv activated or not, no extra packages):

    python3 tests/backend_e2e_test.py

The server is pointed at a temporary data folder (WHITEBOARD_DATA_DIR), so
your real data/canvas.json is never touched.

Expected final line:  ALL <n> TESTS PASSED
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

ROOM = "TESTROOM"
TIMEOUT = 3.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    """Start/stop a real server subprocess."""

    def __init__(self, *extra_args, data_dir=None):
        self.port = free_port()
        self.data_dir = data_dir or tempfile.mkdtemp(prefix="wb_test_")
        env = dict(os.environ, WHITEBOARD_DATA_DIR=self.data_dir, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "server.server", "--host", "127.0.0.1",
             "--port", str(self.port), *extra_args],
            cwd=PROJECT_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.output = []
        threading.Thread(target=self._pump, daemon=True).start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start:\n" + "".join(self.output))

    def _pump(self):
        for line in self.proc.stdout:
            self.output.append(line)

    def log(self):
        return "".join(self.output)

    def stop(self, sig=signal.SIGINT):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        time.sleep(0.1)


class Client:
    """A test client that collects every message the server sends."""

    def __init__(self, port, read=True, rcvbuf=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            # Must be set before connect() to limit the TCP receive window
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.connect(("127.0.0.1", port))
        self.messages = []
        self.cond = threading.Condition()
        self.closed = False
        if read:
            threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        with self.cond:
                            self.messages.append(json.loads(line))
                            self.cond.notify_all()
        except OSError:
            pass
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def send(self, message):
        self.sock.sendall((json.dumps(message) + "\n").encode())

    def send_raw(self, data):
        self.sock.sendall(data)

    def wait_for(self, pred, timeout=TIMEOUT, start=0):
        """Wait until a message matching pred arrives (searching from index start)."""
        deadline = time.time() + timeout
        with self.cond:
            while True:
                for m in self.messages[start:]:
                    if pred(m):
                        return m
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)

    def wait_type(self, t, timeout=TIMEOUT, start=0):
        return self.wait_for(lambda m: m.get("type") == t, timeout, start)

    def wait_closed(self, timeout=TIMEOUT):
        deadline = time.time() + timeout
        with self.cond:
            while not self.closed and time.time() < deadline:
                self.cond.wait(deadline - time.time())
            return self.closed

    def of_type(self, t, start=0):
        with self.cond:
            return [m for m in self.messages[start:] if m.get("type") == t]

    def mark(self):
        with self.cond:
            return len(self.messages)

    def quiet(self, seconds=0.3):
        time.sleep(seconds)

    def join(self, username, room=ROOM, **extra):
        start = self.mark()
        self.send({"type": "join", "username": username, "room_code": room, **extra})
        return self.wait_for(lambda m: m.get("type") in ("welcome", "error"), start=start)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)   # sends FIN even while reader is in recv()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def stroke(sid, color="#000000", width=3, points=None, **extra):
    s = {"stroke_id": sid, "color": color, "width": width,
         "points": points or [[10, 10], [20, 20]]}
    s.update(extra)
    return s


def ids(strokes):
    return [s["stroke_id"] for s in strokes]


# ---------------------------------------------------------------------------
# Tests  (numbers match docs/BACKEND_CHANGES.md)
# ---------------------------------------------------------------------------

def test_01_single_draw_is_stored_once():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        a.send({"type": "draw", "stroke": stroke("s1")})
        a.wait_type("stroke_added"); a.quiet()
        assert len(a.of_type("stroke_added")) == 1, "draw broadcast more than once"
        c = Client(srv.port); c.join("C")
        cs = c.wait_type("canvas_state")
        assert ids(cs["strokes"]) == ["s1"], cs
        a.send({"type": "undo"})
        u = a.wait_for(lambda m: m.get("type") == "canvas_state" and m.get("action") == "undo")
        assert u and u["strokes"] == [], "undo should empty the canvas"
    finally:
        srv.stop()


def test_02_load_of_missing_file_keeps_canvas():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        a.send({"type": "draw", "stroke": stroke("keep")}); a.wait_type("stroke_added")
        m = a.mark()
        a.send({"type": "load"})
        err = a.wait_type("error", start=m)
        assert err and "Load failed" in err["message"], err
        assert not a.of_type("canvas_state", start=m), "canvas must not be replaced"
        # Corrupt file
        with open(os.path.join(srv.data_dir, "canvas.json"), "w") as f:
            f.write("{not json")
        m = a.mark()
        a.send({"type": "load"})
        err = a.wait_type("error", start=m)
        assert err and "corrupt" in err["message"], err
        c = Client(srv.port); c.join("C")
        assert ids(c.wait_type("canvas_state")["strokes"]) == ["keep"]
    finally:
        srv.stop()


def test_03_room_code_is_required():
    srv = Server("--room", ROOM)
    try:
        b = Client(srv.port)
        b.send({"type": "join", "username": "B"})
        r = b.wait_for(lambda m: m.get("type") in ("welcome", "error"))
        assert r["type"] == "error" and "required" in r["message"], r
        r = b.join("B", room="WRONG")
        assert r["type"] == "error" and r["message"] == "Invalid room code", r
        r = b.join("B", room=" testroom ")     # case/whitespace-insensitive
        assert r["type"] == "welcome", r
    finally:
        srv.stop()


def test_04_oversized_message_disconnects():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        watcher = Client(srv.port); watcher.join("W")
        try:
            a.send_raw(b"x" * (600 * 1024))     # no newline, > 512 KB
        except OSError:
            pass
        assert a.wait_closed(), "server should close an oversized connection"
        left = watcher.wait_for(lambda m: m.get("type") == "user_left" and m["username"] == "A")
        assert left, "others should be told the user left"
        # Server still healthy
        d = Client(srv.port)
        assert d.join("D")["type"] == "welcome"
    finally:
        srv.stop()


def test_05_chat_timestamp_and_history():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        for i in range(3):
            a.send({"type": "chat", "message": f"msg {i}"})
        a.wait_for(lambda m: m.get("type") == "chat" and m["message"] == "msg 2")
        live = a.of_type("chat")
        for m in live:
            assert m["username"] == "A" and "timestamp" in m and "time" in m, m
        seqs = [m["sequence"] for m in live]
        assert seqs == sorted(seqs)
        c = Client(srv.port); c.join("C")
        c.wait_for(lambda m: m.get("type") == "chat" and m["message"] == "msg 2")
        hist = c.of_type("chat")
        assert [m["message"] for m in hist] == ["msg 0", "msg 1", "msg 2"], hist
        assert all(m.get("history") is True for m in hist)
    finally:
        srv.stop()


def test_06_room_code_from_command_line_or_random():
    srv = Server("--room", "CN2026", "--password", "pw")
    try:
        a = Client(srv.port)
        assert a.join("A", room="CN2026")["message"] == "Incorrect room password"
        w = a.join("A", room="CN2026", password="pw")
        assert w["type"] == "welcome" and w["room_code"] == "CN2026", w
    finally:
        srv.stop()
    srv = Server()                              # no --room: random code
    try:
        time.sleep(0.3)
        line = [l for l in srv.output if "Room code:" in l]
        assert line, srv.log()
        code = line[0].split("Room code:")[1].strip()
        assert len(code) == 6 and code != "WHITEBOARD", code
        a = Client(srv.port)
        assert a.join("A", room="WHITEBOARD")["type"] == "error"
        assert a.join("A", room=code)["type"] == "welcome"
    finally:
        srv.stop()


def test_07_per_user_undo_redo():
    srv = Server("--room", ROOM)                # default --undo-mode user
    try:
        a = Client(srv.port); a.join("A")
        b = Client(srv.port); b.join("B")
        a.send({"type": "draw", "stroke": stroke("a1")}); a.wait_type("stroke_added")
        b.send({"type": "draw", "stroke": stroke("b1")})
        a.wait_for(lambda m: m.get("type") == "stroke_added" and m["stroke"]["stroke_id"] == "b1")
        m = a.mark()
        a.send({"type": "undo"})                # must remove a1, NOT b1 (the latest)
        u = a.wait_type("canvas_state", start=m)
        assert ids(u["strokes"]) == ["b1"] and u["by"] == "A", u
        m = a.mark()
        b.send({"type": "redo"})                # B has nothing to redo
        a.quiet()
        assert not a.of_type("canvas_state", start=m), "B must not redo A's stroke"
        a.send({"type": "redo"})
        r = a.wait_type("canvas_state", start=m)
        assert ids(r["strokes"]) == ["b1", "a1"], r
    finally:
        srv.stop()
    srv = Server("--room", ROOM, "--undo-mode", "global")
    try:
        a = Client(srv.port); a.join("A")
        b = Client(srv.port); b.join("B")
        a.send({"type": "draw", "stroke": stroke("a1")}); a.wait_type("stroke_added")
        b.send({"type": "draw", "stroke": stroke("b1")})
        a.wait_for(lambda m: m.get("type") == "stroke_added" and m["stroke"]["stroke_id"] == "b1")
        m = a.mark()
        a.send({"type": "undo"})                # global: removes the latest (b1)
        assert ids(a.wait_type("canvas_state", start=m)["strokes"]) == ["a1"]
    finally:
        srv.stop()


def test_08_ordering_under_concurrent_drawing():
    srv = Server("--room", ROOM)
    try:
        N = 150
        a = Client(srv.port); a.join("A")
        b = Client(srv.port); b.join("B")
        obs = Client(srv.port); obs.join("Obs")

        def spam(cl, prefix):
            for i in range(N):
                cl.send({"type": "draw", "stroke": stroke(f"{prefix}{i}")})

        late_holder = {}

        def late_join():
            time.sleep(0.01)
            late = Client(srv.port); late.join("Late")
            late_holder["c"] = late

        threads = [threading.Thread(target=spam, args=(a, "a")),
                   threading.Thread(target=spam, args=(b, "b")),
                   threading.Thread(target=late_join)]
        for t in threads: t.start()
        for t in threads: t.join()

        deadline = time.time() + 10
        while time.time() < deadline and not all(len(c.of_type("stroke_added")) >= 2 * N for c in (a, b, obs)):
            time.sleep(0.05)

        orders = []
        for cl in (a, b, obs):
            sa = cl.of_type("stroke_added")
            assert len(sa) == 2 * N, f"expected {2*N}, got {len(sa)}"
            seqs = [m["sequence"] for m in sa]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "sequence not increasing"
            orders.append([m["stroke"]["stroke_id"] for m in sa])
        assert orders[0] == orders[1] == orders[2], "clients saw different orders"

        # Late joiner: snapshot + following deltas == everything, no overlap
        late = late_holder["c"]
        deadline = time.time() + 5
        cs = late.wait_type("canvas_state")
        snap_seq = cs["sequence"]
        while time.time() < deadline:
            after = [m for m in late.of_type("stroke_added")]
            if len(cs["strokes"]) + len(after) >= 2 * N:
                break
            time.sleep(0.05)
        after = late.of_type("stroke_added")
        assert all(m["sequence"] > snap_seq for m in after), "delta older than snapshot"
        combined = ids(cs["strokes"]) + [m["stroke"]["stroke_id"] for m in after]
        assert combined == orders[0], "late joiner state differs from others"
    finally:
        srv.stop()


def test_09_save_load_host_only_and_validated():
    srv = Server("--room", ROOM)
    try:
        host = Client(srv.port); host.join("Host")
        guest = Client(srv.port); guest.join("Guest")
        for t in ("save", "load"):
            m = guest.mark()
            guest.send({"type": t})
            e = guest.wait_type("error", start=m)
            assert e and "Only the host" in e["message"], e
        host.send({"type": "draw", "stroke": stroke("h1")}); host.wait_type("stroke_added")
        # Many saves at once must not corrupt the file
        for _ in range(20):
            host.send({"type": "save"})
        deadline = time.time() + 5
        while len(host.of_type("save_success")) < 20 and time.time() < deadline:
            time.sleep(0.05)
        assert len(host.of_type("save_success")) == 20
        with open(os.path.join(srv.data_dir, "canvas.json")) as f:
            assert ids(json.load(f)) == ["h1"]
        assert not [n for n in os.listdir(srv.data_dir) if n.endswith(".tmp")], "temp files left"
        # A file with one good and two bad strokes
        with open(os.path.join(srv.data_dir, "canvas.json"), "w") as f:
            json.dump([stroke("good", tool="pen", username="X"),
                       stroke("bad", width=999),
                       {"nonsense": True}], f)
        m = host.mark()
        host.send({"type": "load"})
        ok = host.wait_type("load_success", start=m)
        assert ok and "skipped 2" in ok["message"], ok
        assert ids(host.wait_type("canvas_state", start=m)["strokes"]) == ["good"]
    finally:
        srv.stop()


def test_10_save_on_shutdown_and_restore():
    data_dir = tempfile.mkdtemp(prefix="wb_test_")
    srv = Server("--room", ROOM, data_dir=data_dir)
    try:
        a = Client(srv.port); a.join("A")
        a.send({"type": "draw", "stroke": stroke("persist1")}); a.wait_type("stroke_added")
        a.close()
    finally:
        srv.stop(signal.SIGTERM)                # like `kill <pid>`; Ctrl+C is SIGINT
    with open(os.path.join(data_dir, "canvas.json")) as f:
        assert ids(json.load(f)) == ["persist1"], "canvas not saved on shutdown"
    srv = Server("--room", ROOM, data_dir=data_dir)
    try:
        c = Client(srv.port); c.join("C")
        assert ids(c.wait_type("canvas_state")["strokes"]) == ["persist1"]
    finally:
        srv.stop()


def test_11_dead_peer_detection_configured():
    # Unplugging a cable can't be simulated on localhost; verify the socket
    # options the server applies instead.
    from server.server import configure_socket
    lst = socket.socket(); lst.bind(("127.0.0.1", 0)); lst.listen()
    cli = socket.create_connection(lst.getsockname())
    conn, _ = lst.accept()
    configure_socket(conn)
    assert conn.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 10
    if hasattr(socket, "TCP_USER_TIMEOUT"):
        assert conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 30000
    for s in (cli, conn, lst):
        s.close()


def test_12_unknown_stroke_fields_stripped():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        a.send({"type": "draw", "stroke": stroke("x1", evil="<script>", username="Mallory", tool="laser")})
        s = a.wait_type("stroke_added")["stroke"]
        assert set(s) == {"stroke_id", "color", "width", "points", "tool", "username"}, s
        assert s["username"] == "A" and s["tool"] == "pen", s
        a.send({"type": "erase", "stroke": stroke("x2")})
        e = a.wait_for(lambda m: m.get("type") == "stroke_added" and m["stroke"]["stroke_id"] == "x2")
        assert e["stroke"]["tool"] == "eraser"
    finally:
        srv.stop()


def test_13_bad_input_never_kills_connection():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        bad = [b"not json\n", b"[1,2,3]\n", b'{"type":"draw","stroke":null}\n',
               b'{"type":"draw","stroke":{"stroke_id":5}}\n', b'{"type":"chat","message":42}\n',
               b'{"type":"nope"}\n', b'{"type":"join","username":"A","room_code":"TESTROOM"}\n',
               b"\xff\xfe\n"]
        for line in bad:
            a.send_raw(line)
        a.send({"type": "ping"})
        assert a.wait_type("pong"), "connection should survive bad input"
        assert "Traceback" not in srv.log(), srv.log()
    finally:
        srv.stop()


def test_14_join_messages_and_snapshot_sequence():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        b = Client(srv.port); w = b.join("B")
        cs = b.wait_type("canvas_state")
        assert cs["sequence"] == w["sequence"], "snapshot must carry the join sequence"
        b.quiet()
        assert not b.of_type("user_joined"), "joiner should not get its own user_joined"
        uj = a.wait_type("user_joined")
        assert uj["username"] == "B" and uj["participants"] == ["A", "B"] and uj["host"] == "A"
    finally:
        srv.stop()


def test_15_slow_client_does_not_stall_others():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        # Joins, then never reads. A tiny receive buffer makes the result
        # the same on any machine, whatever its kernel TCP buffer sizes.
        slow = Client(srv.port, read=False, rcvbuf=4096)
        slow.send({"type": "join", "username": "Slow", "room_code": ROOM})
        time.sleep(0.3)
        big = [[i % 1000, (i * 7) % 1000] for i in range(1000)]
        t0 = time.time()
        total, batch = 3000, 50
        for i in range(total):
            a.send({"type": "draw", "stroke": stroke(f"s{i}", points=big)})
            # A real user draws at human speed. Here A waits for its own
            # echo every `batch` strokes, so A never falls behind its own
            # stream, however slow the machine. Only "Slow" should be dropped.
            if i % batch == batch - 1:
                sid = f"s{i}"
                got = a.wait_for(lambda m: m.get("type") == "stroke_added"
                                 and m["stroke"]["stroke_id"] == sid, timeout=30)
                assert got, f"A stopped receiving at {sid}\n--- server log ---\n{srv.log()[-2000:]}"
        left = a.wait_for(lambda m: m.get("type") == "user_left" and m["username"] == "Slow", timeout=20)
        assert left, f"slow client should be disconnected\n--- server log ---\n{srv.log()[-2000:]}"
        assert "[SLOW CLIENT] Slow" in srv.log()
        assert "[SLOW CLIENT] A " not in srv.log(), "A must not be treated as slow"
        assert srv.log().count("[SLOW CLIENT] Slow") == 1, "drop should be logged once"
        print(f"      ({total} x 1000-point strokes delivered in {time.time() - t0:.1f}s)")
    finally:
        srv.stop()


def test_16_regressions_session_rules():
    srv = Server("--room", ROOM)
    try:
        host = Client(srv.port); host.join("Host")
        guest = Client(srv.port); guest.join("Guest")
        dup = Client(srv.port)
        r = dup.join("host")                     # case-insensitive duplicate
        assert r["type"] == "error" and r["message"] == "Username already taken"
        guest.send({"type": "clear"})
        assert "Only the host" in guest.wait_type("error")["message"]
        host.close()
        left = guest.wait_type("user_left")
        assert left["host"] == "Guest", "host should migrate"
        m = guest.mark()
        guest.send({"type": "clear"})
        c = guest.wait_type("canvas_state", start=m)
        assert c and c["strokes"] == [] and c["action"] == "clear"
    finally:
        srv.stop()


def test_18_duplicate_stroke_is_idempotent():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        a.send({"type": "draw", "stroke": stroke("dup")}); a.wait_type("stroke_added")
        a.send({"type": "draw", "stroke": stroke("dup")})     # re-sent after "reconnect"
        d = a.wait_type("stroke_duplicate")
        assert d and d["stroke_id"] == "dup", d
        a.quiet()
        assert len(a.of_type("stroke_added")) == 1, "duplicate must not be broadcast"
        m = a.mark()
        a.send({"type": "undo"})
        assert a.wait_for(lambda x: x.get("action") == "undo", start=m)
        m = a.mark()
        a.send({"type": "draw", "stroke": stroke("dup")})     # still in redo stack
        assert a.wait_type("stroke_duplicate", start=m), "undone stroke id must stay reserved"
    finally:
        srv.stop()


def test_19_sync_returns_current_snapshot():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        a.send({"type": "draw", "stroke": stroke("s1")})
        added = a.wait_type("stroke_added")
        m = a.mark()
        a.send({"type": "sync"})
        cs = a.wait_type("canvas_state", start=m)
        assert cs["action"] == "sync" and ids(cs["strokes"]) == ["s1"]
        assert cs["sequence"] == added["sequence"], (cs["sequence"], added["sequence"])
    finally:
        srv.stop()


def test_20_cursor_and_preview_relay():
    srv = Server("--room", ROOM)
    try:
        a = Client(srv.port); a.join("A")
        b = Client(srv.port); b.join("B")
        a.send({"type": "cursor", "x": 100, "y": 200,
                "preview": {"stroke_id": "p1", "color": "#123456", "width": 4,
                            "tool": "pen", "points": [[100, 200], [101, 202]]}})
        c = b.wait_type("cursor")
        assert c["username"] == "A" and c["x"] == 100 and "sequence" not in c, c
        assert c["preview"]["points"] == [[100, 200], [101, 202]]
        time.sleep(0.05)
        a.send({"type": "cursor", "hidden": True})
        assert b.wait_for(lambda m: m.get("type") == "cursor" and m.get("hidden"))
        a.quiet()
        assert not a.of_type("cursor"), "sender must not get its own cursor back"
        # Bad cursor data is ignored silently, connection survives
        time.sleep(0.05)
        a.send({"type": "cursor", "x": -5, "y": "z"})
        a.send({"type": "ping"}); assert a.wait_type("pong")
        # Cursors are not stored: a new joiner's canvas is empty
        c2 = Client(srv.port); c2.join("C")
        assert c2.wait_type("canvas_state")["strokes"] == []
    finally:
        srv.stop()


def test_21_netcheck_tool_and_error_hints():
    srv = Server("--room", ROOM)
    try:
        tool = os.path.join(PROJECT_ROOT, "tools", "netcheck.py")
        ok = subprocess.run([sys.executable, tool, "127.0.0.1", str(srv.port)],
                            capture_output=True, text=True, timeout=15)
        assert ok.returncode == 0 and "server reachable" in ok.stdout, ok.stdout + ok.stderr
        # The probe must not have joined: no user, and the server is unaffected
        assert "[JOINED]" not in srv.log()
    finally:
        srv.stop()
    dead = subprocess.run([sys.executable, tool, "127.0.0.1", str(srv.port)],
                          capture_output=True, text=True, timeout=15)
    assert dead.returncode == 1 and "nothing is listening" in dead.stdout, dead.stdout
    from client.network import describe_error
    assert "valid address" in describe_error(socket.gaierror(), "x", 5000)
    assert "server running" in describe_error(ConnectionRefusedError(), "x", 5000)
    assert "same" in describe_error(socket.timeout(), "x", 5000)


def test_17_canvas_manager_unit():
    from server.canvas_manager import CanvasManager, sanitize_stroke
    c = CanvasManager()
    assert c.validate_stroke(stroke("1", "#FF0000", 4))
    assert not c.validate_stroke(stroke("1", "#FF0000", 999))
    assert not c.validate_stroke(stroke("1", "#GG0000", 4))
    assert not c.validate_stroke(stroke("1", width=True))
    assert not c.validate_stroke(stroke("1", points=[[1, float("nan")]]))
    assert sanitize_stroke(stroke("1", points=[(1, 2)]))["points"] == [[1, 2]]


# ---------------------------------------------------------------------------

def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"PASS  {name}  ({time.time() - t0:.1f}s)")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL  {name}: {e!r}")
            traceback.print_exc()
    print("-" * 60)
    if failed:
        print(f"{failed} OF {len(tests)} TESTS FAILED")
        sys.exit(1)
    print(f"ALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()
