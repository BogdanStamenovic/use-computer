import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from use_computer import secretpage


def _run(page, **kw):
    lines: list[str] = []
    result: dict[str, bool] = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "ok", secretpage.serve(page, host="127.0.0.1", log=lines.append, **kw)), daemon=True)
    t.start()
    for _ in range(100):
        if lines and lines[0].startswith("open: "):
            break
        time.sleep(0.02)
    return lines[0][6:], t, result, lines


def _req(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(url, body, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _page(secret="right"):
    got: list[str] = []

    def submit(value, flags):
        got.append(value)
        return (True, "done") if value == secret else (False, "nope")
    return secretpage.Page(title="t", where="w", prompt="p", on_submit=submit), got


def test_wrong_then_right_then_closed():
    page, got = _page()
    url, t, result, lines = _run(page, timeout=10)
    assert _req(re.sub(r"/[^/]+$", "/guess", url))[0] == 404
    assert _req(url)[0] == 200
    code, body = _req(url, {"secret": "wrong"})
    assert code == 401 and "4 tries left" in body
    assert _req(url, {"secret": "right"})[0] == 200
    assert _req(url)[0] == 410
    t.join(5)
    assert result["ok"] is True and got == ["wrong", "right"]
    assert not any("right" in ln or "wrong" in ln for ln in lines), "secret leaked into the log"


def test_attempts_run_out():
    page, _ = _page()
    url, t, result, _ = _run(page, timeout=10)
    codes = [_req(url, {"secret": f"x{i}"})[0] for i in range(secretpage.MAX_ATTEMPTS)]
    assert codes[-1] == 403 and set(codes[:-1]) == {401}
    t.join(5)
    assert result["ok"] is False


def test_timeout_closes():
    page, _ = _page()
    _, t, result, lines = _run(page, timeout=0.3)
    t.join(5)
    assert result["ok"] is False and any("timed out" in ln for ln in lines)
