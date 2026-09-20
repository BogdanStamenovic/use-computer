"""Environment checks: what works on this machine and what to fix."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _check(label: str, fn: Callable[[], tuple[str, str]]) -> str:
    try:
        status, detail = fn()
    except Exception as exc:  # doctor must never crash
        status, detail = FAIL, f"{type(exc).__name__}: {exc}"
    print(f"[{status:>4}] {label}: {detail}")
    return status


def _session() -> tuple[str, str]:
    t = os.environ.get("XDG_SESSION_TYPE", "?")
    d = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    if t == "wayland" and "GNOME" in d:
        return OK, f"{d} on {t}"
    return FAIL, f"{d} on {t}; only GNOME Shell on Wayland is supported"


def _dbus_name(name: str) -> Callable[[], tuple[str, str]]:
    def check() -> tuple[str, str]:
        from gi.repository import Gio
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                          "org.freedesktop.DBus", "NameHasOwner",
                          __import__("gi").repository.GLib.Variant("(s)", (name,)),
                          None, 0, 2000, None)
        return (OK, "available") if r.unpack()[0] else (FAIL, "not on the session bus")
    return check


def _gst() -> tuple[str, str]:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    if Gst.ElementFactory.find("pipewiresrc") is None:
        return FAIL, "GStreamer pipewiresrc missing (pacman -S gst-plugin-pipewire)"
    return OK, "pipewiresrc present"


def _a11y() -> tuple[str, str]:
    out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface",
                          "toolkit-accessibility"], capture_output=True, text=True)
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    desk = Atspi.get_desktop(0)
    apps = [desk.get_child_at_index(i).get_name() for i in range(desk.get_child_count())]
    enabled = out.stdout.strip() == "true"
    detail = f"toolkit-accessibility={out.stdout.strip()}, {len(apps)} apps registered"
    if not apps:
        return FAIL, detail + " (registry unreachable?)"
    return (OK if enabled else WARN), detail


def _tesseract() -> tuple[str, str]:
    if not shutil.which("tesseract"):
        return FAIL, "not installed (pacman -S tesseract tesseract-data-eng)"
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    names = [ln for ln in langs.stdout.splitlines()[1:] if ln.strip()]
    return (OK if "eng" in names else WARN), "languages: " + ", ".join(names)


def _tool(name: str, why: str) -> Callable[[], tuple[str, str]]:
    return lambda: (OK, shutil.which(name) or "") if shutil.which(name) else (WARN, f"missing: {why}")


def _audio() -> tuple[str, str]:
    from .audio import model_status
    st = model_status()
    if not st["installed"]:
        return WARN, "not installed (optional): use-computer audio setup"
    if not st["downloaded"]:
        return WARN, f"faster-whisper installed, model {st['model']} not downloaded: " \
                     "use-computer audio setup --no-install"
    return OK, f"model {st['model']} ready"


def _mcp() -> tuple[str, str]:
    from .register import NAME, config_path
    p = config_path()
    if not p.exists():
        return WARN, f"{p} does not exist"
    entry = json.loads(p.read_text()).get("mcpServers", {}).get(NAME)
    if not entry:
        return WARN, "not registered: use-computer mcp-register"
    cmd = entry.get("command", "")
    if not os.path.exists(cmd):
        return FAIL, f"registered command does not exist: {cmd}"
    return OK, cmd


def _daemon() -> tuple[str, str]:
    from .client import Client
    try:
        c = Client("doctor", autostart=False)
        st = c.call("status")
        c.close()
        return OK, json.dumps(st)
    except Exception:
        return OK, "not running (starts on first use)"


def _virtual_desktops() -> tuple[str, str]:
    """Can we start an isolated GNOME Shell, and are any running?"""
    from . import vd
    missing = [t for t in ("gnome-shell", "dbus-run-session") if not shutil.which(t)]
    helpers = [n for n in ("at-spi-bus-launcher", "at-spi2-registryd")
               if not any(os.path.exists(f"{d}/{n}") for d in
                          ("/usr/lib", "/usr/libexec", "/usr/lib/at-spi2-core"))]
    if missing or helpers:
        return FAIL, "cannot start one, missing: " + ", ".join(missing + helpers)
    running = vd.list_all()
    live = [d for d in running if d.alive]
    detail = f"supported; {len(live)} running"
    if live:
        detail += " (" + ", ".join(f"{d.name} {d.size}" for d in live) + ")"
    stale = [d.name for d in running if not d.alive]
    if stale:
        return WARN, detail + f"; stale state for {', '.join(stale)} (use-computer vd stop --all)"
    return OK, detail


def _target() -> tuple[str, str]:
    from . import vd
    name = os.environ.get("USE_COMPUTER_DESKTOP", vd.DEFAULT_NAME)
    if name.casefold() in vd.REAL_NAMES:
        return WARN, "real desktop — actions will move the user's own pointer"
    d = vd.load(name)
    state = "running" if (d is not None and d.alive) else "starts on first use"
    return OK, f"virtual desktop {name!r} ({state})"


def _watch_tty() -> tuple[str, str]:
    if not shutil.which("chafa"):
        return WARN, "chafa missing: `use-computer watch --tty` needs it (pacman -S chafa)"
    out = subprocess.run(["chafa", "--version"], capture_output=True, text=True)
    return OK, out.stdout.splitlines()[0] if out.stdout else "present"


def run() -> int:
    print(f"python {sys.version.split()[0]} at {sys.executable}")
    results = [
        _check("desktop session", _session),
        _check("target", _target),
        _check("virtual desktops", _virtual_desktops),
        _check("watch --tty", _watch_tty),
        _check("Mutter RemoteDesktop", _dbus_name("org.gnome.Mutter.RemoteDesktop")),
        _check("Mutter ScreenCast", _dbus_name("org.gnome.Mutter.ScreenCast")),
        _check("GStreamer PipeWire", _gst),
        _check("accessibility", _a11y),
        _check("tesseract OCR", _tesseract),
        _check("wl-copy", _tool("wl-copy", "clipboard is lost when a session ends")),
        _check("pw-record", _tool("pw-record", "listen needs PipeWire tools")),
        _check("audio (whisper)", _audio),
        _check("MCP registration", _mcp),
        _check("daemon", _daemon),
    ]
    return 1 if FAIL in results else 0
