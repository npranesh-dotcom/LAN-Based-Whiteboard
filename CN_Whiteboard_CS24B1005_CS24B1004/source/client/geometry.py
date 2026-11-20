"""
geometry.py
-----------
Pure functions with no Tkinter or network code, so they are easy to unit
test:

* stroke simplification (fewer points = less network traffic),
* turning shapes (line / rectangle / ellipse) into point lists,
* client-side stroke validation that mirrors the server's rules,
* stable per-user colours and unique stroke ids.

Shapes are sent as ordinary strokes (polylines). The server, the undo stack
and persistence treat them like any other stroke, so shapes needed no
protocol change.
"""

import hashlib
import math
import uuid

from client import config


def new_stroke_id():
    """Globally unique id; the server uses it to reject duplicates."""
    return uuid.uuid4().hex


def user_color(username):
    """Same username -> same colour on every client (hash, not random)."""
    digest = hashlib.md5(username.lower().encode("utf-8")).digest()
    return config.USER_COLORS[digest[0] % len(config.USER_COLORS)]


def clamp_point(x, y):
    """Keep coordinates inside the range the server accepts; 1 decimal place."""
    x = min(max(x, 0), config.MAX_COORD)
    y = min(max(y, 0), config.MAX_COORD)
    return [round(x, 1), round(y, 1)]


def _perpendicular_distance(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    return abs(dy * px - dx * py + bx * ay - by * ax) / math.hypot(dx, dy)


def simplify(points, epsilon=config.SIMPLIFY_EPSILON):
    """
    Ramer-Douglas-Peucker line simplification (iterative, so long strokes
    can't hit Python's recursion limit). It removes points that lie within
    `epsilon` pixels of the line through their neighbours. A typical
    freehand stroke loses 50-80 % of its points with no visible change.
    """
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        max_dist, index = 0.0, None
        for i in range(start + 1, end):
            d = _perpendicular_distance(points[i], points[start], points[end])
            if d > max_dist:
                max_dist, index = d, i
        if index is not None and max_dist > epsilon:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    return [p for p, k in zip(points, keep) if k]


def shape_points(tool, x0, y0, x1, y1, constrain=False):
    """
    Points for a shape dragged from (x0, y0) to (x1, y1).
    constrain=True (Shift held) gives a square / circle / 45-degree line.
    """
    if constrain:
        if tool == "line":
            angle = round(math.atan2(y1 - y0, x1 - x0) / (math.pi / 4)) * (math.pi / 4)
            length = math.hypot(x1 - x0, y1 - y0)
            x1, y1 = x0 + length * math.cos(angle), y0 + length * math.sin(angle)
        else:
            side = max(abs(x1 - x0), abs(y1 - y0))
            x1 = x0 + math.copysign(side, (x1 - x0) or 1)
            y1 = y0 + math.copysign(side, (y1 - y0) or 1)

    if tool == "line":
        pts = [(x0, y0), (x1, y1)]
    elif tool == "rect":
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    elif tool == "ellipse":
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rx, ry = abs(x1 - x0) / 2, abs(y1 - y0) / 2
        n = config.ELLIPSE_SEGMENTS
        pts = [(cx + rx * math.cos(2 * math.pi * i / n),
                cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]
    else:
        raise ValueError(f"unknown shape {tool}")
    return [clamp_point(x, y) for x, y in pts]


def split_points(points, limit=config.MAX_POINTS):
    """Split an over-long stroke into server-sized chunks that overlap by one point."""
    if len(points) <= limit:
        return [points]
    chunks, start = [], 0
    while start < len(points) - 1:
        chunks.append(points[start:start + limit])
        start += limit - 1
    return chunks


def flatten(points):
    """[[x, y], ...] -> [x, y, x, y, ...] as Tkinter's create_line expects."""
    flat = [c for p in points for c in p]
    if len(points) == 1:                      # a single click: draw a dot
        flat += [points[0][0] + 0.1, points[0][1] + 0.1]
    return flat


def valid_stroke(stroke):
    """Same rules as server/canvas_manager.sanitize_stroke (catch bugs before sending)."""
    try:
        color, width, pts = stroke["color"], stroke["width"], stroke["points"]
        int(color[1:], 16)
        return (isinstance(stroke["stroke_id"], str) and 0 < len(stroke["stroke_id"]) <= 64
                and len(color) == 7 and color[0] == "#"
                and config.MIN_WIDTH <= width <= config.MAX_WIDTH
                and 1 <= len(pts) <= config.MAX_POINTS
                and all(0 <= c <= config.MAX_COORD for p in pts for c in p))
    except (KeyError, TypeError, ValueError):
        return False
