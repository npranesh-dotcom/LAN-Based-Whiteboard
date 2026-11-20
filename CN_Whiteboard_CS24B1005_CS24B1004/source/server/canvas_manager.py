"""
canvas_manager.py
-----------------
Holds the shared whiteboard state on the server.

The canvas is represented as an ordered list of strokes. Every stroke is a
dictionary:

    {
        "stroke_id": "abc123",          # client-generated id (1-64 chars)
        "color":     "#FF0000",         # 7-char hex colour
        "width":     4,                 # brush width, 1-50
        "points":    [[x, y], ...],     # 1-5000 points, 0 <= x, y <= 10000
        "tool":      "pen" | "eraser",  # set by the server
        "username":  "Manieesh"         # set by the server (stroke owner)
    }

Drawing order is list order: later strokes are painted on top of earlier
ones. An eraser stroke is painted in the canvas background colour, so it is
undoable like any other stroke.

Undo / redo semantics (multi-user)
----------------------------------
Two modes are supported, chosen when the server starts:

* "user"   (default) - Undo removes the most recent stroke drawn by the
                       user who pressed Undo. Each user has their own redo
                       stack. A user can never undo someone else's work.
                       Drawing a new stroke clears only that user's redo
                       stack.
* "global"           - Undo removes the most recent stroke on the board,
                       whoever drew it. One shared redo stack, cleared by
                       any new stroke.

In both modes "clear" and "load" wipe every redo stack, because the strokes
they referred to no longer exist.

All methods are protected by one lock, so the manager is safe to call from
many client threads at the same time.
"""

import copy
import math
import threading

ALLOWED_TOOLS = ("pen", "eraser")
UNDO_MODES = ("user", "global")

MAX_STROKE_ID_LENGTH = 64
MIN_WIDTH, MAX_WIDTH = 1, 50
MAX_POINTS = 5000
MIN_COORD, MAX_COORD = 0, 10000
MAX_USERNAME_LENGTH = 20

# Key used for the single shared redo stack in "global" mode.
_GLOBAL_KEY = None


def _is_number(value):
    """True for real, finite ints/floats (bool is excluded on purpose)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def sanitize_stroke(stroke):
    """
    Validate a stroke and return a clean copy that contains ONLY the known
    fields. Returns None if the stroke is invalid.

    Unknown fields sent by a client are dropped, so a client cannot make the
    server store or rebroadcast arbitrary data.

    "tool" and "username" are optional here (the server sets them for live
    strokes) but, if present, they must be valid. This lets the same function
    validate strokes loaded from canvas.json.
    """

    if not isinstance(stroke, dict):
        return None

    stroke_id = stroke.get("stroke_id")
    color = stroke.get("color")
    width = stroke.get("width")
    points = stroke.get("points")

    # Stroke ID
    if (not isinstance(stroke_id, str) or not stroke_id
            or len(stroke_id) > MAX_STROKE_ID_LENGTH):
        return None

    # Colour: "#RRGGBB"
    if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
        return None
    try:
        int(color[1:], 16)
    except ValueError:
        return None

    # Brush width
    if not _is_number(width) or not MIN_WIDTH <= width <= MAX_WIDTH:
        return None

    # Points
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        return None

    clean_points = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        x, y = point
        if not _is_number(x) or not _is_number(y):
            return None
        if not (MIN_COORD <= x <= MAX_COORD and MIN_COORD <= y <= MAX_COORD):
            return None
        clean_points.append([x, y])

    clean = {
        "stroke_id": stroke_id,
        "color": color,
        "width": width,
        "points": clean_points,
    }

    # Optional fields (present in saved files, set by server for live strokes)
    tool = stroke.get("tool")
    if tool is not None:
        if tool not in ALLOWED_TOOLS:
            return None
        clean["tool"] = tool

    username = stroke.get("username")
    if username is not None:
        if (not isinstance(username, str) or not username
                or len(username) > MAX_USERNAME_LENGTH):
            return None
        clean["username"] = username

    return clean


class CanvasManager:

    def __init__(self, undo_mode="user"):

        if undo_mode not in UNDO_MODES:
            raise ValueError(f"undo_mode must be one of {UNDO_MODES}")

        self.undo_mode = undo_mode
        self.strokes = []

        # One redo stack per user ("user" mode) or one shared stack
        # under _GLOBAL_KEY ("global" mode).
        self.redo_stacks = {}

        self.lock = threading.Lock()

    # Which redo stack an action by `username` uses
    def _key(self, username):
        return username if self.undo_mode == "user" else _GLOBAL_KEY

    # Add a new drawing stroke
    def add_stroke(self, stroke, username=None):

        with self.lock:
            self.strokes.append(stroke)

            # A new operation invalidates the redo history of whoever
            # performed it (everyone, in global mode).
            self.redo_stacks.pop(self._key(username), None)

    # Return a copy of the complete canvas state
    def get_canvas_state(self):

        with self.lock:
            return copy.deepcopy(self.strokes)

    # True if a stroke with this id is on the canvas or waiting in a redo
    # stack. Used to make "draw" idempotent: a client that re-sends a stroke
    # after a reconnect can never create a duplicate.
    def has_stroke(self, stroke_id):

        with self.lock:
            if any(s["stroke_id"] == stroke_id for s in self.strokes):
                return True
            return any(
                s["stroke_id"] == stroke_id
                for stack in self.redo_stacks.values() for s in stack
            )

    # Number of strokes currently on the canvas
    def stroke_count(self):

        with self.lock:
            return len(self.strokes)

    # Undo: remove a stroke according to the undo mode
    def undo(self, username=None):

        with self.lock:

            if not self.strokes:
                return None

            if self.undo_mode == "user":
                # Search backwards for this user's most recent stroke
                index = None
                for i in range(len(self.strokes) - 1, -1, -1):
                    if self.strokes[i].get("username") == username:
                        index = i
                        break
                if index is None:
                    return None
            else:
                index = len(self.strokes) - 1

            stroke = self.strokes.pop(index)
            self.redo_stacks.setdefault(self._key(username), []).append(stroke)

            return copy.deepcopy(stroke)

    # Redo: put back the most recently undone stroke
    def redo(self, username=None):

        with self.lock:

            stack = self.redo_stacks.get(self._key(username))

            if not stack:
                return None

            stroke = stack.pop()

            # A redone stroke goes on top of the canvas
            self.strokes.append(stroke)

            return copy.deepcopy(stroke)

    # Clear the entire canvas
    def clear_canvas(self):

        with self.lock:
            self.strokes.clear()
            self.redo_stacks.clear()

    # Replace the current canvas (used on startup and by "load")
    def set_canvas_state(self, strokes):

        with self.lock:
            self.strokes = copy.deepcopy(strokes)
            self.redo_stacks.clear()

    # Kept for backward compatibility with earlier tests
    def validate_stroke(self, stroke):
        return sanitize_stroke(stroke) is not None
