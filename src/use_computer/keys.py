"""Key names, combos and text segmentation, expressed as X keysyms.

Mutter's NotifyKeyboardKeysym only produces a key press when the keysym exists in
the active keyboard layout; anything else is dropped silently. Text is therefore
split into runs that can be typed (ASCII, newline, tab) and runs that must be
pasted through the clipboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import UseComputerError

_NAMED: dict[str, int] = {
    "return": 0xFF0D, "enter": 0xFF0D, "kp_enter": 0xFF8D,
    "tab": 0xFF09, "iso_left_tab": 0xFE20,
    "escape": 0xFF1B, "esc": 0xFF1B,
    "backspace": 0xFF08,
    "delete": 0xFFFF, "del": 0xFFFF,
    "insert": 0xFF63,
    "home": 0xFF50, "end": 0xFF57,
    "page_up": 0xFF55, "pageup": 0xFF55, "prior": 0xFF55,
    "page_down": 0xFF56, "pagedown": 0xFF56, "next": 0xFF56,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "space": 0x20,
    "menu": 0xFF67,
    "print": 0xFF61,
    "caps_lock": 0xFFE5, "num_lock": 0xFF7F, "scroll_lock": 0xFF14,
    "pause": 0xFF13,
    "shift": 0xFFE1, "shift_l": 0xFFE1, "shift_r": 0xFFE2,
    "ctrl": 0xFFE3, "control": 0xFFE3, "control_l": 0xFFE3, "control_r": 0xFFE4,
    "alt": 0xFFE9, "alt_l": 0xFFE9, "alt_r": 0xFFEA, "altgr": 0xFE03,
    "super": 0xFFEB, "super_l": 0xFFEB, "super_r": 0xFFEC,
    "meta": 0xFFEB, "win": 0xFFEB, "windows": 0xFFEB, "cmd": 0xFFEB,
    "xf86audioraisevolume": 0x1008FF13, "xf86audiolowervolume": 0x1008FF11,
    "xf86audiomute": 0x1008FF12, "xf86audioplay": 0x1008FF14,
    "xf86audionext": 0x1008FF17, "xf86audioprev": 0x1008FF16,
}
for _i in range(1, 25):
    _NAMED[f"f{_i}"] = 0xFFBE + _i - 1

_PUNCT_NAMES: dict[str, str] = {
    "minus": "-", "plus": "+", "equal": "=", "comma": ",", "period": ".", "slash": "/",
    "backslash": "\\", "semicolon": ";", "apostrophe": "'", "grave": "`",
    "bracketleft": "[", "bracketright": "]", "question": "?", "exclam": "!",
}

MODIFIERS = {0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE9, 0xFFEA, 0xFE03, 0xFFEB, 0xFFEC}


def keysym(name: str) -> int:
    """Resolve one key name (xdotool/X11 style, case-insensitive) or single character."""
    if len(name) == 1:
        ks = char_keysym(name)
        if ks is None:
            raise UseComputerError(
                f"key {name!r} is not on a standard layout; use type for text like this"
            )
        return ks
    low = name.lower()
    if low in _NAMED:
        return _NAMED[low]
    if low in _PUNCT_NAMES:
        return ord(_PUNCT_NAMES[low])
    if low.startswith("0x"):
        try:
            return int(low, 16)
        except ValueError:
            pass
    raise UseComputerError(f"unknown key name {name!r}")


def parse_combo(combo: str) -> list[int]:
    """'ctrl+shift+t' -> keysyms in press order. A literal '+' key is written 'plus'."""
    parts = [p for p in combo.split("+") if p != ""]
    if combo.endswith("++") or combo == "+":
        parts.append("+")
    if not parts:
        raise UseComputerError(f"empty key combo {combo!r}")
    return [keysym(p) for p in parts]


def parse_keys(spec: str) -> list[list[int]]:
    """Space-separated combos: 'ctrl+a Delete Return'."""
    combos = [parse_combo(c) for c in spec.split()]
    if not combos:
        raise UseComputerError("no keys given")
    return combos


def char_keysym(ch: str) -> int | None:
    """Keysym for a character that every Latin layout can type, else None."""
    if ch == "\n":
        return _NAMED["return"]
    if ch == "\t":
        return _NAMED["tab"]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E:
        return cp
    return None


@dataclass(frozen=True)
class Segment:
    kind: str  # "keys" or "paste"
    text: str


def segment_text(text: str) -> list[Segment]:
    out: list[Segment] = []
    for ch in text:
        kind = "keys" if char_keysym(ch) is not None else "paste"
        if out and out[-1].kind == kind:
            out[-1] = Segment(kind, out[-1].text + ch)
        else:
            out.append(Segment(kind, ch))
    return out
