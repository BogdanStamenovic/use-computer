from __future__ import annotations

from use_computer.geometry import Rect, Screen


def test_1080p_maps_to_1280x720() -> None:
    s = Screen(1920, 1080, 1920, 1080)
    assert s.shot_size == (1280, 720)
    assert s.shot_to_stream(640, 360) == (960.0, 540.0)
    assert s.stream_to_shot(960, 540) == (640.0, 360.0)


def test_small_screen_is_not_upscaled() -> None:
    s = Screen(1024, 768, 1024, 768)
    assert s.shot_scale == 1.0


def test_hidpi_frame_rect() -> None:
    s = Screen(1440, 900, 2880, 1800)
    assert s.frame_scale == 2.0
    assert s.rect_stream_to_frame(Rect(10, 20, 100, 50)) == (20, 40, 220, 140)


def test_clamps_to_screen() -> None:
    s = Screen(1920, 1080, 1920, 1080)
    assert s.to_stream(-5, 99999, "screen") == (0.0, 1079.0)
