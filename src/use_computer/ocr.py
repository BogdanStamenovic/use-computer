"""OCR through the tesseract CLI, returning lines with boxes."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from dataclasses import dataclass

from PIL import Image

from . import UseComputerError
from .geometry import Rect


@dataclass(frozen=True)
class Word:
    text: str
    conf: float
    rect: Rect  # in the coordinate space of the image passed to ocr()


@dataclass(frozen=True)
class Line:
    text: str
    words: tuple[Word, ...]

    @property
    def rect(self) -> Rect:
        return _union([w.rect for w in self.words])

    @property
    def conf(self) -> float:
        return sum(w.conf for w in self.words) / len(self.words)


def default_lang() -> str:
    return os.environ.get("USE_COMPUTER_OCR_LANG", "eng")


def ocr(image: Image.Image, lang: str | None = None, upscale: float | None = None) -> list[Line]:
    """Screen text is far smaller than tesseract wants (~30px capitals). Measured on a
    1920x1080 GNOME screen: native scale misreads most UI labels, 2x reads them all
    for +50% time (0.45s -> 0.68s), 3x adds nothing but time."""
    if shutil.which("tesseract") is None:
        raise UseComputerError("tesseract is not installed (pacman -S tesseract tesseract-data-eng)")
    lang = lang or default_lang()
    if upscale is None:
        upscale = 3.0 if max(image.size) <= 600 else 2.0 if max(image.size) <= 2600 else 1.0
    img = image.convert("L")
    if upscale != 1.0:
        img = img.resize((round(img.width * upscale), round(img.height * upscale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", lang, "--psm", "11", "tsv"],
            input=buf.getvalue(), capture_output=True, timeout=120, check=False,
            env={**os.environ, "OMP_THREAD_LIMIT": os.environ.get("OMP_THREAD_LIMIT", "4")},
        )
    except subprocess.TimeoutExpired as exc:
        raise UseComputerError("tesseract timed out") from exc
    if proc.returncode != 0:
        raise UseComputerError(f"tesseract failed: {proc.stderr.decode(errors='replace').strip()}")
    return _parse_tsv(proc.stdout.decode("utf-8", errors="replace"), 1.0 / upscale)


def _parse_tsv(tsv: str, scale: float) -> list[Line]:
    groups: dict[tuple[str, str, str, str], list[Word]] = {}
    rows = tsv.splitlines()
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if not text or conf < 0:
            continue
        left, top, width, height = (int(c) for c in cols[6:10])
        word = Word(text, conf, Rect(left * scale, top * scale, width * scale, height * scale))
        groups.setdefault((cols[1], cols[2], cols[3], cols[4]), []).append(word)
    lines = [Line(" ".join(w.text for w in ws), tuple(ws)) for ws in groups.values()]
    lines.sort(key=lambda ln: (round(ln.rect.y / 8), ln.rect.x))
    return lines


def find_text(lines: list[Line], query: str) -> list[tuple[str, Rect, float]]:
    """Case-insensitive substring search; returns (line text, box of matched words, confidence)."""
    q = " ".join(query.casefold().split())
    if not q:
        return []
    hits = []
    for line in lines:
        hay = line.text.casefold()
        start = hay.find(q)
        while start != -1:
            end = start + len(q)
            covered, pos = [], 0
            for w in line.words:
                w_start, w_end = pos, pos + len(w.text)
                if w_end > start and w_start < end:
                    covered.append(w)
                pos = w_end + 1
            if covered:
                hits.append((line.text, _union([w.rect for w in covered]),
                             sum(w.conf for w in covered) / len(covered)))
            start = hay.find(q, start + 1)
    return hits


def _union(rects: list[Rect]) -> Rect:
    x0 = min(r.x for r in rects)
    y0 = min(r.y for r in rects)
    x1 = max(r.x + r.w for r in rects)
    y1 = max(r.y + r.h for r in rects)
    return Rect(x0, y0, x1 - x0, y1 - y0)
