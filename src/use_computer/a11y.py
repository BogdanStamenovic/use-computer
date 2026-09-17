"""AT-SPI accessibility tree: snapshots with refs, element actions, window origins.

Coordinates: on Wayland most toolkits report WINDOW-relative extents correctly
and SCREEN extents as if the window sat at (0, 0). X11 (XWayland) apps report real
SCREEN extents. A frame whose SCREEN and WINDOW extents differ is treated as
X11-style; otherwise its screen origin is recovered from GNOME Shell's own window
actors (see locate.py).
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Atspi", "2.0")
import numpy as np
from gi.repository import Atspi, GLib
from PIL import Image

from . import UseComputerError
from .geometry import Rect, Screen
from .locate import locate_inner_rect, to_gray

S = Atspi.StateType
WIN = Atspi.CoordType.WINDOW
SCR = Atspi.CoordType.SCREEN

# Actions every GTK4 label exposes; they say nothing about interactivity.
_NOISE_ACTIONS = ("clipboard.", "selection.", "link.", "menu.popup")
_INTERACTIVE_ROLES = {
    "button", "push button", "toggle button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "combo box", "text", "entry", "password text",
    "spin button", "slider", "link", "page tab", "list item", "tree item", "switch",
    "menu", "toggle", "tool bar item", "search box", "tab", "option", "cell", "table cell",
}
_CLICK_ACTIONS = ("click", "press", "activate", "toggle", "jump", "open", "select", "default")
_SHOWN_STATES = {
    S.FOCUSED: "focused", S.CHECKED: "checked", S.SELECTED: "selected", S.PRESSED: "pressed",
    S.EXPANDED: "expanded", S.COLLAPSED: "collapsed", S.EDITABLE: "editable",
    S.INDETERMINATE: "mixed", S.REQUIRED: "required", S.INVALID_ENTRY: "invalid",
    S.READ_ONLY: "readonly", S.MODAL: "modal", S.ACTIVE: "active",
}

TOGGLE_ROLES = ("check box", "toggle button", "switch", "check menu item", "radio button",
                "radio menu item")

MAX_NODES = 4000
MAX_CHILDREN = 400
TIME_BUDGET = 4.0

FrameProvider = Callable[[], np.ndarray]


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except (GLib.Error, RuntimeError, TypeError, AttributeError):
        return default


def _action_names(acc: Atspi.Accessible) -> list[str]:
    ai = _safe(acc.get_action_iface)
    if ai is None:
        return []
    n = _safe(ai.get_n_actions, 0) or 0
    # Atspi.Action.get_name() returns "" on current libatspi; the deprecated
    # get_action_name() is the one that answers.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        names = [_safe(lambda i=i: ai.get_action_name(i), "") or "" for i in range(n)]
    return [name for name in names if name]


@dataclass
class Node:
    ref: str
    acc: Atspi.Accessible
    role: str
    name: str
    depth: int
    states: list[str]
    actions: list[str]
    value: str | None
    rect: Rect | None  # stream coordinates, None if unknown
    interactive: bool
    showing: bool
    children: list[Node] = field(default_factory=list)


@dataclass
class FramePos:
    mode: str  # "offset" (window-relative + origin) or "screen" (trust SCREEN extents)
    origin: tuple[float, float] | None
    confidence: str  # "exact", "matched", "ambiguous", "unresolved"


class A11y:
    def __init__(self) -> None:
        self._refs: dict[str, Atspi.Accessible] = {}
        self._ids: dict[int, str] = {}
        self._next = 1
        self._pos_cache: dict[int, tuple[tuple, FramePos]] = {}

    # ---- refs ----------------------------------------------------------------

    def ref_for(self, acc: Atspi.Accessible) -> str:
        key = hash(acc)
        ref = self._ids.get(key)
        if ref is None or self._refs.get(ref) is not acc:
            if len(self._refs) > 50000:
                self._refs.clear()
                self._ids.clear()
            ref = f"ref_{self._next}"
            self._next += 1
            self._refs[ref] = acc
            self._ids[key] = ref
        return ref

    def resolve(self, ref: str) -> Atspi.Accessible:
        acc = self._refs.get(ref)
        if acc is None:
            raise UseComputerError(f"unknown ref {ref!r}; take a fresh read_screen/find")
        if _safe(acc.get_role_name) is None:
            raise UseComputerError(f"{ref} no longer exists (the UI changed); re-read the screen")
        return acc

    # ---- discovery -----------------------------------------------------------

    def apps(self) -> list[Atspi.Accessible]:
        desk = Atspi.get_desktop(0)
        n = _safe(desk.get_child_count, 0) or 0
        return [a for i in range(n) if (a := _safe(lambda i=i: desk.get_child_at_index(i)))]

    def frames(self) -> list[tuple[Atspi.Accessible, Atspi.Accessible]]:
        out = []
        for app in self.apps():
            if _safe(app.get_name) == "gnome-shell":
                continue
            n = _safe(app.get_child_count, 0) or 0
            for i in range(min(n, 50)):
                f = _safe(lambda i=i, app=app: app.get_child_at_index(i))
                if f is not None:
                    out.append((app, f))
        return out

    def windows(self) -> list[dict[str, Any]]:
        out = []
        for app, f in self.frames():
            st = _safe(f.get_state_set)
            out.append({
                "ref": self.ref_for(f),
                "app": _safe(app.get_name, ""),
                "title": _safe(f.get_name, ""),
                "role": _safe(f.get_role_name, ""),
                "active": bool(st and st.contains(S.ACTIVE)),
                "showing": bool(st and st.contains(S.SHOWING)),
                "pid": _safe(app.get_process_id),
            })
        return out

    def active_frame(self) -> tuple[Atspi.Accessible, Atspi.Accessible] | None:
        for app, f in self.frames():
            st = _safe(f.get_state_set)
            if st and st.contains(S.ACTIVE):
                return app, f
        return None

    def frame_of(self, acc: Atspi.Accessible) -> Atspi.Accessible | None:
        node = acc
        for _ in range(200):
            parent = _safe(node.get_parent)
            if parent is None:
                return None
            if _safe(parent.get_role_name) == "application":
                return node
            node = parent
        return None

    # ---- positions -----------------------------------------------------------

    def _shell_actors(self) -> list[Rect]:
        shell = next((a for a in self.apps() if _safe(a.get_name) == "gnome-shell"), None)
        if shell is None:
            return []
        rects: list[Rect] = []

        def walk(n: Atspi.Accessible, depth: int) -> None:
            if depth > 7:
                return
            name = _safe(n.get_name, "") or ""
            if name.endswith(" window") or name in ("Wayland window", "X11 window"):
                st = _safe(n.get_state_set)
                comp = _safe(n.get_component_iface)
                e = comp and _safe(lambda: comp.get_extents(SCR))
                if st and st.contains(S.SHOWING) and e and e.width > 0 and e.height > 0:
                    rects.append(Rect(e.x, e.y, e.width, e.height))
                return
            for i in range(min(_safe(n.get_child_count, 0) or 0, 200)):
                c = _safe(lambda i=i: n.get_child_at_index(i))
                if c is not None:
                    walk(c, depth + 1)

        walk(shell, 0)
        return rects

    def frame_position(self, frame: Atspi.Accessible, screen: Screen,
                       get_frame: FrameProvider) -> FramePos:
        comp = _safe(frame.get_component_iface)
        if comp is None:
            return FramePos("offset", None, "unresolved")
        we = _safe(lambda: comp.get_extents(WIN))
        se = _safe(lambda: comp.get_extents(SCR))
        if we is None or se is None:
            return FramePos("offset", None, "unresolved")
        if (se.x, se.y) != (we.x, we.y):
            return FramePos("screen", None, "exact")
        fw, fh = we.width, we.height
        actors = [a for a in self._shell_actors()
                  if 0 <= a.w - fw <= 160 and 0 <= a.h - fh <= 160]
        key = (tuple((a.x, a.y, a.w, a.h) for a in actors), fw, fh)
        cached = self._pos_cache.get(hash(frame))
        if cached and cached[0] == key:
            return cached[1]
        if not actors:
            pos = FramePos("offset", None, "unresolved")
        else:
            exact = [a for a in actors if a.w == fw and a.h == fh]
            if len(exact) == 1 and len(actors) == 1:
                pos = FramePos("offset", (exact[0].x, exact[0].y), "exact")
            else:
                pos = self._match(actors, fw, fh, screen, get_frame())
        self._pos_cache[hash(frame)] = (key, pos)
        return pos

    def _match(self, actors: list[Rect], fw: int, fh: int, screen: Screen,
               pixels: np.ndarray) -> FramePos:
        best: tuple[float, float, tuple[float, float]] | None = None
        for a in actors:
            crop = self._actor_crop(a, screen, pixels)
            p = locate_inner_rect(crop, fw, fh)
            if p is None:
                continue
            if best is None or p.score > best[0]:
                best = (p.score, p.margin, (a.x + p.dx, a.y + p.dy))
        if best is None or best[0] < 4:
            return FramePos("offset", None, "unresolved")
        return FramePos("offset", best[2], "matched" if best[1] > 2 else "ambiguous")

    @staticmethod
    def _actor_crop(a: Rect, screen: Screen, pixels: np.ndarray) -> np.ndarray:
        """Grayscale actor region in stream units, with a 1px ring of surroundings."""
        f = screen.frame_scale
        H, W = pixels.shape[:2]
        x0, y0 = round((a.x - 1) * f), round((a.y - 1) * f)
        x1, y1 = round((a.x + a.w + 1) * f), round((a.y + a.h + 1) * f)
        out = np.zeros((y1 - y0, x1 - x0, 3), dtype=np.uint8)
        sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if sx1 > sx0 and sy1 > sy0:
            out[sy0 - y0: sy1 - y0, sx0 - x0: sx1 - x0] = pixels[sy0:sy1, sx0:sx1]
        if f != 1.0:
            img = Image.fromarray(out).resize((round(a.w) + 2, round(a.h) + 2), Image.BILINEAR)
            out = np.asarray(img)
        return to_gray(out.astype(np.int32))

    def rect_of(self, acc: Atspi.Accessible, pos: FramePos) -> Rect | None:
        comp = _safe(acc.get_component_iface)
        if comp is None:
            return None
        if pos.mode == "screen":
            e = _safe(lambda: comp.get_extents(SCR))
            return Rect(e.x, e.y, e.width, e.height) if e else None
        if pos.origin is None:
            return None
        e = _safe(lambda: comp.get_extents(WIN))
        if e is None or (e.width <= 0 and e.height <= 0):
            return None
        return Rect(pos.origin[0] + e.x, pos.origin[1] + e.y, e.width, e.height)

    # ---- snapshot ------------------------------------------------------------

    def snapshot(self, root: Atspi.Accessible, pos: FramePos, *, interactive_only: bool,
                 max_depth: int = 60, max_nodes: int = MAX_NODES) -> tuple[Node | None, dict]:
        stats = {"visited": 0, "truncated": False}
        deadline = time.monotonic() + TIME_BUDGET

        def build(acc: Atspi.Accessible, depth: int) -> Node | None:
            if stats["visited"] >= max_nodes or time.monotonic() > deadline:
                stats["truncated"] = True
                return None
            stats["visited"] += 1
            role = _safe(acc.get_role_name, "") or ""
            st = _safe(acc.get_state_set)
            showing = bool(st and st.contains(S.SHOWING))
            if depth > 0 and st is not None and not showing and role not in ("menu", "combo box"):
                return None
            actions = [a for a in _action_names(acc) if not a.startswith(_NOISE_ACTIONS)]
            states = [label for s, label in _SHOWN_STATES.items() if st and st.contains(s)]
            if st and not st.contains(S.SENSITIVE):
                states.append("disabled")
            editable = bool(st and st.contains(S.EDITABLE))
            interactive = bool(actions) or editable or role in _INTERACTIVE_ROLES or bool(
                st and st.contains(S.FOCUSABLE) and role not in ("frame", "panel", "filler"))
            node = Node(
                ref=self.ref_for(acc), acc=acc, role=role, name=_safe(acc.get_name, "") or "",
                depth=depth, states=states, actions=actions, value=_value_of(acc, role),
                rect=self.rect_of(acc, pos), interactive=interactive, showing=showing,
            )
            if depth < max_depth:
                n = _safe(acc.get_child_count, 0) or 0
                if n > MAX_CHILDREN:
                    stats["truncated"] = True
                for i in range(min(n, MAX_CHILDREN)):
                    c = _safe(lambda i=i, acc=acc: acc.get_child_at_index(i))
                    if c is not None and (child := build(c, depth + 1)) is not None:
                        node.children.append(child)
            return node

        tree = build(root, 0)
        return tree, stats

    # ---- actions -------------------------------------------------------------

    def do_click_action(self, acc: Atspi.Accessible) -> str | None:
        ai = _safe(acc.get_action_iface)
        if ai is None:
            return None
        names = _action_names(acc)
        for wanted in _CLICK_ACTIONS:
            for i, name in enumerate(names):
                if name.lower() == wanted or name.lower().endswith("." + wanted):
                    if _safe(lambda i=i: ai.do_action(i), False):
                        return name
        return None

    def do_named_action(self, acc: Atspi.Accessible, action: str) -> None:
        ai = _safe(acc.get_action_iface)
        names = _action_names(acc)
        if ai is None or action not in names:
            raise UseComputerError(f"element has no action {action!r}; available: {names}")
        if not _safe(lambda: ai.do_action(names.index(action)), False):
            raise UseComputerError(f"action {action!r} was refused by the application")

    def set_value(self, acc: Atspi.Accessible, value: Any,
                  mouse_click: Callable[[], None] | None = None) -> str:
        """mouse_click: fallback for toggles that expose no action (GTK4 check boxes)."""
        role = _safe(acc.get_role_name, "") or ""
        st = _safe(acc.get_state_set)
        if role in TOGGLE_ROLES:
            want = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            is_on = bool(st and (st.contains(S.CHECKED) or st.contains(S.PRESSED)))
            if is_on == want:
                return f"{role} already {want}"
            if self.do_click_action(acc) is not None:
                return f"{role} set to {want}"
            if mouse_click is None:
                raise UseComputerError(f"cannot toggle this {role}: it exposes no action")
            mouse_click()
            return f"{role} clicked with the mouse to set it to {want} (no accessibility action)"
        vi = _safe(acc.get_value_iface)
        if vi is not None and _is_number(value):
            if _safe(lambda: vi.set_current_value(float(value)), False):
                return f"value set to {value}"
        et = _safe(acc.get_editable_text_iface)
        if et is not None:
            if _safe(lambda: et.set_text_contents(str(value)), False):
                return "text replaced"
            raise UseComputerError("the application refused set_text_contents; "
                                   "click the field and type instead")
        raise UseComputerError(f"form_input does not support role {role!r}; use clicks and typing")

    def focused(self, frame: Atspi.Accessible, max_nodes: int = 1500) -> Atspi.Accessible | None:
        for acc in _iter_showing(frame, max_nodes):
            st = _safe(acc.get_state_set)
            if st and st.contains(S.FOCUSED):
                return acc
        return None

    def text_of(self, acc: Atspi.Accessible) -> str | None:
        ti = _safe(acc.get_text_iface)
        if ti is None:
            return None
        n = _safe(ti.get_character_count, 0) or 0
        return _safe(lambda: Atspi.Text.get_text(ti, 0, min(n, 100000)))


# PyGObject resolves `acc.get_text(...)` to the deprecated Accessible.get_text()
# (which returns the interface), not Text.get_text(start, end); interface methods
# are therefore called unbound, e.g. Atspi.Text.get_text(acc, 0, n).


def _iter_showing(root: Atspi.Accessible, max_nodes: int) -> Iterator[Atspi.Accessible]:
    stack = [root]
    seen = 0
    while stack and seen < max_nodes:
        acc = stack.pop()
        seen += 1
        yield acc
        st = _safe(acc.get_state_set)
        if seen > 1 and st is not None and not st.contains(S.SHOWING):
            continue
        n = _safe(acc.get_child_count, 0) or 0
        for i in reversed(range(min(n, MAX_CHILDREN))):
            c = _safe(lambda i=i, acc=acc: acc.get_child_at_index(i))
            if c is not None:
                stack.append(c)


def _is_number(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _value_of(acc: Atspi.Accessible, role: str) -> str | None:
    if role in ("text", "entry", "password text", "search box", "spin button", "combo box",
                "terminal", "document text", "paragraph"):
        if role == "password text":
            return "<hidden>"
        ti = _safe(acc.get_text_iface)
        if ti is not None:
            n = _safe(ti.get_character_count, 0) or 0
            t = _safe(lambda: Atspi.Text.get_text(ti, 0, min(n, 200)))
            if t:
                return t + ("…" if n > 200 else "")
    vi = _safe(acc.get_value_iface)
    if vi is not None:
        v = _safe(vi.get_current_value)
        if v is not None:
            return f"{v:g}"
    return None


def render(node: Node, screen: Screen | None, *, interactive_only: bool, max_chars: int) -> str:
    lines: list[str] = []
    size = 0

    def emit(n: Node, depth: int) -> bool:
        nonlocal size
        keep = (not interactive_only) or n.interactive or n.role == "frame"
        if keep and n.role in ("panel", "filler", "section", "grouping") and not n.name \
                and not n.actions and n.children and not interactive_only:
            keep = False  # anonymous layout containers only add indentation
        next_depth = depth
        if keep:
            parts = [f"[{n.ref}]", n.role]
            if n.name:
                parts.append(_quote(n.name))
            if n.rect is not None and screen is not None:
                r = screen.rect_stream_to_shot(n.rect)
                parts.append(f"@({round(r.x)},{round(r.y)},{round(r.w)},{round(r.h)})")
            if n.value is not None and n.value != n.name:
                parts.append(f"value={_quote(n.value)}")
            if n.states:
                parts.append("[" + ",".join(n.states) + "]")
            if n.actions:
                parts.append("actions=" + ",".join(n.actions[:6]))
            line = "  " * depth + " ".join(parts)
            if size + len(line) + 1 > max_chars:
                lines.append("… output truncated; narrow the scope or use ref_id")
                return False
            lines.append(line)
            size += len(line) + 1
            next_depth = depth + 1
        for c in n.children:
            if not emit(c, next_depth):
                return False
        return True

    emit(node, 0)
    return "\n".join(lines)


def _quote(s: str, limit: int = 120) -> str:
    s = s.replace("\n", " ")
    if len(s) > limit:
        s = s[:limit] + "…"
    return '"' + s.replace('"', '\\"') + '"'


def flatten(node: Node) -> Iterator[Node]:
    yield node
    for c in node.children:
        yield from flatten(c)
