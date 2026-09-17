"""Find where a window's content rectangle sits inside its compositor actor.

On Wayland, apps don't know their own screen position, so AT-SPI extents are
only window-relative. GNOME Shell's accessibility tree does expose each window
actor's screen rectangle, but that rectangle includes client-side shadows whose
per-side sizes vary by toolkit and theme (Adwaita GTK4: 14 left/right, 12 top,
17 bottom). We know the content size from the app, so we search every possible
offset of a content-sized rectangle inside the actor and pick the one whose
perimeter has the strongest edges: shadows are smooth gradients, the window
border is a sharp step.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAX_SLACK = 160


@dataclass(frozen=True)
class Placement:
    dx: int
    dy: int
    score: float
    margin: float  # best score minus the best score at least 3px away; ~0 means ambiguous


def locate_inner_rect(gray: np.ndarray, inner_w: int, inner_h: int) -> Placement | None:
    """gray: actor crop with a 1px border of surrounding screen on every side.

    The border matters: without it an edge lying on the actor boundary would be
    compared against padding instead of real pixels.
    """
    H, W = gray.shape
    H -= 2
    W -= 2
    sx, sy = W - inner_w, H - inner_h
    if sx < 0 or sy < 0 or sx > MAX_SLACK or sy > MAX_SLACK:
        return None
    if sx == 0 and sy == 0:
        return Placement(0, 0, float("inf"), float("inf"))
    g = gray.astype(np.int32)
    # eh[y, x]: step between padded rows y and y+1 -> edge above actor row y is eh[y]
    eh = np.abs(np.diff(g, axis=0))[:, 1:-1]  # (H+1, W)
    ev = np.abs(np.diff(g, axis=1))[1:-1, :]  # (H, W+1)
    ch = np.zeros((H + 1, W + 1), dtype=np.int64)
    ch[:, 1:] = np.cumsum(eh, axis=1)
    cv = np.zeros((H + 1, W + 1), dtype=np.int64)
    cv[1:, :] = np.cumsum(ev, axis=0)

    dy = np.arange(sy + 1)[:, None]
    dx = np.arange(sx + 1)[None, :]
    top = ch[dy, dx + inner_w] - ch[dy, dx]
    bottom = ch[dy + inner_h, dx + inner_w] - ch[dy + inner_h, dx]
    left = cv[dy + inner_h, dx] - cv[dy, dx]
    right = cv[dy + inner_h, dx + inner_w] - cv[dy, dx + inner_w]
    score = (top + bottom) / (2 * inner_w) + (left + right) / (2 * inner_h)

    by, bx = np.unravel_index(int(np.argmax(score)), score.shape)
    best = float(score[by, bx])
    yy, xx = np.ogrid[: sy + 1, : sx + 1]
    far = (np.abs(yy - by) >= 3) | (np.abs(xx - bx) >= 3)
    runner_up = float(score[far].max()) if far.any() else 0.0
    return Placement(int(bx), int(by), best, best - runner_up)


def to_gray(rgb: np.ndarray) -> np.ndarray:
    return (rgb[..., 0] * 299 + rgb[..., 1] * 587 + rgb[..., 2] * 114) // 1000
