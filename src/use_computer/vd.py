"""Virtual desktops: isolated headless GNOME Shells an agent can drive alone.

Why: driving the user's real screen means fighting them for the pointer, a
screen-sharing indicator in their top bar, and no way to run unattended. A virtual
desktop is a second GNOME Shell with its own session bus, its own accessibility bus
and its own virtual monitor. It is a full desktop -- their extensions, their dock,
real apps -- that simply nobody is looking at until they ask to watch it.

Targeting needs no new plumbing: `daemon.socket_path()` already hashes
DBUS_SESSION_BUS_ADDRESS into the socket name, so a client whose environment points
at a virtual desktop's bus automatically gets its own daemon, its own Mutter session
and its own control lease. Everything downstream (controller, a11y, OCR, clipboard)
is unchanged.

Teardown is the part with teeth. The shell spawns xdg-document-portal and gvfsd,
which leave FUSE mounts inside the runtime dir that outlive it and make `rm -rf`
fail. And the shell must be killed by process group: pattern-killing
`at-spi2-registryd` by name would take down the real desktop's accessibility bus.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import UseComputerError

DEFAULT_NAME = os.environ.get("USE_COMPUTER_DESKTOP_DEFAULT", "agent")
DEFAULT_SIZE = os.environ.get("USE_COMPUTER_VD_SIZE", "1920x1080")
REAL_NAMES = {"real", "host", "none", "0", "false"}
START_TIMEOUT = float(os.environ.get("USE_COMPUTER_VD_TIMEOUT", "40"))


@dataclass
class Desktop:
    name: str
    pgid: int
    runtime: str
    bus: str
    wayland: str
    size: str
    started: float

    @property
    def env(self) -> dict[str, str]:
        """Environment that points a client at this desktop."""
        return {"DBUS_SESSION_BUS_ADDRESS": self.bus, "XDG_RUNTIME_DIR": self.runtime,
                "WAYLAND_DISPLAY": self.wayland, "GDK_BACKEND": "wayland",
                "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME",
                "USE_COMPUTER_DESKTOP": self.name}

    @property
    def alive(self) -> bool:
        if self.pgid <= 1:
            return False
        try:
            os.killpg(self.pgid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return Path(self.bus.partition("unix:path=")[2].partition(",")[0] or "/nonexistent").exists()

    @property
    def age(self) -> float:
        return time.time() - self.started


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    d = Path(base) / "use-computer" / "desktops"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _state_file(name: str) -> Path:
    return state_dir() / f"{name}.json"


def _runtime_dir(name: str) -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp")) / f"use-computer-vd-{os.getuid()}-{name}"


def load(name: str) -> Desktop | None:
    try:
        return Desktop(**json.loads(_state_file(name).read_text()))
    except (OSError, ValueError, TypeError):
        return None


def list_all() -> list[Desktop]:
    out = []
    for f in sorted(state_dir().glob("*.json")):
        d = load(f.stem)
        if d is not None:
            out.append(d)
    return out


def _shell_script() -> Path:
    p = Path(__file__).with_name("vd_shell.sh")
    if not p.exists():
        raise UseComputerError(f"virtual desktop supervisor missing at {p}")
    return p


def start(name: str = DEFAULT_NAME, size: str = DEFAULT_SIZE,
          timeout: float = START_TIMEOUT) -> Desktop:
    """Start a virtual desktop and wait until its session bus is up."""
    existing = load(name)
    if existing is not None and existing.alive:
        return existing
    if existing is not None:
        stop(name, quiet=True)
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.environ.get("XDG_RUNTIME_DIR"):
        raise UseComputerError("no session environment; run this from a logged-in user session")

    rt = _runtime_dir(name)
    envfile = state_dir() / f"{name}.env"
    envfile.unlink(missing_ok=True)
    log = state_dir() / f"{name}.log"
    env = {**os.environ, "UC_VD_ENVFILE": str(envfile), "UC_VD_RUNTIME": str(rt),
           "UC_VD_SIZE": size, "UC_VD_DISPLAY": f"uc-vd-{name}"}
    with open(log, "wb") as logf:
        proc = subprocess.Popen(["bash", str(_shell_script())], env=env, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf)

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if envfile.exists():
            break
        if proc.poll() is not None:
            raise UseComputerError(
                f"virtual desktop {name!r} exited immediately (rc={proc.returncode}); see {log}")
        time.sleep(0.1)
    else:
        _killpg(proc.pid)
        raise UseComputerError(f"virtual desktop {name!r} did not come up in {timeout:g}s; see {log}")

    values = dict(ln.split("=", 1) for ln in envfile.read_text().splitlines() if "=" in ln)
    d = Desktop(name=name, pgid=proc.pid, runtime=values["XDG_RUNTIME_DIR"],
                bus=values["DBUS_SESSION_BUS_ADDRESS"], wayland=values["WAYLAND_DISPLAY"],
                size=size, started=time.time())
    _state_file(name).write_text(json.dumps(asdict(d), indent=2))
    _wait_for_mutter(d, end)
    return d


SETTLE = float(os.environ.get("USE_COMPUTER_VD_SETTLE", "2.0"))


def _wait_for_mutter(d: Desktop, end: float) -> None:
    """The bus exists before Mutter claims its names; clients must not race that.

    Mutter answering is necessary but not sufficient: GNOME Shell's own UI (the
    Activities overview that `open_app` drives) is still coming up for a moment
    after that, and an `open_app` issued into the gap silently does nothing. There
    is no crisp readiness signal for the overview, so this settles for a short
    fixed wait -- a heuristic, but it turns a confusing no-op into a working call.
    """
    while time.monotonic() < end:
        probe = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.ScreenCast",
             "--object-path", "/org/gnome/Mutter/ScreenCast", "--method",
             "org.freedesktop.DBus.Properties.Get", "org.gnome.Mutter.ScreenCast", "Version"],
            env={**os.environ, **d.env}, capture_output=True, text=True, check=False)
        if probe.returncode == 0:
            time.sleep(SETTLE)
            return
        time.sleep(0.2)
    raise UseComputerError(f"virtual desktop {d.name!r} started but Mutter never appeared on its bus")


def _pgid_alive(pgid: int) -> bool:
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _killpg(pgid: int, sig: int = signal.SIGTERM) -> None:
    # Guard rails: killpg(0) would kill our own group, and so would our real pgid.
    if pgid <= 1 or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _teardown_runtime(runtime: str) -> list[str]:
    """Unmount the FUSE mounts the shell's portals leave behind, then remove the dir.

    xdg-document-portal (doc/) and gvfsd (gvfs/) outlive the shell and keep the
    runtime dir busy; without unmounting them the directory can never be removed.
    """
    rt = Path(runtime)
    notes: list[str] = []
    if not rt.is_dir() or "use-computer-vd-" not in rt.name:
        return notes  # never touch a directory we did not create
    for sub in ("doc", "gvfs"):
        m = rt / sub
        if not m.is_dir():
            continue
        if subprocess.run(["mountpoint", "-q", str(m)], capture_output=True,
                          check=False).returncode != 0:
            continue
        for tool in ("fusermount3", "fusermount"):
            if subprocess.run([tool, "-u", str(m)], capture_output=True,
                              check=False).returncode == 0:
                notes.append(f"unmounted {m}")
                break
    import shutil
    shutil.rmtree(rt, ignore_errors=True)
    if rt.exists():
        notes.append(f"could not fully remove {rt}")
    return notes


def stop(name: str = DEFAULT_NAME, quiet: bool = False) -> dict[str, object]:
    d = load(name)
    if d is None:
        if quiet:
            return {"done": f"no virtual desktop {name!r}"}
        raise UseComputerError(f"no virtual desktop named {name!r}")
    _killpg(d.pgid, signal.SIGTERM)
    # Wait on the process group, not on `alive`: the session bus socket disappears
    # as soon as dbus-daemon goes, which would make the desktop look dead while
    # gnome-shell is still running and never get the SIGKILL it needs.
    end = time.monotonic() + 8
    while time.monotonic() < end and _pgid_alive(d.pgid):
        time.sleep(0.1)
    if _pgid_alive(d.pgid):
        _killpg(d.pgid, signal.SIGKILL)
        end = time.monotonic() + 5
        while time.monotonic() < end and _pgid_alive(d.pgid):
            time.sleep(0.1)
    notes = _teardown_runtime(d.runtime)
    _state_file(name).unlink(missing_ok=True)
    (state_dir() / f"{name}.env").unlink(missing_ok=True)
    return {"done": f"stopped virtual desktop {name!r}", "notes": notes}


def ensure(name: str = DEFAULT_NAME, size: str = DEFAULT_SIZE) -> Desktop:
    d = load(name)
    if d is not None and d.alive:
        return d
    return start(name, size)


def resolve(target: str | None = None, start: bool = True) -> Desktop | None:
    """Pick the desktop a client should talk to. None means the real one.

    `start=False` attaches to a virtual desktop only if it is already running, so
    read-only commands like `status` never spin a GNOME Shell up just to answer.
    """
    name = target if target is not None else os.environ.get("USE_COMPUTER_DESKTOP", DEFAULT_NAME)
    name = (name or "").strip()
    if not name or name.casefold() in REAL_NAMES:
        return None
    if not start:
        d = load(name)
        return d if d is not None and d.alive else None
    return ensure(name)


def apply_env(d: Desktop | None, bus_only: bool = False) -> None:
    """Point this process (and anything it spawns, incl. the daemon) at `d`.

    `bus_only` redirects D-Bus but leaves Wayland and the runtime dir alone: that is
    what the viewer needs, so it talks to the virtual desktop while its own window
    still opens on the real session.
    """
    if d is None:
        os.environ["USE_COMPUTER_DESKTOP"] = "real"
        return
    if bus_only:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = d.bus
        os.environ["USE_COMPUTER_DESKTOP"] = d.name
        return
    os.environ.update(d.env)


def describe(d: Desktop | None) -> str:
    if d is None:
        return "real desktop (this session's screen)"
    return f"virtual desktop {d.name!r} ({d.size}, up {d.age / 60:.0f}m)"
