"""Watching a virtual desktop: a window on the real session, or the terminal itself.

Both renderers share one frame source -- the same `RemoteSession` the daemon uses --
rather than opening a second PipeWire consumer on Mutter's stream. The session is
created against the virtual desktop's bus while the window stays on the real one:
D-Bus decides which desktop we talk to, Wayland decides where the window appears,
and PipeWire is shared between them because the virtual desktop's runtime dir
symlinks the real PipeWire sockets.

The GTK viewer is an ordinary window, so it can be fullscreened or parked on
another workspace. The terminal viewer exists because `watch` is something you
type: it draws into the shell you typed it in, and in `symbols` mode a frame is
under 2 KB, which is what makes watching a remote machine's desktop over SSH
practical.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from . import UseComputerError
from .vd import Desktop


def _session(desktop: Desktop | None, cursor_embedded: bool = True) -> Any:
    """A session on the watched desktop's bus, without touching this process's env.

    Redirecting DBUS_SESSION_BUS_ADDRESS in os.environ would work for D-Bus but
    would also send GTK's accessibility registration to the watched desktop, making
    the viewer window appear in that desktop's own window list as a phantom.
    """
    from .mutter import CURSOR_EMBEDDED, CURSOR_HIDDEN, RemoteSession
    s = RemoteSession(connector=os.environ.get("USE_COMPUTER_MONITOR", ""),
                      cursor=CURSOR_EMBEDDED if cursor_embedded else CURSOR_HIDDEN,
                      bus_address=desktop.bus if desktop is not None else "")
    s.start()
    return s


# ---- terminal ---------------------------------------------------------------

def _chafa_size(reserve: int = 1) -> str:
    cols, rows = shutil.get_terminal_size((100, 30))
    return f"{max(20, cols)}x{max(10, rows - reserve)}"


def watch_tty(desktop: Desktop | None, fps: float = 8.0, fmt: str | None = None,
              once: bool = False) -> int:
    """Draw the desktop into this terminal until interrupted."""
    if shutil.which("chafa") is None:
        raise UseComputerError("chafa is not installed (pacman -S chafa); "
                               "it renders frames as terminal graphics")
    import io

    from PIL import Image

    session = _session(desktop)
    label = f"virtual desktop {desktop.name!r}" if desktop else "real desktop"
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))
    hide_cursor, show_cursor, home = "\033[?25l", "\033[?25h", "\033[H"
    if not once:
        sys.stdout.write("\033[2J" + hide_cursor)
    period = 1.0 / max(0.5, fps)
    last_seq = -1
    try:
        while not stop["now"]:
            started = time.monotonic()
            session.pump(0.02)
            try:
                frame = session.frame()
            except UseComputerError:
                time.sleep(0.1)
                continue
            if frame.seq != last_seq or once:
                last_seq = frame.seq
                buf = io.BytesIO()
                # compress_level=1: chafa re-decodes this immediately; spending CPU
                # on compression here would just lower the frame rate.
                Image.fromarray(frame.pixels).save(buf, format="PNG", compress_level=1)
                cmd = ["chafa", "--size", _chafa_size(), "--animate", "off"]
                if fmt:
                    cmd += ["--format", fmt]
                out = subprocess.run(cmd + ["-"], input=buf.getvalue(),
                                     capture_output=True, check=False)
                if out.returncode != 0:
                    raise UseComputerError(
                        f"chafa failed: {out.stderr.decode(errors='replace').strip()}")
                sys.stdout.write(("" if once else home) + out.stdout.decode(errors="replace"))
                if not once:
                    sys.stdout.write(f"\n{label} — {frame.pixels.shape[1]}x"
                                     f"{frame.pixels.shape[0]} — ctrl-c to stop\033[K")
                sys.stdout.flush()
            if once:
                break
            left = period - (time.monotonic() - started)
            if left > 0:
                session.pump(left)
    finally:
        if not once:
            sys.stdout.write(show_cursor + "\n")
            sys.stdout.flush()
        session.stop()
    return 0


# ---- window -----------------------------------------------------------------

def watch_window(desktop: Desktop | None, control: bool = False, fps: float = 30.0,
                 fullscreen: bool = False) -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    session = _session(desktop)
    screen = session.screen
    label = f"virtual desktop: {desktop.name}" if desktop else "real desktop"
    title = f"use-computer — {label}" + ("" if control else " (view only)")

    GLib.set_prgname("use-computer")  # otherwise the window is attributed to "python"
    GLib.set_application_name("use-computer")
    app = Gtk.Application(application_id="dev.usecomputer.viewer")
    # Python's SIGINT handler never runs while GLib owns the loop, so ctrl-c in the
    # terminal that launched the viewer would otherwise be ignored.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (app.quit(), False)[1])
    state: dict[str, Any] = {"seq": -1, "picture": None, "win": None}

    def to_stream(px: float, py: float) -> tuple[float, float] | None:
        """Widget pixels -> Mutter stream coordinates, undoing CONTAIN letterboxing."""
        pic = state["picture"]
        if pic is None:
            return None
        w, h = pic.get_width(), pic.get_height()
        if w <= 0 or h <= 0:
            return None
        scale = min(w / screen.stream_w, h / screen.stream_h)
        off_x, off_y = (w - screen.stream_w * scale) / 2, (h - screen.stream_h * scale) / 2
        x, y = (px - off_x) / scale, (py - off_y) / scale
        if not (0 <= x < screen.stream_w and 0 <= y < screen.stream_h):
            return None
        return x, y

    def tick() -> bool:
        try:
            frame = session.frame()
        except UseComputerError:
            return True
        if frame.seq == state["seq"]:
            return True
        state["seq"] = frame.seq
        h, w, _ = frame.pixels.shape
        data = GLib.Bytes.new(frame.pixels.tobytes())
        texture = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, data, w * 3)
        state["picture"].set_paintable(texture)
        return True

    def on_activate(a: Any) -> None:
        win = Gtk.ApplicationWindow(application=a, title=title)
        win.set_default_size(min(1600, screen.stream_w), min(900, screen.stream_h))
        pic = Gtk.Picture()
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_can_focus(True)
        state["picture"], state["win"] = pic, win
        win.set_child(pic)
        if control:
            _wire_input(win, pic, session, to_stream, Gtk, Gdk)
        esc = Gtk.EventControllerKey()

        def on_esc(_c: Any, keyval: int, _k: int, _m: Any) -> bool:
            if keyval == Gdk.KEY_Escape and win.is_fullscreen():
                win.unfullscreen()
                return True
            return False
        esc.connect("key-pressed", on_esc)
        win.add_controller(esc)
        if fullscreen:
            win.fullscreen()
        win.present()
        GLib.timeout_add(int(1000 / max(1.0, fps)), tick)

    app.connect("activate", on_activate)
    try:
        return app.run([])
    finally:
        session.stop()


def _wire_input(win: Any, pic: Any, session: Any, to_stream: Any, Gtk: Any, Gdk: Any) -> None:
    """Forward the viewer's mouse and keyboard into the watched desktop."""
    motion = Gtk.EventControllerMotion()
    motion.connect("motion", lambda _c, x, y: _move(session, to_stream(x, y)))
    pic.add_controller(motion)

    click = Gtk.GestureClick()
    click.set_button(0)  # any button
    names = {1: "left", 2: "middle", 3: "right"}

    def press(g: Any, _n: int, x: float, y: float) -> None:
        pic.grab_focus()
        pos = to_stream(x, y)
        if pos is None:
            return
        _move(session, pos)
        session.button(names.get(g.get_current_button(), "left"), True)

    def release(g: Any, _n: int, x: float, y: float) -> None:
        pos = to_stream(x, y)
        if pos is not None:
            _move(session, pos)
        session.button(names.get(g.get_current_button(), "left"), False)

    click.connect("pressed", press)
    click.connect("released", release)
    pic.add_controller(click)

    scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)

    def on_scroll(_c: Any, dx: float, dy: float) -> bool:
        if dy:
            session.scroll("down" if dy > 0 else "up", max(1, round(abs(dy))))
        if dx:
            session.scroll("right" if dx > 0 else "left", max(1, round(abs(dx))))
        return True
    scroll.connect("scroll", on_scroll)
    pic.add_controller(scroll)

    keys = Gtk.EventControllerKey()
    # GDK keyvals are X keysyms, which is exactly what Mutter's NotifyKeyboardKeysym
    # wants, so no translation table is needed here.
    keys.connect("key-pressed", lambda _c, kv, _k, _m: _key(session, kv, True))
    keys.connect("key-released", lambda _c, kv, _k, _m: _key(session, kv, False))
    win.add_controller(keys)


def _move(session: Any, pos: tuple[float, float] | None) -> None:
    if pos is not None:
        try:
            session.move(*pos)
        except UseComputerError:
            pass


def _key(session: Any, keyval: int, pressed: bool) -> bool:
    if keyval in (0xFFC8,):  # F11 stays with the viewer window
        return False
    try:
        session.key(keyval, pressed)
    except UseComputerError:
        return False
    return True
