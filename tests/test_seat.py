from pathlib import Path

from use_computer import seat


def test_caps_multiword(tmp_path: Path) -> None:
    caps = tmp_path / "device" / "capabilities"
    caps.mkdir(parents=True)
    # bit 64 (second word) and bit 30 (KEY_A) set; sysfs prints most significant first
    (caps / "key").write_text("1 40000000\n")
    assert seat._caps(tmp_path, "key") == (1 << 64) | (1 << 30)
    assert seat._caps(tmp_path, "rel") == 0


def test_fit_size_caps_4k() -> None:
    assert seat._fit_size("3840x2160", 1920) == "1920x1080"
    assert seat._fit_size("1366x768", 1920) == "1366x768"
    assert seat._fit_size("garbage", 1920) == seat.vd.DEFAULT_SIZE
