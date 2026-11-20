"""
side_panel.py
-------------
Right-hand panel: the participant list and the group chat.

Participants: coloured dot (the user's identity colour, also used for
their cursor), a crown for the host, and "(you)" for this user.

Chat: messages are shown in chronological order with timestamp and sender.
History replayed on join is shown first, followed by a divider. Join, leave
and host-change notices appear in grey italics. Messages are inserted into a
read-only Text widget with one colour tag per user.
"""

import tkinter as tk
from tkinter import ttk

from client import config, geometry
from client.widgets import FlatButton, section_label

T = config.THEME


class ParticipantList(tk.Frame):

    def __init__(self, master):
        super().__init__(master, bg=T["panel"])
        self.title = section_label(self, "Participants")
        self.title.pack(fill="x", padx=12, pady=(10, 4))
        self.box = tk.Text(self, height=7, bg=T["panel"], fg=T["text"], bd=0,
                           highlightthickness=0, font=T["font"], cursor="arrow",
                           padx=12, pady=2, state="disabled")
        self.box.pack(fill="x")

    def show(self, participants, host, me):
        self.title.configure(text=f"PARTICIPANTS  ·  {len(participants)} ONLINE")
        box = self.box
        box.configure(state="normal")
        box.delete("1.0", "end")
        for name in participants:
            tag = f"u_{name}"
            box.tag_configure(tag, foreground=geometry.user_color(name))
            box.insert("end", "● ", tag)
            box.insert("end", name)
            if name == host:
                box.insert("end", "  ♛ host", "host")
            if name == me:
                box.insert("end", "  (you)", "me")
            box.insert("end", "\n")
        box.tag_configure("host", foreground=T["warn"], font=T["font_small"])
        box.tag_configure("me", foreground=T["muted"], font=T["font_small"])
        box.configure(height=max(3, min(len(participants), 8)), state="disabled")


class ChatPanel(tk.Frame):

    def __init__(self, master, on_send):
        super().__init__(master, bg=T["panel"])
        self.on_send = on_send
        self._known_tags = set()
        self._history_open = False

        section_label(self, "Group chat").pack(fill="x", padx=12, pady=(10, 4))
        body = tk.Frame(self, bg=T["panel"])
        body.pack(fill="both", expand=True, padx=(12, 4))
        self.log = tk.Text(body, bg=T["panel_alt"], fg=T["text"], bd=0, wrap="word",
                           highlightthickness=0, font=T["font"], padx=8, pady=6,
                           spacing1=2, spacing3=4, state="disabled", width=30)
        bar = ttk.Scrollbar(body, command=self.log.yview, style="Wb.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=bar.set)
        self.log.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.log.tag_configure("time", foreground=T["muted"], font=T["font_small"])
        self.log.tag_configure("system", foreground=T["muted"],
                               font=(config.FONT, 9, "italic"))
        self.log.tag_configure("divider", foreground=T["border"], justify="center",
                               font=T["font_small"])

        entry_row = tk.Frame(self, bg=T["panel"])
        entry_row.pack(fill="x", padx=12, pady=10)
        self.entry_var = tk.StringVar()
        self.entry_var.trace_add("write", self._limit)
        self.entry = tk.Entry(entry_row, textvariable=self.entry_var, bg=T["panel_alt"],
                              fg=T["text"], insertbackground=T["text"], bd=0,
                              disabledbackground=T["panel"], disabledforeground=T["muted"],
                              highlightthickness=1, highlightbackground=T["border"],
                              highlightcolor=T["accent"], font=T["font"])
        self.entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.entry.bind("<Return>", lambda e: self._send())
        self.send_btn = FlatButton(entry_row, "Send", self._send, accent=True)
        self.send_btn.pack(side="left", padx=(6, 0))
        self.counter = tk.Label(self, text="", bg=T["panel"], fg=T["muted"],
                                font=(config.FONT, 8), anchor="e")
        self.counter.pack(fill="x", padx=12, pady=(0, 6))

    # ----- input ---------------------------------------------------------------

    def _limit(self, *_):
        text = self.entry_var.get()
        if len(text) > config.MAX_CHAT:
            self.entry_var.set(text[:config.MAX_CHAT])
        n = len(self.entry_var.get())
        self.counter.configure(text=f"{n}/{config.MAX_CHAT}" if n > config.MAX_CHAT - 100 else "")

    def _send(self):
        text = self.entry_var.get().strip()
        if text and self.on_send(text):
            self.entry_var.set("")

    def set_online(self, online):
        self.entry.configure(state="normal" if online else "disabled")
        self.send_btn.set_enabled(online)

    # ----- output --------------------------------------------------------------

    def _append(self, parts):
        log = self.log
        at_bottom = log.yview()[1] > 0.98         # don't yank the view if user scrolled up
        log.configure(state="normal")
        for text, tag in parts:
            log.insert("end", text, tag)
        log.insert("end", "\n")
        # Bound memory: keep the last 1000 lines
        if int(log.index("end-1c").split(".")[0]) > 1000:
            log.delete("1.0", "200.0")
        log.configure(state="disabled")
        if at_bottom:
            log.see("end")

    def add_message(self, msg):
        """Render a server chat message (live or history)."""
        user = msg.get("username", "?")
        tag = f"u_{user}"
        if tag not in self._known_tags:
            self.log.tag_configure(tag, foreground=geometry.user_color(user),
                                   font=T["font_bold"])
            self._known_tags.add(tag)
        if msg.get("history"):
            self._history_open = True
        elif self._history_open:
            self._history_open = False
            self._append([("── earlier messages above ──", "divider")])
        stamp = msg.get("time") or msg.get("timestamp", "")[-8:]
        self._append([(f"{stamp}  ", "time"), (user, tag), ("  " + msg.get("message", ""), None)])

    def add_system(self, text):
        if self._history_open:
            self._history_open = False
            self._append([("── earlier messages above ──", "divider")])
        self._append([(text, "system")])

    def clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._history_open = False
