"""All operations, independent of transport. The daemon feeds requests in here.

Conventions for results: plain JSON-able dicts. Images are returned as
{"image": <base64>, "mime": ..., "width": ..., "height": ...} so both the MCP
server and the CLI can decode them.
"""

from __future__ import annotations

import base64
import io
import os
import subprocess
import time
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
from PIL import Image

from . import UseComputerError
from .a11y import A11y, FramePos, Node, _iter_showing, flatten, render
from .geometry import Rect, Screen
from .keys import MODIFIERS, char_keysym, parse_combo, parse_keys, segment_text
from .mutter import ControlRevoked, RemoteSession
from .ocr import find_text, ocr

LEASE_SECONDS = 30.0
TERMINAL_APPS = {"kgx", "gnome-terminal-server", "ptyxis", "console", "kitty", "alacritty",
                 "foot", "wezterm-gui", "konsole", "xterm", "tilix", "terminator", "blackbox"}


class Busy(UseComputerError):
    pass


def _image_result(img: Image.Image, fmt: str, quality: int, **extra: Any) -> dict[str, Any]:
    buf = io.BytesIO()
    fmt = fmt.lower()
    if fmt in ("jpg", "jpeg"):
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        mime = "image/jpeg"
    elif fmt == "png":
        img.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    else:
        img.save(buf, format="WEBP", quality=quality, method=4)
        mime = "image/webp"
    data = buf.getvalue()
    return {"image": base64.b64encode(data).decode("ascii"), "mime": mime,
            "width": img.width, "height": img.height, "bytes": len(data), **extra}


class Controller:
    def __init__(self) -> None:
        self.session: RemoteSession | None = None
        self.a11y = A11y()
        self.revoked = False
        self.owner: str | None = None
        self.owner_seen = 0.0
        self.ops: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            name[3:]: getattr(self, name) for name in dir(self) if name.startswith("op_")
        }

    # ---- plumbing ------------------------------------------------------------

    def dispatch(self, op: str, args: dict[str, Any], client: str = "anon") -> dict[str, Any]:
        fn = self.ops.get(op)
        if fn is None:
            raise UseComputerError(f"unknown operation {op!r}")
        if op not in ("status", "stop", "resume", "windows", "batch"):
            self._claim(client, bool(args.get("takeover")))
        return fn(args)

    def _claim(self, client: str, takeover: bool) -> None:
        now = time.monotonic()
        if (self.owner not in (None, client) and now - self.owner_seen < LEASE_SECONDS
                and not takeover):
            raise Busy(f"another agent ({self.owner}) is controlling the computer; "
                       f"wait {LEASE_SECONDS:.0f}s after it goes idle or pass takeover")
        self.owner, self.owner_seen = client, now

    def ensure(self) -> RemoteSession:
        if self.revoked:
            raise ControlRevoked("control was revoked by the user (screen sharing stopped from "
                                 "the top bar). Ask them before calling resume.")
        s = self.session
        if s is not None and s.closed_by_user:
            self.revoked = True
            self.session = None
            return self.ensure()
        if s is None or not s.alive:
            s = RemoteSession(connector=os.environ.get("USE_COMPUTER_MONITOR", ""))
            s.start()
            self.session = s
        return s

    @property
    def screen(self) -> Screen:
        s = self.ensure()
        assert s.screen is not None
        return s.screen

    def idle_tick(self, session_idle: float) -> None:
        s = self.session
        if s is None:
            return
        if s.closed_by_user:
            self.revoked = True
            self.session = None
            return
        s.pump(0)
        if s.alive and time.monotonic() - s.last_used > session_idle:
            s.stop()
            self.session = None

    def _xy(self, args: dict[str, Any], key: str = "coordinate") -> tuple[float, float]:
        """Target point in stream coordinates from a coordinate or a ref."""
        if args.get("ref"):
            rect = self._ref_rect(args["ref"])
            return rect.center
        c = args.get(key)
        if not c or len(c) != 2:
            raise UseComputerError(f"{key} [x, y] or ref is required")
        return self.screen.to_stream(float(c[0]), float(c[1]), args.get("space", "shot"))

    def _ref_rect(self, ref: str) -> Rect:
        acc = self.a11y.resolve(ref)
        frame = self.a11y.frame_of(acc)
        if frame is None:
            raise UseComputerError(f"{ref} is not inside a window")
        pos = self._frame_pos(frame)
        rect = self.a11y.rect_of(acc, pos)
        if rect is None or rect.w <= 0 or rect.h <= 0:
            raise UseComputerError(
                f"screen position of {ref} is unknown ({pos.confidence}); use an accessibility "
                "action (click without mouse), or locate it on a screenshot")
        return rect

    def _frame_pos(self, frame: Any) -> FramePos:
        s = self.ensure()
        return self.a11y.frame_position(frame, self.screen, lambda: s.frame().pixels)

    def _settle(self, args: dict[str, Any]) -> None:
        if args.get("settle", True):
            self.ensure().wait_quiet(0.12, float(args.get("settle_timeout", 1.0)))

    # ---- screen --------------------------------------------------------------

    def op_status(self, args: dict[str, Any]) -> dict[str, Any]:
        s = self.session
        out: dict[str, Any] = {
            "session": bool(s and s.alive), "revoked": self.revoked, "owner": self.owner,
            "pid": os.getpid(),
        }
        if s and s.alive and s.screen:
            sc = s.screen
            out.update(screen=[sc.stream_w, sc.stream_h], shot=list(sc.shot_size),
                       shot_scale=round(sc.shot_scale, 4))
        return out

    def op_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        s = self.ensure()
        self._settle(args)
        frame = s.frame()
        sc = self.screen
        img = Image.fromarray(frame.pixels)
        if (img.width, img.height) != sc.shot_size:
            img = img.resize(sc.shot_size, Image.LANCZOS)
        return _image_result(img, args.get("format", "webp"), int(args.get("quality", 82)),
                             scale=round(sc.shot_scale, 4), screen=[sc.stream_w, sc.stream_h])

    def op_zoom(self, args: dict[str, Any]) -> dict[str, Any]:
        region = args.get("region")
        if not region or len(region) != 4:
            raise UseComputerError("region [x0, y0, x1, y1] is required")
        s = self.ensure()
        self._settle(args)
        sc = self.screen
        x0, y0 = sc.to_stream(region[0], region[1], args.get("space", "shot"))
        x1, y1 = sc.to_stream(region[2], region[3], args.get("space", "shot"))
        if x1 <= x0 or y1 <= y0:
            raise UseComputerError("region must have x1 > x0 and y1 > y0")
        fx0, fy0, fx1, fy1 = sc.rect_stream_to_frame(Rect(x0, y0, x1 - x0, y1 - y0))
        crop = Image.fromarray(s.frame().pixels[fy0:fy1, fx0:fx1])
        fit = min(sc.max_w / crop.width, sc.max_h / crop.height, 3.0)
        if fit < 1.0 or fit >= 1.5:
            crop = crop.resize((max(1, round(crop.width * fit)), max(1, round(crop.height * fit))),
                               Image.LANCZOS)
        return _image_result(crop, args.get("format", "webp"), int(args.get("quality", 90)),
                             region_shot=region, note="zoomed view; click using coordinates "
                             "from the regular screenshot, not from this image")

    # ---- pointer -------------------------------------------------------------

    def _with_modifiers(self, modifiers: str | None, fn: Callable[[], None]) -> None:
        s = self.ensure()
        mods = parse_combo(modifiers) if modifiers else []
        bad = [m for m in mods if m not in MODIFIERS]
        if bad:
            raise UseComputerError(f"modifiers must be modifier keys, got {modifiers!r}")
        for m in mods:
            s.key(m, True)
        try:
            fn()
        finally:
            for m in reversed(mods):
                s.key(m, False)

    def op_click(self, args: dict[str, Any]) -> dict[str, Any]:
        button = args.get("button", "left")
        count = int(args.get("count", 1))
        if (args.get("ref") and button == "left" and count == 1 and not args.get("modifiers")
                and not args.get("mouse")):
            acc = self.a11y.resolve(args["ref"])
            used = self.a11y.do_click_action(acc)
            if used is not None:
                s = self.ensure()
                s.pump(0.05)
                return {"done": f"activated {args['ref']} via accessibility action '{used}'"}
        s = self.ensure()
        x, y = self._xy(args)

        def clicks() -> None:
            s.move(x, y)
            s.pump(0.04)
            for i in range(count):
                s.button(button, True)
                s.pump(0.02)
                s.button(button, False)
                if i + 1 < count:
                    s.pump(0.06)

        self._with_modifiers(args.get("modifiers"), clicks)
        s.pump(0.05)
        sx, sy = self.screen.stream_to_shot(x, y)
        return {"done": f"{button} click x{count} at ({round(sx)}, {round(sy)})"}

    def op_move(self, args: dict[str, Any]) -> dict[str, Any]:
        s = self.ensure()
        x, y = self._xy(args)
        s.move(x, y)
        s.pump(0.05)
        return {"done": "pointer moved"}

    def op_button(self, args: dict[str, Any]) -> dict[str, Any]:
        s = self.ensure()
        if args.get("coordinate") or args.get("ref"):
            s.move(*self._xy(args))
            s.pump(0.03)
        s.button(args.get("button", "left"), args.get("state", "down") == "down")
        return {"done": f"{args.get('button', 'left')} {args.get('state', 'down')}"}

    def op_drag(self, args: dict[str, Any]) -> dict[str, Any]:
        s = self.ensure()
        if args.get("start_ref"):
            x0, y0 = self._ref_rect(args["start_ref"]).center
        else:
            x0, y0 = self._xy({"coordinate": args.get("start_coordinate"),
                               "space": args.get("space", "shot")}, "coordinate")
        x1, y1 = self._xy(args)
        button = args.get("button", "left")
        steps = int(args.get("steps", 12))

        def drag() -> None:
            s.move(x0, y0)
            s.pump(0.05)
            s.button(button, True)
            s.pump(0.08)
            for i in range(1, steps + 1):
                t = i / steps
                s.move(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
                s.pump(0.02)
            s.pump(0.08)
            s.button(button, False)

        self._with_modifiers(args.get("modifiers"), drag)
        s.pump(0.05)
        return {"done": "dragged"}

    def op_scroll(self, args: dict[str, Any]) -> dict[str, Any]:
        s = self.ensure()
        direction = args.get("scroll_direction", args.get("direction", "down"))
        if direction not in ("up", "down", "left", "right"):
            raise UseComputerError("scroll_direction must be up, down, left or right")
        amount = int(args.get("scroll_amount", args.get("amount", 3)))
        if args.get("coordinate") or args.get("ref"):
            s.move(*self._xy(args))
            s.pump(0.04)
        self._with_modifiers(args.get("modifiers"), lambda: s.scroll(direction, amount))
        return {"done": f"scrolled {direction} {amount}"}

    # ---- keyboard ------------------------------------------------------------

    def op_key(self, args: dict[str, Any]) -> dict[str, Any]:
        s = self.ensure()
        spec = args.get("text") or args.get("keys")
        if not spec:
            raise UseComputerError("text with key names is required, e.g. 'ctrl+s' or 'Tab Return'")
        combos = parse_keys(spec)
        for _ in range(max(1, int(args.get("repeat", 1)))):
            for combo in combos:
                for ks in combo:
                    s.key(ks, True)
                    s.pump(0.008)
                for ks in reversed(combo):
                    s.key(ks, False)
                    s.pump(0.008)
                s.pump(0.03)
        return {"done": f"pressed {spec}"}

    def op_type(self, args: dict[str, Any]) -> dict[str, Any]:
        text = args.get("text")
        if text is None:
            raise UseComputerError("text is required")
        s = self.ensure()
        if args.get("ref"):
            x, y = self._ref_rect(args["ref"]).center
            s.move(x, y)
            s.pump(0.03)
            s.button("left", True)
            s.button("left", False)
            s.pump(0.15)
        delay = float(args.get("delay", 0.004))
        pasted = False
        for seg in segment_text(text):
            if seg.kind == "keys":
                for ch in seg.text:
                    ks = char_keysym(ch)
                    assert ks is not None
                    s.key(ks, True)
                    s.key(ks, False)
                    s.pump(delay)
            else:
                self._paste(seg.text)
                pasted = True
        s.pump(0.05)
        return {"done": f"typed {len(text)} characters" + (" (non-ASCII via clipboard paste)"
                                                          if pasted else ""),
                "verified": self._verify_typed(text)}

    def _paste(self, text: str) -> None:
        s = self.ensure()
        previous = s.clipboard_get()
        s.clipboard_set(text)
        s.pump(0.05)
        combo = "ctrl+shift+v" if self._terminal_focused() else "ctrl+v"
        keysyms = parse_combo(combo)
        for ks in keysyms:
            s.key(ks, True)
        for ks in reversed(keysyms):
            s.key(ks, False)
        s.pump(0.35)  # the target app requests the data asynchronously; serve it
        if previous is not None:
            s.clipboard_set(previous)

    def _terminal_focused(self) -> bool:
        active = self.a11y.active_frame()
        if active is None:
            return False
        app, frame = active
        name = (app.get_name() or "").lower()
        if name in TERMINAL_APPS:
            return True
        focused = self.a11y.focused(frame, 800)
        return bool(focused is not None and focused.get_role_name() == "terminal")

    def _verify_typed(self, text: str) -> bool | None:
        active = self.a11y.active_frame()
        if active is None:
            return None
        focused = self.a11y.focused(active[1], 1500)
        if focused is None:
            return None
        if focused.get_role_name() == "password text":
            return None
        content = self.a11y.text_of(focused)
        if content is None:
            return None
        tail = text.rstrip("\n")
        return tail in content if tail else None

    # ---- accessibility -------------------------------------------------------

    def _scope_frames(self, scope: str | None) -> list[Any]:
        frames = self.a11y.frames()
        if scope in (None, "", "focused", "active"):
            active = self.a11y.active_frame()
            if active is None:
                raise UseComputerError("no active window exposes accessibility; try scope='all' "
                                       "or take a screenshot")
            return [active[1]]
        if scope == "all":
            showing = []
            for _app, f in frames:
                st = f.get_state_set()
                if st is not None and st.contains(Atspi.StateType.SHOWING):
                    showing.append(f)
            return showing
        q = scope.casefold()
        hits = [f for app, f in frames
                if q in (app.get_name() or "").casefold() or q in (f.get_name() or "").casefold()]
        if not hits:
            names = sorted({f"{a.get_name()}: {f.get_name()}" for a, f in frames})
            raise UseComputerError(f"no window matches {scope!r}; windows: {names}")
        return hits

    def op_read_screen(self, args: dict[str, Any]) -> dict[str, Any]:
        interactive = args.get("filter", "interactive") != "all"
        max_chars = int(args.get("max_chars", 20000))
        depth = int(args.get("depth", 60))
        sc = self.screen
        chunks = []
        if args.get("ref_id"):
            acc = self.a11y.resolve(args["ref_id"])
            frame = self.a11y.frame_of(acc) or acc
            roots = [(acc, frame)]
        else:
            roots = [(f, f) for f in self._scope_frames(args.get("scope"))]
        for root, frame in roots:
            pos = self._frame_pos(frame)
            tree, stats = self.a11y.snapshot(root, pos, interactive_only=interactive,
                                             max_depth=depth)
            if tree is None:
                continue
            app = frame.get_application()
            header = (f"# window {_q(frame.get_name())} app={_q(app.get_name() if app else '')} "
                      f"positions={pos.confidence}")
            if pos.origin is None and pos.mode == "offset":
                header += " (no screen coordinates: use refs, or find things on a screenshot)"
            if stats["truncated"]:
                header += " (tree truncated)"
            chunks.append(header + "\n" + render(tree, sc, interactive_only=interactive,
                                                 max_chars=max_chars))
        text = "\n\n".join(chunks) if chunks else "(nothing accessible in scope)"
        return {"text": text, "coordinates": "screenshot space"}

    def op_find(self, args: dict[str, Any]) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            raise UseComputerError("query is required")
        q = query.casefold()
        sc = self.screen
        results = []
        try:
            frames = self._scope_frames(args.get("scope", "all"))
        except UseComputerError:
            frames = []
        for frame in frames:
            pos = self._frame_pos(frame)
            tree, _ = self.a11y.snapshot(frame, pos, interactive_only=False)
            if tree is None:
                continue
            for n in flatten(tree):
                hay = " ".join(filter(None, [n.name, n.value, n.role])).casefold()
                if q not in hay:
                    continue
                rank = 0 if n.name.casefold() == q else 1 if n.name.casefold().startswith(q) else 2
                rank += 0 if n.interactive else 3
                results.append((rank, n))
        results.sort(key=lambda r: r[0])
        out = [self._node_brief(n, sc) for _, n in results[: int(args.get("limit", 20))]]
        source = "accessibility"
        if not out and args.get("ocr", True):
            source = "ocr"
            for text, rect, conf in self._ocr_find(query):
                out.append({"text": text, "box": rect.as_list(), "confidence": round(conf),
                            "click_at": [round(rect.center[0]), round(rect.center[1])]})
        return {"source": source, "matches": out}

    @staticmethod
    def _node_brief(n: Node, sc: Screen) -> dict[str, Any]:
        d: dict[str, Any] = {"ref": n.ref, "role": n.role, "name": n.name}
        if n.value:
            d["value"] = n.value
        if n.states:
            d["states"] = n.states
        if n.actions:
            d["actions"] = n.actions
        if n.rect is not None:
            r = sc.rect_stream_to_shot(n.rect)
            d["box"] = r.as_list()
        return d

    def op_form_input(self, args: dict[str, Any]) -> dict[str, Any]:
        if "ref" not in args or "value" not in args:
            raise UseComputerError("ref and value are required")
        self.ensure()
        acc = self.a11y.resolve(args["ref"])
        return {"done": self.a11y.set_value(
            acc, args["value"], lambda: self.op_click({"ref": args["ref"], "mouse": True}))}

    def op_action(self, args: dict[str, Any]) -> dict[str, Any]:
        self.ensure()
        acc = self.a11y.resolve(args.get("ref", ""))
        self.a11y.do_named_action(acc, args.get("name", ""))
        return {"done": f"performed {args.get('name')}"}

    def op_windows(self, args: dict[str, Any]) -> dict[str, Any]:
        wins = self.a11y.windows()
        if args.get("positions") and wins:
            sc = self.screen
            for w in wins:
                if not w["showing"]:
                    continue
                acc = self.a11y.resolve(w["ref"])
                pos = self._frame_pos(acc)
                rect = self.a11y.rect_of(acc, pos)
                if rect is not None:
                    w["box"] = sc.rect_stream_to_shot(rect).as_list()
                w["positions"] = pos.confidence
        return {"windows": wins}

    def op_open_app(self, args: dict[str, Any]) -> dict[str, Any]:
        name = (args.get("name") or "").strip()
        if not name:
            raise UseComputerError("name is required")
        s = self.ensure()
        seq = s.frame_seq
        self.op_key({"text": "super"})
        # Wait for the overview to actually start appearing before waiting for it to
        # settle. wait_quiet alone returns immediately when no frames have begun
        # flowing yet -- which is exactly the case on a freshly started desktop, and
        # the search text was then typed into nothing.
        s.wait_change(seq, 3.0)
        s.wait_quiet(0.25, 1.5)
        self.op_type({"text": name})
        s.pump(0.6)
        s.wait_quiet(0.25, 2.0)
        self.op_key({"text": "Return"})
        s.wait_quiet(0.3, 3.0)
        return {"done": f"searched the Activities overview for {name!r} and pressed Return; "
                        "take a screenshot to confirm the right window is focused"}

    # ---- OCR -----------------------------------------------------------------

    def _ocr_image(self, region: list[float] | None, space: str) -> tuple[Image.Image, float,
                                                                          float, float]:
        """Crop at full resolution; returns image and the mapping back to shot space."""
        s = self.ensure()
        self._settle({})
        sc = self.screen
        pixels = s.frame().pixels
        if region:
            x0, y0 = sc.to_stream(region[0], region[1], space)
            x1, y1 = sc.to_stream(region[2], region[3], space)
        else:
            x0, y0, x1, y1 = 0.0, 0.0, float(sc.stream_w), float(sc.stream_h)
        fx0, fy0, fx1, fy1 = sc.rect_stream_to_frame(Rect(x0, y0, x1 - x0, y1 - y0))
        img = Image.fromarray(pixels[fy0:fy1, fx0:fx1])
        # frame px -> shot px
        k = sc.shot_scale / sc.frame_scale
        return img, k, x0 * sc.shot_scale, y0 * sc.shot_scale

    def _ocr_find(self, query: str, region: list[float] | None = None) -> list[tuple[str, Rect,
                                                                                   float]]:
        img, k, ox, oy = self._ocr_image(region, "shot")
        lines = ocr(img)
        return [(t, Rect(ox + r.x * k, oy + r.y * k, r.w * k, r.h * k), c)
                for t, r, c in find_text(lines, query)]

    def op_ocr(self, args: dict[str, Any]) -> dict[str, Any]:
        img, k, ox, oy = self._ocr_image(args.get("region"), args.get("space", "shot"))
        lines = ocr(img, args.get("lang"))
        min_conf = float(args.get("min_confidence", 40))
        out = []
        for ln in lines:
            if ln.conf < min_conf:
                continue
            r = ln.rect
            out.append({"text": ln.text, "confidence": round(ln.conf),
                        "box": Rect(ox + r.x * k, oy + r.y * k, r.w * k, r.h * k).as_list()})
        return {"lines": out}

    # ---- waiting -------------------------------------------------------------

    def op_wait(self, args: dict[str, Any]) -> dict[str, Any]:
        seconds = min(float(args.get("duration", args.get("seconds", 1))), 30.0)
        if self.session is not None and self.session.alive:
            self.session.pump(seconds)
        else:
            time.sleep(seconds)
        return {"done": f"waited {seconds}s"}

    def op_wait_for(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout = min(float(args.get("timeout", 10)), 120.0)
        gone = bool(args.get("gone"))
        text, name, role = args.get("text"), args.get("name"), args.get("role")
        if args.get("screen_idle"):
            idle = self.ensure().wait_quiet(float(args.get("quiet", 0.5)), timeout)
            return {"met": idle}
        if not (text or name):
            raise UseComputerError("give text (OCR), name (accessibility), or screen_idle")
        s = self.ensure()
        end = time.monotonic() + timeout
        last_seq = -1
        while True:
            present = False
            if name:
                present = self._a11y_present(name, role, args.get("scope", "all"))
            elif s.frame_seq != last_seq:
                last_seq = s.frame_seq
                present = bool(self._ocr_find(text, args.get("region")))
            if present != gone:
                return {"met": True, "after": round(timeout - (end - time.monotonic()), 2)}
            if time.monotonic() > end:
                return {"met": False, "after": timeout}
            s.pump(0.3)

    def _a11y_present(self, name: str, role: str | None, scope: str) -> bool:
        q = name.casefold()
        try:
            frames = self._scope_frames(scope)
        except UseComputerError:
            return False
        for frame in frames:
            for acc in _iter_showing(frame, 3000):
                try:
                    if q in (acc.get_name() or "").casefold() and (
                            role is None or acc.get_role_name() == role):
                        return True
                except Exception:
                    continue
        return False

    # ---- clipboard / session -------------------------------------------------

    def op_clipboard_get(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"text": self.ensure().clipboard_get()}

    def op_clipboard_set(self, args: dict[str, Any]) -> dict[str, Any]:
        text = args.get("text")
        if text is None:
            raise UseComputerError("text is required")
        self.ensure().clipboard_set(str(text))
        return {"done": "clipboard set"}

    def op_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.session is not None:
            self.session.stop()
            self.session = None
        self.owner = None
        return {"done": "session stopped; the screen-sharing indicator is gone"}

    def op_resume(self, args: dict[str, Any]) -> dict[str, Any]:
        self.revoked = False
        return {"done": "control may be taken again on the next action"}

    def op_launch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Start a program directly (no GUI search). argv list, detached."""
        argv = args.get("argv")
        if not argv or not isinstance(argv, list):
            raise UseComputerError("argv (list) is required")
        proc = subprocess.Popen([str(a) for a in argv], start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return {"done": f"started pid {proc.pid}"}

    def op_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        actions = args.get("actions") or []
        client = args.get("_client", "anon")
        results = []
        for i, item in enumerate(actions):
            op = item.get("op")
            try:
                results.append({"op": op, **self.dispatch(op, item.get("args", {}), client)})
            except UseComputerError as exc:
                results.append({"op": op, "error": str(exc)})
                return {"results": results, "stopped_at": i}
        return {"results": results}


def _q(s: str | None) -> str:
    return '"' + (s or "").replace('"', '\\"') + '"'
