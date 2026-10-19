"""Offline tests: agent request -> per-task gateway (unix socket) -> llm-bridge -> fake upstream.

No GPU, no Docker. Covers Messages <-> Chat Completions translation (JSON and streaming, tool calls),
Anthropic pass-through, server-side tool / MCP / beta stripping, token metering and budgets, and auth.

    uv run --with aiohttp==3.14.5 --with pyyaml==6.0.3 python tests/test_gateway_bridge.py
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TMP = Path(tempfile.mkdtemp(prefix="agentbox-test-"))
UP, BR = free_port(), free_port()
(TMP / "models.yaml").write_text(f"""
paths: {{models_dir: /x, vllm_cache: /x}}
default_model: fake-openai
models:
  fake-openai:
    backend: openai
    endpoint: http://127.0.0.1:{UP}/v1
    model_name: upstream-model
    api_key_env: FAKE_KEY
    token_param: max_completion_tokens
    drop_params: [temperature]
    max_output_tokens: 4096
  fake-claude:
    backend: anthropic
    endpoint: http://127.0.0.1:{UP}
    model_name: claude-upstream
    api_key_env: FAKE_KEY
""")
os.environ.update(AGENTBOX_DATA=str(TMP), AGENTBOX_MODELS=str(TMP / "models.yaml"), FAKE_KEY="sk-test-123",
                  AGENTBOX_BRIDGE_TOKEN="b" * 32, AGENTBOX_BRIDGE_URL=f"http://127.0.0.1:{BR}")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from agentbox.bridge import Bridge  # noqa: E402
from agentbox.common import load_registry  # noqa: E402
from agentbox.gateway import Gateway  # noqa: E402

SEEN: list[tuple[str, dict, dict]] = []   # (path, headers, body) received by the fake upstream
FAILS: list[str] = []


def check(name: str, cond: bool, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- fake upstream (OpenAI + Anthropic)
async def chat(req: web.Request):
    body = await req.json()
    SEEN.append((req.path, dict(req.headers), body))
    if not body.get("stream"):
        return web.json_response({"choices": [{"finish_reason": "tool_calls", "message": {
            "content": "ok", "tool_calls": [{"id": "call_9", "type": "function",
                                             "function": {"name": "Read", "arguments": '{"path": "a.py"}'}}]}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5}})
    resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
    await resp.prepare(req)
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
        {"choices": [{"index": 0, "delta": {"content": "lo"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                             "function": {"name": "Bash", "arguments": '{"comm'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'and": "ls"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
    ]
    for c in chunks:
        await resp.write(f"data: {json.dumps(c)}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def messages(req: web.Request):
    body = await req.json()
    SEEN.append((req.path, dict(req.headers), body))
    return web.json_response({"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                              "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                              "usage": {"input_tokens": 7, "output_tokens": 3}})


async def models(req):
    return web.json_response({"data": []})


async def start(app: web.Application, port: int):
    r = web.AppRunner(app, access_log=None)
    await r.setup()
    await web.TCPSite(r, "127.0.0.1", port).start()
    return r


def parse_sse(raw: bytes) -> list[dict]:
    return [json.loads(l[5:]) for l in raw.decode().splitlines() if l.startswith("data:")]


AGENT_BODY = {
    "model": "whatever-the-agent-asks-for", "max_tokens": 32000, "temperature": 1, "stream": True,
    "system": [{"type": "text", "text": "You are Claude Code."}],
    "messages": [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": [{"type": "text", "text": "Sure."},
                                          {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.py\nb.py"},
                                     {"type": "text", "text": "now read a.py"}]},
    ],
    "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
              {"type": "web_search_20260209", "name": "web_search"},
              {"type": "web_fetch_20260209", "name": "web_fetch"}],
    "tool_choice": {"type": "tool", "name": "web_fetch"},
    "mcp_servers": [{"type": "url", "url": "https://evil.example/mcp", "name": "x"}],
    "container": "cntr_1", "context_management": {"edits": []},
}


async def main():
    up = web.Application()
    up.add_routes([web.post("/v1/chat/completions", chat), web.post("/v1/messages", messages),
                   web.get("/v1/models", models)])
    r1 = await start(up, UP)
    b = Bridge()
    br = web.Application(middlewares=[b.auth])
    br.add_routes([web.post("/v1/messages", b.messages), web.get("/check/{model}", b.check)])
    r2 = await start(br, BR)
    reg = load_registry()

    async def agent_call(gw: Gateway, body: dict, token: str | None = None, path="/v1/messages", method="POST"):
        conn = aiohttp.UnixConnector(path=str(gw.sock_dir / "llm.sock"))
        async with aiohttp.ClientSession(connector=conn) as s:
            async with s.request(method, f"http://agent{path}", json=body,
                                 headers={"authorization": f"Bearer {token or gw.token}",
                                          "anthropic-version": "2023-06-01",
                                          "anthropic-beta": "mcp-client-2025-04-04,code-execution-2025-05-22"}) as r:
                return r.status, await r.read()

    print("OpenAI-compatible upstream via llm-bridge (streaming):")
    gw = Gateway(tid="t-openai", model=reg.models["fake-openai"], token_budget=10_000)
    await gw.start()
    st, raw = await agent_call(gw, json.loads(json.dumps(AGENT_BODY)))
    check("200 from gateway", st == 200, st)
    path, hdr, ob = SEEN[-1]
    check("upstream got /v1/chat/completions", path == "/v1/chat/completions")
    check("provider key injected by bridge", hdr.get("Authorization") == "Bearer sk-test-123")
    check("model mapped to upstream name", ob["model"] == "upstream-model")
    check("system message first", ob["messages"][0] == {"role": "system", "content": "You are Claude Code."})
    asst = ob["messages"][2]
    check("assistant tool_use -> tool_calls", asst["tool_calls"][0]["function"]["name"] == "Bash"
          and json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"})
    check("tool_result -> role=tool msg", ob["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1",
                                                                "content": "a.py\nb.py"})
    check("trailing user text kept", ob["messages"][4] == {"role": "user", "content": "now read a.py"})
    check("server tools stripped (only Bash left)", [t["function"]["name"] for t in ob["tools"]] == ["Bash"])
    check("tool_choice for stripped tool -> auto", ob.get("tool_choice") == "auto", ob.get("tool_choice"))
    check("token_param + output clamp", ob.get("max_completion_tokens") == 4096 and "max_tokens" not in ob)
    check("drop_params removed temperature", "temperature" not in ob)
    check("stream usage requested", ob.get("stream_options") == {"include_usage": True})
    check("no MCP/container fields upstream", not {"mcp_servers", "container", "context_management"} & set(ob))
    evs = parse_sse(raw)
    text = "".join(e["delta"]["text"] for e in evs if e.get("type") == "content_block_delta"
                   and e["delta"]["type"] == "text_delta")
    check("text deltas reassembled", text == "Hello", text)
    tstart = [e for e in evs if e.get("type") == "content_block_start" and e["content_block"]["type"] == "tool_use"]
    check("tool_use block started", tstart and tstart[0]["content_block"]["name"] == "Bash")
    args = "".join(e["delta"]["partial_json"] for e in evs if e.get("type") == "content_block_delta"
                   and e["delta"]["type"] == "input_json_delta")
    check("tool args reassembled", json.loads(args) == {"command": "ls"}, args)
    md = [e for e in evs if e.get("type") == "message_delta"][0]
    check("stop_reason tool_use", md["delta"]["stop_reason"] == "tool_use")
    check("event order ends with message_stop", evs[-1]["type"] == "message_stop" and evs[0]["type"] == "message_start")
    check("gateway metered 100 in / 20 out", (gw.usage.input_tokens, gw.usage.output_tokens) == (100, 20),
          gw.usage.as_dict())

    print("Non-streaming:")
    body = json.loads(json.dumps(AGENT_BODY)); body["stream"] = False
    st, raw = await agent_call(gw, body)
    msg = json.loads(raw)
    check("JSON message with text + tool_use", [c["type"] for c in msg["content"]] == ["text", "tool_use"]
          and msg["content"][1]["input"] == {"path": "a.py"} and msg["stop_reason"] == "tool_use", msg)
    check("usage accumulated", (gw.usage.input_tokens, gw.usage.output_tokens) == (150, 25), gw.usage.as_dict())

    print("Gateway guards:")
    st, _ = await agent_call(gw, AGENT_BODY, token="wrong-token-" + "x" * 20)
    check("wrong agent token -> 401", st == 401, st)
    st, _ = await agent_call(gw, {}, path="/v1/models", method="GET")
    check("other endpoints -> 404", st == 404, st)
    st, raw = await agent_call(gw, {"messages": [{"role": "user", "content": "x" * 400}]}, path="/v1/messages/count_tokens")
    check("count_tokens answered locally", st == 200 and json.loads(raw)["input_tokens"] > 0)
    gw.token_budget = 100
    n = len(SEEN)
    st, raw = await agent_call(gw, AGENT_BODY)
    check("budget exhausted -> 403, not forwarded", st == 403 and len(SEEN) == n, (st, raw[:100]))
    await gw.stop()

    print("Anthropic pass-through via llm-bridge:")
    gw2 = Gateway(tid="t-claude", model=reg.models["fake-claude"])
    await gw2.start()
    body = json.loads(json.dumps(AGENT_BODY)); body["stream"] = False
    st, raw = await agent_call(gw2, body)
    path, hdr, ab = SEEN[-1]
    check("200 + forwarded to /v1/messages", st == 200 and path == "/v1/messages", (st, path))
    check("x-api-key injected, agent token not forwarded", hdr.get("x-api-key") == "sk-test-123"
          and gw2.token not in json.dumps(hdr))
    check("no anthropic-beta header upstream", "anthropic-beta" not in {k.lower() for k in hdr})
    check("server tools stripped", [t["name"] for t in ab["tools"]] == ["Bash"])
    check("mcp_servers/container/context_management stripped",
          not {"mcp_servers", "container", "context_management"} & set(ab))
    check("model mapped", ab["model"] == "claude-upstream")
    check("usage metered", (gw2.usage.input_tokens, gw2.usage.output_tokens) == (7, 3), gw2.usage.as_dict())
    await gw2.stop()

    print("llm-bridge guards:")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{BR}/v1/messages", json={"model": "fake-openai"}) as r:
            check("bridge without token -> 401", r.status == 401, r.status)
        async with s.get(f"http://127.0.0.1:{BR}/check/fake-openai",
                         headers={"authorization": "Bearer " + "b" * 32}) as r:
            check("bridge /check ok", (await r.json())["ok"] is True)
        os.environ.pop("FAKE_KEY")
        async with s.get(f"http://127.0.0.1:{BR}/check/fake-openai",
                         headers={"authorization": "Bearer " + "b" * 32}) as r:
            res = await r.json()
            check("missing key reported", res["ok"] is False and "FAKE_KEY" in res["error"], res)
    await r1.cleanup()
    await r2.cleanup()
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
