"""
connect_screen.py
-----------------
First screen of the app. The user either:

* Joins a session: enters the server's LAN IP, port, room code, optional
  password and a username; or
* Hosts a session: the server runs inside this app, a room code is
  generated, and the user joins it automatically. Other machines then join
  using this computer's LAN IP (shown after hosting).

Inputs are validated before anything touches the network. The last values
used (never the password) are remembered in ~/.cn_whiteboard.json.
"""

import json
import os
import tkinter as tk

from client import config
from client.widgets import FlatButton

T = config.THEME
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".cn_whiteboard.json")


class ConnectScreen(tk.Frame):

    def __init__(self, master, on_submit):
        super().__init__(master, bg=T["bg"])
        self.on_submit = on_submit            # callback(params: dict, host: bool)
        saved = self._load_settings()

        card = tk.Frame(self, bg=T["panel"], padx=36, pady=30,
                        highlightthickness=1, highlightbackground=T["border"])
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="Collaborative Whiteboard", bg=T["panel"], fg=T["text"],
                 font=T["font_title"]).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(card, text="Real-time drawing over your local network. No internet needed.",
                 bg=T["panel"], fg=T["muted"], font=T["font_small"]).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 18))

        self.vars = {}
        fields = [
            ("username", "Your name", saved.get("username", ""), False),
            ("host", "Server address (LAN IP)", saved.get("host", config.DEFAULT_HOST), False),
            ("port", "Port", str(saved.get("port", config.DEFAULT_PORT)), False),
            ("room", "Room code", saved.get("room", ""), False),
            ("password", "Room password (optional)", "", True),
        ]
        for i, (key, label, value, secret) in enumerate(fields):
            tk.Label(card, text=label, bg=T["panel"], fg=T["muted"],
                     font=T["font_small"]).grid(row=2 + 2 * i, column=0, columnspan=2,
                                                sticky="w")
            var = tk.StringVar(value=value)
            entry = tk.Entry(card, textvariable=var, show="•" if secret else "",
                             bg=T["panel_alt"], fg=T["text"], insertbackground=T["text"],
                             bd=0, highlightthickness=1, highlightbackground=T["border"],
                             highlightcolor=T["accent"], font=T["font"], width=36)
            entry.grid(row=3 + 2 * i, column=0, columnspan=2, sticky="ew", ipady=6,
                       pady=(2, 10))
            entry.bind("<Return>", lambda e: self._submit(False))
            self.vars[key] = var
            if key == "username":
                self.first_entry = entry

        buttons = tk.Frame(card, bg=T["panel"])
        buttons.grid(row=20, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.join_btn = FlatButton(buttons, "Join session", lambda: self._submit(False),
                                   accent=True)
        self.join_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.host_btn = FlatButton(buttons, "Host new session", lambda: self._submit(True),
                                   tooltip="Run the server on this computer and join it")
        self.host_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.message = tk.Label(card, text="", bg=T["panel"], fg=T["danger"],
                                font=T["font_small"], wraplength=330, justify="left")
        self.message.grid(row=21, column=0, columnspan=2, sticky="w", pady=(12, 0))
        tk.Label(card, text="Hosting: leave Room code empty to generate one.",
                 bg=T["panel"], fg=T["muted"], font=(config.FONT, 8)).grid(
            row=22, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def focus(self):
        self.first_entry.focus_set()

    # ----- validation ------------------------------------------------------------

    def _submit(self, host_mode):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        v["password"] = self.vars["password"].get()       # keep spaces in passwords
        error = None
        if not v["username"] or len(v["username"]) > config.MAX_USERNAME:
            error = f"Name must be 1-{config.MAX_USERNAME} characters."
        elif not v["port"].isdigit() or not 0 < int(v["port"]) < 65536:
            error = "Port must be a number between 1 and 65535."
        elif not host_mode and not v["host"]:
            error = "Enter the server's IP address (ask the host)."
        elif not host_mode and not v["room"]:
            error = "Enter the room code shown on the host's screen."
        if error:
            self.show_message(error)
            return
        v["port"] = int(v["port"])
        self._save_settings(v)
        self.show_message("Starting server…" if host_mode else "Connecting…", ok=True)
        self.on_submit(v, host_mode)

    def show_message(self, text, ok=False):
        self.message.configure(text=text, fg=T["ok"] if ok else T["danger"])

    def set_busy(self, busy):
        self.join_btn.set_enabled(not busy)
        self.host_btn.set_enabled(not busy)

    # ----- remembered settings -------------------------------------------------

    @staticmethod
    def _load_settings():
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save_settings(v):
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump({k: v[k] for k in ("username", "host", "port", "room")}, f)
        except OSError:
            pass
