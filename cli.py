# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.3.0"]
# ///
"""Command-line client for the agentbox MCP server (scripts and benchmarks use the same path as Claude Code).

  uv run --script cli.py models
  uv run --script cli.py delegate --repo /path/to/repo --task-file spec.md \
      [--model nemotron-3.5-bolt] [--venv PATH] [--no-guide] [--timeout-min 30] [--max-turns 60]
  uv run --script cli.py check --repo /path/to/repo --branch agent/<id> --command "python -m pytest -q" [--venv PATH]
  uv run --script cli.py cleanup --repo /path/to/repo --branch agent/<id>

Token: $AGENTBOX_MCP_TOKEN, else read from .env next to this file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

URL = os.environ.get("AGENTBOX_URL", "http://127.0.0.1:8765/mcp")


def token() -> str:
    if t := os.environ.get("AGENTBOX_MCP_TOKEN"):
        return t
    for line in (Path(__file__).resolve().parent / ".env").read_text().splitlines():
        if line.startswith("AGENTBOX_MCP_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("AGENTBOX_MCP_TOKEN not set")


async def call(tool: str, args: dict, timeout_s: float) -> dict:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token()}"},
                                  timeout=httpx2.Timeout(30, read=timeout_s))
    async with http, streamable_http_client(URL, http_client=http) as (r, w), \
            ClientSession(r, w, read_timeout_seconds=timeout_s) as s:
        await s.initialize()
        res = await s.call_tool(tool, args)
        if res.structured_content is not None:
            out = res.structured_content
            return out.get("result", out) if isinstance(out, dict) and set(out) == {"result"} else out
        return json.loads(res.content[0].text)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models")
    d = sub.add_parser("delegate")
    d.add_argument("--repo", required=True)
    d.add_argument("--task-file", required=True, help="spec text file ('-' for stdin)")
    d.add_argument("--model")
    d.add_argument("--venv")
    d.add_argument("--no-guide", action="store_true")
    d.add_argument("--timeout-min", type=int, default=30)
    d.add_argument("--max-turns", type=int, default=60)
    d.add_argument("--raw", action="store_true", help="send the task text verbatim (no subagent preamble)")
    k = sub.add_parser("check", help="run a command on a branch inside the sandbox (run_checks)")
    k.add_argument("--repo", required=True)
    k.add_argument("--branch", required=True)
    k.add_argument("--command", required=True)
    k.add_argument("--venv")
    k.add_argument("--timeout-min", type=int, default=15)
    c = sub.add_parser("cleanup")
    c.add_argument("--repo", required=True)
    c.add_argument("--branch", required=True)
    a = ap.parse_args()
    if a.cmd == "models":
        out = asyncio.run(call("list_models", {}, 60))
    elif a.cmd == "check":
        args = {"repo_path": str(Path(a.repo).resolve()), "branch": a.branch, "command": a.command,
                "timeout_minutes": a.timeout_min}
        if a.venv:
            args["python_env"] = str(Path(a.venv).resolve())
        out = asyncio.run(call("run_checks", args, (a.timeout_min + 5) * 60))
    elif a.cmd == "cleanup":
        out = asyncio.run(call("cleanup_delegation", {"repo_path": a.repo, "branch": a.branch}, 60))
    else:
        task = sys.stdin.read() if a.task_file == "-" else Path(a.task_file).read_text()
        args = {"task": task, "repo_path": str(Path(a.repo).resolve()), "max_turns": a.max_turns,
                "timeout_minutes": a.timeout_min, "use_executor_guide": not a.no_guide,
                "preamble": not a.raw}
        if a.model:
            args["model"] = a.model
        if a.venv:
            args["python_env"] = str(Path(a.venv).resolve())
        # backend load (<=60 min wait) + agent run + collection
        out = asyncio.run(call("delegate_task", args, (a.timeout_min + 75) * 60))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
