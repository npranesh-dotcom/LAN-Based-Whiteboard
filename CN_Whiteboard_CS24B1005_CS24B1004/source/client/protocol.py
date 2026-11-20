"""
protocol.py
-----------
The client side of the application-level protocol.

Framing: every message is ONE JSON object encoded as UTF-8 and terminated
by "\\n". TCP is a byte stream, so a single recv() may hold half a message
or several messages. LineDecoder buffers the bytes and returns only
complete lines.

This module also has one builder function per client -> server message, so
message field names are defined in exactly one place.
"""

import json


class FramingError(Exception):
    """Raised when the peer sends a line longer than the allowed maximum."""


def encode(message):
    """dict -> newline-terminated UTF-8 frame (compact separators = fewer bytes)."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


class LineDecoder:
    """Turns an arbitrary stream of byte chunks into parsed JSON messages."""

    def __init__(self, max_line):
        self.buffer = bytearray()
        self.max_line = max_line

    def feed(self, data):
        """Add bytes; return a list of complete decoded messages (dicts)."""
        self.buffer += data
        messages = []
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self.buffer[:newline])
            del self.buffer[:newline + 1]
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue                      # corrupt line: skip it, keep going
            if isinstance(message, dict):
                messages.append(message)
        if len(self.buffer) > self.max_line:
            raise FramingError("incoming message exceeds size limit")
        return messages


# --- Client -> server message builders -----------------------------------------

def join(username, room_code, password=""):
    return {"type": "join", "username": username,
            "room_code": room_code, "password": password}


def draw(stroke, eraser=False):
    """The server sets 'tool' and 'username' itself, so they aren't sent."""
    body = {k: stroke[k] for k in ("stroke_id", "color", "width", "points")}
    return {"type": "erase" if eraser else "draw", "stroke": body}


def chat(text):
    return {"type": "chat", "message": text}


def simple(kind):
    """undo / redo / clear / save / load / sync / ping"""
    return {"type": kind}


def cursor(x, y, preview=None):
    msg = {"type": "cursor", "x": x, "y": y}
    if preview:
        msg["preview"] = preview
    return msg


def cursor_hidden(preview=None):
    msg = {"type": "cursor", "hidden": True}
    if preview:
        msg["preview"] = preview
    return msg


# Server -> client messages that carry a global sequence number.
SEQUENCED = {"welcome", "stroke_added", "user_joined", "user_left",
             "chat", "canvas_state"}
