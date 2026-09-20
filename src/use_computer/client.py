"""Client side of the daemon protocol; starts the daemon on first use."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

from . import UseComputerError
from .daemon import log_path, socket_path


def target(explicit: str | None = None, bus_only: bool = False,
           start: bool = True) -> Any:
    """Resolve and enter the desktop this process should drive.

    Virtual by default: without an explicit target, agents and CLI calls land on a
    virtual desktop rather than the user's screen. `--real` / USE_COMPUTER_DESKTOP=real
    opts back in to it.
    """
    from . import vd
    d = vd.resolve(explicit, start=start)
    vd.apply_env(d, bus_only=bus_only)
    return d


class RemoteError(UseComputerError):
    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def _connect(timeout: float) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(socket_path()))
    return s


def start_daemon(wait: float = 10.0) -> None:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise UseComputerError("DBUS_SESSION_BUS_ADDRESS is not set; run inside the GNOME session")
    with open(log_path(), "ab") as logf:
        subprocess.Popen([sys.executable, "-m", "use_computer.daemon"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            _connect(1.0).close()
            return
        except OSError:
            time.sleep(0.05)
    raise UseComputerError(f"daemon did not start; see {log_path()}")


class Client:
    def __init__(self, client_id: str | None = None, autostart: bool = True,
                 timeout: float = 300.0) -> None:
        self.client_id = client_id or os.environ.get("USE_COMPUTER_CLIENT") or f"pid:{os.getpid()}"
        self.autostart = autostart
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    def _socket(self) -> socket.socket:
        if self._sock is None:
            try:
                self._sock = _connect(self.timeout)
            except OSError:
                if not self.autostart:
                    raise UseComputerError("use-computer daemon is not running") from None
                start_daemon()
                self._sock = _connect(self.timeout)
        return self._sock

    def call(self, op: str, **args: Any) -> dict[str, Any]:
        payload = (json.dumps({"op": op, "args": args, "client": self.client_id}) + "\n").encode()
        # Retry only when the request never went out: resending after a lost reply
        # could repeat a click or a keystroke.
        for attempt in (1, 2):
            try:
                sock = self._socket()
                sock.sendall(payload)
            except OSError:
                self.close()
                if attempt == 2:
                    raise UseComputerError(f"cannot reach the use-computer daemon; see {log_path()}"
                                           ) from None
                continue
            try:
                reply = self._read_line(sock)
            except (OSError, ConnectionError):
                self.close()
                raise UseComputerError("lost connection to the daemon mid-request (the action may "
                                       f"or may not have happened); see {log_path()}") from None
            break
        msg = json.loads(reply)
        if not msg.get("ok"):
            raise RemoteError(msg.get("error", "unknown error"), msg.get("kind", ""))
        return msg["result"]

    def _read_line(self, sock: socket.socket) -> bytes:
        while (nl := self._buf.find(b"\n")) == -1:
            chunk = sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            self._buf.extend(chunk)
        line = bytes(self._buf[:nl])
        del self._buf[: nl + 1]
        return line

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buf.clear()
