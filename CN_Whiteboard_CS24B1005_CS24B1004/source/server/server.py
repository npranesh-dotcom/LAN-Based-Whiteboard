"""
server.py
---------
Central TCP server for the LAN collaborative whiteboard.

Run from the project root:

    python3 -m server.server                       # random room code
    python3 -m server.server --room WHITEBOARD     # fixed room code
    python3 -m server.server --room CN2026 --password secret --port 5000
    python3 -m server.server --undo-mode global    # board-wide undo

Architecture
============
* One listening socket on HOST:PORT (default 0.0.0.0:5000, every interface).
* For every client, two threads:
    - a READER thread (handle_client) that receives bytes, splits them into
      newline-delimited JSON messages and processes each one;
    - a WRITER thread (client_writer) that takes already-encoded messages
      from that client's outbox queue and sends them with sendall().
  A slow or frozen client only fills its own queue. It can never block the
  thread that is broadcasting to everyone else. If its queue overflows the
  client is disconnected.

Message framing
===============
Every message, in both directions, is one JSON object followed by "\n".
TCP is a byte stream, so the reader keeps a buffer and only parses complete
lines. A line longer than MAX_MESSAGE_BYTES closes the connection, so a
client can't exhaust server memory by never sending a newline.

Ordering and consistency
========================
Every state-changing event (join, leave, stroke, undo, redo, clear, load,
chat) is handled entirely inside `state_lock`:

    1. change the shared state,
    2. take the next global sequence number,
    3. put the resulting message into every client's outbox.

Each outbox is FIFO and is filled in sequence order under the same lock, so
every client receives events in exactly the same order, and sequence
numbers always increase. On join, the snapshot (canvas_state) is taken
under the lock with the join's sequence number N, so any later event has
sequence > N and arrives after the snapshot. A joining user can't miss or
double-apply a stroke.

Lock order is always state_lock -> canvas_manager.lock / session_manager.lock.
Those two never call back into the server, so deadlock is impossible.

Recovery
========
* Canvas is loaded from data/canvas.json on startup.
* Canvas is saved automatically on Ctrl+C / SIGTERM.
* Reconnecting users receive the full canvas and the recent chat history.
* TCP keep-alive plus TCP_USER_TIMEOUT detect dead peers (pulled cable, VM
  network dropped) in about 30 s, so a vanished user's name is freed and
  they can rejoin.
"""

import argparse
import collections
import json
import queue
import signal
import socket
import threading
import time
import traceback
from datetime import datetime

from server.canvas_manager import CanvasManager, sanitize_stroke, UNDO_MODES
from server.persistence import save_canvas, load_canvas
from server.session_manager import SessionManager, generate_room_code

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000

RECV_SIZE = 4096
MAX_MESSAGE_BYTES = 512 * 1024      # one JSON line; a 5000-point stroke is ~80 KB
OUTBOX_LIMIT = 2000                 # queued messages before a client is "too slow"
CHAT_HISTORY_LIMIT = 200            # messages replayed to a new joiner
MAX_CHAT_LENGTH = 500
MAX_USERNAME_LENGTH = 20
CURSOR_MIN_INTERVAL = 0.02          # seconds; caps cursor relay at 50/s per client
MAX_PREVIEW_POINTS = 300            # points per live-preview message

# Dead-peer detection (Linux socket options; skipped where unavailable)
KEEPALIVE_IDLE = 10                 # seconds idle before first probe
KEEPALIVE_INTERVAL = 5              # seconds between probes
KEEPALIVE_COUNT = 3                 # failed probes before the link is dead
TCP_USER_TIMEOUT_MS = 30000         # unacknowledged data -> dead after 30 s

# ---------------------------------------------------------------------------
# Shared server state
# ---------------------------------------------------------------------------

canvas_manager = CanvasManager()
session_manager = SessionManager()

# conn -> {"username", "addr", "outbox": Queue, "writer": Thread}
clients = {}

# Guards: clients, server_sequence, chat_history, and the ORDER in which
# messages are queued. See "Ordering and consistency" above.
state_lock = threading.RLock()

server_sequence = 0

chat_history = collections.deque(maxlen=CHAT_HISTORY_LIMIT)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def encode(message):
    """Serialise one message into a newline-terminated UTF-8 frame."""
    return (json.dumps(message) + "\n").encode("utf-8")


def next_sequence():
    """Return the next global sequence number. Caller must hold state_lock."""
    global server_sequence
    server_sequence += 1
    return server_sequence


def configure_socket(conn):
    """Enable TCP keep-alive so dead peers are detected without app pings."""
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE)
        if hasattr(socket, "TCP_KEEPINTVL"):
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL)
        if hasattr(socket, "TCP_KEEPCNT"):
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_COUNT)
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, TCP_USER_TIMEOUT_MS)
        # Small JSON messages: send immediately instead of batching (lower latency)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as e:
        print(f"[WARN] Could not set socket options: {e}")


def drop_connection(conn):
    """
    Force a connection closed from any thread. The reader thread's recv()
    then returns and its normal cleanup (user_left broadcast) runs.
    """
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def client_writer(conn, outbox, addr):
    """WRITER thread: send queued frames to one client, in order."""
    while True:
        data = outbox.get()

        if data is None:          # sentinel: connection is closing
            break

        try:
            conn.sendall(data)
        except OSError:
            drop_connection(conn)
            break


def _queue_frame(conn, client, data):
    """Put one encoded frame in a client's outbox (never blocks)."""
    if client.get("dropped"):
        return                     # already being disconnected
    try:
        client["outbox"].put_nowait(data)
    except queue.Full:
        client["dropped"] = True
        print(f"[SLOW CLIENT] {client['username']} has {OUTBOX_LIMIT} unsent "
              f"messages (not reading fast enough); disconnecting")
        drop_connection(conn)


def send_message(conn, message):
    """
    Send one message to one client.

    Registered clients: the frame is queued for their writer thread.
    Unregistered clients (still joining): sent directly. Only their own
    reader thread uses the socket at that point, so this is safe and lets
    join errors such as "Username already taken" reach them.
    """
    data = encode(message)

    with state_lock:
        client = clients.get(conn)
        if client is not None:
            _queue_frame(conn, client, data)
            return True

    try:
        conn.sendall(data)
        return True
    except OSError:
        return False


def broadcast(message, exclude=None):
    """
    Queue a message for every registered client (except `exclude`).
    Encoded once and shared by all clients. Call while holding state_lock
    when the message describes a state change, so ordering is preserved.
    """
    data = encode(message)

    with state_lock:
        for conn, client in list(clients.items()):
            if conn is not exclude:
                _queue_frame(conn, client, data)


def error(conn, text):
    send_message(conn, {"type": "error", "message": text})


# ---------------------------------------------------------------------------
# Message handlers (one per message type)
# ---------------------------------------------------------------------------

def handle_join(conn, addr, message):
    """
    Process a join request. Returns the username on success, None otherwise.
    """
    requested_name = message.get("username")
    room_code = message.get("room_code")          # REQUIRED, no default
    password = message.get("password", "")

    if not isinstance(requested_name, str):
        error(conn, "Invalid username")
        return None

    if room_code is None:
        error(conn, "Room code is required")
        return None

    if not isinstance(room_code, str) or not isinstance(password, str):
        error(conn, "Invalid room credentials")
        return None

    requested_name = requested_name.strip()

    if not requested_name or len(requested_name) > MAX_USERNAME_LENGTH:
        error(conn, f"Username must be 1-{MAX_USERNAME_LENGTH} characters")
        return None

    with state_lock:

        success, result = session_manager.add_participant(
            conn, requested_name, room_code, password
        )

        if success:
            username = requested_name

            outbox = queue.Queue(maxsize=OUTBOX_LIMIT)
            writer = threading.Thread(
                target=client_writer, args=(conn, outbox, addr), daemon=True
            )
            clients[conn] = {
                "username": username,
                "addr": addr,
                "outbox": outbox,
                "writer": writer,
            }
            writer.start()

            join_sequence = next_sequence()
            participants = session_manager.get_participants()
            host = session_manager.get_host()

            # 1. Welcome (to the new user only)
            send_message(conn, {
                "type": "welcome",
                "username": username,
                "participants": participants,
                "host": host,
                "room_code": session_manager.room_code,
                "undo_mode": canvas_manager.undo_mode,
                "sequence": join_sequence,
            })

            # 2. Snapshot of the canvas as of join_sequence
            send_message(conn, {
                "type": "canvas_state",
                "strokes": canvas_manager.get_canvas_state(),
                "sequence": join_sequence,
            })

            # 3. Chat history, oldest first, as normal chat messages
            #    flagged with "history": true
            for entry in chat_history:
                replay = dict(entry)
                replay["history"] = True
                send_message(conn, replay)

            # 4. Tell everyone else
            broadcast({
                "type": "user_joined",
                "username": username,
                "participants": participants,
                "host": host,
                "sequence": join_sequence,
            }, exclude=conn)

    if not success:
        # Sent outside the lock: this client isn't registered yet, so the
        # send is direct and must not hold up other clients.
        error(conn, result)
        return None

    print(f"[JOINED] {username} joined from {addr[0]}:{addr[1]} "
          f"(participants: {len(participants)}, host: {host})")
    return username


def handle_draw(conn, username, message, msg_type):
    raw = message.get("stroke")

    # "tool" and "username" are set by the server, so whatever the client
    # sent for them is ignored rather than validated.
    if isinstance(raw, dict):
        raw = {k: v for k, v in raw.items() if k not in ("tool", "username")}

    stroke = sanitize_stroke(raw)

    if stroke is None:
        error(conn, "Invalid stroke data")
        return

    # Ownership and tool are decided by the server, never trusted from client
    stroke["username"] = username
    stroke["tool"] = "eraser" if msg_type == "erase" else "pen"

    with state_lock:
        # Idempotency: a stroke id seen before (client re-sent it after a
        # reconnect, or a duplicated packet) is acknowledged but not re-added.
        if canvas_manager.has_stroke(stroke["stroke_id"]):
            send_message(conn, {
                "type": "stroke_duplicate",
                "stroke_id": stroke["stroke_id"],
            })
            return

        canvas_manager.add_stroke(stroke, username)
        broadcast({
            "type": "stroke_added",
            "stroke": stroke,
            "sequence": next_sequence(),
        })


def handle_chat(conn, username, message):
    chat_text = message.get("message")

    if not isinstance(chat_text, str) or not chat_text.strip():
        return

    if len(chat_text) > MAX_CHAT_LENGTH:
        error(conn, "Message too long")
        return

    now = datetime.now()

    with state_lock:
        entry = {
            "type": "chat",
            "username": username,
            "message": chat_text.strip(),
            "timestamp": now.isoformat(timespec="seconds"),  # e.g. 2026-11-10T23:45:01
            "time": now.strftime("%H:%M:%S"),                # for display
            "sequence": next_sequence(),
        }
        chat_history.append(entry)
        broadcast(entry)


def handle_undo_redo(conn, username, msg_type):
    with state_lock:
        if msg_type == "undo":
            stroke = canvas_manager.undo(username)
        else:
            stroke = canvas_manager.redo(username)

        if stroke is None:
            return          # nothing to undo/redo: silently ignored

        # Full snapshot: every client rebuilds the same canvas. "action",
        # "by" and "stroke_id" are extra info a client may ignore.
        broadcast({
            "type": "canvas_state",
            "strokes": canvas_manager.get_canvas_state(),
            "action": msg_type,
            "by": username,
            "stroke_id": stroke.get("stroke_id"),
            "sequence": next_sequence(),
        })

    print(f"[{msg_type.upper()}] {username} -> stroke {stroke.get('stroke_id')}")


def handle_clear(conn, username):
    with state_lock:
        if not session_manager.is_host(conn):
            error(conn, "Only the host can clear the canvas")
            return

        canvas_manager.clear_canvas()
        broadcast({
            "type": "canvas_state",
            "strokes": [],
            "action": "clear",
            "by": username,
            "sequence": next_sequence(),
        })

    print(f"[CLEAR] Canvas cleared by host: {username}")


def handle_save(conn, username):
    if not session_manager.is_host(conn):
        error(conn, "Only the host can save the canvas")
        return

    # Consistent snapshot; file I/O happens outside state_lock so drawing
    # is never paused by a slow disk.
    strokes = canvas_manager.get_canvas_state()

    if save_canvas(strokes):
        send_message(conn, {
            "type": "save_success",
            "message": f"Canvas saved successfully ({len(strokes)} strokes)",
        })
        print(f"[SAVE] Requested by host: {username}")
    else:
        error(conn, "Failed to save canvas")


def handle_load(conn, username):
    if not session_manager.is_host(conn):
        error(conn, "Only the host can load the canvas")
        return

    raw_strokes, load_error = load_canvas()

    # Missing/corrupt file: keep the current canvas untouched
    if raw_strokes is None:
        error(conn, f"Load failed: {load_error}. Canvas unchanged.")
        return

    strokes = []
    for raw in raw_strokes:
        clean = sanitize_stroke(raw)
        if clean is not None:
            clean.setdefault("tool", "pen")
            strokes.append(clean)
    skipped = len(raw_strokes) - len(strokes)

    with state_lock:
        canvas_manager.set_canvas_state(strokes)
        broadcast({
            "type": "canvas_state",
            "strokes": strokes,
            "action": "load",
            "by": username,
            "sequence": next_sequence(),
        })

    text = f"Canvas loaded successfully ({len(strokes)} strokes)"
    if skipped:
        text += f"; skipped {skipped} invalid stroke(s)"

    send_message(conn, {"type": "load_success", "message": text})
    print(f"[LOAD] {text} - requested by host: {username}")


def handle_sync(conn):
    """
    Resend the full canvas to ONE client. A client asks for this when it
    sees a gap in sequence numbers (a missing update). The snapshot carries
    the CURRENT sequence, so the client resumes exactly from there.
    """
    with state_lock:
        send_message(conn, {
            "type": "canvas_state",
            "strokes": canvas_manager.get_canvas_state(),
            "action": "sync",
            "sequence": server_sequence,
        })


def _valid_coord(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and 0 <= value <= 10000)


def handle_cursor(conn, username, message):
    """
    Relay a user's live cursor position, and optionally the points of the
    stroke they are drawing right now ("preview"), to everyone else.

    This is TRANSIENT data: it isn't stored, isn't sequenced and isn't
    part of the canvas. The final stroke still arrives as a normal "draw".
    It lets other users see who is drawing where while it happens. A
    per-client rate limit keeps the traffic bounded.
    """
    now = time.monotonic()

    with state_lock:
        client = clients.get(conn)
        if client is None:
            return
        # At most ~50 cursor updates per second per client
        if now - client.get("last_cursor", 0) < CURSOR_MIN_INTERVAL:
            return
        client["last_cursor"] = now

    relay = {"type": "cursor", "username": username}

    if message.get("hidden"):
        relay["hidden"] = True
    else:
        x, y = message.get("x"), message.get("y")
        if not (_valid_coord(x) and _valid_coord(y)):
            return
        relay["x"], relay["y"] = x, y

    preview = message.get("preview")
    if isinstance(preview, dict):
        sid = preview.get("stroke_id")
        if not isinstance(sid, str) or not 0 < len(sid) <= 64:
            return
        if preview.get("cancel"):
            relay["preview"] = {"stroke_id": sid, "cancel": True}
        else:
            points = preview.get("points")
            check = sanitize_stroke({
                "stroke_id": sid, "color": preview.get("color"),
                "width": preview.get("width"), "points": points,
            })
            if check is None or len(points) > MAX_PREVIEW_POINTS:
                return
            check["tool"] = "eraser" if preview.get("tool") == "eraser" else "pen"
            check["replace"] = bool(preview.get("replace"))
            relay["preview"] = check

    broadcast(relay, exclude=conn)


def handle_message(conn, username, message):
    """Dispatch one message from an already-joined client."""
    msg_type = message.get("type")

    if msg_type in ("draw", "erase"):
        handle_draw(conn, username, message, msg_type)

    elif msg_type == "chat":
        handle_chat(conn, username, message)

    elif msg_type in ("undo", "redo"):
        handle_undo_redo(conn, username, msg_type)

    elif msg_type == "clear":
        handle_clear(conn, username)

    elif msg_type == "save":
        handle_save(conn, username)

    elif msg_type == "load":
        handle_load(conn, username)

    elif msg_type == "cursor":
        handle_cursor(conn, username, message)

    elif msg_type == "sync":
        handle_sync(conn)

    elif msg_type == "ping":
        # Optional application-level heartbeat for clients
        send_message(conn, {"type": "pong"})

    elif msg_type == "join":
        error(conn, "Already joined")

    else:
        error(conn, "Unknown message type")


# ---------------------------------------------------------------------------
# Per-client reader thread
# ---------------------------------------------------------------------------

def handle_client(conn, addr):
    """READER thread: receive, frame and process messages from one client."""

    username = None
    buffer = b""

    configure_socket(conn)
    print(f"[NEW CONNECTION] {addr[0]}:{addr[1]} connected")

    try:
        while True:
            data = conn.recv(RECV_SIZE)

            if not data:
                break                         # client closed the connection

            buffer += data

            # Process every complete newline-delimited message
            while b"\n" in buffer:
                raw_message, buffer = buffer.split(b"\n", 1)

                if len(raw_message) > MAX_MESSAGE_BYTES:
                    error(conn, "Message too large")
                    raise ConnectionError("message too large")

                if not raw_message.strip():
                    continue

                try:
                    message = json.loads(raw_message.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    error(conn, "Invalid JSON message")
                    continue

                if not isinstance(message, dict):
                    error(conn, "Message must be a JSON object")
                    continue

                # First message must be a join request
                if username is None:
                    if message.get("type") != "join":
                        error(conn, "Please join before sending messages")
                        continue
                    username = handle_join(conn, addr, message)
                    continue

                # One bad message must never kill the whole connection
                try:
                    handle_message(conn, username, message)
                except Exception:
                    print(f"[HANDLER ERROR] from {username}:")
                    traceback.print_exc()
                    error(conn, "Internal server error")

            # A partial line that is already too long: refuse to keep buffering
            if len(buffer) > MAX_MESSAGE_BYTES:
                error(conn, "Message too large")
                print(f"[DROPPED] {addr[0]}:{addr[1]} sent an oversized message")
                break

    except (ConnectionError, OSError) as e:
        print(f"[DISCONNECT] {addr[0]}:{addr[1]}: {e}")

    except Exception:
        # Catch-all so cleanup below ALWAYS runs
        print(f"[UNEXPECTED ERROR] {addr[0]}:{addr[1]}:")
        traceback.print_exc()

    finally:
        client = None

        if username is not None:
            with state_lock:
                session_manager.remove_participant(conn)
                client = clients.pop(conn, None)

                participants = session_manager.get_participants()
                current_host = session_manager.get_host()

                broadcast({
                    "type": "user_left",
                    "username": username,
                    "participants": participants,
                    "host": current_host,
                    "sequence": next_sequence(),
                })

            print(f"[LEFT] {username} disconnected")
            if current_host is not None:
                print(f"[HOST] Current host: {current_host}")

        # Stop the writer thread, give it a moment to flush, then close
        if client is not None:
            try:
                client["outbox"].put_nowait(None)
            except queue.Full:
                drop_connection(conn)
            client["writer"].join(timeout=2)

        try:
            conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Server start-up / shutdown
# ---------------------------------------------------------------------------

def get_lan_addresses():
    """Best-effort list of this machine's IPv4 addresses, for the banner."""
    addresses = set()
    try:
        # No packets are sent; this only asks the OS which interface it
        # would use for an outside address.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        addresses.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    addresses.discard("127.0.1.1")
    return sorted(addresses) or ["127.0.0.1"]


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="LAN Collaborative Whiteboard server")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="interface to bind (default 0.0.0.0 = all)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="TCP port (default 5000)")
    parser.add_argument("--room", default=None,
                        help="room code (default: a random 6-character code)")
    parser.add_argument("--password", default="",
                        help="optional room password (default: none)")
    parser.add_argument("--undo-mode", choices=UNDO_MODES, default="user",
                        help="'user' = undo your own last stroke (default); "
                             "'global' = undo the last stroke on the board")
    return parser.parse_args(argv)


_listener = None
_stopping = threading.Event()


def stop_server():
    """
    Stop a server started with start_server() (used when the client app
    hosts a session in-process). Closing the listening socket makes accept()
    fail, and start_server's `finally` saves the canvas and disconnects
    everyone.
    """
    _stopping.set()
    if _listener is not None:
        try:
            _listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        _listener.close()


def start_server(argv=None, ready=None):
    """
    Start the central TCP server and block until it stops.

    `ready` (optional threading.Event) is set once the port is bound. The
    client uses it when hosting a session in a background thread.
    """
    global canvas_manager, session_manager, _listener

    args = parse_args(argv)

    room_code = args.room.strip() if args.room and args.room.strip() else generate_room_code()

    session_manager = SessionManager(room_code=room_code, password=args.password)
    canvas_manager = CanvasManager(undo_mode=args.undo_mode)

    # Restore the last saved canvas (invalid strokes are skipped)
    raw_strokes, _ = load_canvas()
    restored = []
    for raw in raw_strokes or []:
        clean = sanitize_stroke(raw)
        if clean is not None:
            clean.setdefault("tool", "pen")
            restored.append(clean)
    canvas_manager.set_canvas_state(restored)
    if raw_strokes and len(restored) != len(raw_strokes):
        print(f"[LOAD] Skipped {len(raw_strokes) - len(restored)} invalid stroke(s)")

    # Treat `kill <pid>` like Ctrl+C so the canvas is still saved
    # (signal handlers can only be installed from the main thread)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    _stopping.clear()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen()
    _listener = server
    if ready is not None:
        ready.set()

    print("=" * 50)
    print("       COLLABORATIVE WHITEBOARD SERVER")
    print("=" * 50)
    print(f"[STARTED]  Listening on {args.host}:{args.port}")
    for ip in get_lan_addresses():
        print(f"[ADDRESS]  Clients connect to {ip}:{args.port}")
    print(f"[ROOM]     Room code: {session_manager.room_code}")
    print(f"[ROOM]     Password : {'(set)' if args.password else '(none)'}")
    print(f"[CANVAS]   {len(restored)} stroke(s) restored, undo mode: {args.undo_mode}")
    print("[WAITING]  Waiting for clients... (Ctrl+C to stop and save)")
    print("=" * 50)

    try:
        while True:
            try:
                conn, addr = server.accept()
            except OSError:
                if _stopping.is_set():        # stop_server() closed the socket
                    print("\n[SHUTDOWN] Server stopped by host")
                    break
                raise

            threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True,
            ).start()

    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Server shutting down...")

    finally:
        # Save on shutdown so work is not lost
        save_canvas(canvas_manager.get_canvas_state())

        with state_lock:
            connected = list(clients.keys())

        for conn in connected:
            drop_connection(conn)

        try:
            server.close()
        except OSError:
            pass
        time.sleep(0.2)          # let reader threads print their cleanup
        print("[STOPPED] Server closed")


if __name__ == "__main__":
    start_server()
