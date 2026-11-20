"""
client_gui_test.py
------------------
Integration test of the REAL Tkinter client against the REAL server.

The GUI is driven with synthetic mouse events. A second participant ("Bot")
is a raw TCP client, so every check verifies what actually went over the
network.

Run from the project root (needs a display; on the VM just run it):

    python3 tests/client_gui_test.py
    python3 tests/client_gui_test.py --screenshots    # also saves PNGs to docs/screenshots

Expected final line:  ALL CLIENT GUI CHECKS PASSED
"""

import os
import signal
import subprocess
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "tests"))

import tkinter as tk                                          # noqa: E402
from tkinter import messagebox                                # noqa: E402

from backend_e2e_test import Client, Server, free_port, stroke   # noqa: E402
from client.app import WhiteboardApp                          # noqa: E402

ROOM = "GUITEST"
SHOTS = "--screenshots" in sys.argv
messagebox.askyesno = lambda *a, **k: True                    # auto-confirm dialogs
messagebox.showerror = lambda *a, **k: None
results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + ("" if condition else f"   {detail}"))


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.005)


def wait(root, cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def drag(root, canvas, points, slow=0.0):
    canvas.event_generate("<ButtonPress-1>", x=points[0][0], y=points[0][1])
    for x, y in points[1:]:
        canvas.event_generate("<B1-Motion>", x=x, y=y)
        if slow:
            pump(root, slow)
    canvas.event_generate("<ButtonRelease-1>", x=points[-1][0], y=points[-1][1])
    root.update()


def screenshot(root, name):
    if not SHOTS:
        return
    folder = os.path.join(PROJECT_ROOT, "docs", "screenshots")
    os.makedirs(folder, exist_ok=True)
    pump(root, 0.3)
    try:
        from PIL import ImageGrab
        x, y = root.winfo_rootx(), root.winfo_rooty()
        ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(
            os.path.join(folder, f"{name}.png"))
    except Exception:
        subprocess.run(["import", "-window", "root", os.path.join(folder, f"{name}.png")],
                       check=False)


def main():
    data_dir = tempfile.mkdtemp(prefix="wb_gui_")
    srv = Server("--room", ROOM, data_dir=data_dir)
    root = tk.Tk()
    app = WhiteboardApp(root, prefill={"host": "127.0.0.1", "port": srv.port,
                                       "room": ROOM, "username": "Manieesh"})
    screenshot(root, "01_connect_screen")
    app.connect_screen._submit(False)
    check("joins and shows the main view", wait(root, lambda: app.main is not None and app.online))
    check("first user is host", app.host_name == "Manieesh")

    bot = Client(srv.port)
    bot.join("Teammate", room=ROOM)
    check("participant list updates on join",
          wait(root, lambda: app.participants == ["Manieesh", "Teammate"]))
    canvas = app.canvas.canvas
    pump(root, 0.3)

    # --- freehand stroke + live preview -------------------------------------------
    pts = [(100 + i * 6, 150 + int(30 * ((i % 10) / 10))) for i in range(40)]
    drag(root, canvas, pts, slow=0.01)
    added = bot.wait_type("stroke_added")
    check("own stroke reaches other users", added and added["stroke"]["username"] == "Manieesh")
    check("stroke points were simplified",
          added and len(added["stroke"]["points"]) < len(pts), added and len(added["stroke"]["points"]))
    check("other user saw live preview while drawing",
          any(m.get("preview") for m in bot.of_type("cursor")))
    check("pending stroke confirmed by echo",
          wait(root, lambda: not app.canvas.pending and len(app.canvas.order) == 1))

    # --- remote stroke, cursor, chat ---------------------------------------------------
    bot.send({"type": "draw", "stroke": stroke("bot1", color="#E74C3C", width=8,
                                               points=[[300, 300], [500, 420], [700, 300]])})
    check("remote stroke rendered", wait(root, lambda: "bot1" in app.canvas.items))
    bot.send({"type": "cursor", "x": 520, "y": 260})
    check("remote cursor with name shown", wait(root, lambda: "Teammate" in app.canvas.cursors))
    bot.send({"type": "chat", "message": "hello from the other laptop"})
    check("chat shows sender and timestamp", wait(root, lambda: "hello from the other laptop"
                                                    in app.chat.log.get("1.0", "end")))
    app.chat.entry_var.set("hi teammate")
    app.chat._send()
    got = bot.wait_for(lambda m: m.get("type") == "chat" and m["message"] == "hi teammate")
    check("chat from GUI broadcast", got and got["username"] == "Manieesh")

    # --- shapes ---------------------------------------------------------------------------
    app.toolbar.select_tool("rect")
    drag(root, canvas, [(150, 450), (250, 520), (350, 600)])
    m = bot.wait_for(lambda m: m.get("type") == "stroke_added" and len(m["stroke"]["points"]) == 5)
    check("rectangle sent as 5-point stroke", m is not None)
    app.toolbar.select_tool("ellipse")
    drag(root, canvas, [(800, 450), (900, 520), (1000, 600)])
    check("ellipse sent", bot.wait_for(lambda m: m.get("type") == "stroke_added"
                                       and len(m["stroke"]["points"]) == 65) is not None)
    app.toolbar.select_tool("eraser")
    drag(root, canvas, [(300, 300), (400, 360), (500, 420)])
    er = bot.wait_for(lambda m: m.get("type") == "stroke_added" and m["stroke"]["tool"] == "eraser")
    check("eraser stroke tagged eraser by server", er is not None)
    app.toolbar.select_tool("pen")
    wait(root, lambda: len(app.canvas.order) == 5)
    screenshot(root, "02_drawing_session")

    # --- per-user undo ----------------------------------------------------------------------
    before = list(app.canvas.order)
    mark = bot.mark()
    app._action("undo")
    u = bot.wait_for(lambda m: m.get("action") == "undo", start=mark)
    check("undo removes my last stroke, not the teammate's",
          u and u["by"] == "Manieesh" and "bot1" in [s["stroke_id"] for s in u["strokes"]])
    check("canvas updated by diff", wait(root, lambda: len(app.canvas.order) == len(before) - 1))
    app._action("redo")
    check("redo restores it", wait(root, lambda: len(app.canvas.order) == len(before)))

    # --- duplicate and gap handling --------------------------------------------------------
    n = len(app.canvas.order)
    app.events.put(("message", {"type": "stroke_added", "sequence": app.last_seq,
                                "stroke": stroke("ghost", username="X", tool="pen")}))
    pump(root, 0.3)
    check("duplicate (old sequence) ignored", "ghost" not in app.canvas.items and
          len(app.canvas.order) == n)
    # Simulate a LOST update: the client silently drops the next stroke_added
    # from the network, then a later update arrives with a sequence gap.
    real_handler, dropped = app._on_message, []

    def lossy(m):
        if m.get("type") == "stroke_added" and not dropped:
            dropped.append(m["stroke"]["stroke_id"])
            return
        real_handler(m)

    app._on_message = lossy
    bot.send({"type": "draw", "stroke": stroke("lost1", points=[[900, 100], [950, 150]])})
    wait(root, lambda: dropped, 3)
    check("update was lost on the way", dropped == ["lost1"] and "lost1" not in app.canvas.items)
    sent = []
    app.net.send, real_send = (lambda msg: sent.append(msg["type"]) or real_send(msg)), app.net.send
    bot.send({"type": "draw", "stroke": stroke("after_gap", points=[[960, 100], [990, 150]])})
    check("gap detected -> client requests sync", wait(root, lambda: "sync" in sent))
    check("resync recovers the lost stroke",
          wait(root, lambda: "lost1" in app.canvas.items and "after_gap" in app.canvas.items
               and not app.sync_requested))
    app._on_message, app.net.send = real_handler, real_send

    # --- host-only controls --------------------------------------------------------------
    mark = bot.mark()
    bot.send({"type": "clear"})
    check("non-host cannot clear", bot.wait_type("error", start=mark) is not None)
    app._action("save")
    check("host can save", wait(root, lambda: "saved" in app.notice.cget("text").lower()))

    # --- export ---------------------------------------------------------------------------
    png = os.path.join(data_dir, "board.png")
    app.canvas.export(png)
    check("export PNG", os.path.getsize(png) > 1000)

    # --- server crash, offline drawing, auto-reconnect -------------------------------------
    bot.close()
    srv.stop(signal.SIGINT)
    check("detects lost connection", wait(root, lambda: not app.online, timeout=8))
    screenshot(root, "03_reconnecting")
    drag(root, canvas, [(600, 700), (650, 720), (700, 700), (750, 720)])
    check("can keep drawing offline", len(app.canvas.pending) == 1)
    offline_id = next(iter(app.canvas.pending))
    port = srv.port
    env = dict(os.environ, WHITEBOARD_DATA_DIR=data_dir)
    proc = subprocess.Popen([sys.executable, "-m", "server.server", "--host", "127.0.0.1",
                             "--port", str(port), "--room", ROOM], cwd=PROJECT_ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        check("auto-reconnects after server restart", wait(root, lambda: app.online, timeout=15))
        check("offline stroke synced after reconnect",
              wait(root, lambda: offline_id in app.canvas.items and not app.canvas.pending, 5))
        check("saved board restored after restart", "bot1" in app.canvas.items)
        late = Client(port)
        late.join("Latecomer", room=ROOM)
        cs = late.wait_type("canvas_state")
        check("new joiner receives full board incl. offline stroke",
              cs and offline_id in [s["stroke_id"] for s in cs["strokes"]])
        late.close()
    finally:
        app._leave(confirm=False)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)

    # --- hosting from the GUI ---------------------------------------------------------------
    host_port = free_port()
    app.connect_screen.vars["port"].set(str(host_port))
    app.connect_screen.vars["room"].set("")
    os.environ["WHITEBOARD_DATA_DIR"] = data_dir
    app.connect_screen._submit(True)
    check("host mode starts embedded server and joins",
          wait(root, lambda: app.main is not None and app.online and app.hosting, 8))
    room = app.connect_screen.vars["room"].get()
    guest = Client(host_port)
    check("guest joins hosted session with generated code", guest.join("Guest", room=room)
          .get("type") == "welcome")
    wait(root, lambda: "Guest" in app.participants)
    screenshot(root, "04_hosting")
    guest.close()
    app._leave(confirm=False)
    check("leaving stops the hosted server", wait(root, lambda: app.main is None))

    root.destroy()
    failed = [n for n, ok in results if not ok]
    print("-" * 60)
    print(f"{len(failed)} CHECK(S) FAILED: {failed}" if failed else
          f"ALL CLIENT GUI CHECKS PASSED ({len(results)})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
