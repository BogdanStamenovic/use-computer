"""MCP server: the agent-facing tools. Thin; every screen op goes through the daemon.

Tool shapes deliberately mirror the Claude in Chrome tools (computer / read_page /
find / form_input) so an agent that knows one knows the other.
"""

from __future__ import annotations

import os
import threading
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import UseComputerError, __version__
from .client import Client

INSTRUCTIONS = """\
Controls a GNOME desktop (mouse, keyboard, screen). By default this is a VIRTUAL desktop --
a second GNOME session nobody is looking at -- so you can work freely without taking over the
user's screen. Tell them they can watch it with `use-computer watch`. The `desktop` tool says
which one you are on and can switch to the real screen; only do that if the task genuinely
needs their own session, and ask them first.
Workflow: screenshot -> read_screen for refs -> act by ref when possible, by coordinate
otherwise -> verify with a screenshot. Coordinates are pixels of the most recent screenshot.
Batch predictable steps with computer_batch. If a result says control was revoked, stop and
ask the user. Never type passwords, payment or identity data; never solve CAPTCHAs."""

mcp = MCPServer("use-computer", instructions=INSTRUCTIONS, version=__version__)
_client = Client(client_id=f"mcp:{os.getpid()}")
_lock = threading.Lock()


def _enter_desktop(name: str | None = None) -> str:
    """Point this server at a desktop. Virtual by default; 'real' is the opt-in."""
    from . import client as client_mod
    from . import vd
    global _entered
    with _lock:
        _client.close()
        d = client_mod.target(name)
        _entered = True
        return vd.describe(d)


_entered = False


def _call_sync(op: str, **args: Any) -> dict[str, Any]:
    global _entered
    with _lock:
        if not _entered:
            # Lazily, so merely loading the MCP server never spawns a GNOME Shell.
            from . import client as client_mod
            client_mod.target(os.environ.get("USE_COMPUTER_DESKTOP"),
                              start=op not in ("status", "stop"))
            _entered = True
        return _client.call(op, **{k: v for k, v in args.items() if v is not None})


async def _call(op: str, **args: Any) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(lambda: _call_sync(op, **args))


def _content(result: dict[str, Any], text: str | None = None) -> list[Any]:
    blocks: list[Any] = []
    if text:
        blocks.append(TextContent(type="text", text=text))
    if "image" in result:
        blocks.append(ImageContent(type="image", data=result["image"], mime_type=result["mime"]))
    return blocks


def _error(exc: Exception) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"Error: {exc}")], is_error=True)


def _describe(result: dict[str, Any]) -> str:
    rest = {k: v for k, v in result.items() if k not in ("image", "mime", "bytes")}
    if list(rest) == ["done"]:
        return str(rest["done"])
    import json
    return json.dumps(rest, ensure_ascii=False)


Coordinate = Annotated[list[float], Field(min_length=2, max_length=2,
                                          description="[x, y] in screenshot pixels")]

_ACTION_OPS = {
    "left_click": ("click", {"button": "left", "count": 1}),
    "right_click": ("click", {"button": "right", "count": 1}),
    "middle_click": ("click", {"button": "middle", "count": 1}),
    "double_click": ("click", {"button": "left", "count": 2}),
    "triple_click": ("click", {"button": "left", "count": 3}),
    "hover": ("move", {}),
    "mouse_move": ("move", {}),
    "left_click_drag": ("drag", {}),
    "scroll": ("scroll", {}),
    "type": ("type", {}),
    "key": ("key", {}),
    "wait": ("wait", {}),
    "screenshot": ("screenshot", {}),
    "zoom": ("zoom", {}),
}


def _computer_request(action: str, coordinate: list[float] | None, ref: str | None,
                      text: str | None, modifiers: str | None, scroll_direction: str | None,
                      scroll_amount: int | None, start_coordinate: list[float] | None,
                      region: list[float] | None, duration: float | None, repeat: int | None,
                      mouse: bool | None) -> tuple[str, dict[str, Any]]:
    if action not in _ACTION_OPS:
        raise UseComputerError(f"unknown action {action!r}")
    op, extra = _ACTION_OPS[action]
    args: dict[str, Any] = {**extra, "coordinate": coordinate, "ref": ref, "text": text,
                            "modifiers": modifiers, "scroll_direction": scroll_direction,
                            "scroll_amount": scroll_amount, "start_coordinate": start_coordinate,
                            "region": region, "duration": duration, "repeat": repeat,
                            "mouse": mouse}
    return op, {k: v for k, v in args.items() if v is not None}


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True))
async def computer(
    action: Annotated[Literal["screenshot", "left_click", "right_click", "middle_click",
                              "double_click", "triple_click", "hover", "left_click_drag",
                              "scroll", "type", "key", "wait", "zoom"],
                      Field(description="What to do")],
    coordinate: Coordinate | None = None,
    ref: Annotated[str | None, Field(description="Element ref from read_screen/find; used "
                                     "instead of coordinate. left_click on a ref uses the "
                                     "element's accessibility action when it has one.")] = None,
    text: Annotated[str | None, Field(description="type: the text (any Unicode). key: "
                                      "space-separated combos like 'ctrl+s' or 'Tab Return'")] = None,
    modifiers: Annotated[str | None, Field(description="Held during a click/scroll/drag, "
                                           "e.g. 'ctrl' or 'ctrl+shift'")] = None,
    scroll_direction: Literal["up", "down", "left", "right"] | None = None,
    scroll_amount: Annotated[int | None, Field(ge=1, le=30, description="Wheel steps")] = None,
    start_coordinate: Annotated[list[float] | None, Field(
        min_length=2, max_length=2, description="left_click_drag start [x, y]")] = None,
    region: Annotated[list[float] | None, Field(
        min_length=4, max_length=4, description="zoom: [x0, y0, x1, y1] in screenshot pixels")] = None,
    duration: Annotated[float | None, Field(ge=0, le=30, description="wait: seconds")] = None,
    repeat: Annotated[int | None, Field(ge=1, le=100, description="key: repeat count")] = None,
    mouse: Annotated[bool | None, Field(description="With ref: click with the real pointer "
                                        "even if an accessibility action exists")] = None,
) -> CallToolResult:
    """Mouse, keyboard and screen on the user's GNOME desktop.

    Coordinates are pixels in the most recent screenshot (it is downscaled, e.g. 1280x720
    for a 1920x1080 screen; the tool maps them back). A screenshot waits briefly for the
    screen to stop changing. Text outside ASCII is pasted via the clipboard, which is
    restored afterwards. Actions other than screenshot/zoom return a short confirmation,
    not an image: take a screenshot when you need to see the outcome."""
    try:
        op, args = _computer_request(action, coordinate, ref, text, modifiers, scroll_direction,
                                     scroll_amount, start_coordinate, region, duration, repeat,
                                     mouse)
        result = await _call(op, **args)
    except UseComputerError as exc:
        return _error(exc)
    if "image" in result:
        note = (f"{result['width']}x{result['height']} screenshot"
                if op == "screenshot" else result.get("note", ""))
        return CallToolResult(content=_content(result, note))
    return CallToolResult(content=[TextContent(type="text", text=_describe(result))])


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def read_screen(
    scope: Annotated[str, Field(description="'focused' (active window), 'all' (every visible "
                                "window), or an app name / window title substring")] = "focused",
    filter: Annotated[Literal["interactive", "all"], Field(
        description="'interactive' keeps controls; 'all' adds labels and text")] = "interactive",
    ref_id: Annotated[str | None, Field(description="Only the subtree under this ref")] = None,
    max_chars: Annotated[int, Field(ge=1000, le=100000)] = 20000,
) -> CallToolResult:
    """Accessibility tree of windows: one line per element with [ref], role, name,
    @(x,y,w,h) box in screenshot pixels, value, states and actions.

    Prefer refs over pixel guessing. Apps that do not expose accessibility (games,
    canvases, some Electron apps) show little or nothing; use screenshots and ocr there.
    'positions=unresolved' means refs still work for actions but boxes are unknown."""
    try:
        r = await _call("read_screen", scope=scope, filter=filter, ref_id=ref_id,
                        max_chars=max_chars)
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=r["text"])])


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def find(
    query: Annotated[str, Field(description="Text to look for: a button label, field name, "
                                "visible text")],
    scope: Annotated[str, Field(description="'all', 'focused', or app/title substring")] = "all",
    ocr: Annotated[bool, Field(description="Fall back to OCR when accessibility finds "
                               "nothing")] = True,
) -> CallToolResult:
    """Find elements by name. Returns refs with boxes (accessibility) or, if nothing
    accessible matches, OCR text boxes with a click_at point in screenshot pixels."""
    try:
        r = await _call("find", query=query, scope=scope, ocr=ocr)
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
async def form_input(
    ref: Annotated[str, Field(description="Element ref")],
    value: Annotated[str | bool | float, Field(description="Text for fields (replaces the "
                                               "content), true/false for checkboxes and "
                                               "switches, a number for sliders/spin buttons")],
) -> CallToolResult:
    """Set a form control's value directly through accessibility, without typing."""
    try:
        r = await _call("form_input", ref=ref, value=value)
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def ocr(
    region: Annotated[list[float] | None, Field(
        min_length=4, max_length=4, description="[x0, y0, x1, y1] in screenshot pixels; "
        "whole screen if omitted")] = None,
    lang: Annotated[str | None, Field(description="tesseract languages, e.g. 'eng', "
                                      "'eng+srp_latn', 'srp'")] = None,
) -> CallToolResult:
    """Read text from the screen with tesseract: lines with confidence and boxes in
    screenshot pixels. Use when accessibility has nothing (images, canvases, terminals,
    remote desktops). ~0.7 s for a full 1080p screen."""
    try:
        r = await _call("ocr", region=region, lang=lang)
    except UseComputerError as exc:
        return _error(exc)
    lines = "\n".join(f"{ln['box']} ({ln['confidence']}) {ln['text']}" for ln in r["lines"])
    return CallToolResult(content=[TextContent(type="text", text=lines or "(no text found)")])


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def windows(
    positions: Annotated[bool, Field(description="Also resolve on-screen boxes (slower)")] = False,
) -> CallToolResult:
    """List open windows (app, title, active, ref) from accessibility."""
    try:
        r = await _call("windows", positions=positions)
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
async def open_app(
    name: Annotated[str, Field(description="App name as it appears in GNOME's search, "
                               "e.g. 'Firefox', 'Files', 'Settings'")],
) -> CallToolResult:
    """Open or switch to an app through the Activities search (Super, type, Enter).
    Confirm with a screenshot afterwards."""
    try:
        r = await _call("open_app", name=name)
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def wait_for(
    text: Annotated[str | None, Field(description="Visible text to wait for (OCR)")] = None,
    name: Annotated[str | None, Field(description="Accessible element name to wait for")] = None,
    role: Annotated[str | None, Field(description="With name: required role, e.g. 'button'")] = None,
    screen_idle: Annotated[bool, Field(description="Wait until the screen stops changing")] = False,
    gone: Annotated[bool, Field(description="Wait for it to disappear instead")] = False,
    timeout: Annotated[float, Field(ge=0.5, le=120)] = 10,
) -> CallToolResult:
    """Wait for something instead of sleeping. Returns met=true/false."""
    try:
        r = await _call("wait_for", text=text, name=name, role=role, screen_idle=screen_idle,
                        gone=gone, timeout=timeout)
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
async def clipboard(
    action: Literal["get", "set"],
    text: Annotated[str | None, Field(description="set: new clipboard text")] = None,
) -> CallToolResult:
    """Read or replace the clipboard text."""
    try:
        if action == "get":
            r = await _call("clipboard_get")
            return CallToolResult(content=[TextContent(type="text",
                                                       text=r["text"] if r["text"] is not None
                                                       else "(clipboard empty or not text)")])
        r = await _call("clipboard_set", text=text or "")
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


_BATCH_TOOLS = {"computer", "read_screen", "find", "form_input", "ocr", "wait_for", "clipboard",
                "open_app"}


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True))
async def computer_batch(
    actions: Annotated[list[dict[str, Any]], Field(
        description="Steps run in order, stopping at the first error. Each is "
        "{name, input} where name is computer|read_screen|find|form_input|ocr|wait_for|"
        "clipboard|open_app and input is exactly that tool's arguments.")],
) -> CallToolResult:
    """Run several steps in one round trip, e.g. click a field, type, press Return, wait,
    screenshot. Returns every step's text and any images, in order."""
    blocks: list[Any] = []
    for i, step in enumerate(actions):
        name, inp = step.get("name"), step.get("input") or {}
        if name not in _BATCH_TOOLS:
            blocks.append(TextContent(type="text", text=f"step {i}: unknown tool {name!r}; stopped"))
            return CallToolResult(content=blocks, is_error=True)
        result = await globals()[name](**inp)
        for b in result.content:
            if isinstance(b, TextContent):
                blocks.append(TextContent(type="text", text=f"step {i} {name}: {b.text}"))
            else:
                blocks.append(b)
        if result.is_error:
            blocks.append(TextContent(type="text", text=f"stopped at step {i}"))
            return CallToolResult(content=blocks, is_error=True)
    return CallToolResult(content=blocks)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def listen(
    seconds: Annotated[float, Field(gt=0, le=300, description="How long to record")],
    source: Annotated[Literal["output", "mic"], Field(
        description="'output' = what the computer is playing; 'mic' = the microphone. Only "
        "use mic when the user asked for it.")] = "output",
    language: Annotated[str | None, Field(description="ISO code like 'en' or 'sr'; "
                                          "auto-detect if omitted")] = None,
) -> CallToolResult:
    """Record audio for a fixed time and transcribe it with Whisper (local, CPU). The
    recording is deleted afterwards. Start playback before calling. Transcription takes about
    as long as the audio (minimum ~15 s, Whisper works in 30-second windows)."""
    from . import audio

    try:
        r = await anyio.to_thread.run_sync(lambda: audio.listen(seconds, source, language=language))
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def transcribe(
    path: Annotated[str, Field(description="Audio or video file path")],
    language: str | None = None,
) -> CallToolResult:
    """Transcribe an audio/video file with Whisper (local, CPU)."""
    from . import audio

    try:
        r = await anyio.to_thread.run_sync(lambda: audio.transcribe(path, language=language))
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


@mcp.tool()
async def session(
    action: Annotated[Literal["status", "stop", "resume"], Field(
        description="status; stop = end control now (removes the sharing indicator); "
        "resume = only after the user explicitly agreed to give control back")],
) -> CallToolResult:
    """Inspect or end the control session."""
    try:
        r = await _call(action)
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=_describe(r))])


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False))
async def desktop(
    action: Annotated[Literal["status", "use_virtual", "use_real", "list", "stop"], Field(
        description="status = which desktop you are driving; use_virtual = switch to an "
        "isolated virtual desktop (the default); use_real = take over the user's own screen, "
        "which needs their agreement first; list = show virtual desktops; stop = shut one down")],
    name: Annotated[str | None, Field(
        description="virtual desktop name (default: 'agent')")] = None,
) -> CallToolResult:
    """Choose which desktop to drive, or inspect the virtual ones.

    Work happens on a virtual desktop unless the user asks otherwise. The user can
    see it at any time with `use-computer watch`.
    """
    try:
        from . import vd
        if action == "status":
            d = vd.load(os.environ.get("USE_COMPUTER_DESKTOP", "")) if os.environ.get(
                "USE_COMPUTER_DESKTOP", "").casefold() not in vd.REAL_NAMES else None
            text = vd.describe(d)
            if d is not None:
                text += f"\n  the user can watch it with: use-computer watch {d.name}"
        elif action == "list":
            text = "\n".join(
                f"{x.name}: {x.size}, {'up' if x.alive else 'dead'}, {x.age / 60:.0f}m"
                for x in vd.list_all()) or "no virtual desktops"
        elif action == "stop":
            text = str(vd.stop(name or vd.DEFAULT_NAME, quiet=True)["done"])
        else:
            text = "now driving the " + _enter_desktop("real" if action == "use_real" else name)
    except UseComputerError as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=text)])


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
