"""Unlocking the user's real login keyring inside a virtual desktop.

Why: apps keep their logins behind the keyring. Chrome encrypts its cookie store with
a key it fetches from the Secret Service ("Chrome Safe Storage"), so a desktop with no
unlocked keyring shows every account as logged out. A virtual desktop has its own
session bus, so the keyring daemon of any other session is invisible to it; vd_shell.sh
therefore starts a gnome-keyring-daemon on the desktop's own bus, locked, reading the
real ~/.local/share/keyrings.

Unlocking goes through gnome-keyring's UnlockWithMasterPassword D-Bus call (what
Seahorse uses), not `gnome-keyring-daemon --unlock`:
- `--start --unlock` is refused by the daemon ("incompatible options");
- `--replace --unlock` spawns a fresh daemon outside the desktop's process group,
  which would outlive `vd stop` and leave an unlocked keyring with no owner.
A wrong password returns org.gnome.keyring.Error.Denied and leaves the file untouched.

The password never touches disk and is never passed on a command line. It arrives
over HTTP on a single-use tokenized URL (agents must not type passwords, so the user
does), goes straight into the D-Bus call on a user-only socket, and is dropped.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import UseComputerError
from .vd import Desktop

BUS_NAME = "org.freedesktop.secrets"
SERVICE = "/org/freedesktop/secrets"
LOGIN = "/org/freedesktop/secrets/collection/login"
GUILT = "org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface"


def keyring_file() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "keyrings" / "login.keyring"


def _bus(d: Desktop):
    from gi.repository import Gio

    flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
             | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
    return Gio.DBusConnection.new_for_address_sync(d.bus, flags, None, None)


def _call(conn, path: str, iface: str, method: str, args, rtype: str | None):
    from gi.repository import Gio, GLib

    return conn.call_sync(BUS_NAME, path, iface, method, args,
                          GLib.VariantType(rtype) if rtype else None,
                          Gio.DBusCallFlags.NO_AUTO_START, 10000, None)


def status(d: Desktop) -> dict[str, object]:
    """{'running', 'locked'} for the login keyring on this desktop's bus."""
    from gi.repository import GLib

    try:
        conn = _bus(d)
        v = _call(conn, LOGIN, "org.freedesktop.DBus.Properties", "Get",
                  GLib.Variant("(ss)", ("org.freedesktop.Secret.Collection", "Locked")), "(v)")
    except GLib.Error as exc:
        if "ServiceUnknown" in str(exc) or "NameHasNoOwner" in str(exc):
            return {"running": False, "locked": None,
                    "hint": "no keyring daemon on this desktop; it predates keyring support, "
                            "restart it with `use-computer vd stop` and run again"}
        if "UnknownObject" in str(exc) or "NoSuchObject" in str(exc):
            return {"running": True, "locked": None, "hint": "no login keyring exists"}
        raise UseComputerError(f"keyring status failed: {exc.message}") from None
    return {"running": True, "locked": bool(v.unpack()[0])}


def unlock(d: Desktop, password: bytes) -> bool:
    """True if unlocked, False if the password was wrong. Other failures raise."""
    from gi.repository import GLib

    conn = _bus(d)
    _, session = _call(conn, SERVICE, "org.freedesktop.Secret.Service", "OpenSession",
                       GLib.Variant("(sv)", ("plain", GLib.Variant("s", ""))), "(vo)").unpack()
    try:
        _call(conn, SERVICE, GUILT, "UnlockWithMasterPassword",
              GLib.Variant("(o(oayays))", (LOGIN, (session, b"", password, "text/plain"))), None)
    except GLib.Error as exc:
        if "Denied" in str(exc):
            return False
        raise UseComputerError(f"keyring unlock failed: {exc.message}") from None
    finally:
        try:
            _call(conn, session, "org.freedesktop.Secret.Session", "Close", None, None)
        except GLib.Error:
            pass
    # Trust the property, not the call's success: the user is told "unlocked" only if
    # the collection actually says so.
    return status(d).get("locked") is False


def serve(d: Desktop, host: str | None = None, timeout: float = 600.0,
          qr_path: Path | None = None) -> bool:
    """Serve a single-use unlock page; True once the login keyring reads unlocked."""
    from . import secretpage

    if not keyring_file().exists():
        # Unlocking a missing login keyring makes gnome-keyring CREATE one with the
        # typed password -- never do that to someone's real keyring by accident.
        raise UseComputerError(f"no login keyring at {keyring_file()}; refusing to create one")
    st = status(d)
    if not st["running"] or st["locked"] is None:
        raise UseComputerError(str(st.get("hint")))
    if st["locked"] is False:
        print(f"login keyring on {d.name!r} is already unlocked", file=sys.stderr)
        return True

    def submit(secret: str, _flags: dict[str, bool]) -> tuple[bool, str]:
        if unlock(d, secret.encode()):
            return True, "Unlocked. You can close this page."
        return False, "Wrong password."

    page = secretpage.Page(
        title="Unlock login keyring", where=f"archserver - virtual desktop '{d.name}'",
        prompt="Your login password. It unlocks the keyring so apps on this desktop "
               "come up signed in. The agent never sees it.",
        on_submit=submit, placeholder="Login password")
    return secretpage.serve(page, host=host, timeout=timeout, qr_path=qr_path)
