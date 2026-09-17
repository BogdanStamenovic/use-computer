from __future__ import annotations

import numpy as np

from use_computer.locate import locate_inner_rect


def _scene(actor_w: int, actor_h: int, inner: tuple[int, int, int, int], bg: int = 40,
           noise: bool = True) -> np.ndarray:
    """Actor crop (+1px ring) with a soft shadow and a light window, like Adwaita."""
    rng = np.random.default_rng(0)
    img = np.full((actor_h + 2, actor_w + 2), bg, dtype=np.float64)
    if noise:
        img += rng.normal(0, 3, img.shape)
    dx, dy, w, h = inner
    yy, xx = np.mgrid[0: actor_h + 2, 0: actor_w + 2]
    # distance outside the window rect, shadow offset downward like Adwaita
    ox = np.maximum(np.maximum(dx + 1 - xx, xx - (dx + 1 + w)), 0)
    oy = np.maximum(np.maximum(dy + 1 + 3 - yy, yy - (dy + 1 + h + 3)), 0)
    dist = np.hypot(ox, oy)
    img -= 25 * np.exp(-dist / 5.0)
    img[dy + 1: dy + 1 + h, dx + 1: dx + 1 + w] = 240
    img[dy + 1: dy + 1 + h, dx + 1: dx + 1 + w] -= rng.normal(0, 2, (h, w)) if noise else 0
    return np.clip(img, 0, 255).astype(np.int32)


def test_finds_asymmetric_adwaita_shadow() -> None:
    # measured on GNOME 50 / GTK4: 628x429 actor for a 600x400 window at (+14, +12)
    crop = _scene(628, 429, (14, 12, 600, 400))
    p = locate_inner_rect(crop, 600, 400)
    assert p is not None
    assert (p.dx, p.dy) == (14, 12)
    assert p.margin > 2


def test_exact_size_needs_no_search() -> None:
    p = locate_inner_rect(np.zeros((402, 802), dtype=np.int32), 800, 400)
    assert p is not None and (p.dx, p.dy) == (0, 0)


def test_rejects_implausible_slack() -> None:
    assert locate_inner_rect(np.zeros((1002, 1002), dtype=np.int32), 300, 300) is None
    assert locate_inner_rect(np.zeros((102, 102), dtype=np.int32), 300, 300) is None


def test_dark_window_on_light_background() -> None:
    crop = _scene(660, 470, (30, 20, 600, 420), bg=220)
    crop = 255 - crop
    p = locate_inner_rect(crop, 600, 420)
    assert p is not None and (p.dx, p.dy) == (30, 20)
