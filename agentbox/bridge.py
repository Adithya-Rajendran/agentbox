"""llm-bridge: the only agentbox component that holds provider API keys and talks to remote LLM APIs.

Agents have no network. Their per-task gateway (in the agentbox container) forwards model traffic here as
Anthropic Messages requests addressed to an agentbox model id. The bridge looks the id up in models.yaml
and either
  - backend: openai    -> translates to Chat Completions for the OpenAI API or any OpenAI-compatible server
                          (llama.cpp, LM Studio, SGLang, TGI, a remote vLLM, OpenRouter, Groq, ...), and
                          translates the JSON/SSE response back; or
  - backend: anthropic -> passes the request through to the Anthropic API (or any /v1/messages endpoint).

Before anything leaves, server-side tools (web search/fetch, code execution), MCP servers, hosted containers
and beta headers are stripped: the remote API must never fetch, search or connect on the agent's behalf.

Keys come from the bridge's own environment (secrets.env); agentbox and the agents never see them.
Every request must carry `Authorization: Bearer $AGENTBOX_BRIDGE_TOKEN`.

  POST /v1/messages         {model: <agentbox id>, ...Anthropic Messages body}
  GET  /check/{model}       key configured? upstream reachable?
  GET  /health
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets

import aiohttp
from aiohttp import web

from .common import BRIDGE_TOKEN, load_registry
from .openai_compat import request_to_openai, response_from_openai, sanitize_request, stream_from_openai

log = logging.getLogger("agentbox.bridge")
ERR_KIND = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error"}


def _err(status: int, kind: str, msg: str) -> web.Response:
    return web.json_response({"type": "error", "error": {"type": kind, "message": msg}}, status=status)


def _ev(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


class Bridge:
    def __init__(self):
        self.reg = load_registry()
        self.token = f"Bearer {BRIDGE_TOKEN}".encode()
        self._s: aiohttp.ClientSession | None = None

    @property
    def s(self) -> aiohttp.ClientSession:
        if self._s is None:
            self._s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20,
                                                                          sock_read=1800))
        return self._s

    @web.middleware
    async def auth(self, req: web.Request, handler):
        if req.path != "/health" and not secrets.compare_digest(
                req.headers.get("authorization", "").encode(), self.token):
            return _err(401, "authentication_error", "bad bridge token")
        return await handler(req)

    def _model(self, mid: str):
        m = self.reg.models.get(mid)
        if not m or not m.external:
            raise web.HTTPNotFound(text=f"{mid!r} is not an external (openai/anthropic) model")
        return m

    async def health(self, req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "models": [m for m, v in self.reg.models.items() if v.external]})

    async def check(self, req: web.Request) -> web.Response:
        m = self._model(req.match_info["model"])
        if m.api_key_env and not m.api_key:
            return web.json_response({"ok": False, "error": f"{m.api_key_env} is not set in secrets.env"})
        if m.backend == "openai":
            url, headers = f"{m.upstream_url}/models", {"authorization": f"Bearer {m.api_key}"} if m.api_key else {}
        else:
            url = f"{m.upstream_url}/v1/models"
            headers = {"x-api-key": m.api_key or "", "anthropic-version": "2023-06-01"}
        try:
            async with self.s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status in (401, 403):
                    return web.json_response({"ok": False, "error": f"upstream rejected the key ({r.status})"})
                if r.status >= 500:
                    return web.json_response({"ok": False, "error": f"upstream error {r.status}"})
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return web.json_response({"ok": False, "error": f"cannot reach {m.upstream_url}: {e}"})
        return web.json_response({"ok": True, "upstream": m.upstream_url})   # 404: no /models, still fine

    async def messages(self, req: web.Request) -> web.StreamResponse:
        body = sanitize_request(await req.json())   # again here: the bridge is the egress point
        m = self._model(str(body.get("model", "")))
        if m.api_key_env and not m.api_key:
            return _err(500, "api_error", f"bridge: {m.api_key_env} is not set in secrets.env")
        try:
            if m.backend == "openai":
                return await self._openai(req, m, body)
            return await self._anthropic(req, m, body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("%s upstream unavailable: %s", m.id, e)
            return _err(529, "overloaded_error", f"upstream unavailable: {e}")

    # ------------------------------------------------------------------ anthropic pass-through
    async def _anthropic(self, req: web.Request, m, body: dict) -> web.StreamResponse:
        body["model"] = m.model_name
        # No anthropic-beta header: betas turn on server-side tools that could reach the network.
        headers = {"content-type": "application/json",
                   "anthropic-version": req.headers.get("anthropic-version", "2023-06-01")}
        if m.api_key:
            headers["x-api-key"] = m.api_key
        async with self.s.post(f"{m.upstream_url}/v1/messages", json=body, headers=headers) as up:
            if up.status >= 400 or "text/event-stream" not in up.headers.get("content-type", ""):
                data = await up.read()
                if up.status >= 400:
                    log.warning("%s anthropic upstream -> %s %s", m.id, up.status, data[:300])
                return web.Response(status=up.status, body=data, content_type="application/json")
            resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream",
                                                           "cache-control": "no-cache"})
            await resp.prepare(req)
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            return resp

    # ------------------------------------------------------------------ openai translation
    async def _openai(self, req: web.Request, m, body: dict) -> web.StreamResponse:
        oreq = request_to_openai(body, m.model_name, m.max_output_tokens, m.extra_body, m.token_param,
                                 m.drop_params)
        headers = {"content-type": "application/json"}
        if m.api_key:
            headers["authorization"] = f"Bearer {m.api_key}"
        async with self.s.post(f"{m.upstream_url}/chat/completions", json=oreq, headers=headers) as up:
            if up.status >= 400:
                text = (await up.text())[:500]
                log.warning("%s openai upstream -> %s %s", m.id, up.status, text[:300])
                return _err(up.status if up.status in ERR_KIND or up.status >= 500 else 502,
                            ERR_KIND.get(up.status, "api_error"), f"upstream: {text}")
            if "text/event-stream" not in up.headers.get("content-type", ""):
                out = response_from_openai(await up.json(content_type=None), m.id)
                return web.json_response(out) if not body.get("stream") else await self._replay(req, out)
            resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream",
                                                           "cache-control": "no-cache"})
            await resp.prepare(req)

            async def lines():
                buf = b""
                async for chunk in up.content.iter_any():
                    buf += chunk
                    *done, buf = buf.split(b"\n")
                    for line in done:
                        yield line
                if buf:
                    yield buf

            async for ev in stream_from_openai(lines(), m.id):
                await resp.write(ev)
            await resp.write_eof()
            return resp

    async def _replay(self, req: web.Request, msg: dict) -> web.StreamResponse:
        """The client asked to stream but the upstream answered with plain JSON: emit it as SSE events."""
        resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream"})
        await resp.prepare(req)
        await resp.write(_ev("message_start", {"type": "message_start", "message": {
            **msg, "content": [], "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}))
        for i, b in enumerate(msg["content"]):
            start = {**b, "text": ""} if b["type"] == "text" else {**b, "input": {}}
            await resp.write(_ev("content_block_start", {"type": "content_block_start", "index": i,
                                                         "content_block": start}))
            delta = ({"type": "text_delta", "text": b["text"]} if b["type"] == "text"
                     else {"type": "input_json_delta", "partial_json": json.dumps(b["input"])})
            await resp.write(_ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                         "delta": delta}))
            await resp.write(_ev("content_block_stop", {"type": "content_block_stop", "index": i}))
        await resp.write(_ev("message_delta", {"type": "message_delta", "usage": msg["usage"],
                                               "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None}}))
        await resp.write(_ev("message_stop", {"type": "message_stop"}))
        await resp.write_eof()
        return resp


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if len(BRIDGE_TOKEN) < 24:
        raise SystemExit("AGENTBOX_BRIDGE_TOKEN (>= 24 chars) is required")
    b = Bridge()
    app = web.Application(client_max_size=64 << 20, middlewares=[b.auth])
    app.add_routes([web.post("/v1/messages", b.messages), web.get("/check/{model}", b.check),
                    web.get("/health", b.health)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host = os.environ.get("AGENTBOX_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENTBOX_BRIDGE_PORT", "8790"))
    await web.TCPSite(runner, host, port).start()
    ext = [f"{k}->{v.backend}:{v.upstream_url}" for k, v in b.reg.models.items() if v.external]
    log.info("llm-bridge on %s:%s  routes: %s", host, port, ", ".join(ext) or "(none configured)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
