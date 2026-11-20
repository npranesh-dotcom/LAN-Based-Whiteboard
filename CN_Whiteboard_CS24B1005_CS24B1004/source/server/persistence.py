"""
persistence.py
--------------
Saves and restores the canvas to/from data/canvas.json.

Saving is crash-safe: the strokes are written to a uniquely named temporary
file in the same folder, flushed to disk, and then atomically swapped in with
os.replace(). A crash mid-save therefore leaves the previous canvas.json
intact. A module-level lock makes sure two saves never interleave.

load_canvas() returns a (strokes, error) pair so the caller can tell
"the file is missing or corrupt" apart from "the saved canvas is empty".
Validation of the individual strokes is done by the caller (server.py) using
canvas_manager.sanitize_stroke().
"""

import json
import os
import tempfile
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# WHITEBOARD_DATA_DIR lets the automated tests use a temporary folder so
# they never overwrite your real data/canvas.json.
DATA_DIR = os.environ.get("WHITEBOARD_DATA_DIR") or os.path.join(BASE_DIR, "data")

CANVAS_FILE = os.path.join(DATA_DIR, "canvas.json")

# Serialises saves so two concurrent saves can't clash.
_save_lock = threading.Lock()


def save_canvas(strokes):
    """Write strokes to canvas.json atomically. Returns True on success."""

    with _save_lock:

        temp_file = None

        try:
            os.makedirs(DATA_DIR, exist_ok=True)

            # Unique temp file in the same directory (same filesystem, so
            # os.replace is atomic).
            fd, temp_file = tempfile.mkstemp(
                dir=DATA_DIR, prefix="canvas_", suffix=".tmp"
            )

            with os.fdopen(fd, "w") as file:
                json.dump(strokes, file, indent=4)
                file.flush()
                os.fsync(file.fileno())

            os.replace(temp_file, CANVAS_FILE)
            temp_file = None

            print(f"[SAVE] Canvas saved successfully ({len(strokes)} strokes)")

            return True

        except (OSError, TypeError, ValueError) as e:

            print(f"[SAVE ERROR] {e}")

            return False

        finally:
            # Remove a leftover temp file if the save failed part-way
            if temp_file is not None and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass


def load_canvas():
    """
    Read canvas.json.

    Returns:
        (strokes, None)  on success (strokes is a list, possibly empty)
        (None, reason)   if the file is missing, unreadable or corrupt
    """

    if not os.path.exists(CANVAS_FILE):
        print("[LOAD] No previous canvas found")
        return None, "No saved canvas found"

    try:
        with open(CANVAS_FILE, "r") as file:
            strokes = json.load(file)

        if not isinstance(strokes, list):
            raise ValueError("Invalid canvas format")

        print(f"[LOAD] Read {len(strokes)} strokes from {CANVAS_FILE}")

        return strokes, None

    except (OSError, json.JSONDecodeError, ValueError) as e:

        print(f"[LOAD ERROR] {e}")

        return None, "Saved canvas file is corrupt or unreadable"
