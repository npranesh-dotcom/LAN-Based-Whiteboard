"""
widgets.py
----------
Small reusable widgets for the dark theme. Tk's default buttons look dated
and ignore colours on some platforms, so FlatButton is a Label with hover,
pressed, selected and disabled states that looks the same on Linux and
Windows.
"""

import tkinter as tk
from tkinter import ttk

from client import config

T = config.THEME


class FlatButton(tk.Label):

    def __init__(self, master, text, command, tooltip=None, width=None,
                 font=None, accent=False, danger=False, **kw):
        self.base = T["accent"] if accent else (T["danger"] if danger else T["panel_alt"])
        self.hover = T["accent_hover"] if accent else T["border"]
        super().__init__(master, text=text, bg=self.base, fg=T["text"],
                         font=font or T["font"], padx=10, pady=6, cursor="hand2",
                         width=width, **kw)
        self.command = command
        self.enabled = True
        self.selected = False
        self.tooltip = tooltip
        self._tip = None
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonRelease-1>", self._click)

    def _paint(self, hovering=False):
        if not self.enabled:
            self.configure(bg=T["panel"], fg=T["border"], cursor="arrow")
        elif self.selected:
            self.configure(bg=T["accent"], fg="white", cursor="hand2")
        else:
            self.configure(bg=self.hover if hovering else self.base, fg=T["text"], cursor="hand2")

    def _enter(self, _e):
        self._paint(True)
        if self.tooltip:
            self._tip = tk.Toplevel(self)
            self._tip.wm_overrideredirect(True)
            self._tip.wm_geometry(f"+{self.winfo_rootx() + self.winfo_width() + 6}"
                                  f"+{self.winfo_rooty() + 4}")
            tk.Label(self._tip, text=self.tooltip, bg="#11121A", fg=T["text"],
                     font=T["font_small"], padx=6, pady=3).pack()

    def _leave(self, _e):
        self._paint(False)
        if self._tip:
            self._tip.destroy()
            self._tip = None

    def _click(self, _e):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._paint()

    def set_selected(self, selected):
        self.selected = selected
        self._paint()


class ScrollFrame(tk.Frame):
    """
    Vertical scroll container. Put widgets in `.inner`. A scrollbar appears
    only when the content is taller than the space available, so on a big
    screen it looks like a plain frame and on a small one (VirtualBox's
    default 1024x768, a laptop with 125 % scaling) nothing gets cut off.
    The mouse wheel scrolls it while the pointer is over it.
    """

    def __init__(self, master, bg):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 style="Wb.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._resize)
        self.canvas.bind("<Configure>", self._resize)
        for sequence, units in (("<MouseWheel>", None), ("<Button-4>", -3), ("<Button-5>", 3)):
            self.bind_all(sequence, lambda e, u=units: self._wheel(e, u), add="+")

    def _overflows(self):
        return self.inner.winfo_reqheight() > self.canvas.winfo_height() > 1

    def _resize(self, _event=None):
        need_w, need_h = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        self.canvas.configure(width=need_w, scrollregion=(0, 0, need_w, need_h))
        if self._overflows():
            self.bar.pack(side="right", fill="y")
        else:
            self.bar.pack_forget()
            self.canvas.yview_moveto(0)

    def _wheel(self, event, units):
        pointed = self.winfo_containing(event.x_root, event.y_root)
        if pointed is None or not str(pointed).startswith(str(self)) or not self._overflows():
            return
        if units is None:                       # Windows / macOS give +-120 per notch
            units = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(units, "units")


def section_label(master, text):
    return tk.Label(master, text=text.upper(), bg=T["panel"], fg=T["muted"],
                    font=(config.FONT, 8, "bold"), anchor="w")
