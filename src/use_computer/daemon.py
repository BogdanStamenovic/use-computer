"""The daemon: owns the Mutter session and serializes every client through one thread.

Why a daemon: a Mutter session dies with the D-Bus connection that created it and
takes ~100 ms to set up, CLI calls are separate processes, and several agents
(MCP servers in different Claude sessions) must not fight over one pointer.

Protocol: newline-delimited JSON over a unix socket.
  request  {"op": str, "args": {...}, "client": str}
  response {"ok": true, "result": {...}} | {"ok": false, "error": str, "kind": str}

The socket name includes a hash of DBUS_SESSION_BUS_ADDRESS, so a daemon started
against one GNOME session is never reused by a client of another (tests run a
headless shell on its own bus).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import selectors
import signal
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from . import UseComputerError

SESSION_IDLE = float(os.environ.get("USE_COMPUTER_SESSION_IDLE", "90"))
DAEMON_IDLE = float(os.environ.get("USE_COMPUTER_DAEMON_IDLE", "1800"))


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(base) / "use-computer"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def socket_path() -> Path:
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    tag = hashlib.sha1(bus.encode()).hexdigest()[:10]
    return runtime_dir() / f"daemon-{tag}.sock"


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    d = Path(base) / "use-computer"
    d.mkdir(parents=True, exist_ok=True)
    return d / "daemon.log"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def serve() -> int:
    from .controller import Controller  # heavy gi imports only in the daemon

    path = socket_path()
    lock = open(str(path) + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another daemon already serves this session")
        return 0
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    listener.listen(16)
    listener.setblocking(False)

    ctl = Controller()
    sel = selectors.DefaultSelector()
    sel.register(listener, selectors.EVENT_READ, None)
    buffers: dict[socket.socket, bytearray] = {}
    last_request = time.monotonic()
    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"listening on {path}")

    try:
        while running:
            for key, _ in sel.select(timeout=0.02):
                if key.data is None:
                    try:
                        conn, _addr = listener.accept()
                    except BlockingIOError:
                        continue
                    conn.setblocking(False)
                    buffers[conn] = bytearray()
                    sel.register(conn, selectors.EVENT_READ, "client")
                    continue
                conn = key.fileobj  # type: ignore[assignment]
                try:
                    chunk = conn.recv(1 << 20)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    sel.unregister(conn)
                    buffers.pop(conn, None)
                    conn.close()
                    continue
                buf = buffers[conn]
                buf.extend(chunk)
                while (nl := buf.find(b"\n")) != -1:
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    last_request = time.monotonic()
                    reply = handle(ctl, line)
                    if reply.get("result", {}).get("_shutdown"):
                        running = False
                    send(conn, reply)
            try:
                ctl.idle_tick(SESSION_IDLE)
            except Exception:
                log("idle tick failed:\n" + traceback.format_exc())
            if time.monotonic() - last_request > DAEMON_IDLE and ctl.session is None:
                log("idle, exiting")
                break
    finally:
        if ctl.session is not None:
            ctl.session.stop()
        path.unlink(missing_ok=True)
        listener.close()
    return 0


def handle(ctl: Any, line: bytes) -> dict[str, Any]:
    try:
        req = json.loads(line)
        op = req["op"]
        args = req.get("args") or {}
        client = str(req.get("client") or "anon")
    except (ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "error": f"bad request: {exc}", "kind": "protocol"}
    if op == "shutdown":
        return {"ok": True, "result": {"done": "daemon exiting", "_shutdown": True}}
    started = time.monotonic()
    try:
        if op == "batch":
            args = {**args, "_client": client}
        result = ctl.dispatch(op, args, client)
        return {"ok": True, "result": result}
    except UseComputerError as exc:
        return {"ok": False, "error": str(exc), "kind": type(exc).__name__}
    except Exception as exc:  # keep serving; report the bug loudly
        log(f"op {op} crashed:\n" + traceback.format_exc())
        return {"ok": False, "error": f"internal error in {op}: {exc!r} (see {log_path()})",
                "kind": "internal"}
    finally:
        took = time.monotonic() - started
        if took > 2:
            log(f"op {op} took {took:.1f}s")


def send(conn: socket.socket, reply: dict[str, Any]) -> None:
    data = (json.dumps(reply, ensure_ascii=False) + "\n").encode()
    conn.setblocking(True)
    try:
        conn.sendall(data)
    except OSError:
        pass
    finally:
        try:
            conn.setblocking(False)
        except OSError:
            pass


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
