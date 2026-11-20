"""
toolbar.py
----------
Vertical drawing toolbar: tools, colour palette, custom colour, brush size
with a live preview dot, undo/redo and the host-only board actions.

The toolbar holds no drawing logic. It calls back into the app, which
forwards tool changes to the canvas and actions to the network.
"""

import tkinter as tk
from tkinter import colorchooser, ttk

from client import config
from client.widgets import FlatButton, section_label

T = config.THEME

TOOLS = [  # (tool id, icon, tooltip with shortcut)
    ("pen", "✎", "Pen  (P)"),
    ("line", "╱", "Line  (L, Shift = 45°)"),
    ("rect", "▭", "Rectangle  (R, Shift = square)"),
    ("ellipse", "◯", "Ellipse  (O, Shift = circle)"),
    ("eraser", "⌫", "Eraser  (E)"),
]


class Toolbar(tk.Frame):

    def __init__(self, master, on_tool, on_color, on_width, on_action):
        super().__init__(master, bg=T["panel"], padx=10, pady=10)
        self.on_tool, self.on_color, self.on_width = on_tool, on_color, on_width
        self.on_action = on_action
        self.color = "#000000"

        section_label(self, "Tools").pack(fill="x", pady=(0, 4))
        grid = tk.Frame(self, bg=T["panel"])
        grid.pack(fill="x")
        self.tool_buttons = {}
        for i, (tool, icon, tip) in enumerate(TOOLS):
            b = FlatButton(grid, icon, lambda t=tool: self.select_tool(t), tooltip=tip,
                           font=T["font_icon"], width=2)
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2, sticky="ew")
            self.tool_buttons[tool] = b

        section_label(self, "Colour").pack(fill="x", pady=(12, 4))
        swatches = tk.Frame(self, bg=T["panel"])
        swatches.pack(fill="x")
        self.swatches = {}
        for i, color in enumerate(config.PALETTE):
            s = tk.Frame(swatches, bg=color, width=24, height=24, cursor="hand2",
                         highlightthickness=2, highlightbackground=T["panel"])
            s.grid(row=i // 4, column=i % 4, padx=3, pady=3)
            s.bind("<Button-1>", lambda e, c=color: self.select_color(c))
            self.swatches[color] = s
        FlatButton(self, "＋ Custom…", self._pick_color).pack(fill="x", pady=(4, 0))

        size_row = tk.Frame(self, bg=T["panel"])
        size_row.pack(fill="x", pady=(12, 4))
        section_label(size_row, "Brush size").pack(side="left")
        self.size_label = tk.Label(size_row, text="4 px", bg=T["panel"], fg=T["text"],
                                   font=T["font_small"])
        self.size_label.pack(side="right")
        self.size_var = tk.DoubleVar(value=4)
        ttk.Scale(self, from_=config.MIN_WIDTH, to=config.MAX_WIDTH, orient="horizontal",
                  variable=self.size_var, command=self._size_changed,
                  style="Wb.Horizontal.TScale").pack(fill="x")
        self.preview = tk.Canvas(self, width=150, height=56, bg=config.CANVAS_BG,
                                 highlightthickness=1, highlightbackground=T["border"])
        self.preview.pack(pady=(4, 0))

        section_label(self, "Edit").pack(fill="x", pady=(12, 4))
        row = tk.Frame(self, bg=T["panel"])
        row.pack(fill="x")
        self.undo_btn = FlatButton(row, "↶ Undo", lambda: on_action("undo"), tooltip="Ctrl+Z")
        self.redo_btn = FlatButton(row, "↷ Redo", lambda: on_action("redo"), tooltip="Ctrl+Y")
        self.undo_btn.pack(side="left", expand=True, fill="x", padx=(0, 2))
        self.redo_btn.pack(side="left", expand=True, fill="x", padx=(2, 0))
        self.undo_hint = tk.Label(self, text="", bg=T["panel"], fg=T["muted"],
                                  font=(config.FONT, 8), wraplength=150, justify="left")
        self.undo_hint.pack(fill="x", pady=(2, 0))

        section_label(self, "Board (host only)").pack(fill="x", pady=(12, 4))
        self.host_buttons = []
        for label, action, tip in (("✖ Clear", "clear", "Remove everything for everyone"),
                                   ("✔ Save", "save", "Save board on the server (Ctrl+S)"),
                                   ("⟲ Restore", "load", "Restore last saved board")):
            b = FlatButton(self, label, lambda a=action: on_action(a), tooltip=tip,
                           danger=(action == "clear"))
            b.pack(fill="x", pady=2)
            self.host_buttons.append(b)

        section_label(self, "File").pack(fill="x", pady=(12, 4))
        FlatButton(self, "⤓ Export PNG…", lambda: on_action("export"),
                   tooltip="Save a picture of the board on this computer").pack(fill="x")

        self.select_tool("pen")
        self.select_color("#000000")

    # ----- state changes ----------------------------------------------------------

    def select_tool(self, tool):
        for t, b in self.tool_buttons.items():
            b.set_selected(t == tool)
        self.tool = tool
        self.on_tool(tool)
        self._draw_preview()

    def select_color(self, color):
        self.color = color
        for c, s in self.swatches.items():
            s.configure(highlightbackground=T["accent"] if c.upper() == color.upper() else T["panel"])
        self.on_color(color)
        self._draw_preview()

    def _pick_color(self):
        _, hex_color = colorchooser.askcolor(color=self.color, title="Choose colour")
        if hex_color:
            self.select_color(hex_color.upper())

    def _size_changed(self, _value):
        value = int(round(float(self.size_var.get())))   # ttk.Scale gives floats
        self.size_var.set(value)
        self.size_label.configure(text=f"{value} px")
        self.on_width(value)
        self._draw_preview()

    def change_size(self, delta):
        value = max(config.MIN_WIDTH, min(config.MAX_WIDTH, int(self.size_var.get()) + delta))
        self.size_var.set(value)
        self._size_changed(value)

    def _draw_preview(self):
        if not hasattr(self, "preview"):
            return
        # A sample S-curve drawn with the current colour and width
        self.preview.delete("all")
        width = min(int(self.size_var.get()), 40)
        eraser = getattr(self, "tool", "pen") == "eraser"
        color = "#D8DBE5" if eraser else self.color
        self.preview.create_line(22, 40, 55, 14, 95, 42, 128, 16, smooth=True,
                                 width=width, fill=color, capstyle=tk.ROUND)

    def set_host(self, is_host, online):
        for b in self.host_buttons:
            b.set_enabled(is_host and online)

    def set_online(self, online):
        self.undo_btn.set_enabled(online)
        self.redo_btn.set_enabled(online)

    def set_undo_mode(self, mode):
        self.undo_hint.configure(text="Undo removes your own last stroke" if mode == "user"
                                 else "Undo removes the last stroke on the board")
