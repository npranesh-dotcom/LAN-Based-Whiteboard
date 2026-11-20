"""
app.py
------
The application controller. It builds the windows, connects the widgets to
the NetworkClient, and applies every server message to the UI.

Threading model
===============
Only the Tk main thread touches widgets. The network threads put events on
`self.events`; `_pump()` drains them every POLL_MS ms through root.after(),
at most MAX_EVENTS_PER_POLL per tick so a burst (e.g. a 2000-stroke
canvas_state) never freezes the window.

Ordering, duplicates and gaps (requirement 3: "Ordering and Reliability")
===========================================================================
Every state-changing server message carries a global, increasing
`sequence`. The client remembers the last one it applied (`last_seq`):
    seq <= last_seq      -> duplicate or late: ignored
    seq == last_seq + 1  -> normal: applied
    seq >  last_seq + 1  -> a gap (an update went missing): applied, and a
                            "sync" is requested; the server answers with a
                            full canvas_state at its current sequence.
A canvas_state is a snapshot, so it resets last_seq. Stroke ids also make
drawing idempotent on both sides.

Recovery (requirement 5)
========================
After a dropped connection NetworkClient reconnects by itself. The new
welcome and canvas_state rebuild the board. Strokes the user drew while
offline, or whose acknowledgement was lost, are still "pending" and are
re-sent after the snapshot. The server ignores any it already has.
"""

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from client import config, geometry, protocol
from client.canvas_view import WhiteboardCanvas
from client.connect_screen import ConnectScreen
from client.network import NetworkClient
from client.side_panel import ChatPanel, ParticipantList
from client.toolbar import Toolbar
from client.widgets import FlatButton, ScrollFrame

T = config.THEME
STATUS_COLORS = {"connected": T["ok"], "connecting": T["warn"],
                 "reconnecting": T["warn"], "disconnected": T["danger"]}


class WhiteboardApp:

    def __init__(self, root, prefill=None):
        self.root = root
        root.title("Collaborative Whiteboard")
        # Fit the window to the screen (a VirtualBox VM is often only 1024x768)
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        win_w, win_h = min(1440, screen_w - 40), min(880, screen_h - 90)
        root.geometry(f"{win_w}x{win_h}+{(screen_w - win_w) // 2}"
                      f"+{max((screen_h - win_h) // 2 - 20, 0)}")
        root.minsize(min(960, win_w), min(560, win_h))
        root.configure(bg=T["bg"])
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._style_ttk(root)

        self.events = queue.Queue()
        self.net = NetworkClient(self.events)
        self.main = None                 # main view frame (built after first welcome)
        self.hosting = False
        self._reset_session_state()

        self.connect_screen = ConnectScreen(root, self._on_submit)
        self.connect_screen.pack(fill="both", expand=True)
        if prefill:
            for key, value in prefill.items():
                if value is not None and key in self.connect_screen.vars:
                    self.connect_screen.vars[key].set(str(value))
        self.connect_screen.focus()

        self._pump()
        self._live_loop()
        self._maintenance_loop()

    @staticmethod
    def _style_ttk(root):
        """Dark styles for the ttk slider and scrollbars ('clam' allows recolouring)."""
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Wb.Horizontal.TScale", background=T["accent"],
                        troughcolor=T["panel_alt"], bordercolor=T["panel"],
                        lightcolor=T["accent"], darkcolor=T["accent"])
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"Wb.{orient}.TScrollbar", background=T["border"],
                            troughcolor=T["panel"], bordercolor=T["panel"],
                            arrowcolor=T["muted"], lightcolor=T["border"],
                            darkcolor=T["border"], gripcount=0)
            style.map(f"Wb.{orient}.TScrollbar", background=[("active", T["muted"])])

    def _reset_session_state(self):
        self.me = None
        self.host_name = None
        self.participants = []
        self.last_seq = 0
        self.sync_requested = False
        self.resend_after_snapshot = False
        self.seen_chat = set()
        self.online = False
        self.user_left_on_purpose = False

    # ===== connect / host ========================================================

    def _on_submit(self, params, host_mode):
        self.connect_screen.set_busy(True)
        self.params = params
        if host_mode:
            self._start_hosting(params)
        else:
            self.net.connect(params["host"], params["port"], params["username"],
                             params["room"], params["password"])

    def _start_hosting(self, params):
        """Run the server in a background thread of THIS process, then join it."""
        from server import server as srv                 # only needed when hosting
        from server.session_manager import generate_room_code

        room = params["room"] or generate_room_code()
        params["room"] = room
        self.connect_screen.vars["room"].set(room)
        argv = ["--host", "0.0.0.0", "--port", str(params["port"]), "--room", room]
        if params["password"]:
            argv += ["--password", params["password"]]
        ready, failure = threading.Event(), {}

        def run():
            try:
                srv.start_server(argv, ready=ready)
            except OSError as e:
                failure["error"] = e
                ready.set()

        threading.Thread(target=run, daemon=True).start()

        def wait_ready(tries=0):
            if not ready.is_set() and tries < 60:
                self.root.after(50, wait_ready, tries + 1)
                return
            if "error" in failure or not ready.is_set():
                err = failure.get("error", "timed out")
                self.connect_screen.show_message(
                    f"Could not start server on port {params['port']}: {err}. "
                    "Is another server already running?")
                self.connect_screen.set_busy(False)
                return
            self.hosting = True
            self.lan_addresses = srv.get_lan_addresses()
            self.net.connect("127.0.0.1", params["port"], params["username"], room,
                             params["password"])

        wait_ready()

    def _stop_hosting(self):
        if self.hosting:
            from server import server as srv
            srv.stop_server()
            self.hosting = False

    # ===== main view =============================================================

    def _build_main(self):
        root = self.root
        self.main = tk.Frame(root, bg=T["bg"])

        # --- top bar -------------------------------------------------------------
        top = tk.Frame(self.main, bg=T["panel"], height=52, padx=14)
        top.pack(fill="x", side="top")
        tk.Label(top, text="◆ Whiteboard", bg=T["panel"], fg=T["accent"],
                 font=(config.FONT, 13, "bold")).pack(side="left")
        self.room_chip = tk.Label(top, text="", bg=T["panel_alt"], fg=T["text"],
                                  font=T["font_bold"], padx=10, pady=4)
        self.room_chip.pack(side="left", padx=(16, 6), pady=10)
        self.addr_label = tk.Label(top, text="", bg=T["panel"], fg=T["muted"],
                                   font=T["font_small"])
        self.addr_label.pack(side="left", padx=6)
        self.leave_btn = FlatButton(top, "Leave", self._leave, danger=True)
        self.leave_btn.pack(side="right", pady=8)
        self.status_pill = tk.Label(top, text="", bg=T["panel"], fg=T["text"],
                                    font=T["font_small"], padx=10)
        self.status_pill.pack(side="right", padx=8)
        self.me_label = tk.Label(top, text="", bg=T["panel"], fg=T["text"],
                                 font=T["font_bold"])
        self.me_label.pack(side="right", padx=8)

        # --- status bar ----------------------------------------------------------
        bottom = tk.Frame(self.main, bg=T["panel"], height=26, padx=12)
        bottom.pack(fill="x", side="bottom")
        self.notice = tk.Label(bottom, text="", bg=T["panel"], fg=T["muted"],
                               font=T["font_small"], anchor="w")
        self.notice.pack(side="left", fill="x", expand=True)
        self.stats = tk.Label(bottom, text="", bg=T["panel"], fg=T["muted"],
                              font=T["font_small"])
        self.stats.pack(side="right")
        self.hover_label = tk.Label(bottom, text="", bg=T["panel"], fg=T["accent"],
                                    font=T["font_small"])
        self.hover_label.pack(side="right", padx=16)

        # --- body: toolbar | canvas | side panel ---------------------------------
        body = tk.Frame(self.main, bg=T["bg"])
        body.pack(fill="both", expand=True)
        self.canvas = WhiteboardCanvas(body, self._send_stroke,
                                       lambda text: self.hover_label.configure(text=text))
        tool_holder = ScrollFrame(body, T["panel"])      # scrolls only if the window is short
        self.toolbar = Toolbar(tool_holder.inner,
                               on_tool=lambda t: setattr(self.canvas, "tool", t),
                               on_color=lambda c: setattr(self.canvas, "color", c),
                               on_width=lambda w: setattr(self.canvas, "width", w),
                               on_action=self._action)
        side = tk.Frame(body, bg=T["panel"], width=310)
        side.pack_propagate(False)
        self.toolbar.pack(fill="both", expand=True)
        tool_holder.pack(side="left", fill="y")
        side.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True, padx=1)

        self.people = ParticipantList(side)
        self.people.pack(fill="x")
        tk.Frame(side, bg=T["border"], height=1).pack(fill="x", padx=12, pady=6)
        self.chat = ChatPanel(side, self._send_chat)
        self.chat.pack(fill="both", expand=True)

        self._bind_shortcuts()

    def _show_main(self):
        if self.main is None:
            self._build_main()
        self.connect_screen.pack_forget()
        self.main.pack(fill="both", expand=True)

    def _show_connect(self, message=""):
        if self.main is not None:
            self.main.destroy()
            self.main = None
        self.connect_screen.pack(fill="both", expand=True)
        self.connect_screen.set_busy(False)
        self.connect_screen.show_message(message)

    def _bind_shortcuts(self):
        def guarded(fn):
            def handler(event):
                if isinstance(event.widget, tk.Entry):      # typing in chat
                    return None
                fn()
                return "break"
            return handler
        r = self.root
        r.bind("<Control-z>", guarded(lambda: self._action("undo")))
        r.bind("<Control-y>", guarded(lambda: self._action("redo")))
        r.bind("<Control-Shift-Z>", guarded(lambda: self._action("redo")))
        r.bind("<Control-s>", guarded(lambda: self._action("save")))
        for key, tool in (("p", "pen"), ("l", "line"), ("r", "rect"),
                          ("o", "ellipse"), ("e", "eraser")):
            r.bind(f"<KeyPress-{key}>", guarded(lambda t=tool: self.toolbar.select_tool(t)))
        r.bind("<bracketleft>", guarded(lambda: self.toolbar.change_size(-2)))
        r.bind("<bracketright>", guarded(lambda: self.toolbar.change_size(2)))

    # ===== UI -> network ==========================================================

    def _send_stroke(self, stroke, eraser):
        """Canvas finished a stroke (already shown as pending)."""
        if not self.net.send(protocol.draw(stroke, eraser)):
            self._notice("Offline - your drawing is kept and will sync when reconnected",
                         T["warn"])

    def _send_chat(self, text):
        if self.net.send(protocol.chat(text)):
            return True
        self._notice("Not connected - message not sent", T["danger"])
        return False

    def _action(self, action):
        if self.main is None:
            return
        if action == "export":
            return self._export()
        if action in ("clear", "save", "load") and self.host_name != self.me:
            return self._notice("Only the host can do that", T["danger"])
        if action == "clear" and not messagebox.askyesno(
                "Clear board", "Remove everything from the board for ALL participants?"):
            return
        if action == "load" and not messagebox.askyesno(
                "Restore board", "Replace the current board with the last saved version?"):
            return
        if not self.net.send(protocol.simple(action)):
            self._notice("Not connected", T["danger"])

    def _export(self):
        path = filedialog.asksaveasfilename(
            title="Export board", defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("PostScript", "*.ps")])
        if not path:
            return
        try:
            self.canvas.export(path)
            self._notice(f"Exported to {path}", T["ok"])
        except ImportError:
            messagebox.showwarning("Pillow missing",
                                   "PNG export needs Pillow (pip install pillow). "
                                   "Choose a .ps file name instead.")
        except OSError as e:
            messagebox.showerror("Export failed", str(e))

    def _live_loop(self):
        """Send cursor position and live-stroke preview, batched every 40 ms."""
        if self.main is not None and self.online:
            update = self.canvas.take_live_update()
            if update:
                preview = update.get("preview")
                if update.get("hidden"):
                    self.net.send(protocol.cursor_hidden(preview))
                else:
                    self.net.send(protocol.cursor(update["x"], update["y"], preview))
        self.root.after(config.PREVIEW_INTERVAL_MS, self._live_loop)

    def _maintenance_loop(self):
        if self.main is not None:
            for _ in self.canvas.expire_pending(self.online):
                self._notice("A stroke was not confirmed by the server and was removed",
                             T["warn"])
            self.canvas.expire_cursors()
            self._refresh_stats()
        self.root.after(1000, self._maintenance_loop)

    # ===== network -> UI ==========================================================

    def _pump(self):
        """Drain network events on the Tk thread."""
        handled = 0
        try:
            while handled < config.MAX_EVENTS_PER_POLL:
                kind, payload = self.events.get_nowait()
                handled += 1
                if kind == "message":
                    self._on_message(payload)
                elif kind == "status":
                    self._on_status(payload)
                elif kind == "join_failed":
                    self._on_join_failed(payload)
                elif kind == "latency":
                    self.latency = payload
        except queue.Empty:
            pass
        except Exception as e:                       # never let one bad event kill the loop
            import traceback
            traceback.print_exc()
            self._notice(f"Client error: {e}", T["danger"])
        if handled and self.main is not None:
            self._refresh_stats()
        self.root.after(1 if handled >= config.MAX_EVENTS_PER_POLL else config.POLL_MS,
                        self._pump)

    def _check_sequence(self, msg):
        """Apply the ordering rules. Returns False if the message must be ignored."""
        kind, seq = msg.get("type"), msg.get("sequence")
        if kind not in protocol.SEQUENCED or seq is None or msg.get("history"):
            return True
        if kind == "welcome":
            self.last_seq = seq                       # new connection: new baseline
            return True
        if kind == "canvas_state":
            # A snapshot we asked for is always authoritative, even if our
            # counter got ahead of it; any other older snapshot is stale.
            requested = self.sync_requested and msg.get("action") == "sync"
            if seq < self.last_seq and not requested:
                return False
            self.last_seq, self.sync_requested = seq, False
            return True
        if seq <= self.last_seq:
            return False                              # duplicate / already applied
        if seq > self.last_seq + 1 and not self.sync_requested:
            self.sync_requested = True                # gap: ask for a fresh snapshot
            self.net.send(protocol.simple("sync"))
            self._notice(f"Missed update(s) {self.last_seq + 1}-{seq - 1}; resyncing",
                         T["warn"])
        self.last_seq = seq
        return True

    def _on_message(self, m):
        kind = m.get("type")
        if not self._check_sequence(m):
            return
        handler = getattr(self, f"_msg_{kind}", None)
        if handler:
            handler(m)

    def _msg_welcome(self, m):
        reconnect = self.me is not None
        self.me = m["username"]
        self._show_main()
        self.online = True
        self.resend_after_snapshot = bool(self.canvas.pending)
        self.room_chip.configure(text=f"Room  {m.get('room_code', self.params['room'])}")
        if self.hosting:
            # 127.x only works on this machine, so show it only if nothing else exists
            lan = [ip for ip in self.lan_addresses if not ip.startswith("127.")] \
                or self.lan_addresses
            addrs = ", ".join(f"{ip}:{self.params['port']}" for ip in lan)
            self.addr_label.configure(text=f"Hosting · others join at {addrs}")
        else:
            self.addr_label.configure(text=f"Server {self.params['host']}:{self.params['port']}")
        self.me_label.configure(text=f"● {self.me}", fg=geometry.user_color(self.me))
        self.toolbar.set_undo_mode(m.get("undo_mode", "user"))
        self.canvas.clear_remote()
        self._set_people(m["participants"], m.get("host"))
        self.chat.add_system("Reconnected - board restored." if reconnect else
                             f"You joined as {self.me}.")
        self._set_online(True)

    def _msg_canvas_state(self, m):
        self.canvas.apply_state(m["strokes"])
        action, by = m.get("action"), m.get("by")
        if action == "clear":
            self.chat.add_system(f"{by} cleared the board.")
        elif action == "load":
            self.chat.add_system(f"{by} restored the last saved board.")
        elif action in ("undo", "redo") and by != self.me:
            self._notice(f"{by} used {action}")
        if self.resend_after_snapshot:
            self.resend_after_snapshot = False
            unsent = list(self.canvas.pending.values())
            for p in unsent:
                self.net.send(protocol.draw(p["stroke"], p["stroke"]["tool"] == "eraser"))
            self.canvas.touch_pending()
            if unsent:
                self._notice(f"Synced {len(unsent)} stroke(s) drawn while offline", T["ok"])

    def _msg_stroke_added(self, m):
        self.canvas.add_stroke(m["stroke"])

    def _msg_stroke_duplicate(self, m):
        self.canvas.drop_pending(m["stroke_id"])

    def _msg_user_joined(self, m):
        self._set_people(m["participants"], m.get("host"))
        self.chat.add_system(f"{m['username']} joined.")

    def _msg_user_left(self, m):
        old_host = self.host_name
        self._set_people(m["participants"], m.get("host"))
        self.canvas.remove_user(m["username"])
        self.chat.add_system(f"{m['username']} left.")
        if self.host_name != old_host and self.host_name:
            self.chat.add_system("You are now the host." if self.host_name == self.me
                                 else f"{self.host_name} is now the host.")

    def _msg_chat(self, m):
        seq = m.get("sequence")
        if seq in self.seen_chat:                    # history replayed after reconnect
            return
        self.seen_chat.add(seq)
        self.chat.add_message(m)

    def _msg_cursor(self, m):
        self.canvas.remote_cursor(m)

    def _msg_error(self, m):
        text = m.get("message", "Error")
        self._notice(text, T["danger"])
        if text == "Invalid stroke data":
            self.chat.add_system("The server rejected a stroke.")

    def _msg_save_success(self, m):
        self._notice(m.get("message", "Saved"), T["ok"])

    def _msg_load_success(self, m):
        self._notice(m.get("message", "Restored"), T["ok"])

    def _on_status(self, s):
        state = s["state"]
        if self.main is None:
            if state in ("connecting", "reconnecting"):
                self.connect_screen.show_message("Connecting…", ok=True)
            return
        labels = {"connected": "● Connected", "connecting": "● Connecting…",
                  "reconnecting": "● Reconnecting…", "disconnected": "● Offline"}
        self.status_pill.configure(text=labels[state], fg=STATUS_COLORS[state])
        if state != "connected":
            self.online = False
            self._set_online(False)
            if s.get("detail"):
                self._notice(s["detail"], T["warn"])
        if state == "disconnected" and not self.user_left_on_purpose:
            self.chat.add_system("Disconnected from the server. Press Leave to reconnect.")

    def _on_join_failed(self, reason):
        if self.hosting:
            self._stop_hosting()
        if self.main is None:
            self.connect_screen.show_message(reason)
            self.connect_screen.set_busy(False)
        else:
            messagebox.showerror("Could not rejoin", reason)
            self._leave(confirm=False)

    # ===== helpers ===============================================================

    def _set_people(self, participants, host):
        self.participants, self.host_name = participants, host
        self.people.show(participants, host, self.me)
        self.toolbar.set_host(host == self.me, self.online)

    def _set_online(self, online):
        self.canvas.enabled = True            # drawing works offline too (queued)
        self.toolbar.set_online(online)
        self.toolbar.set_host(self.host_name == self.me, online)
        self.chat.set_online(online)
        if online:
            self.status_pill.configure(text="● Connected", fg=T["ok"])

    def _notice(self, text, color=None):
        if self.main is None:
            return
        self.notice.configure(text=text, fg=color or T["muted"])
        token = object()
        self._notice_token = token
        self.root.after(6000, lambda: getattr(self, "_notice_token", None) is token
                        and self.main is not None and self.notice.configure(text=""))

    def _refresh_stats(self):
        latency = getattr(self, "latency", None)
        parts = [f"{len(self.canvas.order)} strokes"]
        if self.canvas.pending:
            parts.append(f"{len(self.canvas.pending)} pending")
        if latency is not None and self.online:
            parts.append(f"{latency:.0f} ms")
        parts.append(f"seq {self.last_seq}")
        self.stats.configure(text="   ·   ".join(parts))

    def _leave(self, confirm=True):
        if confirm and self.hosting and not messagebox.askyesno(
                "Stop hosting", "You are hosting. Leaving stops the server for everyone "
                                "(the board is saved). Continue?"):
            return
        self.user_left_on_purpose = True
        self.net.disconnect()
        self._stop_hosting()
        self.events = queue.Queue()              # discard events from the old session
        self.net = NetworkClient(self.events)
        self._reset_session_state()
        self._show_connect("You left the session.")

    def _on_close(self):
        if self.hosting and not messagebox.askyesno(
                "Quit", "You are hosting. Quitting stops the server for everyone "
                        "(the board is saved). Quit?"):
            return
        self.net.disconnect()
        self._stop_hosting()
        self.root.destroy()
