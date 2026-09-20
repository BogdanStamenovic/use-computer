"""A Mutter RemoteDesktop + ScreenCast session: input injection, frames, clipboard.

Why Mutter's private D-Bus API rather than the xdg-desktop-portal: it needs no
consent dialog and no restore token, gives absolute pointer positions, and is the
same API gnome-remote-desktop uses. The cost is GNOME-only.

The session lives as long as the D-Bus connection that created it, which is why
this runs inside a long-lived daemon. While it is live GNOME shows the
screen-sharing indicator in the top bar; stopping sharing from there closes the
session, and the daemon treats that as the user revoking control.

Threading: everything here runs on the daemon's single thread, which pumps the
default GLib main context. The only other thread is GStreamer's streaming
thread, which just parks the newest sample under a lock.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
import numpy as np
from gi.repository import Gio, GLib, Gst

from . import UseComputerError
from .geometry import Screen

RD = "org.gnome.Mutter.RemoteDesktop"
SC = "org.gnome.Mutter.ScreenCast"
RD_SESSION = RD + ".Session"
PROPS = "org.freedesktop.DBus.Properties"

BTN = {"left": 0x110, "right": 0x111, "middle": 0x112, "back": 0x113, "forward": 0x114}
TEXT_MIMES = ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT"]

CURSOR_HIDDEN, CURSOR_EMBEDDED = 0, 1


class ControlRevoked(UseComputerError):
    pass


@dataclass
class Frame:
    pixels: np.ndarray  # (h, w, 3) uint8 RGB
    seq: int
    time: float


class RemoteSession:
    def __init__(self, connector: str = "", cursor: int = CURSOR_HIDDEN,
                 bus_address: str = "") -> None:
        self.connector = connector
        self.cursor = cursor
        if bus_address:
            self.bus = Gio.DBusConnection.new_for_address_sync(
                bus_address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
        else:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.ctx = GLib.MainContext.default()
        self.rd_path = ""
        self.stream_path = ""
        self.node_id: int | None = None
        self.screen: Screen | None = None
        self.alive = False
        self.closed_by_user = False
        self._stopping = False
        self._subs: list[int] = []
        self._pipeline: Gst.Pipeline | None = None
        self._lock = threading.Lock()
        self._sample: Gst.Sample | None = None
        self._seq = 0
        self._frame_time = 0.0
        self._clip_text: str | None = None
        self._clip_owned = False
        self.last_used = time.monotonic()

    # ---- D-Bus helpers -------------------------------------------------------

    def _call(self, dest: str, path: str, iface: str, method: str,
              args: GLib.Variant | None = None, timeout_ms: int = 5000) -> tuple:
        try:
            ret = self.bus.call_sync(dest, path, iface, method, args, None,
                                     Gio.DBusCallFlags.NONE, timeout_ms, None)
        except GLib.Error as exc:
            if not self.alive and self.closed_by_user:
                raise ControlRevoked(_REVOKED) from exc
            raise UseComputerError(f"{iface}.{method} failed: {exc.message}") from exc
        return ret.unpack() if ret is not None else ()

    def _rd(self, method: str, sig: str | None = None, *values: object) -> tuple:
        self._check()
        args = GLib.Variant(sig, values) if sig else None
        return self._call(RD, self.rd_path, RD_SESSION, method, args)

    def _check(self) -> None:
        if self.closed_by_user:
            raise ControlRevoked(_REVOKED)
        if not self.alive:
            raise UseComputerError("remote desktop session is not running")

    def pump(self, seconds: float = 0.0) -> None:
        end = time.monotonic() + seconds
        while True:
            while self.ctx.iteration(False):
                pass
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.004, left))

    # ---- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self.alive:
            return
        Gst.init(None)
        try:
            self.rd_path = self._call(RD, "/org/gnome/Mutter/RemoteDesktop", RD, "CreateSession")[0]
        except UseComputerError as exc:
            raise UseComputerError(
                "cannot create a Mutter remote desktop session; this needs GNOME Shell on "
                f"Wayland ({exc})"
            ) from exc
        self.alive = True
        sid = self._call(RD, self.rd_path, PROPS, "Get",
                         GLib.Variant("(ss)", (RD_SESSION, "SessionId")))[0]
        sc_path = self._call(SC, "/org/gnome/Mutter/ScreenCast", SC, "CreateSession",
                             GLib.Variant("(a{sv})", ({"remote-desktop-session-id":
                                                       GLib.Variant("s", sid)},)))[0]
        self.stream_path = self._call(
            SC, sc_path, SC + ".Session", "RecordMonitor",
            GLib.Variant("(sa{sv})", (self.connector,
                                      {"cursor-mode": GLib.Variant("u", self.cursor)})))[0]

        def sub(iface: str, member: str, path: str, cb) -> None:  # type: ignore[no-untyped-def]
            self._subs.append(self.bus.signal_subscribe(
                None, iface, member, path, None, Gio.DBusSignalFlags.NONE, cb))

        sub(SC + ".Stream", "PipeWireStreamAdded", self.stream_path, self._on_stream_added)
        sub(RD_SESSION, "Closed", self.rd_path, self._on_closed)
        sub(RD_SESSION, "SelectionTransfer", self.rd_path, self._on_selection_transfer)
        sub(RD_SESSION, "SelectionOwnerChanged", self.rd_path, self._on_owner_changed)

        self._call(RD, self.rd_path, RD_SESSION, "Start")
        self._wait(lambda: self.node_id is not None, 5.0, "PipeWire stream did not appear")
        params = self._call(SC, self.stream_path, PROPS, "Get",
                            GLib.Variant("(ss)", (SC + ".Stream", "Parameters")))[0]
        sw, sh = params["size"]
        try:
            self._call(RD, self.rd_path, RD_SESSION, "EnableClipboard",
                       GLib.Variant("(a{sv})", ({},)))
        except UseComputerError:
            pass  # clipboard features degrade to "unavailable"
        self._start_pipeline()
        self._wait(lambda: self._sample is not None, 5.0, "no frame arrived from the screencast")
        with self._lock:
            assert self._sample is not None
            s = self._sample.get_caps().get_structure(0)
            fw, fh = s.get_value("width"), s.get_value("height")
        self.screen = Screen(int(sw), int(sh), int(fw), int(fh),
                             int(os.environ.get("USE_COMPUTER_MAX_W", "1280")),
                             int(os.environ.get("USE_COMPUTER_MAX_H", "800")))
        self.last_used = time.monotonic()

    def _start_pipeline(self) -> None:
        # always-copy: Mutter hands out DMA-BUF/MemFd buffers that are recycled;
        # without copying, a parked sample's memory is overwritten underneath us.
        desc = (f"pipewiresrc path={self.node_id} always-copy=true do-timestamp=true ! "
                "videoconvert ! video/x-raw,format=RGB ! "
                "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false")
        pipeline = Gst.parse_launch(desc)
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample)
        pipeline.set_state(Gst.State.PLAYING)
        self._pipeline = pipeline

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        try:
            if self.alive and self._clip_owned and self._clip_text is not None:
                self._persist_clipboard(self._clip_text)
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
                self._pipeline = None
            if self.alive:
                try:
                    self._call(RD, self.rd_path, RD_SESSION, "Stop", timeout_ms=2000)
                except UseComputerError:
                    pass
            for s in self._subs:
                self.bus.signal_unsubscribe(s)
            self._subs.clear()
        finally:
            self.alive = False
            self._stopping = False

    def _persist_clipboard(self, text: str) -> None:
        # Mutter drops our selection when the session ends, which would empty the
        # user's clipboard. wl-copy forks a process that keeps owning it.
        if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
            try:
                subprocess.run(["wl-copy", "--", text], timeout=3, check=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _wait(self, pred, timeout: float, message: str) -> None:  # type: ignore[no-untyped-def]
        end = time.monotonic() + timeout
        while not pred():
            if self.closed_by_user:
                raise ControlRevoked(_REVOKED)
            if time.monotonic() > end:
                raise UseComputerError(message)
            self.pump(0.01)

    # ---- signals -------------------------------------------------------------

    def _on_stream_added(self, _c, _s, _p, _i, _sig, params: GLib.Variant) -> None:
        self.node_id = params.unpack()[0]

    def _on_closed(self, *_args: object) -> None:
        if not self._stopping:
            self.closed_by_user = True
        self.alive = False
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None

    def _on_new_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        with self._lock:
            self._sample = sample
            self._seq += 1
            self._frame_time = time.monotonic()
        return Gst.FlowReturn.OK

    def _on_owner_changed(self, _c, _s, _p, _i, _sig, params: GLib.Variant) -> None:
        opts = params.unpack()[0]
        if not opts.get("session-is-owner", False):
            self._clip_owned = False

    def _on_selection_transfer(self, _c, _s, _p, _i, _sig, params: GLib.Variant) -> None:
        _mime, serial = params.unpack()
        data = (self._clip_text or "").encode("utf-8")
        ok = False
        try:
            ret, fds = self.bus.call_with_unix_fd_list_sync(
                RD, self.rd_path, RD_SESSION, "SelectionWrite", GLib.Variant("(u)", (serial,)),
                None, Gio.DBusCallFlags.NONE, 2000, None, None)
            fd = fds.get(ret.unpack()[0])
            try:
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                ok = True
            finally:
                os.close(fd)
        except (GLib.Error, OSError):
            ok = False
        try:
            self._call(RD, self.rd_path, RD_SESSION, "SelectionWriteDone",
                       GLib.Variant("(ub)", (serial, ok)))
        except UseComputerError:
            pass

    # ---- input ---------------------------------------------------------------

    def move(self, x: float, y: float) -> None:
        self._rd("NotifyPointerMotionAbsolute", "(sdd)", self.stream_path, float(x), float(y))
        self.last_used = time.monotonic()

    def button(self, name: str, pressed: bool) -> None:
        if name not in BTN:
            raise UseComputerError(f"unknown mouse button {name!r}")
        self._rd("NotifyPointerButton", "(ib)", BTN[name], pressed)
        self.last_used = time.monotonic()

    def scroll(self, direction: str, amount: int) -> None:
        # Mutter axis 0 is vertical, 1 horizontal; positive steps scroll down/right.
        axis, sign = {"down": (0, 1), "up": (0, -1), "right": (1, 1), "left": (1, -1)}[direction]
        for _ in range(max(1, amount)):
            self._rd("NotifyPointerAxisDiscrete", "(ui)", axis, sign)
            self.pump(0.015)
        self.last_used = time.monotonic()

    def key(self, keysym: int, pressed: bool) -> None:
        self._rd("NotifyKeyboardKeysym", "(ub)", keysym, pressed)
        self.last_used = time.monotonic()

    # ---- frames --------------------------------------------------------------

    def frame(self) -> Frame:
        self._check()
        with self._lock:
            sample, seq, t = self._sample, self._seq, self._frame_time
        if sample is None:
            raise UseComputerError("no frame available yet")
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise UseComputerError("could not map screencast buffer")
        try:
            stride = len(info.data) // h
            arr = np.frombuffer(info.data, dtype=np.uint8).reshape(h, stride)[:, : w * 3]
            pixels = arr.reshape(h, w, 3).copy()
        finally:
            buf.unmap(info)
        self.last_used = time.monotonic()
        return Frame(pixels, seq, t)

    @property
    def frame_seq(self) -> int:
        with self._lock:
            return self._seq

    def wait_quiet(self, quiet: float = 0.15, timeout: float = 1.5) -> bool:
        """Wait until no new frame has arrived for `quiet` seconds.

        The screencast is damage-driven, so frame arrival is a direct signal that
        something on screen changed. Returns False if the screen kept changing
        (video, animation) until the timeout.
        """
        end = time.monotonic() + timeout
        while True:
            self._check()
            self.pump(0.01)
            with self._lock:
                since = time.monotonic() - self._frame_time
            if since >= quiet:
                return True
            if time.monotonic() >= end:
                return False

    def wait_change(self, since_seq: int, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while self.frame_seq == since_seq:
            self._check()
            if time.monotonic() >= end:
                return False
            self.pump(0.01)
        return True

    # ---- clipboard -----------------------------------------------------------

    def clipboard_set(self, text: str) -> None:
        self._clip_text = text
        self._rd("SetSelection", "(a{sv})", {"mime-types": GLib.Variant("as", TEXT_MIMES)})
        self._clip_owned = True
        self.pump(0.02)

    def clipboard_get(self, timeout: float = 2.0) -> str | None:
        self._check()
        if self._clip_owned:
            return self._clip_text
        for mime in TEXT_MIMES[:3]:
            try:
                ret, fds = self.bus.call_with_unix_fd_list_sync(
                    RD, self.rd_path, RD_SESSION, "SelectionRead", GLib.Variant("(s)", (mime,)),
                    None, Gio.DBusCallFlags.NONE, 2000, None, None)
            except GLib.Error:
                continue
            fd = fds.get(ret.unpack()[0])
            try:
                return self._drain(fd, timeout).decode("utf-8", errors="replace")
            except TimeoutError:
                continue
            finally:
                os.close(fd)
        return None

    def _drain(self, fd: int, timeout: float) -> bytes:
        os.set_blocking(fd, False)
        chunks: list[bytes] = []
        end = time.monotonic() + timeout
        while True:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                if time.monotonic() > end:
                    raise TimeoutError from None
                self.pump(0.005)
                continue
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


_REVOKED = (
    "control was revoked: the user stopped screen sharing from the GNOME top bar. "
    "Do not resume without asking them; resume with the `resume` tool/command once they agree."
)
