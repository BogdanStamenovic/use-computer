"""Coordinate spaces.

- stream: Mutter's logical coordinates for the recorded monitor. Pointer motion
  is sent in this space.
- frame: pixels of the captured PipeWire buffer (differs from stream under
  HiDPI scaling).
- shot: pixels of the downscaled screenshot the agent sees. Every coordinate the
  agent passes in is in this space unless it asks for "screen".
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_W = 1280
DEFAULT_MAX_H = 800


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def as_list(self) -> list[int]:
        return [round(self.x), round(self.y), round(self.w), round(self.h)]


@dataclass(frozen=True)
class Screen:
    stream_w: int
    stream_h: int
    frame_w: int
    frame_h: int
    max_w: int = DEFAULT_MAX_W
    max_h: int = DEFAULT_MAX_H

    @property
    def shot_scale(self) -> float:
        """shot pixels per stream unit."""
        return min(1.0, self.max_w / self.stream_w, self.max_h / self.stream_h)

    @property
    def shot_size(self) -> tuple[int, int]:
        s = self.shot_scale
        return (round(self.stream_w * s), round(self.stream_h * s))

    @property
    def frame_scale(self) -> float:
        """frame pixels per stream unit."""
        return self.frame_w / self.stream_w

    def shot_to_stream(self, x: float, y: float) -> tuple[float, float]:
        s = self.shot_scale
        return (self.clamp_x(x / s), self.clamp_y(y / s))

    def stream_to_shot(self, x: float, y: float) -> tuple[float, float]:
        s = self.shot_scale
        return (x * s, y * s)

    def rect_stream_to_shot(self, r: Rect) -> Rect:
        s = self.shot_scale
        return Rect(r.x * s, r.y * s, r.w * s, r.h * s)

    def rect_shot_to_stream(self, r: Rect) -> Rect:
        s = self.shot_scale
        return Rect(r.x / s, r.y / s, r.w / s, r.h / s)

    def rect_stream_to_frame(self, r: Rect) -> tuple[int, int, int, int]:
        f = self.frame_scale
        x0 = max(0, min(self.frame_w, round(r.x * f)))
        y0 = max(0, min(self.frame_h, round(r.y * f)))
        x1 = max(0, min(self.frame_w, round((r.x + r.w) * f)))
        y1 = max(0, min(self.frame_h, round((r.y + r.h) * f)))
        return (x0, y0, x1, y1)

    def clamp_x(self, x: float) -> float:
        return max(0.0, min(self.stream_w - 1.0, x))

    def clamp_y(self, y: float) -> float:
        return max(0.0, min(self.stream_h - 1.0, y))

    def to_stream(self, x: float, y: float, space: str) -> tuple[float, float]:
        if space == "screen":
            return (self.clamp_x(x), self.clamp_y(y))
        return self.shot_to_stream(x, y)
