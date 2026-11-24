"""A second person at the same laptop: their own desktop on a TV, their own keyboard and mouse.

Real logind multiseat needs a second DRM device to be the new seat's master; a laptop
with one iGPU has its HDMI connector on the same card as its panel, so that route is
closed. Instead this composes pieces use-computer already has:

  * a virtual desktop (headless GNOME Shell) is the guest's session,
  * the viewer, fullscreened on the TV connector of the *real* session, is their screen,
  * their USB keyboard/mouse are opened with evdev and EVIOCGRAB'd, so the real
    session's libinput stops receiving them, and every event is replayed into the
    virtual desktop through the same Mutter RemoteDesktop session that feeds the viewer.

Keys go in as evdev keycodes (NotifyKeyboardKeycode), not keysyms, so the guest
desktop applies its own keyboard layout and modifiers behave natively.

Device access: /dev/input/event* is root:input 0660. Rather than putting the user in
`input` (which lets any process keylog every keyboard), each guest device gets a
per-device ACL via `sudo setfacl`; the ACL lives on the device node and disappears
when the device is unplugged.
"""

from __future__ import annotations

import errno
import math
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import UseComputerError, vd

KEY_A, REL_X, BTN_LEFT = 30, 0, 0x110
REL_HWHEEL, REL_WHEEL = 6, 8
EV_SYN, EV_KEY, EV_REL = 0, 1, 2


def _log(msg: str) -> None:
    import time
    print(f"seat: {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


# ---- device discovery ----------------------------------------------------------

def _caps(event_dir: Path, kind: str) -> int:
    """Parse a sysfs capability bitmap ("hex words, most significant first")."""
    try:
        words = (event_dir / "device" / "capabilities" / kind).read_text().split()
    except OSError:
        return 0
    value = 0
    for w in words:
        value = (value << 64) | int(w, 16)
    return value


def _is_guest_candidate(event_dir: Path) -> bool:
    # Only USB devices: the built-in keyboard and touchpad sit on i8042/i2c and stay
    # with the laptop's owner. A USB webcam's button node has neither KEY_A nor REL_X.
    if "/usb" not in os.path.realpath(event_dir / "device"):
        return False
    keys, rel = _caps(event_dir, "key"), _caps(event_dir, "rel")
    return bool(keys >> KEY_A & 1) or bool(rel >> REL_X & 1 and keys >> BTN_LEFT & 1)


def find_usb_input() -> list[str]:
    out = []
    for ev in sorted(Path("/sys/class/input").glob("event*")):
        if _is_guest_candidate(ev):
            out.append(f"/dev/input/{ev.name}")
    return out


def _ensure_access(path: str) -> None:
    if os.access(path, os.R_OK | os.W_OK):
        return
    user = os.environ.get("USER") or str(os.getuid())
    r = subprocess.run(["sudo", "-n", "setfacl", "-m", f"u:{user}:rw", path],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise UseComputerError(
            f"no access to {path} and passwordless sudo setfacl failed "
            f"({r.stderr.strip()}); run: sudo setfacl -m u:{user}:rw {path}")


# ---- input forwarding ----------------------------------------------------------

class Forwarder:
    """Replays grabbed evdev devices into a Mutter RemoteDesktop session."""

    def __init__(self, session: Any, speed: float) -> None:
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.session = session
        self.speed = speed
        self.w, self.h = session.screen.stream_w, session.screen.stream_h
        self.x, self.y = self.w / 2, self.h / 2
        self.devices: dict[str, tuple[Any, int]] = {}  # path -> (InputDevice, glib source)
        self.down_keys: set[int] = set()
        self.down_buttons: set[int] = set()
        self.failed: set[str] = set()

    def _notify(self, method: str, sig: str, *values: object) -> None:
        # Fire-and-forget on the session's connection: a 1 kHz mouse would otherwise
        # block the GTK loop on a D-Bus round trip per report. Messages on one
        # connection are delivered in order, so press/move/release ordering holds.
        from .mutter import RD, RD_SESSION
        self.session.bus.call(RD, self.session.rd_path, RD_SESSION, method,
                              self.GLib.Variant(sig, values), None,
                              self.Gio.DBusCallFlags.NONE, -1, None, None, None)

    def _move(self) -> None:
        self._notify("NotifyPointerMotionAbsolute", "(sdd)",
                     self.session.stream_path, self.x, self.y)

    def scan(self, explicit: list[str] | None = None) -> bool:
        import evdev
        for path in explicit if explicit is not None else find_usb_input():
            if path in self.devices or path in self.failed:
                continue
            dev = None
            try:
                _ensure_access(path)
                dev = evdev.InputDevice(path)
                dev.grab()
            except OSError as exc:
                if dev is not None:
                    dev.close()
                if exc.errno == errno.EBUSY:
                    continue  # another grabber (e.g. a seat being replaced); retry next scan
                _log(f"skipping {path}: {exc}")
                self.failed.add(path)
                continue
            except UseComputerError as exc:
                _log(f"skipping {path}: {exc}")
                self.failed.add(path)
                continue
            cond = self.GLib.IOCondition
            src = self.GLib.unix_fd_add_full(
                self.GLib.PRIORITY_HIGH, dev.fd, cond.IN | cond.HUP | cond.ERR,
                lambda _fd, _c, p=path: self._on_ready(p))
            self.devices[path] = (dev, src)
            _log(f"grabbed {path}: {dev.name}")
        # Forget failures for nodes that went away, so a replug is retried.
        self.failed = {p for p in self.failed if os.path.exists(p)}
        return True  # keep the GLib timeout alive

    def _drop(self, path: str) -> None:
        dev, src = self.devices.pop(path)
        self.GLib.source_remove(src)
        try:
            dev.close()
        except OSError:
            pass
        _log(f"released {path}")
        if not self.devices:
            self.release_all()

    def release_all(self) -> None:
        """Lift anything held, so an unplug mid-keypress can't leave shift stuck."""
        for code in list(self.down_keys):
            self._notify("NotifyKeyboardKeycode", "(ub)", code, False)
        for code in list(self.down_buttons):
            self._notify("NotifyPointerButton", "(ib)", code, False)
        self.down_keys.clear()
        self.down_buttons.clear()

    def close(self) -> None:
        for path in list(self.devices):
            self._drop(path)

    def _on_ready(self, path: str) -> bool:
        dev, _ = self.devices[path]
        try:
            events = list(dev.read())
        except BlockingIOError:
            return True
        except OSError as exc:
            if exc.errno in (errno.ENODEV, errno.EIO):
                self._drop(path)
                return False
            raise
        dx = dy = 0
        for e in events:
            if e.type == EV_REL:
                if e.code == REL_X:
                    dx += e.value
                elif e.code == REL_X + 1:
                    dy += e.value
                elif e.code == REL_WHEEL:
                    self._notify("NotifyPointerAxisDiscrete", "(ui)", 0, -e.value)
                elif e.code == REL_HWHEEL:
                    self._notify("NotifyPointerAxisDiscrete", "(ui)", 1, e.value)
            elif e.type == EV_KEY:
                if dx or dy:  # a click lands where the pointer is *now*
                    self._apply_motion(dx, dy)
                    dx = dy = 0
                if e.value == 2:
                    continue  # autorepeat: Wayland clients repeat on their own
                pressed = e.value == 1
                if 0x110 <= e.code < 0x120:
                    (self.down_buttons.add if pressed else self.down_buttons.discard)(e.code)
                    self._notify("NotifyPointerButton", "(ib)", e.code, pressed)
                else:
                    (self.down_keys.add if pressed else self.down_keys.discard)(e.code)
                    self._notify("NotifyKeyboardKeycode", "(ub)", e.code, pressed)
        if dx or dy:
            self._apply_motion(dx, dy)
        return True

    def _apply_motion(self, dx: int, dy: int) -> None:
        # Grabbing the device bypasses libinput, so its pointer acceleration is gone
        # too. A mild speed-dependent curve keeps both slow aiming and crossing a
        # 1080p screen in one flick workable.
        v = math.hypot(dx, dy)
        gain = self.speed * (1.0 + min(v, 40.0) * 0.05)
        self.x = min(max(self.x + dx * gain, 0.0), self.w - 1.0)
        self.y = min(max(self.y + dy * gain, 0.0), self.h - 1.0)
        self._move()


# ---- TV ------------------------------------------------------------------------

def external_connectors() -> list[tuple[str, str]]:
    """(connector, preferred mode) for connected non-panel outputs."""
    out = []
    for c in sorted(Path("/sys/class/drm").glob("card*-*")):
        try:
            if c.joinpath("status").read_text().strip() != "connected":
                continue
            modes = c.joinpath("modes").read_text().split()
        except OSError:
            continue
        name = c.name.split("-", 1)[1]
        if name.startswith(("eDP", "LVDS", "DSI")):
            continue
        out.append((name, modes[0] if modes else ""))
    return out


def _same_connector(a: str, b: str) -> bool:
    # The kernel names HDMI ports "HDMI-A-1" (sysfs), Mutter/GDK call the same port
    # "HDMI-1"; DVI is "DVI-D-1" vs "DVI-1". Compare with the subtype letter dropped.
    def norm(c: str) -> str:
        parts = c.split("-")
        if len(parts) == 3 and len(parts[1]) == 1:
            parts.pop(1)
        return "-".join(parts)
    return norm(a) == norm(b)


def _fit_size(mode: str, cap_w: int) -> str:
    try:
        w, h = (int(v) for v in mode.split("x"))
    except ValueError:
        return vd.DEFAULT_SIZE
    if w > cap_w:  # every frame is copied on the CPU; a 4K stream costs 4x for little gain
        h, w = round(h * cap_w / w), cap_w
    return f"{w}x{h}"


# ---- guest browser -------------------------------------------------------------

def _profile_dir(name: str) -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) \
        / "use-computer" / "seat" / name / "chrome"


def _guest_launchers(name: str, exe: str) -> Path:
    """An XDG data dir whose google-chrome.desktop opens the guest profile.

    The guest shell shares the owner's home, so the dock's Chrome icon would run plain
    google-chrome-stable; Chrome's singleton then hands the request to the *owner's*
    running Chrome and the window opens on the owner's screen, signed in as the owner.
    Prepending this dir to XDG_DATA_DIRS for the guest shell shadows the system
    launcher (nothing in ~/.local/share/applications shadows it first).
    """
    root = _profile_dir(name).parent / "xdg"
    apps = root / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    flags = (f"--user-data-dir={_profile_dir(name)} --ozone-platform=wayland --no-first-run "
             "--no-default-browser-check --password-store=basic")
    entry = ("[Desktop Entry]\nVersion=1.0\nType=Application\nName=Google Chrome\n"
             f"Exec={exe} {flags} %U\nIcon=google-chrome\nTerminal=false\n"
             "Categories=Network;WebBrowser;\nStartupWMClass=Google-chrome\n"
             "MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;\n")
    for fn in ("google-chrome.desktop", "com.google.Chrome.desktop"):
        (apps / fn).write_text(entry)
    return root


def launch_browser(d: vd.Desktop, name: str) -> subprocess.Popen[bytes] | None:
    exe = next((shutil.which(b) for b in ("google-chrome-stable", "google-chrome",
                                           "chromium") if shutil.which(b)), None)
    if exe is None:
        _log("no Chrome/Chromium found; the guest can open apps from Activities")
        return None
    prof = _profile_dir(name)
    if _profile_pids(prof):
        _log("guest Chrome already running; leaving it")
        return None
    prof.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_SOCKET")}
    env.update(d.env)
    # A separate --user-data-dir is a separate Chrome instance: no singleton handoff
    # to the owner's running Chrome, no shared cookies or signed-in accounts.
    # password-store=basic: the virtual desktop has no unlocked keyring, and Chrome
    # would otherwise block on an unlock prompt.
    cmd = [exe, f"--user-data-dir={prof}", "--ozone-platform=wayland", "--no-first-run",
           "--no-default-browser-check", "--password-store=basic", "--start-maximized"]
    return subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def _profile_pids(prof: Path) -> list[int]:
    r = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", f"--user-data-dir={prof}"],
                       capture_output=True, text=True, check=False)
    return [int(p) for p in r.stdout.split()]


def _stop_browser(chrome: subprocess.Popen[bytes] | None, prof: Path) -> None:
    """Stop every process on the guest profile, not just Chrome's process group:
    crashpad and some helpers leave the group, and anything still alive keeps
    writing into the profile after it has been deleted."""
    import time
    if chrome is not None and chrome.poll() is None:
        try:
            os.killpg(chrome.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    end = time.monotonic() + 5
    while (pids := _profile_pids(prof)) and time.monotonic() < end:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(0.2)
    for pid in _profile_pids(prof):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if chrome is not None:
        try:
            chrome.wait(2)
        except subprocess.TimeoutExpired:
            pass


# ---- main ----------------------------------------------------------------------

def run(name: str = "guest", monitor: str | None = None, size: str | None = None,
        devices: list[str] | None = None, speed: float = 1.0, browser: bool = True,
        keep_profile: bool = False, keep_desktop: bool = False, fps: float = 30.0) -> int:
    try:
        import evdev  # noqa: F401
    except ImportError as exc:
        raise UseComputerError("seat needs python-evdev: uv pip install evdev "
                               "(or install use-computer with the 'seat' extra)") from exc
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    from .viewer import _session

    outs = external_connectors()
    if monitor is None and outs:
        monitor = outs[0][0]
    if size is None:
        mode = next((m for c, m in outs if c == monitor), "")
        size = _fit_size(mode, 1920) if mode else vd.DEFAULT_SIZE
    _log(f"TV: {monitor or 'none connected (windowed)'}; guest desktop {size}")

    exe = next((shutil.which(b) for b in ("google-chrome-stable", "google-chrome")
                if shutil.which(b)), None)
    prev = vd.load(name)
    if exe is not None and (prev is None or not prev.alive):
        xdg = _guest_launchers(name, exe)
        dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
        os.environ["XDG_DATA_DIRS"] = f"{xdg}:{dirs}"
    d = vd.start(name, size)
    chrome = launch_browser(d, name) if browser else None
    session = _session(d, cursor_embedded=True)
    fwd = Forwarder(session, speed)

    GLib.set_prgname("use-computer-seat")
    # NON_UNIQUE: a second seat (or a test run) must not hand off to the running one.
    from gi.repository import Gio
    app = Gtk.Application(application_id="dev.usecomputer.seat",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (app.quit(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (app.quit(), False)[1])
    state: dict[str, Any] = {"seq": -1}

    def start_display(pic: Any) -> None:
        """Show the guest's screencast straight from PipeWire on the GPU.

        The generic viewer path (appsink -> videoconvert to RGB -> numpy -> MemoryTexture,
        polled every 33 ms) measured ~35 fps and 50-80 ms hand-to-screen on this
        laptop: fine to watch an agent, sluggish to *use*. gtk4paintablesink imports
        Mutter's DMA-BUFs as GL textures and repaints per frame, so there is no CPU
        conversion and no polling interval.
        """
        from gi.repository import Gst
        # The RemoteSession's own appsink pipeline is only for screenshots; a second
        # consumer on the node would make Mutter render for both. Drop it.
        if session._pipeline is not None:
            session._pipeline.set_state(Gst.State.NULL)
            session._pipeline = None
        desc = (f"pipewiresrc path={session.node_id} do-timestamp=true keepalive-time=1000 ! "
                "queue max-size-buffers=1 leaky=downstream ! "
                "gtk4paintablesink name=sink sync=false")
        pipe = Gst.parse_launch(desc)
        sink = pipe.get_by_name("sink")
        pic.set_paintable(sink.get_property("paintable"))
        bus = pipe.get_bus()
        bus.add_signal_watch()

        def on_msg(_b: Any, msg: Any) -> None:
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                _log(f"display pipeline error: {err.message} ({dbg})")
        bus.connect("message", on_msg)
        pipe.set_state(Gst.State.PLAYING)
        state["pipe"] = pipe

    def on_activate(a: Any) -> None:
        win = Gtk.ApplicationWindow(application=a, title=f"use-computer seat — {name}")
        pic = Gtk.Picture()
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        # The owner's pointer can still wander onto the TV output; hide it there so
        # the only cursor on the TV is the guest's (embedded in the stream).
        pic.set_cursor_from_name("none")
        state["pic"] = pic
        win.set_child(pic)
        target = None
        mons = Gdk.Display.get_default().get_monitors()
        for i in range(mons.get_n_items()):
            m = mons.get_item(i)
            if monitor and _same_connector(m.get_connector() or "", monitor):
                target = m
        if target is not None:
            win.fullscreen_on_monitor(target)
        else:
            win.set_default_size(1280, 720)
        win.present()
        start_display(pic)
        fwd.scan(devices)
        if devices is None:
            GLib.timeout_add_seconds(1, fwd.scan)
        _log("ready — plug in the guest keyboard/mouse (ctrl-c here to end)")

    app.connect("activate", on_activate)
    try:
        return app.run([])
    finally:
        _log("shutting down")
        if state.get("pipe") is not None:
            from gi.repository import Gst
            state["pipe"].set_state(Gst.State.NULL)
        fwd.release_all()
        fwd.close()
        session.stop()
        _stop_browser(chrome, _profile_dir(name))
        _log("guest browser stopped")
        if not keep_desktop:
            _log("stopping guest desktop")
            vd.stop(name, quiet=True)
        prof = _profile_dir(name)
        if keep_profile:
            _log(f"guest Chrome profile kept at {prof}")
        elif prof.parent.parent.name == "seat" and prof.exists():
            shutil.rmtree(prof, ignore_errors=True)
            _log("guest Chrome profile wiped")
