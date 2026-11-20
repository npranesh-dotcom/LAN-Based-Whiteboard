"""
netcheck.py
-----------
Checks whether this computer can reach a whiteboard server. Run it BEFORE
the demo, on the machine that will join, so network problems show up early.

    python3 tools/netcheck.py                      # show this machine's IP addresses
    python3 tools/netcheck.py 192.168.1.20         # check server at that IP, port 5000
    python3 tools/netcheck.py 192.168.1.20 5000

It never joins the session. It connects, sends a join request with a
deliberately wrong room code, and checks that the reply is the whiteboard
server's "Invalid room code" error. That proves the address, port, firewall
and server are all fine, without changing anything on the server.

Exit status: 0 = server reachable, 1 = problem found.
"""

import errno
import json
import socket
import sys
import time

DEFAULT_PORT = 5000
TIMEOUT = 4.0


def local_addresses():
    """IPv4 addresses of this machine (best effort, no packets leave the LAN)."""
    found = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))          # only selects an interface
        found.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    found.discard("127.0.1.1")
    return sorted(found)


def describe_adapter(ip):
    """Explain what an IP address says about the VirtualBox network mode."""
    if ip.startswith("10.0.2."):
        return "VirtualBox NAT (NOT visible to other computers - use Bridged Adapter)"
    if ip.startswith("192.168.56."):
        return "VirtualBox Host-Only (only this laptop can reach it)"
    if ip.startswith("127."):
        return "loopback (this machine only)"
    return "LAN address - give this one to your teammate"


def explain_error(exc, host, port):
    """Turn a socket exception into a plain-English diagnosis."""
    if isinstance(exc, socket.gaierror):
        return (f"'{host}' is not a valid IP address or name. "
                "Use the numbers shown by the server, e.g. 192.168.1.20")
    if isinstance(exc, ConnectionRefusedError):
        return (f"The computer answered, but nothing is listening on port {port}. "
                "Is the server running? Is the port number the same on both sides?")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ("No answer (timed out). Typical causes: wrong IP; the two VMs are not on the "
                "same network (use Bridged Adapter); a firewall is blocking the port "
                f"(on Ubuntu: sudo ufw allow {port}/tcp); or the Wi-Fi blocks devices "
                "from talking to each other (try a phone hotspot).")
    if getattr(exc, "errno", None) in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return ("No route to that address. This VM is probably on a different network. "
                "Check that its adapter is set to Bridged and that `hostname -I` shows an "
                "address in the same range as the server's.")
    return f"{exc.__class__.__name__}: {exc}"


def check(host, port):
    print(f"Checking whiteboard server at {host}:{port}")
    started = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
    except OSError as exc:
        print(f"  [FAIL] TCP connect: {explain_error(exc, host, port)}")
        return False
    rtt = (time.monotonic() - started) * 1000
    print(f"  [ OK ] TCP connection established ({rtt:.1f} ms)")

    try:
        sock.settimeout(TIMEOUT)
        probe = {"type": "join", "username": "netcheck", "room_code": "__netcheck__"}
        sock.sendall((json.dumps(probe) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        reply = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  [FAIL] Connected, but this does not look like a whiteboard server ({exc}).")
        return False
    finally:
        sock.close()

    if reply.get("type") == "error":
        print("  [ OK ] Whiteboard server answered the test message "
              f"(\"{reply.get('message')}\" is the expected reply)")
        print("\nRESULT: server reachable. Join with the real room code.")
        return True
    print(f"  [FAIL] Unexpected reply: {reply}")
    return False


def main(argv):
    if len(argv) < 2:
        print("This computer's addresses:")
        for ip in local_addresses() or ["(none found)"]:
            print(f"  {ip:<16} {describe_adapter(ip)}")
        print("\nTo check a server:  python3 tools/netcheck.py <server-ip> [port]")
        return 0
    host = argv[1]
    try:
        port = int(argv[2]) if len(argv) > 2 else DEFAULT_PORT
    except ValueError:
        print("Port must be a number.")
        return 1
    return 0 if check(host, port) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
