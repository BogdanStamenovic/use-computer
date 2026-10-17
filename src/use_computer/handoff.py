"""Hand a login to the user: they type a secret on a web page, it lands in the desktop.

The agent gets the desktop to a login form and focuses the password (or 2FA) field,
then starts a handoff (`use-computer handoff`, or the MCP `handoff` tool). The page shows
a fresh screenshot of the desktop so the user can see which field their text goes into;
on submit the text is typed with `type(secret=True)`, which refuses unless an app's
password field has focus, never leaves the secret in the clipboard, and reports nothing
about the content. The agent's own context never holds the secret.

`call` is the caller's way of reaching the daemon, so the handoff runs under the
caller's control lease (the CLI's client id, or the MCP server's) instead of
competing with it as a second driver.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import secretpage

Call = Callable[..., dict[str, Any]]


def page(prompt: str, desktop_label: str, call: Call, enter: bool = True,
         visible: bool = False) -> secretpage.Page:
    def preface() -> str:
        try:
            shot = call("screenshot", format="jpeg")
        except Exception as exc:  # the page still works without the picture
            return f'<p class="where">(screenshot unavailable: {exc})</p>'
        return (f'<img alt="Current screen of the desktop" '
                f'src="data:{shot["mime"]};base64,{shot["image"]}">')

    def submit(secret: str, flags: dict[str, bool]) -> tuple[bool, str]:
        call("type", text=secret, secret=True, visible=visible)
        if flags.get("enter"):
            call("key", text="Return")
        return True, "Typed into the desktop. You can close this page."

    return secretpage.Page(
        title="Log in on the agent's desktop", where=f"archserver - {desktop_label}",
        prompt=prompt, on_submit=submit, preface=preface,
        input_type="text" if visible else "password",
        placeholder="Code" if visible else "Password",
        options={"enter": ("Press Enter afterwards", enter)})


def serve(prompt: str, desktop_label: str, call: Call, host: str | None = None,
          timeout: float = 600.0, enter: bool = True, visible: bool = False,
          qr_path: Path | None = None) -> bool:
    p = page(prompt, desktop_label, call, enter=enter, visible=visible)
    return secretpage.serve(p, host=host, timeout=timeout, qr_path=qr_path)
