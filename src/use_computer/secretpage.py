"""A single-use web page for the user to type a secret the agent must never see.

Agents do not type passwords, and they should not even hold one in their context. So
when a secret is needed (the keyring password, a site login, a 2FA code) the agent runs
a command that serves this page; the user opens it on their phone or laptop, types the
secret, and `on_submit` consumes it inside this process. The agent only learns whether
it worked.

Exposure: bound to the tailnet IP by default so the user's phone reaches it. Tailscale
traffic is WireGuard-encrypted, so plain HTTP does not put the secret on the wire in
clear. The tailnet is shared with other people's devices, which is what the 192-bit
token in the path is for; every other path is a bare 404. The link dies after one
success, after MAX_ATTEMPTS failures, or at the timeout.
"""

from __future__ import annotations

import html
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

MAX_ATTEMPTS = 5


def default_host() -> str:
    """The tailnet IPv4 if Tailscale is up (so a phone can reach it), else loopback."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                             timeout=5, check=False).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        out = []
    return out[0] if out else "127.0.0.1"


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class Page:
    title: str
    where: str
    prompt: str
    # Consumes the secret. Returns (ok, message shown to the user). Must not log it.
    on_submit: Callable[[str, dict[str, bool]], tuple[bool, str]]
    input_type: str = "password"
    placeholder: str = "Password"
    # Extra HTML above the form, re-rendered per GET (e.g. a live screenshot).
    preface: Callable[[], str] | None = None
    # name -> (label, default) checkboxes passed to on_submit as flags.
    options: dict[str, tuple[str, bool]] = field(default_factory=dict)


_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title><style>
:root{{--bg:#f6f6f4;--fg:#1b1b1b;--muted:#6b6b6b;--card:#fff;--line:#d9d9d4;--accent:#2f5d50;--bad:#a33}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151615;--fg:#ececea;--muted:#9a9a96;--card:#1e201f;--line:#333;--accent:#7fbfa9;--bad:#e88}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif;
display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}}
main{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;
width:100%;max-width:520px;box-sizing:border-box}}
h1{{font-size:18px;margin:0 0 4px}}.where{{color:var(--muted);margin:0 0 12px;font-size:13px}}
.prompt{{margin:0 0 16px}}img{{width:100%;border:1px solid var(--line);border-radius:8px;margin:0 0 16px}}
input[type=password],input[type=text],button{{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border-radius:8px}}
input[type=password],input[type=text]{{border:1px solid var(--line);background:var(--bg);color:var(--fg);margin-bottom:12px}}
label{{display:block;font-size:14px;margin:0 0 12px;color:var(--muted)}}
button{{border:0;background:var(--accent);color:var(--bg);font-weight:600;cursor:pointer}}
.msg{{margin:0 0 12px;font-size:14px}}.bad{{color:var(--bad)}}.ok{{color:var(--accent)}}
</style></head><body><main>
<h1>{title}</h1><p class="where">{where}</p>{body}</main></body></html>"""


def _qr(url: str, out: Path) -> str | None:
    """Write a QR PNG of the URL and return a terminal rendering. None if qrencode is absent."""
    if not shutil.which("qrencode"):
        return None
    # The QR carries the live token: create it owner-only before qrencode writes it.
    out.touch(mode=0o600, exist_ok=True)
    out.chmod(0o600)
    subprocess.run(["qrencode", "-o", str(out), "-s", "8", "-m", "2", url], check=False)
    term = subprocess.run(["qrencode", "-t", "UTF8", "-m", "1", url], capture_output=True,
                          text=True, check=False)
    return term.stdout if term.returncode == 0 else None


def serve(page: Page, host: str | None = None, port: int = 0, timeout: float = 600.0,
          qr_path: Path | None = None, log: Callable[[str], None] = _stderr) -> bool:
    """Serve the page until a submit succeeds (True), or attempts/time run out (False).

    Prints the URL as its first stderr line, `open: <url>`, so a caller running this in
    the background can grep it and hand it to the user.
    """
    host = host or default_host()
    token = secrets.token_urlsafe(24)
    state = {"done": False, "ok": False, "attempts": 0}
    lock = threading.Lock()
    esc = html.escape

    def form(msg: str = "") -> str:
        pre = page.preface() if page.preface else ""
        opts = "".join(
            f'<label><input type="checkbox" name="{esc(k)}"{" checked" if d else ""}> {esc(lbl)}</label>'
            for k, (lbl, d) in page.options.items())
        return (f'{pre}<p class="prompt">{esc(page.prompt)}</p>{msg}'
                f'<form method="post" autocomplete="off">'
                f'<input type="{page.input_type}" name="secret" autofocus required '
                f'autocomplete="off" aria-label="{esc(page.placeholder)}" '
                f'placeholder="{esc(page.placeholder)}">{opts}'
                f'<button type="submit">Submit</button></form>')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass  # the default logger would print request lines; nothing here is worth it

        def _send(self, code: int, body: str) -> None:
            data = _HTML.format(title=esc(page.title), where=esc(page.where), body=body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not secrets.compare_digest(self.path.split("?")[0], f"/{token}"):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            if state["done"]:
                self._send(410, '<p class="msg">This link is closed.</p>')
                return False
            return True

        def do_GET(self) -> None:
            if self._authorized():
                self._send(200, form())

        def do_POST(self) -> None:
            if not self._authorized():
                return
            n = min(int(self.headers.get("Content-Length") or 0), 8192)
            fields = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            secret = fields.get("secret", [""])[0]
            flags = {k: k in fields for k in page.options}
            with lock:  # one submit at a time; a double-tap must not type twice
                if state["done"]:
                    self._send(410, '<p class="msg">This link is closed.</p>')
                    return
                state["attempts"] += 1
                try:
                    ok, msg = page.on_submit(secret, flags)
                except Exception as exc:  # report, keep the link alive for a retry
                    ok, msg = False, f"Failed: {exc}"
                del secret
                if ok:
                    state["done"] = state["ok"] = True
                    log(f"submitted: {msg}")
                    self._send(200, f'<p class="msg ok">{esc(msg)}</p>')
                    return
                left = MAX_ATTEMPTS - state["attempts"]
                log(f"rejected (attempt {state['attempts']}/{MAX_ATTEMPTS}): {msg}")
                if left <= 0:
                    state["done"] = True
                    self._send(403, f'<p class="msg bad">{esc(msg)} No tries left; link closed.</p>')
                    return
                self._send(401, form(f'<p class="msg bad">{esc(msg)} {left} tries left.</p>'))

    srv = ThreadingHTTPServer((host, port), Handler)
    url_host = f"[{host}]" if ":" in host else host
    url = f"http://{url_host}:{srv.server_address[1]}/{token}"
    log(f"open: {url}")
    if qr_path is not None:
        term = _qr(url, qr_path)
        if term is not None:
            log(f"qr: {qr_path}")
            log(term)
        else:
            log("qr: unavailable (install qrencode)")
    log(f"(single use, {timeout / 60:g} min, {MAX_ATTEMPTS} tries)")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    end = time.monotonic() + timeout
    try:
        while not state["done"] and time.monotonic() < end:
            time.sleep(0.2)
        time.sleep(0.5)  # let the final response flush before the socket closes
    finally:
        srv.shutdown()
        srv.server_close()
        if qr_path is not None:
            qr_path.unlink(missing_ok=True)
    if not state["done"]:
        log("timed out; link closed")
    return bool(state["ok"])
