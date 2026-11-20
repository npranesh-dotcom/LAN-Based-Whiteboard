"""
canvas_view.py
--------------
The whiteboard drawing surface. Owns everything drawn on the Tk canvas:

Layers (bottom -> top)
    committed strokes   : confirmed by the server, in server (sequence) order
    pending strokes     : drawn by THIS user, sent, waiting for the echo
    previews            : strokes OTHER users are drawing right now
    cursors             : other users' pointers with their name tags

Design notes (for the viva)
===========================
* Optimistic local drawing: your stroke appears under the mouse instantly
  as cheap 2-point segments (O(1) per mouse move). On release it is merged
  into a single polyline and kept as "pending" until the server echoes it
  as stroke_added. The canonical copy then replaces it. If the echo never
  arrives the pending stroke expires, so the canvas never shows anything
  the server didn't accept.
* Server order is authoritative. A canvas_state (join, undo, redo, clear,
  load, sync) is applied as a DIFF: strokes that disappeared are deleted,
  new ones created, and the stacking order fixed with tag_raise. Undo on a
  canvas with 2000 strokes therefore deletes one item instead of redrawing
  all of them.
* Duplicates are ignored: a stroke id that's already drawn is never drawn
  twice, which handles duplicated or re-sent updates.
* Fewer points on the wire: samples closer than MIN_POINT_GAP are skipped
  while drawing, and Ramer-Douglas-Peucker simplification runs on release.
"""

import time
import tkinter as tk
from tkinter import ttk

from client import config, geometry

SHAPES = ("line", "rect", "ellipse")


class WhiteboardCanvas(tk.Frame):

    def __init__(self, master, on_stroke, on_hover_author):
        super().__init__(master, bg=config.THEME["bg"])
        self.on_stroke = on_stroke                # callback(stroke, eraser)
        self.on_hover_author = on_hover_author    # callback(text or "")

        self.canvas = tk.Canvas(
            self, bg=config.CANVAS_BG, highlightthickness=0, cursor="crosshair",
            scrollregion=(0, 0, config.CANVAS_WIDTH, config.CANVAS_HEIGHT))
        xs = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview,
                           style="Wb.Horizontal.TScrollbar")
        ys = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                           style="Wb.Vertical.TScrollbar")
        self.canvas.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        # Thin border marking the edge of the shared drawing area
        self.canvas.create_rectangle(0, 0, config.CANVAS_WIDTH, config.CANVAS_HEIGHT,
                                     outline="#D0D4E0", tags="frame")

        # Tool state (set by the toolbar)
        self.tool, self.color, self.width = "pen", "#000000", 4
        self.enabled = False

        # Committed state
        self.strokes = {}          # stroke_id -> stroke dict
        self.items = {}            # stroke_id -> canvas item id
        self.order = []            # stroke ids, bottom -> top

        # Own strokes awaiting server echo: id -> {"item", "stroke", "sent"}
        self.pending = {}

        # Other users' live previews: stroke_id -> {"item", "points", "user"}
        self.previews = {}
        self.cursors = {}          # username -> (dot, label, last_seen)

        # In-progress local stroke
        self._points = []
        self._segments = []
        self._shape_item = None
        self._start = None
        self._live_id = None
        self._unsent = []          # preview points not yet sent
        self._shape_dirty = False
        self._pointer = None       # last pointer position (canvas coords)
        self._pointer_hidden = False
        self._last_hover = 0.0

        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", self._leave)
        c.bind("<ButtonPress-2>", lambda e: c.scan_mark(e.x, e.y))
        c.bind("<B2-Motion>", lambda e: c.scan_dragto(e.x, e.y, gain=1))
        c.bind("<MouseWheel>", self._wheel)                     # Windows / macOS
        c.bind("<Button-4>", lambda e: self._scroll(e, -3))     # Linux
        c.bind("<Button-5>", lambda e: self._scroll(e, 3))

    # ===== rendering helpers ===================================================

    def _draw_line(self, points, color, width, tool, tags):
        fill = config.CANVAS_BG if tool == "eraser" else color
        return self.canvas.create_line(
            geometry.flatten(points), fill=fill, width=width,
            capstyle=tk.ROUND, joinstyle=tk.ROUND, tags=tags)

    def _raise_overlays(self):
        for tag in ("pending", "preview", "cursor"):
            self.canvas.tag_raise(tag)

    # ===== committed strokes (server truth) ====================================

    def add_stroke(self, stroke):
        """Apply a stroke_added. Returns False if it was a duplicate."""
        sid = stroke["stroke_id"]
        if sid in self.items:
            return False
        own = self.pending.pop(sid, None)
        if own:
            self.canvas.delete(own["item"])
        preview = self.previews.pop(sid, None)
        if preview:
            self.canvas.delete(preview["item"])
        self._create(stroke)
        self._raise_overlays()
        return True

    def _create(self, stroke):
        sid = stroke["stroke_id"]
        self.items[sid] = self._draw_line(
            stroke["points"], stroke["color"], stroke["width"], stroke.get("tool", "pen"),
            ("stroke", f"id:{sid}"))
        self.strokes[sid] = stroke
        self.order.append(sid)

    def apply_state(self, strokes):
        """Make the canvas equal to `strokes` with as few item changes as possible."""
        new_ids = [s["stroke_id"] for s in strokes]
        new_set = set(new_ids)
        for sid in self.order:
            if sid not in new_set:
                self.canvas.delete(self.items.pop(sid))
                del self.strokes[sid]
        kept = [sid for sid in self.order if sid in new_set]
        kept_set = set(kept)
        self.order = []
        for stroke in strokes:
            sid = stroke["stroke_id"]
            if sid in kept_set:
                self.order.append(sid)
            elif sid not in self.items:            # skip duplicate ids in a snapshot
                self._create(stroke)
            own = self.pending.pop(sid, None)          # our stroke got committed
            if own:
                self.canvas.delete(own["item"])
        # New items were created on top, in order. That is already correct
        # when the server's order is "surviving strokes, then new ones" (the
        # usual case: undo, redo, appended strokes). Otherwise restack all.
        if self.order[:len(kept)] != kept:
            for sid in self.order:
                self.canvas.tag_raise(self.items[sid])
        self.canvas.tag_lower("frame")
        self._raise_overlays()

    # ===== pending (own, unacknowledged) strokes ===================================

    def add_pending(self, stroke):
        item = self._draw_line(stroke["points"], stroke["color"], stroke["width"],
                               stroke["tool"], ("pending",))
        self.pending[stroke["stroke_id"]] = {"item": item, "stroke": stroke,
                                             "sent": time.monotonic()}

    def drop_pending(self, sid):
        own = self.pending.pop(sid, None)
        if own:
            self.canvas.delete(own["item"])

    def expire_pending(self, connected):
        """Remove own strokes the server never confirmed (only while online)."""
        if not connected:
            return []
        now = time.monotonic()
        old = [sid for sid, p in self.pending.items()
               if now - p["sent"] > config.PENDING_TIMEOUT]
        for sid in old:
            self.drop_pending(sid)
        return old

    def touch_pending(self):
        """Restart the timeout clock (after re-sending on reconnect)."""
        now = time.monotonic()
        for p in self.pending.values():
            p["sent"] = now

    # ===== other users: previews and cursors =======================================

    def remote_cursor(self, msg):
        user = msg["username"]
        color = geometry.user_color(user)
        if msg.get("hidden"):
            self.remove_cursor(user)
        elif "x" in msg:
            x, y = msg["x"], msg["y"]
            if user in self.cursors:
                dot, label, _ = self.cursors[user]
                self.canvas.coords(dot, x - 5, y - 5, x + 5, y + 5)
                self.canvas.coords(label, x + 9, y + 9)
            else:
                dot = self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=color,
                                              outline="white", width=2, tags="cursor")
                label = self.canvas.create_text(x + 9, y + 9, text=user, anchor="nw",
                                                fill=color, font=config.THEME["font_bold"],
                                                tags="cursor")
            self.cursors[user] = (dot, label, time.monotonic())
        preview = msg.get("preview")
        if preview:
            self._remote_preview(user, preview)
        self.canvas.tag_raise("cursor")

    def _remote_preview(self, user, p):
        sid = p["stroke_id"]
        if sid in self.items:                 # already committed: stale preview
            return
        if p.get("cancel"):
            old = self.previews.pop(sid, None)
            if old:
                self.canvas.delete(old["item"])
            return
        entry = self.previews.get(sid)
        if entry is None:
            points = list(p["points"])
            item = self._draw_line(points, p["color"], p["width"], p.get("tool"), ("preview",))
            self.previews[sid] = {"item": item, "points": points, "user": user}
        else:
            # Update the existing item in place (cheaper than delete + create)
            entry["points"] = list(p["points"]) if p.get("replace") else entry["points"] + p["points"]
            self.canvas.coords(entry["item"], geometry.flatten(entry["points"]))
        self.canvas.tag_raise("cursor")

    def remove_cursor(self, user):
        entry = self.cursors.pop(user, None)
        if entry:
            self.canvas.delete(entry[0], entry[1])

    def remove_user(self, user):
        """User left: drop their pointer and any half-drawn preview."""
        self.remove_cursor(user)
        for sid in [s for s, p in self.previews.items() if p["user"] == user]:
            self.canvas.delete(self.previews.pop(sid)["item"])

    def expire_cursors(self, max_age=10.0):
        now = time.monotonic()
        for user in [u for u, (_, _, seen) in self.cursors.items() if now - seen > max_age]:
            self.remove_cursor(user)

    def clear_remote(self):
        for user in list(self.cursors):
            self.remove_cursor(user)
        self.canvas.delete("preview")
        self.previews.clear()

    # ===== local input =============================================================

    def _xy(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _press(self, event):
        if not self.enabled:
            return
        x, y = self._xy(event)
        self._start = (x, y)
        self._live_id = geometry.new_stroke_id()
        if self.tool in SHAPES:
            self._points = []
            self._shape_item = None
        else:
            self._points = [geometry.clamp_point(x, y)]
            self._unsent = list(self._points)
            dot = self._draw_line(self._points, self.color, self.width, self.tool, ("live",))
            self._segments = [dot]

    def _drag(self, event):
        if not self.enabled or self._start is None:
            return
        x, y = self._xy(event)
        self._pointer = (x, y)
        if self.tool in SHAPES:
            constrain = bool(event.state & 0x0001)             # Shift held
            self._points = geometry.shape_points(self.tool, *self._start, x, y, constrain)
            if self._shape_item is None:
                self._shape_item = self._draw_line(self._points, self.color, self.width,
                                                   "pen", ("live",))
            else:
                self.canvas.coords(self._shape_item, geometry.flatten(self._points))
            self._shape_dirty = True
            return
        last = self._points[-1]
        if abs(x - last[0]) < config.MIN_POINT_GAP and abs(y - last[1]) < config.MIN_POINT_GAP:
            return
        point = geometry.clamp_point(x, y)
        self._segments.append(self._draw_line([last, point], self.color, self.width,
                                              self.tool, ("live",)))
        self._points.append(point)
        self._unsent.append(point)

    def _release(self, event):
        if not self.enabled or self._start is None:
            return
        self._drag(event)
        self.canvas.delete("live")
        self._segments, self._shape_item = [], None
        sid, points = self._live_id, self._points
        self._start = None
        self._unsent, self._shape_dirty = [], False

        if self.tool in SHAPES:
            xs = [p[0] for p in points] or [0]
            ys = [p[1] for p in points] or [0]
            if max(xs) - min(xs) < 2 and max(ys) - min(ys) < 2:   # just a click
                self._cancelled_id = sid
                return
            eraser = False
        else:
            points = geometry.simplify(points)
            eraser = self.tool == "eraser"

        for i, chunk in enumerate(geometry.split_points(points)):
            stroke = {"stroke_id": sid if i == 0 else geometry.new_stroke_id(),
                      "color": self.color, "width": self.width, "points": chunk,
                      "tool": "eraser" if eraser else "pen"}
            if geometry.valid_stroke(stroke):
                self.add_pending(stroke)
                self.on_stroke(stroke, eraser)
        self._raise_overlays()

    _cancelled_id = None

    def take_live_update(self):
        """
        Called every PREVIEW_INTERVAL_MS by the app. Returns the cursor message
        fields to send, or None if nothing changed since the last call.
        Batching mouse samples like this caps preview traffic at 25 msgs/s.
        """
        preview = None
        if self._start is not None and self._live_id:
            if self.tool in SHAPES and self._shape_dirty and self._points:
                preview = {"stroke_id": self._live_id, "color": self.color,
                           "width": self.width, "tool": "pen",
                           "points": self._points, "replace": True}
                self._shape_dirty = False
            elif self.tool not in SHAPES and self._unsent:
                pts = self._unsent[-config.MAX_PREVIEW_POINTS:]
                preview = {"stroke_id": self._live_id, "color": self.color,
                           "width": self.width, "tool": self.tool, "points": pts}
                # Overlap by one point so remote segments join up
                self._unsent = [self._unsent[-1]] if len(self._points) > 1 else []
                if len(pts) == 1 and len(self._points) > 1:
                    preview = None
        if self._cancelled_id:
            preview = {"stroke_id": self._cancelled_id, "cancel": True}
            self._cancelled_id = None
        if self._pointer_hidden:
            self._pointer_hidden = False
            return {"hidden": True, "preview": preview}
        if self._pointer is None and preview is None:
            return None
        pos, self._pointer = self._pointer, None
        if pos is None:
            return {"hidden": True, "preview": preview}
        return {"x": round(pos[0], 1), "y": round(pos[1], 1), "preview": preview}

    def _motion(self, event):
        x, y = self._xy(event)
        if 0 <= x <= config.MAX_COORD and 0 <= y <= config.MAX_COORD:
            self._pointer = (x, y)
        # "Who drew this?" - throttled hit-test under the pointer
        now = time.monotonic()
        if now - self._last_hover > 0.12:
            self._last_hover = now
            hit = self.canvas.find_overlapping(x - 2, y - 2, x + 2, y + 2)
            for item in reversed(hit):
                tag = next((t for t in self.canvas.gettags(item) if t.startswith("id:")), None)
                if tag:
                    stroke = self.strokes.get(tag[3:])
                    if stroke:
                        kind = "Eraser" if stroke.get("tool") == "eraser" else "Stroke"
                        self.on_hover_author(f"{kind} by {stroke.get('username', '?')}")
                        return
            self.on_hover_author("")

    def _leave(self, _event):
        self._pointer = None
        self._pointer_hidden = True
        self.on_hover_author("")

    def _wheel(self, event):
        self._scroll(event, -1 if event.delta > 0 else 1)

    def _scroll(self, event, units):
        if event.state & 0x0001:                              # Shift = horizontal
            self.canvas.xview_scroll(units, "units")
        else:
            self.canvas.yview_scroll(units, "units")

    # ===== export ==================================================================

    def all_strokes(self):
        """Committed strokes in order, then own pending ones (what the user sees)."""
        return ([self.strokes[s] for s in self.order]
                + [p["stroke"] for p in self.pending.values()])

    def export(self, path):
        """
        Save the board as PNG (rendered from stroke data with Pillow, so it
        is pixel-exact and independent of scrolling) or as PostScript.
        """
        strokes = self.all_strokes()
        if path.lower().endswith(".ps"):
            self.canvas.postscript(file=path, colormode="color", x=0, y=0,
                                   width=config.CANVAS_WIDTH, height=config.CANVAS_HEIGHT)
            return path
        from PIL import Image, ImageDraw          # imported lazily: optional dependency
        max_x = max((c[0] for s in strokes for c in s["points"]), default=0)
        max_y = max((c[1] for s in strokes for c in s["points"]), default=0)
        w = int(min(max(max_x + 40, 800), config.MAX_COORD))
        h = int(min(max(max_y + 40, 600), config.MAX_COORD))
        image = Image.new("RGB", (w, h), config.CANVAS_BG)
        draw = ImageDraw.Draw(image)
        for s in strokes:
            fill = config.CANVAS_BG if s.get("tool") == "eraser" else s["color"]
            pts = [tuple(p) for p in s["points"]]
            r = s["width"] / 2
            if len(pts) > 1:
                draw.line(pts, fill=fill, width=int(round(s["width"])), joint="curve")
            for x, y in (pts[0], pts[-1]):            # round caps like Tk
                draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)
        image.save(path)
        return path
