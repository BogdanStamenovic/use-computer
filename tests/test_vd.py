"""Virtual desktop bookkeeping and, above all, the parts that kill or delete things."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from use_computer import vd


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _desktop(name: str = "d", pgid: int = 999999, runtime: str = "/nonexistent") -> vd.Desktop:
    return vd.Desktop(name=name, pgid=pgid, runtime=runtime,
                      bus="unix:path=/nonexistent/bus,guid=abc", wayland=f"uc-vd-{name}",
                      size="1920x1080", started=0.0)


def test_env_points_a_client_at_the_desktop() -> None:
    env = _desktop().env
    assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=")
    assert env["WAYLAND_DISPLAY"] == "uc-vd-d"
    assert env["USE_COMPUTER_DESKTOP"] == "d"


def test_dead_pgid_is_not_alive() -> None:
    assert not _desktop(pgid=999999).alive


def test_state_round_trip_and_listing() -> None:
    d = _desktop("alpha")
    (vd.state_dir() / "alpha.json").write_text(json.dumps(d.__dict__))
    assert vd.load("alpha") == d
    assert [x.name for x in vd.list_all()] == ["alpha"]


def test_corrupt_state_is_ignored_not_raised() -> None:
    (vd.state_dir() / "broken.json").write_text("{not json")
    assert vd.load("broken") is None
    assert vd.list_all() == []


@pytest.mark.parametrize("name", ["real", "host", "REAL", "none", "false"])
def test_real_targets_resolve_to_the_real_desktop(name: str) -> None:
    assert vd.resolve(name) is None


def test_resolve_without_start_never_spawns_a_shell() -> None:
    # Nothing is running and start=False, so this must answer None rather than
    # booting a GNOME Shell just to satisfy a read-only command.
    assert vd.resolve("ghost", start=False) is None


def test_apply_env_bus_only_leaves_wayland_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    vd.apply_env(_desktop(), bus_only=True)
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/nonexistent/bus")
    # The viewer's own window must stay on the real session.
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-0"
    assert os.environ["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_apply_env_full_switches_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    vd.apply_env(_desktop())
    assert os.environ["WAYLAND_DISPLAY"] == "uc-vd-d"


def test_teardown_refuses_directories_it_did_not_create(tmp_path: Path) -> None:
    # The guard that stops a bad state file from deleting, say, /home/bodas.
    precious = tmp_path / "definitely-not-ours"
    precious.mkdir()
    (precious / "keep").write_text("x")
    vd._teardown_runtime(str(precious))
    assert (precious / "keep").exists()


def test_teardown_removes_its_own_runtime_dir(tmp_path: Path) -> None:
    rt = tmp_path / "use-computer-vd-1000-x"
    (rt / "sub").mkdir(parents=True)
    vd._teardown_runtime(str(rt))
    assert not rt.exists()


def test_killpg_refuses_to_kill_our_own_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    vd._killpg(0)                 # would signal our own group
    vd._killpg(1)
    vd._killpg(os.getpgrp())      # would kill this test run
    assert sent == []
    vd._killpg(424242)
    assert sent == [(424242, 15)]


def test_stop_on_unknown_desktop_is_quiet_or_loud_on_request() -> None:
    assert "no virtual desktop" in str(vd.stop("nope", quiet=True)["done"])
    with pytest.raises(Exception):
        vd.stop("nope")
