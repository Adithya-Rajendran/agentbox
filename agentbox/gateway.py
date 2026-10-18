"""Per-task LLM gateway: an Anthropic Messages proxy on a unix socket (replaces LiteLLM + socat gw).

One gateway per delegation, listening on SOCKETS/<tid>/llm.sock, which the launcher bind-mounts into
that agent only (agents cannot reach each other's sockets). It
  - accepts only POST /v1/messages and /v1/messages/count_tokens, with the task's one-time token;
  - pins the model: whatever the agent asks for, the request goes to this task's model;
  - answers count_tokens locally (Ollama lacks it; vLLM's is not needed for an estimate);
  - meters input/output tokens from the (streamed) responses and enforces a per-task token budget;
  - never forwards the agent's credentials or arbitrary headers to the backend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from .common import SOCKETS, Model

log = logging.getLogger("agentbox.gateway")
FORWARD_HEADERS = ("anthropic-version", "anthropic-beta")
# Request fields some backends reject; dropped before forwarding (keyed by backend kind).
STRIP_FIELDS = {"ollama": ("context_management", "container", "mcp_servers"),
                "vllm": ("context_management", "container", "mcp_servers")}


def _err(status: int, kind: str, msg: str) -> web.Response:
    return web.json_response({"type": "error", "error": {"type": kind, "message": msg}}, status=status)


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: int = 0
    last_error: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Gateway:
    tid: str
    model: Model
    token_budget: int = 20_000_000
    token: str = field(default_factory=lambda: "abx-" + secrets.token_urlsafe(24))
    usage: Usage = field(default_factory=Usage)
    _runner: web.AppRunner | None = None
    _session: aiohttp.ClientSession | None = None

    @property
    def sock_dir(self) -> Path:
        return SOCKETS / self.tid

    async def start(self) -> None:
        self.sock_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.sock_dir, 0o711)   # the agent runs as a different uid; the token is the gate
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=1800))
        app = web.Application(client_max_size=64 << 20)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        sock = self.sock_dir / "llm.sock"
        sock.unlink(missing_ok=True)
        await web.UnixSite(self._runner, str(sock)).start()
        os.chmod(sock, 0o666)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        if self._session:
            await self._session.close()
        for p in (self.sock_dir / "llm.sock", self.sock_dir):
            try:
                p.unlink() if p.is_socket() else p.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------ request path
    def _authorized(self, req: web.Request) -> bool:
        auth = req.headers.get("authorization", "")
        presented = auth[7:] if auth.lower().startswith("bearer ") else req.headers.get("x-api-key", "")
        return secrets.compare_digest(presented.encode(), self.token.encode())

    async def handle(self, req: web.Request) -> web.StreamResponse:
        if req.method != "POST" or req.path not in ("/v1/messages", "/v1/messages/count_tokens"):
            return _err(404, "not_found_error", f"{req.method} {req.path} is not available to agents")
        if not self._authorized(req):
            return _err(401, "authentication_error", "bad agent token")
        try:
            body = await req.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _err(400, "invalid_request_error", "body must be JSON")
        if req.path.endswith("/count_tokens"):
            approx = len(json.dumps([body.get("system"), body.get("messages"), body.get("tools")])) // 4
            return web.json_response({"input_tokens": approx})
        used = self.usage.input_tokens + self.usage.output_tokens
        if used >= self.token_budget:
            # 403 is not retried by Claude Code, so the agent stops instead of hammering the budget.
            return _err(403, "permission_error", f"agentbox token budget ({self.token_budget}) exhausted")

        body["model"] = self.model.served_name
        for k in STRIP_FIELDS.get(self.model.backend, ()):
            body.pop(k, None)
        headers = {"content-type": "application/json", "x-api-key": "agentbox"}
        headers.update({h: req.headers[h] for h in FORWARD_HEADERS if h in req.headers})
        self.usage.requests += 1
        url = f"{self.model.base_url}/v1/messages"
        try:
            async with self._session.post(url, json=body, headers=headers) as up:
                if up.status >= 400 or "text/event-stream" not in up.headers.get("content-type", ""):
                    data = await up.read()
                    if up.status >= 400:
                        self.usage.errors += 1
                        self.usage.last_error = f"{up.status}: {data[:300]!r}"
                        log.warning("[%s] backend %s -> %s", self.tid, url, self.usage.last_error)
                    else:
                        u = json.loads(data).get("usage") or {}
                        self._add_input(u)
                        self._add_output(u)
                    return web.Response(status=up.status, body=data,
                                        content_type=up.content_type or "application/json")
                resp = web.StreamResponse(status=up.status, headers={"content-type": "text/event-stream",
                                                                     "cache-control": "no-cache"})
                await resp.prepare(req)
                buf = b""
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        self._sse_line(line)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.usage.errors += 1
            self.usage.last_error = f"{type(e).__name__}: {e}"
            return _err(529, "overloaded_error", f"backend unavailable: {e}")

    def _sse_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        try:
            ev = json.loads(line[5:])
        except ValueError:
            return
        # input is reported once in message_start; message_delta carries the final cumulative output count.
        if ev.get("type") == "message_start":
            self._add_input((ev.get("message") or {}).get("usage") or {})
        elif ev.get("type") == "message_delta":
            self._add_output(ev.get("usage") or {})

    def _add_input(self, u: dict) -> None:
        self.usage.input_tokens += int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)

    def _add_output(self, u: dict) -> None:
        self.usage.output_tokens += int(u.get("output_tokens") or 0)
