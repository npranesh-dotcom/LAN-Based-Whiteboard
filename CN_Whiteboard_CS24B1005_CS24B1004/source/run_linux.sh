#!/usr/bin/env bash
# Collaborative Whiteboard - Linux launcher.  Usage: ./run_linux.sh [host-ip]
cd "$(dirname "$0")" || exit 1
if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "Tk is missing. Install it, then run this again:"
  echo "  Debian/Ubuntu: sudo apt install python3-tk"
  echo "  Fedora:        sudo dnf install python3-tkinter"
  echo "  Arch:          sudo pacman -S tk"
  exit 1
fi
[ -n "$1" ] && python3 tools/netcheck.py "$1"
exec python3 -m client.main "$@"
