"""Drive the MCP server over stdio like Claude Code does. Run inside the headless env.

usage: python tests/e2e/mcp_smoke.py OUTDIR
"""

from __future__ import annotations

import base64
import os
import sys
import time

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main(outdir: str) -> int:
    server = StdioServerParameters(command=os.path.join(os.path.dirname(sys.executable),
                                                        "use-computer-mcp"), env=dict(os.environ))
    failures = 0
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("tools:", names)

        async def call(name: str, **args):
            nonlocal failures
            t = time.monotonic()
            r = await session.call_tool(name, args)
            took = time.monotonic() - t
            kinds = []
            for b in r.content:
                if b.type == "text":
                    kinds.append("text:" + b.text[:300].replace("\n", " | "))
                else:
                    raw = base64.b64decode(b.data)
                    path = os.path.join(outdir, f"mcp-{name}-{int(t * 1000)}.{b.mime_type.split('/')[1]}")
                    open(path, "wb").write(raw)
                    kinds.append(f"{b.type}:{b.mime_type}:{len(raw)}B->{path}")
            if r.is_error:
                failures += 1
            print(f"{name}{args} [{took:.2f}s] error={r.is_error}\n   " + "\n   ".join(kinds))
            return r

        await call("session", action="status")
        await call("computer", action="screenshot")
        await call("windows")
        await call("read_screen", scope="all")
        await call("find", query="Enable thing")
        await call("computer", action="zoom", region=[400, 200, 880, 480])
        await call("computer_batch", actions=[
            {"name": "computer", "input": {"action": "key", "text": "ctrl+a"}},
            {"name": "computer", "input": {"action": "wait", "duration": 0.2}},
            {"name": "computer", "input": {"action": "screenshot"}},
        ])
        await call("ocr", region=[400, 200, 880, 480])
        await call("clipboard", action="set", text="from mcp")
        await call("clipboard", action="get")
        r = await call("computer", action="left_click")  # missing coordinate -> error expected
        failures -= 1 if r.is_error else 0
    print("failures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main, sys.argv[1]))
