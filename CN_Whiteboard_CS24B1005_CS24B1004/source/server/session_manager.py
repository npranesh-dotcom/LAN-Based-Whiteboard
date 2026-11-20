"""
session_manager.py
------------------
Session / workspace management for the whiteboard server.

Responsibilities:
* Check the room code and (optional) room password of every join request.
* Reject duplicate usernames (case-insensitive).
* Keep the participant list in join order.
* Track the host. The first participant becomes host; if the host leaves,
  the longest-connected remaining participant becomes the new host.

Room codes are compared case-insensitively ("ab12cd" == "AB12CD") so users
can type them without worrying about caps. Passwords are compared exactly,
using a constant-time comparison.
"""

import secrets
import string
import threading

# Characters used for generated room codes. 0/O and 1/I are left out so the
# code is easy to read aloud and type.
ROOM_CODE_ALPHABET = "".join(
    c for c in string.ascii_uppercase + string.digits if c not in "0O1I"
)
ROOM_CODE_LENGTH = 6


def generate_room_code(length=ROOM_CODE_LENGTH):
    """Return a random, easy-to-type room code such as 'K7QX3M'."""
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(length))


def normalize_room_code(room_code):
    return room_code.strip().upper()


class SessionManager:

    def __init__(self, room_code="WHITEBOARD", password=""):

        self.room_code = normalize_room_code(room_code)
        self.password = password

        # Stores connected clients and their usernames (dicts keep
        # insertion order, so this is also the join order).
        self.participants = {}

        # Stores the connection of the current host
        self.host = None

        self.lock = threading.Lock()

    # Register a new participant
    def add_participant(self, conn, username, room_code, password=""):

        with self.lock:

            # Check room code
            if normalize_room_code(room_code) != self.room_code:
                return False, "Invalid room code"

            # Check password (constant-time compare; both must be str)
            if not secrets.compare_digest(
                    password.encode("utf-8"), self.password.encode("utf-8")):
                return False, "Incorrect room password"

            # Check duplicate username
            existing = [name.lower() for name in self.participants.values()]

            if username.lower() in existing:
                return False, "Username already taken"

            # Register participant
            self.participants[conn] = username

            # First participant becomes host
            if self.host is None:
                self.host = conn

            return True, "Successfully joined session"

    # Remove a participant
    def remove_participant(self, conn):

        with self.lock:

            if conn not in self.participants:
                return None

            username = self.participants.pop(conn)

            # Transfer host role if host disconnects
            if self.host == conn:

                if self.participants:
                    self.host = next(iter(self.participants))
                else:
                    self.host = None

            return username

    # Get all participant usernames (join order)
    def get_participants(self):

        with self.lock:
            return list(self.participants.values())

    # Check whether a client is the host
    def is_host(self, conn):

        with self.lock:
            return self.host == conn

    # Get host username
    def get_host(self):

        with self.lock:

            if self.host is None:
                return None

            return self.participants.get(self.host)
