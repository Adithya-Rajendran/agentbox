"""A tiny fake OpenAI-compatible model server, for testing agentbox end to end without a GPU.

    uv run --with aiohttp==3.14.5 python tests/fake_model.py [--port 18080]

Point a `backend: openai` model at http://127.0.0.1:18080/v1. Behaviour, per request:
  - tools offered and no tool result yet  -> calls the Write tool: /workspace/hello.txt = "hello from the fake model"
  - otherwise                             -> replies "Done." and stops
Supports streaming (stream: true) and plain JSON, like a real server.
"""
from __future__ import annotations

import argparse
import json
import time

from aiohttp import web


def decide(body: dict) -> dict:
    msgs = body.get("messages", [])
    has_tool_result = any(m.get("role") == "tool" for m in msgs)
    tool_names = {t.get("function", {}).get("name") for t in body.get("tools") or []}
    if "Write" in tool_names and not has_tool_result:
        args = {"file_path": "/workspace/hello.txt", "content": "hello from the fake model\n"}
        return {"tool_call": {"id": f"call_{int(time.time() * 1000)}", "name": "Write",
                              "arguments": json.dumps(args)}}
    return {"text": "Done."}


async def chat(req: web.Request) -> web.StreamResponse:
    body = await req.json()
    d = decide(body)
    usage = {"prompt_tokens": 100, "completion_tokens": 10}
    if not body.get("stream"):
        msg = {"role": "assistant", "content": d.get("text")}
        if "tool_call" in d:
            tc = d["tool_call"]
            msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                                  "function": {"name": tc["name"], "arguments": tc["arguments"]}}]
        return web.json_response({"choices": [{"index": 0, "message": msg,
                                               "finish_reason": "tool_calls" if "tool_call" in d else "stop"}],
                                  "usage": usage})
    resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
    await resp.prepare(req)

    async def send(obj):
        await resp.write(f"data: {json.dumps(obj)}\n\n".encode())

    if "tool_call" in d:
        tc = d["tool_call"]
        await send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": tc["id"], "type": "function",
                    "function": {"name": tc["name"], "arguments": ""}}]}}]})
        await send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {
                    "arguments": tc["arguments"]}}]}}]})
        await send({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    else:
        await send({"choices": [{"index": 0, "delta": {"role": "assistant", "content": d["text"]}}]})
        await send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    await send({"choices": [], "usage": usage})
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def models(req: web.Request) -> web.Response:
    return web.json_response({"object": "list", "data": [{"id": "fake", "object": "model"}]})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    a = ap.parse_args()
    app = web.Application(client_max_size=64 << 20)
    app.add_routes([web.post("/v1/chat/completions", chat), web.get("/v1/models", models)])
    web.run_app(app, host="127.0.0.1", port=a.port, print=None)
