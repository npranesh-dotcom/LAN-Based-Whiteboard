"""
main.py
-------
Entry point of the whiteboard client.

    python3 -m client.main
    python3 -m client.main --host 192.168.1.20 --room K7QX3M --name Alice

Command-line values only pre-fill the connection screen; the user still
presses "Join session".
"""

import argparse
import tkinter as tk

from client.app import WhiteboardApp


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collaborative Whiteboard client")
    parser.add_argument("--host", help="server LAN address")
    parser.add_argument("--port", type=int, help="server port (default 5000)")
    parser.add_argument("--room", help="room code")
    parser.add_argument("--name", help="your display name")
    args = parser.parse_args(argv)

    root = tk.Tk()
    WhiteboardApp(root, prefill={"host": args.host, "port": args.port,
                                 "room": args.room, "username": args.name})
    root.mainloop()


if __name__ == "__main__":
    main()
