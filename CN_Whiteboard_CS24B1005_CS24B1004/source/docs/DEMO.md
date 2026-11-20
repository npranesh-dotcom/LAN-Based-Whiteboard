# Demo guide — two laptops, each running Ubuntu in VirtualBox

Evaluation rule from the brief: the demo must run **with the internet disabled**. A LAN with no internet uplink is all this project needs.

Status: every command below was run on my test machine (Python 3.12, Tk 8.6) and the outputs are real. **Not yet tested by me: two physical laptops with bridged VirtualBox networking.** Do the rehearsal in section 6 once, at least a day before.

---

## 1. What "Bridged Adapter" is, and what to change

By default a VirtualBox VM uses **NAT**: it sits behind a private network (address `10.0.2.15`) that only that laptop can see. Your teammate's VM cannot reach it, and it cannot reach theirs.

**Bridged Adapter** plugs the VM into the *same real network* as the laptop (same Wi-Fi or cable). The VM then gets its own address from the router, such as `192.168.1.37`, and other computers on that network can connect to it directly. That is what a "LAN" demo needs.

**Do this on BOTH laptops:**

1. Shut the VM down (Ubuntu menu → Power off).
2. VirtualBox → select the VM → **Settings → Network → Adapter 1**.
3. **Attached to:** `Bridged Adapter`.
4. **Name:** pick the laptop's *active* connection (the Wi-Fi card if you use Wi-Fi, the Ethernet card if you use a cable). The wrong choice is the most common reason it fails.
5. Expand **Advanced** → **Promiscuous Mode:** `Allow All` (helps on Wi-Fi). Leave "Cable Connected" ticked. Click OK.
6. Start the VM and check the address:
   ```bash
   hostname -I
   ```
   Good: `192.168.1.37` (same first three numbers as the laptop's own address and as your teammate's VM).
   Bad: `10.0.2.15` means it is still NAT; the setting did not apply.

**Code changes needed: none.** The server already listens on every interface (`0.0.0.0`), the client takes any address, and the port is configurable. I added two small helpers: `tools/netcheck.py` (checks the connection before the demo) and clearer error messages in the client.

---

## 2. Network for the demo (internet off)

Both laptops must be on the **same network**, with no internet needed. Pick one:

| Option | How | Notes |
|---|---|---|
| **Phone hotspot, mobile data OFF** (easiest) | Phone creates a hotspot, turn mobile data off, both laptops join it | No internet at all, but the hotspot still gives out addresses. Some phones isolate clients from each other; test it |
| Wi-Fi router, uplink unplugged | Both on the same router, WAN cable out | Reliable if you have one |
| Ethernet cable between the laptops | Bridge each VM to its Ethernet card, set addresses by hand (section 7) | Needs no router; slightly more setup |
| Campus / office Wi-Fi | Only to rehearse | Often blocks devices from talking to each other, or refuses to give the VM an address. Don't depend on it |

If you bridge over Wi-Fi and then "disable the internet" by switching the **laptop's Wi-Fi off**, the VM loses its network too. Disconnect the *uplink* (mobile data off, WAN cable out), not the laptop's Wi-Fi.

---

## 3. One-time setup (each laptop, inside Ubuntu)

```bash
sudo apt update && sudo apt install -y python3-tk
cd ~/CollaborativeWhiteboard
source venv/bin/activate
pip install pillow
```
Expected: the last line ends with `Successfully installed pillow-...` (or `Requirement already satisfied`). Do this while you still have internet; it is not needed afterwards.

Check the code is there and passes (takes about 30 s):
```bash
python3 tests/backend_e2e_test.py
```
Expected, last two lines:
```
------------------------------------------------------------
ALL 21 TESTS PASSED
```
You will see 21 lines starting with `PASS`. One line prints `(3000 x 1000-point strokes delivered in X s)`; the time varies by machine.

```bash
python3 tests/client_gui_test.py
```
Expected: windows open and close by themselves for about 25 s, then
```
ALL CLIENT GUI CHECKS PASSED (33)
```
Do not touch the mouse while it runs: it creates its own window and drives it. If a line says `FAIL`, send me the whole output.

---

## 4. Running the demo

### Option A: host from the app (recommended, one program)

**Laptop 1 (host):**
```bash
cd ~/CollaborativeWhiteboard && source venv/bin/activate
hostname -I                       # note the address, e.g. 192.168.1.37
python3 -m client.main
```
The terminal prints nothing; a window opens with the **Collaborative Whiteboard** form. Enter your name, leave Server address and Room code as they are, and press **Host new session**.

You should see the board with a top bar like:
```
◆ Whiteboard   [Room  K7QX3M]   Hosting · others join at 192.168.1.37:5000      ● You   ● Connected   [Leave]
```
**Read the room code and the address from that bar to your teammate.** (The terminal now shows the server's log lines such as `[JOINED] ...`.)

**Laptop 2 (teammate), first check the connection:**
```bash
cd ~/CollaborativeWhiteboard && source venv/bin/activate
python3 tools/netcheck.py 192.168.1.37
```
Expected:
```
Checking whiteboard server at 192.168.1.37:5000
  [ OK ] TCP connection established (2.1 ms)
  [ OK ] Whiteboard server answered the test message ("Invalid room code" is the expected reply)

RESULT: server reachable. Join with the real room code.
```
If you see `[FAIL]`, the message explains the likely cause; see section 5. Then:
```bash
python3 -m client.main
```
Enter name, **Server address** `192.168.1.37`, **Port** `5000`, **Room code** `K7QX3M` (use the host's real values), press **Join session**.

Both screens should now show two names under **PARTICIPANTS · 2 ONLINE**, the first with `♛ host`. On the host's chat you see `<name> joined.`

### Option B: dedicated server (if you want the server in its own terminal)
On laptop 1:
```bash
cd ~/CollaborativeWhiteboard && source venv/bin/activate
python3 -m server.server --room CN2026
```
Expected:
```
[LOAD] No previous canvas found
==================================================
       COLLABORATIVE WHITEBOARD SERVER
==================================================
[STARTED]  Listening on 0.0.0.0:5000
[ADDRESS]  Clients connect to 127.0.0.1:5000
[ADDRESS]  Clients connect to 192.168.1.37:5000
[ROOM]     Room code: CN2026
[ROOM]     Password : (none)
[CANVAS]   0 stroke(s) restored, undo mode: user
[WAITING]  Waiting for clients... (Ctrl+C to stop and save)
==================================================
```
Use the `192.168.x.x` line (not `127.0.0.1`) as the address. Add `--password secret` to protect the room, `--undo-mode global` for board-wide undo. Both laptops then run `python3 -m client.main` and press **Join session**. Stop the server with **Ctrl+C**; it prints `[SAVE] Canvas saved successfully (N strokes)` and the board comes back next time.

---

## 5. If it does not connect

| What you see | Cause and fix |
|---|---|
| `hostname -I` shows `10.0.2.15` | Still NAT. Power the VM off, redo section 1, make sure the right Name is chosen |
| netcheck: `timed out` | Different networks (one VM still NAT?), wrong IP, firewall, or Wi-Fi isolation. Check both `hostname -I` start with the same three numbers. Try `ping 192.168.1.37` from laptop 2. Then `sudo ufw status` on the host; if active: `sudo ufw allow 5000/tcp` |
| netcheck: `nothing is listening on port 5000` | Network is fine; the server is not running, or uses another port |
| Client: `Invalid room code` | Typo; the code is not case-sensitive but must otherwise match exactly |
| Client: `Incorrect room password` | Server was started with `--password` |
| Client: `Username already taken` | Both of you typed the same name |
| Host: `Could not start server on port 5000` | A server is already running on that machine. Close it (`pkill -f server.server`) |
| Ping works but the client times out | Firewall on the host VM, see the `ufw` line above |
| Windows laptop firewall (only if you use NAT forwarding, section 7) | Allow VirtualBox through Windows Defender Firewall |

---

## 6. Demo script (about 8 minutes; each step names the rubric item it shows)

Run it once together the day before.

| # | Do | Say / point at | Rubric item |
|---|---|---|---|
| 1 | Show both terminals; run `python3 tools/netcheck.py <ip>` on laptop 2 | "Client-server over TCP on the LAN, no internet" | Network architecture |
| 2 | Host a session, teammate joins with IP + room code. Try a **wrong code** first, then the right one | "Room code is checked by the server" | Session management, access control |
| 3 | Show the participant list and the join notice in chat | "Server keeps the participant list and notifies everyone" | Participant management |
| 4 | Both draw at once, different colours. Point at the other person's **live cursor with name** | "Strokes are sent as JSON, ordered by sequence numbers" | Real-time drawing, user identification |
| 5 | Draw a rectangle, ellipse, line (hold **Shift**). Use the **eraser** | Extra features | Drawing tools |
| 6 | Chat both ways; show timestamps | "Chat is stored on the server and replayed to new joiners" | Group chat |
| 7 | **Undo** on one laptop: only that user's last stroke disappears | "Per-user undo, defined for concurrent editing" | Undo/redo semantics |
| 8 | Non-host presses **Clear**: it is greyed out. Host presses Clear (confirm) | "Permissions enforced by the server too" | Access control |
| 9 | Host: **Save**, then draw more, then **Restore** | State persistence | Save/restore |
| 10 | Teammate presses **Leave**, then joins again from the form | "A new joiner gets the full board" | State synchronisation |
| 11 | **Recovery:** unplug the network on the teammate's VM (VirtualBox → Devices → Network → untick *Connect Network Adapter*), draw something, wait for **Reconnecting…**, tick it again | "Heartbeat detects the loss in 15 s, client reconnects by itself, offline strokes sync" (the name may stay "taken" for up to ~30 s until the server drops the dead connection; the client keeps retrying, so just wait) | Recovery after disconnection |
| 12 | Stop and restart the host server (Option B) or close and reopen the host (Option A) | "Server saves on exit and restores on start" | Persistence |
| 13 | **Export PNG** | Extra feature | Export |

Shortcuts: `P` pen, `L` line, `R` rectangle, `O` ellipse, `E` eraser, `[` `]` brush size, `Ctrl+Z` / `Ctrl+Y`, `Ctrl+S`.

---

## 7. Fallbacks

**A. Both VMs stay on NAT (no bridging at all).** Works if the two *laptops* can reach each other on the LAN.
On the **host laptop**: VirtualBox → VM → Settings → Network → Adapter 1 (NAT) → Advanced → **Port Forwarding** → add rule: Protocol `TCP`, Host Port `5000`, Guest Port `5000` (leave the IP fields empty). Allow VirtualBox/port 5000 in the host laptop's own firewall.
The teammate's VM (NAT is fine, outgoing connections work) joins using the **host laptop's own LAN address** (e.g. `ipconfig` on Windows), not the VM's.

**B. Direct Ethernet cable, no router.** Bridge each VM to the *Ethernet* adapter, then give each a fixed address (interface name is usually `enp0s3`; check with `ip -br a`):
```bash
sudo ip addr add 192.168.50.1/24 dev enp0s3      # laptop 1
sudo ip addr add 192.168.50.2/24 dev enp0s3      # laptop 2
ping -c 2 192.168.50.1                           # from laptop 2: replies expected
```
Then use `192.168.50.1` as the server address. (Resets on reboot.) I have not tested this one.

**C. Last resort:** both clients on the *same* VM. Open two terminals, run `python3 -m client.main` in each, host in one and join `127.0.0.1` in the other. It proves the software, not the LAN, so say so honestly.

---

## 8. Mixed computers: your VM, a Windows laptop, a professor's Linux machine

**Short answer: yes, all three can be in the same session at once.** The server and client use only plain TCP and JSON text, with no operating-system-specific code, and the server was tested with several clients connected at the same time. **What I have tested:** Linux only. I could not run it on Windows or on your professor's distribution, so do the 5-minute check below with your friend before the demo.

### Who runs what
* **Your VM hosts** (bridged adapter, section 1). It makes the other two simple *clients*: they only make outgoing connections, so no firewall prompts.
* If a Windows laptop hosts instead, Windows Defender Firewall asks "Allow Python to communicate…": tick **Private networks** and Allow, or nobody can join.
* All three computers must be on the **same network** (same Wi-Fi / hotspot) as your laptop, since your VM is bridged to it. Chat timestamps come from the host's clock, so everyone sees the same times.

### What the other two need
Only the **`client/`** folder (copy it by USB stick, zip or Git; add `tools/` if they want the connection check) plus **Python 3.8 or newer with Tk**. They do not need `server/`.

**Windows (friend, VS Code terminal or any terminal)**
1. Install Python from python.org (tick **Add python.exe to PATH**; Tk is included by default). Check: `py -c "import tkinter; print('tk ok')"`
2. In the folder that *contains* `client\`:
```
py tools\netcheck.py 192.168.1.37          (only if tools\ was copied)
py -m client.main
```
3. Optional, for PNG export: `py -m pip install pillow`

**Professor's Linux**
```bash
python3 -c "import tkinter; print('tk ok')"       # if this errors, install Tk:
sudo apt install python3-tk                        # Debian / Ubuntu
sudo dnf install python3-tkinter                   # Fedora
cd /path/to/folder/containing/client
python3 -m client.main
```
If the professor does not want to install anything, they can join from **your** laptop's screen instead, or use a second window on your VM.

In every case: enter **your VM's IP** (from `hostname -I`), port `5000`, the room code, and a unique name; press **Join session**. Expected: three coloured names under **PARTICIPANTS · 3 ONLINE**, your name with the crown.

### 5-minute check with your friend (do it before the demo)
1. Your VM hosts; the friend joins from Windows. Draw in both directions, send a chat message, press Undo.
2. Look for: boxes (☐) instead of tool icons or the crown, text cut off, or a window larger than their screen. These would be cosmetic only; tell me what you see and I will adjust.
3. On Windows the first run of `py` may show a Store pop-up if Python is not installed: install it from python.org instead.

### Known limits
* The window now shrinks to fit small screens (tested at 1024×768); on a high-DPI Windows laptop (125-150 % scaling) text may look slightly larger. That is normal for Tk.
* A standalone `.exe` / Linux binary (PyInstaller) must be built on the same OS it will run on. That is a later deliverable and not part of this section.
