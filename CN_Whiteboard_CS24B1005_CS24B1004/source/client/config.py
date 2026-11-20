"""
config.py
---------
Every tunable constant of the client in one place: network timing, canvas
limits (kept in step with the server's validation rules) and the UI theme.
"""

# --- Network ---------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
CONNECT_TIMEOUT = 5.0          # seconds to wait for TCP connect + welcome
HEARTBEAT_INTERVAL = 5.0       # send a ping this often while idle
DEAD_AFTER = 15.0              # no bytes from server for this long -> reconnect
RECONNECT_DELAYS = (1, 2, 3, 5, 5, 5)   # back-off between attempts (seconds)
RECONNECT_GIVE_UP = 120.0      # stop auto-reconnecting after this long
MAX_LINE_BYTES = 8 * 1024 * 1024        # a full canvas_state can be large

# --- Limits shared with the server (server/canvas_manager.py) ------------------
MAX_POINTS = 5000              # points per stroke
MIN_WIDTH, MAX_WIDTH = 1, 50
MAX_COORD = 10000
MAX_CHAT = 500
MAX_USERNAME = 20
PREVIEW_INTERVAL_MS = 40       # live preview / cursor send rate (25 per second)
MAX_PREVIEW_POINTS = 300

# --- Canvas ----------------------------------------------------------------
CANVAS_WIDTH = 2400            # logical drawing area (scrollable)
CANVAS_HEIGHT = 1600
CANVAS_BG = "#FFFFFF"          # eraser strokes paint with this colour
MIN_POINT_GAP = 1.5            # skip mouse samples closer than this (px)
SIMPLIFY_EPSILON = 0.6         # Ramer-Douglas-Peucker tolerance (px)
ELLIPSE_SEGMENTS = 64
PENDING_TIMEOUT = 10.0         # drop an unacknowledged local stroke after this
POLL_MS = 15                   # GUI checks the network queue this often
MAX_EVENTS_PER_POLL = 400      # keeps the UI responsive during bursts

# --- Fonts: use each operating system's own UI font ----------------------------
import sys

if sys.platform.startswith("win"):
    FONT, ICON_FONT = "Segoe UI", "Segoe UI Symbol"   # Symbol font has the tool icons
elif sys.platform == "darwin":
    FONT = ICON_FONT = "Helvetica Neue"
else:
    FONT = ICON_FONT = "DejaVu Sans"                  # Tk substitutes if it is missing

# --- Theme -----------------------------------------------------------------
THEME = {
    "bg": "#1E1F29",           # window chrome
    "panel": "#262837",
    "panel_alt": "#2F3245",
    "border": "#3A3D52",
    "text": "#E6E8F0",
    "muted": "#9298B0",
    "accent": "#5B8CFF",
    "accent_hover": "#7AA2FF",
    "danger": "#FF5D6C",
    "ok": "#3DDC97",
    "warn": "#FFC24B",
    "font": (FONT, 10),
    "font_bold": (FONT, 10, "bold"),
    "font_small": (FONT, 9),
    "font_title": (FONT, 18, "bold"),
    "font_icon": (ICON_FONT, 15),
}

PALETTE = [
    "#000000", "#FFFFFF", "#7F8C8D", "#E74C3C", "#E67E22", "#F1C40F",
    "#2ECC71", "#1ABC9C", "#3498DB", "#2C3E9F", "#9B59B6", "#FF6FB5",
]

# Distinct colours used to identify users (cursor, chat name, list dot).
USER_COLORS = [
    "#FF6B6B", "#4ECDC4", "#FFD166", "#A78BFA", "#06D6A0", "#F78C6B",
    "#118AB2", "#EF476F", "#83C5BE", "#F4A261", "#90BE6D", "#C77DFF",
]
