"""Command-line interface for use-computer.

stdout carries results only (text, or JSON with --json; screenshots print the saved
path). Progress, warnings and errors go to stderr.
Exit codes: 0 success, 1 operation failed, 2 usage error, 3 control revoked by the user.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from . import UseComputerError, __version__


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _xy(v: str) -> list[float]:
    try:
        x, y = v.split(",")
        return [float(x), float(y)]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected X,Y, got {v!r}") from None


def _region(v: str) -> list[float]:
    parts = v.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"expected X0,Y0,X1,Y1, got {v!r}")
    return [float(p) for p in parts]


def _build_parser() -> argparse.ArgumentParser:
    p = _ArgumentParser(prog="use-computer",
                        description="See and drive the GNOME desktop. Coordinates are in "
                                    "screenshot pixels unless --screen is given.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--json", action="store_true", help="print raw JSON results")
    p.add_argument("--client", help="client id for the control lease (default: cli)")
    p.add_argument("--takeover", action="store_true", help="take control from another agent")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress non-error output")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def target(sp: argparse.ArgumentParser, required: bool = True) -> None:
        g = sp.add_mutually_exclusive_group(required=required)
        g.add_argument("--at", type=_xy, metavar="X,Y", help="coordinate")
        g.add_argument("--ref", help="element ref from tree/find")
        sp.add_argument("--screen", action="store_true", help="--at is in real screen pixels")

    sub.add_parser("status", help="daemon and session state")
    sp = sub.add_parser("screenshot", help="capture the screen to a file, print its path")
    sp.add_argument("-o", "--output", help="file path (default: temp file)")
    sp.add_argument("--format", default="png", choices=["png", "webp", "jpeg"])
    sp = sub.add_parser("zoom", help="capture a region at full resolution")
    sp.add_argument("region", type=_region, metavar="X0,Y0,X1,Y1")
    sp.add_argument("-o", "--output")
    sp.add_argument("--screen", action="store_true")
    for name, button, count in (("click", "left", 1), ("double-click", "left", 2),
                                ("triple-click", "left", 3), ("right-click", "right", 1),
                                ("middle-click", "middle", 1)):
        sp = sub.add_parser(name, help=f"{button} click x{count}")
        target(sp)
        sp.add_argument("--modifiers", help="e.g. ctrl+shift")
        sp.add_argument("--mouse", action="store_true",
                        help="with --ref: always use the mouse, never the accessibility action")
        sp.set_defaults(button=button, count=count)
    sp = sub.add_parser("move", help="move the pointer (hover)")
    target(sp)
    sp = sub.add_parser("drag", help="press at FROM, move to TO, release")
    sp.add_argument("start", type=_xy, metavar="FROM")
    sp.add_argument("end", type=_xy, metavar="TO")
    sp.add_argument("--button", default="left")
    sp.add_argument("--modifiers")
    sp.add_argument("--screen", action="store_true")
    sp = sub.add_parser("scroll", help="scroll at a position")
    target(sp, required=False)
    sp.add_argument("direction", choices=["up", "down", "left", "right"])
    sp.add_argument("amount", type=int, nargs="?", default=3)
    sp = sub.add_parser("key", help="press keys: 'ctrl+s', 'Tab Tab Return'")
    sp.add_argument("keys")
    sp.add_argument("--repeat", type=int, default=1)
    sp = sub.add_parser("type", help="type text (non-ASCII is pasted)")
    sp.add_argument("text")
    sp.add_argument("--ref", help="click this element first")
    sp = sub.add_parser("tree", help="accessibility tree of the focused window")
    sp.add_argument("--scope", default="focused", help="focused | all | app/title substring")
    sp.add_argument("--all", action="store_true", help="include non-interactive nodes")
    sp.add_argument("--ref", help="subtree under this ref")
    sp.add_argument("--max-chars", type=int, default=40000)
    sp = sub.add_parser("find", help="find elements by name (accessibility, then OCR)")
    sp.add_argument("query")
    sp.add_argument("--scope", default="all")
    sp.add_argument("--no-ocr", action="store_true")
    sp = sub.add_parser("form-input", help="set a field / checkbox / slider by ref")
    sp.add_argument("ref")
    sp.add_argument("value")
    sp = sub.add_parser("action", help="run a named accessibility action on a ref")
    sp.add_argument("ref")
    sp.add_argument("name")
    sp = sub.add_parser("ocr", help="read text on screen")
    sp.add_argument("--region", type=_region, metavar="X0,Y0,X1,Y1")
    sp.add_argument("--lang")
    sp = sub.add_parser("windows", help="list windows")
    sp.add_argument("--positions", action="store_true")
    sp = sub.add_parser("open-app", help="open/focus an app via the Activities search")
    sp.add_argument("name")
    sp = sub.add_parser("wait-for", help="wait for text/element to appear (or disappear)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="OCR text")
    g.add_argument("--name", help="accessible name")
    g.add_argument("--idle", action="store_true", help="screen stops changing")
    sp.add_argument("--role")
    sp.add_argument("--gone", action="store_true")
    sp.add_argument("--timeout", type=float, default=10)
    sp = sub.add_parser("clipboard", help="get or set the clipboard")
    sp.add_argument("action", choices=["get", "set"])
    sp.add_argument("text", nargs="?")
    sp = sub.add_parser("batch", help="run a JSON list of {op, args} from a file or -")
    sp.add_argument("file")
    sub.add_parser("stop", help="end the session (removes the screen-sharing indicator)")
    sub.add_parser("resume", help="allow control again after the user revoked it")
    sub.add_parser("shutdown", help="stop the daemon")
    sub.add_parser("daemon", help="run the daemon in the foreground")
    sub.add_parser("doctor", help="check the environment")

    sp = sub.add_parser("audio", help="listen/transcribe (needs the audio extra)")
    asub = sp.add_subparsers(dest="audio_cmd", required=True, metavar="ACTION")
    a = asub.add_parser("setup", help="install faster-whisper and download the model")
    a.add_argument("--model")
    a.add_argument("--no-install", action="store_true", help="only download the model")
    asub.add_parser("status", help="show audio readiness")
    a = asub.add_parser("listen", help="record what the computer plays, then transcribe")
    a.add_argument("seconds", type=float)
    a.add_argument("--source", default="output", choices=["output", "mic"])
    a.add_argument("--language")
    a.add_argument("--model")
    a.add_argument("--keep", action="store_true", help="keep the recording")
    a = asub.add_parser("transcribe", help="transcribe an audio file")
    a.add_argument("path")
    a.add_argument("--language")
    a.add_argument("--model")

    sp = sub.add_parser("mcp-register", help="register the MCP server in ~/.claude.json")
    sp.add_argument("--command", help="server command (default: this install's use-computer-mcp)")
    sub.add_parser("mcp-unregister", help="remove the MCP server from ~/.claude.json")
    return p


def _save_image(result: dict[str, Any], output: str | None, suffix: str) -> str:
    data = base64.b64decode(result["image"])
    if output:
        path = Path(output).expanduser()
    else:
        fd, name = tempfile.mkstemp(prefix="use-computer-", suffix=suffix)
        os.close(fd)
        path = Path(name)
    path.write_bytes(data)
    return str(path)


def _request(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    cmd = args.cmd
    space = "screen" if getattr(args, "screen", False) else "shot"
    if cmd in ("click", "double-click", "triple-click", "right-click", "middle-click"):
        return "click", {"coordinate": args.at, "ref": args.ref, "space": space,
                         "button": args.button, "count": args.count,
                         "modifiers": args.modifiers, "mouse": args.mouse}
    simple: dict[str, tuple[str, dict[str, Any]]] = {
        "status": ("status", {}),
        "move": ("move", {"coordinate": getattr(args, "at", None), "ref": getattr(args, "ref", None),
                          "space": space}),
        "stop": ("stop", {}),
        "resume": ("resume", {}),
        "shutdown": ("shutdown", {}),
    }
    if cmd in simple:
        return simple[cmd]
    if cmd == "screenshot":
        return "screenshot", {"format": args.format}
    if cmd == "zoom":
        return "zoom", {"region": args.region, "space": space, "format": "png"}
    if cmd == "drag":
        return "drag", {"start_coordinate": args.start, "coordinate": args.end,
                        "button": args.button, "modifiers": args.modifiers, "space": space}
    if cmd == "scroll":
        return "scroll", {"coordinate": args.at, "ref": args.ref, "space": space,
                          "scroll_direction": args.direction, "scroll_amount": args.amount}
    if cmd == "key":
        return "key", {"text": args.keys, "repeat": args.repeat}
    if cmd == "type":
        return "type", {"text": args.text, "ref": args.ref}
    if cmd == "tree":
        return "read_screen", {"scope": args.scope, "filter": "all" if args.all else "interactive",
                               "ref_id": args.ref, "max_chars": args.max_chars}
    if cmd == "find":
        return "find", {"query": args.query, "scope": args.scope, "ocr": not args.no_ocr}
    if cmd == "form-input":
        value: Any = args.value
        if value.lower() in ("true", "false"):
            value = value.lower() == "true"
        return "form_input", {"ref": args.ref, "value": value}
    if cmd == "action":
        return "action", {"ref": args.ref, "name": args.name}
    if cmd == "ocr":
        return "ocr", {"region": args.region, "lang": args.lang}
    if cmd == "windows":
        return "windows", {"positions": args.positions}
    if cmd == "open-app":
        return "open_app", {"name": args.name}
    if cmd == "wait-for":
        return "wait_for", {"text": args.text, "name": args.name, "role": args.role,
                            "screen_idle": args.idle, "gone": args.gone, "timeout": args.timeout}
    if cmd == "clipboard":
        if args.action == "get":
            return "clipboard_get", {}
        if args.text is None:
            raise _UsageError("clipboard set needs TEXT")
        return "clipboard_set", {"text": args.text}
    if cmd == "batch":
        raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        return "batch", {"actions": json.loads(raw)}
    raise _UsageError(f"unhandled command {cmd}")


def _print_result(op: str, result: dict[str, Any], args: argparse.Namespace) -> None:
    if "image" in result:
        suffix = {"image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg"}[result["mime"]]
        path = _save_image(result, getattr(args, "output", None), suffix)
        result = {k: v for k, v in result.items() if k != "image"} | {"path": path}
        if not args.json:
            print(path)
            return
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif "text" in result and op == "read_screen":
        print(result["text"])
    elif "done" in result and len(result) == 1:
        print(result["done"])
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _audio(args: argparse.Namespace) -> int:
    from . import audio

    model = getattr(args, "model", None) or audio.DEFAULT_MODEL
    if args.audio_cmd == "status":
        print(json.dumps(audio.model_status(model), indent=2))
        return 0
    if args.audio_cmd == "setup":
        if not args.no_install:
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                import subprocess

                print("installing faster-whisper into this environment…", file=sys.stderr)
                rc = subprocess.run([sys.executable, "-m", "pip", "install", "faster-whisper>=1.1"]
                                    ).returncode
                if rc != 0:
                    rc = subprocess.run(["uv", "pip", "install", "--python", sys.executable,
                                         "faster-whisper>=1.1"]).returncode
                if rc != 0:
                    print("use-computer: error: could not install faster-whisper", file=sys.stderr)
                    return 1
        print(f"downloading whisper model {model} (large-v3-turbo is ~1.6 GB)…", file=sys.stderr)
        print(audio.download(model))
        return 0
    if args.audio_cmd == "listen":
        print(f"listening to {args.source} for {args.seconds:g}s…", file=sys.stderr)
        res = audio.listen(args.seconds, args.source, model, args.language, args.keep)
    else:
        res = audio.transcribe(args.path, model, args.language)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else res["text"])
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"use-computer: error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "daemon":
            from .daemon import serve
            return serve()
        if args.cmd == "doctor":
            from .doctor import run
            return run()
        if args.cmd == "audio":
            return _audio(args)
        if args.cmd in ("mcp-register", "mcp-unregister"):
            from .register import register, unregister
            if args.cmd == "mcp-register":
                cmd = args.command or str(Path(sys.executable).parent / "use-computer-mcp")
                print(register(cmd))
            else:
                print(unregister())
            return 0

        from .client import Client, RemoteError

        op, payload = _request(args)
        if args.takeover:
            payload["takeover"] = True
        payload = {k: v for k, v in payload.items() if v is not None}
        client = Client(args.client or os.environ.get("USE_COMPUTER_CLIENT") or "cli",
                        autostart=op not in ("shutdown", "status", "stop"))
        started = time.monotonic()
        try:
            result = client.call(op, **payload)
        except RemoteError as exc:
            print(f"use-computer: error: {exc}", file=sys.stderr)
            return 3 if exc.kind == "ControlRevoked" else 1
        finally:
            client.close()
        if not args.quiet:
            _print_result(op, result, args)
        if os.environ.get("USE_COMPUTER_TIMING"):
            print(f"{op}: {time.monotonic() - started:.2f}s", file=sys.stderr)
        return 0
    except _UsageError as exc:
        print(f"use-computer: error: {exc}", file=sys.stderr)
        return 2
    except UseComputerError as exc:
        print(f"use-computer: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 2
