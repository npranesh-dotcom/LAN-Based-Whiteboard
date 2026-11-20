# Backend changes — 10 November 2026

All fixes from the backend audit, applied to `server/`.
Verify with: `python3 tests/backend_e2e_test.py` → `ALL 20 TESTS PASSED`.

Status key: **Tested** = covered by an automated end-to-end test against a real server process. **Configured** = code in place, can't be fully simulated on one machine.

## Must-fix bugs

| # | Problem | Fix | File | Status |
|---|---|---|---|---|
| 1 | Draw handler duplicated: each stroke stored and broadcast twice, undo looked broken | Single clean `draw`/`erase` path (`handle_draw`) | server.py | Tested (test_01) |
| 2 | `load` with a missing/corrupt file wiped everyone's canvas and still said "success" | `load_canvas()` returns `(strokes, error)`; on error the server sends `error` and leaves the canvas unchanged | persistence.py, server.py | Tested (test_02) |
| 3 | Room code could be skipped (defaulted to the correct code) | `room_code` is required; missing → `"Room code is required"` | server.py | Tested (test_03) |
| 4 | No message size limit: a client could exhaust server memory by never sending `\n` | `MAX_MESSAGE_BYTES = 512 KB` per line; larger → error + disconnect | server.py | Tested (test_04) |

## Mandatory-requirement gaps

| # | Problem | Fix | File | Status |
|---|---|---|---|---|
| 5 | Chat had no timestamp or history | Every `chat` has `timestamp` (ISO) and `time` (HH:MM:SS). Last 200 messages replayed to new joiners as `chat` messages with `"history": true` | server.py | Tested (test_05) |
| 6 | Room code hard-coded | `--room`, `--password`, `--port`, `--host` options. With no `--room`, a random 6-character code is generated and printed. Room codes are case-insensitive | server.py, session_manager.py | Tested (test_06) |
| 7 | Undo semantics undefined (global LIFO) | **Per-user undo by default:** undo removes *your* latest stroke, with a redo stack per user. `--undo-mode global` gives board-wide undo | canvas_manager.py | Tested (test_07) |
| 8 | Events could be delivered out of order; a joiner could get a stroke before or overlapping their snapshot | One `state_lock` covers *change state → assign sequence → queue to every client*. Join snapshot carries the join sequence; all later events have a higher one | server.py | Tested (test_08: 300 concurrent strokes + mid-stream joiner, all clients identical) |
| 9 | Anyone could save/load; loaded strokes not validated; concurrent saves shared one `.tmp` file | Save/load are host-only; every loaded stroke is validated (bad ones skipped and counted); unique temp file + save lock + fsync | server.py, persistence.py | Tested (test_09) |
| 10 | Ctrl+C lost unsaved work | Canvas saved automatically on Ctrl+C and on `kill` (SIGTERM) | server.py | Tested (test_10) |
| 11 | Dead connections (cable pulled, VM network dropped) never detected, so the username stayed "taken" | TCP keep-alive (10 s idle, 5 s × 3 probes) + `TCP_USER_TIMEOUT` 30 s → dead peer dropped in ~30 s. Optional `ping` → `pong` | server.py | Configured (test_11 checks the socket options) |

## Hardening

| # | Problem | Fix | Status |
|---|---|---|---|
| 12 | Unknown stroke fields were stored and rebroadcast | `sanitize_stroke()` keeps only `stroke_id, color, width, points`; server sets `tool` and `username` itself | Tested (test_12) |
| 13 | An unexpected exception could kill a client thread without cleanup | Per-message `try/except` (sends "Internal server error", connection stays up) + catch-all around the reader so `user_left` cleanup always runs | Tested (test_13: 8 kinds of bad input, connection survives) |
| 14 | Joiner received its own `user_joined`; join `canvas_state` had no `sequence` | `user_joined` goes to everyone *except* the joiner (who gets `welcome`); join `canvas_state` has `sequence` | Tested (test_14) |
| 15 | One client that stopped reading could stall broadcasts to everyone | Each client has an outbox queue + writer thread. A client with 2000 messages waiting is disconnected | Tested (test_15: 3000 × 1000-point strokes, other client unaffected) |
| 16 | Undo/redo resend the whole canvas | **Kept on purpose.** A full snapshot guarantees every client converges to the same canvas, and the existing client already handles `canvas_state`. A delta message would need client changes | By design |

Regression checks (duplicate username, host-only clear, host migration, `validate_stroke`) still pass: test_16, test_17.

## Additions for the client (11 November 2026)

| # | Addition | Why | Status |
|---|---|---|---|
| 17 | `draw` is idempotent: a `stroke_id` already on the board (or in a redo stack) is not added again; the sender gets `stroke_duplicate` | Handles duplicated updates; lets a client safely re-send strokes after a reconnect | Tested (test_18) |
| 18 | `{"type":"sync"}` → the requester gets `canvas_state` with `action:"sync"` and the current sequence | The client asks for it when it detects a missing update (sequence gap) | Tested (test_19) |
| 19 | `{"type":"cursor", x, y, preview?}` relayed to the others as `cursor` with `username`. Not stored and not sequenced; at most 50/s per client; points validated like strokes | Live cursors and in-progress strokes ("visually distinguish users", "minimal delay") | Tested (test_20) |
| 20 | `start_server(argv, ready)` / `stop_server()` work from a background thread | The client's "Host new session" runs the server inside the host's app | Tested (client_gui_test) |

## Protocol changes the client must know about

Message *types* are unchanged. Additions only:

- `join` **must** include `room_code`.
- `welcome` adds `room_code` and `undo_mode`.
- `canvas_state` always has `sequence`. After undo/redo/clear/load it also has `action`, `by` and (undo/redo) `stroke_id`, which the client may ignore.
- `chat` adds `timestamp` and `time`. History replays add `"history": true`. Their `sequence` is lower than the joiner's `welcome`, so **don't drop chat messages for having an old sequence**.
- The joiner no longer receives its own `user_joined`. Take the participant list and host from `welcome`.
- `save` and `load` are host-only (non-hosts get `error`).
- New optional `{"type":"ping"}` → `{"type":"pong"}`.
- Any `tool`/`username` the client puts in a stroke is ignored; the server decides.

## How to run

```bash
cd ~/CollaborativeWhiteboard
source venv/bin/activate
python3 -m server.server --room WHITEBOARD        # fixed code (easy for demos)
python3 -m server.server                          # random code, printed at start
python3 -m server.server --room CN2026 --password secret --undo-mode global
python3 tests/backend_e2e_test.py                 # full test suite (~20 s)
```
