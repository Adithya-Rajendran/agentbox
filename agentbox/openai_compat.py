"""Anthropic Messages <-> OpenAI Chat Completions translation, so agents can use any OpenAI-compatible
endpoint (llama.cpp server, LM Studio, SGLang, TGI, a remote vLLM, hosted APIs).

Claude Code only speaks the Anthropic Messages API. The gateway converts each request to
/chat/completions and converts the response (JSON or SSE stream) back, including tool calls.
Thinking/reasoning content is dropped (Anthropic thinking blocks need signatures the upstream can't make).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

# Request fields that make the *server* act on the agent's behalf (fetch URLs, connect to MCP servers, run
# code in a hosted container) - each would be a network channel out of a network-less sandbox.
SERVER_SIDE_FIELDS = ("mcp_servers", "container", "context_management")


def sanitize_request(body: dict) -> dict:
    """Keep only client-side (custom) tools and drop server-side features, so a model API can never fetch,
    search or connect anywhere for the agent. Applied by the gateway and again by llm-bridge."""
    for k in SERVER_SIDE_FIELDS:
        body.pop(k, None)
    tools = body.get("tools")
    if tools is not None:
        kept = [t for t in tools if isinstance(t, dict) and t.get("type") in (None, "custom") and "input_schema" in t]
        if kept:
            body["tools"] = kept
        else:
            body.pop("tools", None)
            body.pop("tool_choice", None)
        names = {t.get("name") for t in kept}
        tc = body.get("tool_choice")
        if isinstance(tc, dict) and tc.get("type") == "tool" and tc.get("name") not in names:
            body["tool_choice"] = {"type": "auto"}
    return body


STOP_REASON = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
               "function_call": "tool_use", "content_filter": "end_turn"}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "image":
            parts.append("[image omitted]")
    return "\n".join(parts)


def _image_part(b: dict) -> dict | None:
    src = b.get("source") or {}
    if src.get("type") == "base64":
        return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type')};base64,{src.get('data')}"}}
    if src.get("type") == "url":
        return {"type": "image_url", "image_url": {"url": src.get("url")}}
    return None


def request_to_openai(body: dict, model_name: str, max_output_tokens: int | None = None,
                      extra_body: dict | None = None, token_param: str = "max_tokens",
                      drop_params: list[str] | tuple = ()) -> dict:
    msgs: list[dict] = []
    system = body.get("system")
    if system:
        msgs.append({"role": "system", "content": _text_of(system)})
    for m in body.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in content if b.get("type") == "tool_use"]
            out: dict = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = calls
            msgs.append(out)
            continue
        # user turn: tool results become `tool` messages (they must directly follow the assistant turn),
        # remaining text/images become one user message after them.
        rest: list[dict] = []
        for b in content:
            t = b.get("type")
            if t == "tool_result":
                txt = _text_of(b.get("content"))
                if b.get("is_error"):
                    txt = f"[tool error]\n{txt}"
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"), "content": txt})
            elif t == "text":
                rest.append({"type": "text", "text": b.get("text", "")})
            elif t == "image" and (p := _image_part(b)):
                rest.append(p)
        if rest:
            only_text = all(p["type"] == "text" for p in rest)
            msgs.append({"role": "user",
                         "content": "\n".join(p["text"] for p in rest) if only_text else rest})

    req: dict = {"model": model_name, "messages": msgs}
    mt = body.get("max_tokens")
    if mt:
        req[token_param] = min(mt, max_output_tokens) if max_output_tokens else mt
    for k in ("temperature", "top_p"):
        if k in body and k not in drop_params:
            req[k] = body[k]
    if body.get("stop_sequences"):
        req["stop"] = body["stop_sequences"]
    if body.get("tools"):
        req["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in body["tools"] if t.get("name")]
        tc = body.get("tool_choice") or {}
        req["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}.get(tc.get("type"), "auto")
        if tc.get("type") == "tool":
            req["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
    if body.get("stream"):
        req["stream"] = True
        req["stream_options"] = {"include_usage": True}
    if extra_body:
        req.update(extra_body)
    return req


def _parse_args(s: str) -> dict:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        return {"_raw_arguments": s}


def response_from_openai(data: dict, model_id: str) -> dict:
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    blocks: list[dict] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        f = c.get("function") or {}
        blocks.append({"type": "tool_use", "id": c.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                       "name": f.get("name", ""), "input": _parse_args(f.get("arguments", ""))})
    stop = STOP_REASON.get(ch.get("finish_reason"), "end_turn")
    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    u = data.get("usage") or {}
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model_id,
            "content": blocks or [{"type": "text", "text": ""}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def stream_from_openai(lines: AsyncIterator[bytes], model_id: str, on_usage=None) -> AsyncIterator[bytes]:
    """Turn an OpenAI chat.completion.chunk SSE stream into Anthropic message stream events.
    on_usage(prompt_tokens, completion_tokens) is called once at the end."""
    yield _sse("message_start", {"type": "message_start", "message": {
        "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model_id,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0}}})
    idx = -1                 # index of the open Anthropic content block
    open_kind = None         # "text" | ("tool", openai_tool_index)
    finish = None
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    saw_tool = False

    def close():
        return _sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    async for raw in lines:
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if chunk.get("usage"):
            usage.update({k: v for k, v in chunk["usage"].items() if v is not None})
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                if open_kind != "text":
                    if open_kind is not None:
                        yield close()
                    idx += 1
                    open_kind = "text"
                    yield _sse("content_block_start", {"type": "content_block_start", "index": idx,
                                                       "content_block": {"type": "text", "text": ""}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                                                   "delta": {"type": "text_delta", "text": d["content"]}})
            for tc in d.get("tool_calls") or []:
                ti = tc.get("index", 0)
                f = tc.get("function") or {}
                if open_kind != ("tool", ti):
                    if open_kind is not None:
                        yield close()
                    idx += 1
                    open_kind = ("tool", ti)
                    saw_tool = True
                    yield _sse("content_block_start", {"type": "content_block_start", "index": idx,
                               "content_block": {"type": "tool_use",
                                                 "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                                                 "name": f.get("name", ""), "input": {}}})
                if f.get("arguments"):
                    yield _sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                               "delta": {"type": "input_json_delta", "partial_json": f["arguments"]}})
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    if open_kind is not None:
        yield close()
    stop = "tool_use" if saw_tool else STOP_REASON.get(finish, "end_turn")
    if on_usage:
        on_usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
    yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                 "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                                           "output_tokens": usage.get("completion_tokens", 0)}})
    yield _sse("message_stop", {"type": "message_stop"})
