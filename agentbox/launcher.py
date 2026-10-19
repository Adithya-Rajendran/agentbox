"""agentbox launcher - the ONLY component with the Docker socket.

It exposes a tiny API on a unix socket to the (unprivileged) MCP server and builds every container
spec itself. Clients choose *which* task/model to run, never *how*: workspace and gateway paths are
derived from the task id, the hardening flags are fixed here, the command must be `claude ...`, and
only a short allowlist of environment variables is passed through. A compromised MCP server can
therefore start sandboxed agents and the configured model servers - nothing privileged.

  POST /agents          {tid, model, cmd[], env{}, venv?, timeout_s}  -> start a sandboxed agent
  POST /checks          {tid, command, venv?, timeout_s}  -> run a command in the same sandbox, no gateway
  GET  /agents/{tid}    ?wait=1 -> {state, exit_code, stdout, stderr}  (wait blocks until exit)
  DELETE /agents/{tid}  kill + remove
  POST /backends/{model}/start      start this vLLM model (stops other vLLM models if idle; 409 if busy)
  POST /backends/vllm/stop          stop every vLLM backend (409 if any vLLM agent is running)
  GET  /status
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .common import (AGENT_GID, AGENT_IMAGE, AGENT_PREFIX, AGENT_UID, LABEL, LAUNCHER_SOCK, OWNER_GID,
                     OWNER_UID, SOCKETS, TID_RE, VENV_ROOTS, VLLM_PREFIX, WORKTREES, load_registry, under)

log = logging.getLogger("agentbox.launcher")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
MAX_TIMEOUT_S = 4 * 3600
ENV_ALLOW = {"ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "AGENT_NO_GUIDE", "MAX_THINKING_TOKENS"}
AGENT_RUNTIME = os.environ.get("AGENTBOX_AGENT_RUNTIME") or None   # e.g. runsc (gVisor) if installed
AGENT_LIMITS = {
    "memory": int(os.environ.get("AGENTBOX_AGENT_MEM_GB", "8")) << 30,
    "cpus": float(os.environ.get("AGENTBOX_AGENT_CPUS", "4")),
    "pids": int(os.environ.get("AGENTBOX_AGENT_PIDS", "512")),
}


class Docker:
    """Minimal Docker Engine API client over the unix socket."""

    def __init__(self, sock: str = DOCKER_SOCK):
        self.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=sock),
                                             timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))

    async def req(self, method: str, path: str, body=None, params=None, ok=(200, 201, 204, 304)):
        async with self.session.request(method, f"http://docker{path}", json=body, params=params) as r:
            data = await r.read()
            if r.status not in ok:
                raise web.HTTPBadGateway(text=f"docker {method} {path}: {r.status} {data[:500]!r}")
            if r.content_type == "application/json" and data:
                return json.loads(data)
            return data

    async def inspect(self, name: str) -> dict | None:
        try:
            return await self.req("GET", f"/containers/{name}/json", ok=(200,))
        except web.HTTPBadGateway:
            return None

    async def remove(self, name: str):
        await self.req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(204, 404, 409))

    async def stop(self, name: str, t: int = 60):
        await self.req("POST", f"/containers/{name}/stop", params={"t": str(t)}, ok=(204, 304, 404))

    async def ps(self, label: str) -> list[dict]:
        f = json.dumps({"label": [label]})
        return await self.req("GET", "/containers/json", params={"all": "true", "filters": f})

    async def logs(self, name: str) -> tuple[str, str]:
        raw = await self.req("GET", f"/containers/{name}/logs", params={"stdout": "1", "stderr": "1"})
        out, err, i = bytearray(), bytearray(), 0
        while i + 8 <= len(raw):  # multiplexed stream: [stream, 0,0,0, size BE32] + payload
            stream, size = raw[i], struct.unpack(">I", raw[i + 4:i + 8])[0]
            (out if stream == 1 else err).extend(raw[i + 8:i + 8 + size])
            i += 8 + size
        return out.decode(errors="replace"), err.decode(errors="replace")


class Launcher:
    def __init__(self):
        self.docker = Docker()
        self.reg = load_registry()
        self.lock = asyncio.Lock()          # serialises backend start/stop decisions
        self.deadlines: dict[str, asyncio.Task] = {}

    # ---------------------------------------------------------------- helpers
    async def running(self) -> list[dict]:
        rows = []
        for c in await self.docker.ps(LABEL):
            lab = c.get("Labels", {})
            rows.append({"name": c["Names"][0].lstrip("/"), "role": lab.get(LABEL),
                         "model": lab.get("agentbox.model"), "tid": lab.get("agentbox.tid"),
                         "state": c["State"], "status": c["Status"]})
        return rows

    async def busy_models(self) -> set[str]:
        return {r["model"] for r in await self.running() if r["role"] == "agent" and r["state"] == "running"}

    # ---------------------------------------------------------------- agents
    def _workspace(self, tid: str) -> Path:
        if not TID_RE.match(tid):
            raise web.HTTPBadRequest(text="bad tid")
        ws = (WORKTREES / tid).resolve()
        if not (under(ws, WORKTREES) and ws.is_dir()):
            raise web.HTTPBadRequest(text="workspace missing")
        st = ws.stat()
        if (st.st_uid, st.st_gid) != (OWNER_UID, OWNER_GID):
            raise web.HTTPBadRequest(text="workspace must be owned by the agentbox user")
        return ws

    @staticmethod
    def _venv(b: dict) -> tuple[list[str], list[str]]:
        if not b.get("venv"):
            return [], []
        venv = Path(b["venv"]).resolve()
        if not (any(under(venv, r) for r in VENV_ROOTS) and (venv / "bin/python").exists()):
            raise web.HTTPBadRequest(text=f"venv must be a built env under one of {VENV_ROOTS}")
        return ([f"{venv}:/opt/venv:ro"],
                ["VIRTUAL_ENV=/opt/venv", "PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"])

    async def _run_sandbox(self, tid: str, ws: Path, role: str, labels: dict, cmd: list[str],
                           env_list: list[str], binds: list[str], timeout_s: int,
                           entrypoint: list[str] | None = None) -> web.Response:
        """Start one hardened sandbox container. Agents and checks share exactly this spec."""
        uid, gid = AGENT_UID, AGENT_GID
        name = AGENT_PREFIX + tid
        await self.chown_tree(ws, uid, gid)
        spec = {
            "Image": AGENT_IMAGE,
            "Cmd": cmd,
            "Env": env_list + ["PYTHONDONTWRITEBYTECODE=1"],
            "User": f"{uid}:{gid}",
            "WorkingDir": "/workspace",
            "NetworkDisabled": True,
            "Labels": {LABEL: role, "agentbox.tid": tid, **labels},
            "HostConfig": {
                "NetworkMode": "none",            # no interface but lo
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Privileged": False,
                "Init": True,                     # tini: reap zombies left by test runners
                "IpcMode": "private",
                "PidsLimit": AGENT_LIMITS["pids"],
                "Memory": AGENT_LIMITS["memory"], "MemorySwap": AGENT_LIMITS["memory"],
                "NanoCpus": int(AGENT_LIMITS["cpus"] * 1e9),
                "Ulimits": [{"Name": "nofile", "Soft": 4096, "Hard": 4096}],
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=2g,mode=1777",
                          "/home/agent": f"rw,nosuid,nodev,size=1g,uid={uid},gid={gid},mode=0700"},
                "Binds": binds,
                "LogConfig": {"Type": "json-file", "Config": {"max-size": "20m", "max-file": "2"}},
                **({"Runtime": AGENT_RUNTIME} if AGENT_RUNTIME else {}),
            },
        }
        if entrypoint is not None:
            spec["Entrypoint"] = entrypoint
        await self.docker.remove(name)
        await self.docker.req("POST", "/containers/create", spec, params={"name": name})
        await self.docker.req("POST", f"/containers/{name}/start")
        self.deadlines[tid] = asyncio.create_task(self._deadline(tid, timeout_s))
        log.info("%s %s started ws=%s timeout=%ss", role, tid, ws, timeout_s)
        return web.json_response({"name": name, "timeout_s": timeout_s})

    async def start_agent(self, req: web.Request) -> web.Response:
        b = await req.json()
        tid, model_id = str(b.get("tid", "")), str(b.get("model", ""))
        model = self.reg.models.get(model_id)
        if not model:
            raise web.HTTPBadRequest(text="unknown model")
        cmd = b.get("cmd")
        if not (isinstance(cmd, list) and cmd and cmd[0] == "claude" and all(isinstance(a, str) for a in cmd)):
            raise web.HTTPBadRequest(text="cmd must be ['claude', ...]")
        env = {k: str(v) for k, v in (b.get("env") or {}).items()}
        if set(env) - ENV_ALLOW:
            raise web.HTTPBadRequest(text=f"env not allowed: {sorted(set(env) - ENV_ALLOW)}")
        timeout_s = min(int(b.get("timeout_s", 1800)), MAX_TIMEOUT_S)
        ws = self._workspace(tid)
        sockdir = (SOCKETS / tid).resolve()
        if not (under(sockdir, SOCKETS) and (sockdir / "llm.sock").is_socket()):
            raise web.HTTPBadRequest(text="gateway socket missing")
        vbinds, venv_env = self._venv(b)
        # The agent always asks for model.id; the gateway rewrites it to the backend's served name.
        env_list = [f"{k}={v}" for k, v in env.items()] + venv_env + [
            f"ANTHROPIC_MODEL={model.id}", f"ANTHROPIC_DEFAULT_OPUS_MODEL={model.id}",
            f"ANTHROPIC_DEFAULT_SONNET_MODEL={model.id}", f"ANTHROPIC_DEFAULT_HAIKU_MODEL={model.id}",
            f"CLAUDE_CODE_SUBAGENT_MODEL={model.id}"]
        return await self._run_sandbox(tid, ws, "agent", {"agentbox.model": model.id}, cmd, env_list,
                                       [f"{ws}:/workspace:rw", f"{sockdir}:/run/gw:rw", *vbinds], timeout_s)

    async def start_check(self, req: web.Request) -> web.Response:
        """Run a verification command (tests, linters) on a checked-out branch in the same sandbox as the
        agents, but with no gateway socket at all: agent-written code never runs on the host."""
        b = await req.json()
        tid, command = str(b.get("tid", "")), b.get("command")
        if not (isinstance(command, str) and 0 < len(command) <= 4000):
            raise web.HTTPBadRequest(text="command must be a non-empty string (<= 4000 chars)")
        timeout_s = min(int(b.get("timeout_s", 900)), 3600)
        ws = self._workspace(tid)
        vbinds, venv_env = self._venv(b)
        return await self._run_sandbox(tid, ws, "check", {}, [command], venv_env,
                                       [f"{ws}:/workspace:rw", *vbinds], timeout_s,
                                       entrypoint=["bash", "-c"])

    async def _deadline(self, tid: str, timeout_s: int):
        await asyncio.sleep(timeout_s)
        log.warning("agent %s hit its %ss deadline; killing", tid, timeout_s)
        await self.docker.req("POST", f"/containers/{AGENT_PREFIX + tid}/kill", ok=(204, 404, 409))

    async def get_agent(self, req: web.Request) -> web.Response:
        tid = req.match_info["tid"]
        if not TID_RE.match(tid):
            raise web.HTTPBadRequest(text="bad tid")
        name = AGENT_PREFIX + tid
        if req.query.get("wait"):
            await self.docker.req("POST", f"/containers/{name}/wait", ok=(200, 404))
        info = await self.docker.inspect(name)
        if not info:
            raise web.HTTPNotFound(text="no such agent")
        st = info["State"]
        res = {"state": st["Status"], "exit_code": st.get("ExitCode"), "oom_killed": st.get("OOMKilled")}
        if st["Status"] in ("exited", "dead"):
            res["stdout"], res["stderr"] = await self.docker.logs(name)
            if t := self.deadlines.pop(tid, None):
                t.cancel()
        return web.json_response(res)

    async def delete_agent(self, req: web.Request) -> web.Response:
        tid = req.match_info["tid"]
        if not TID_RE.match(tid):
            raise web.HTTPBadRequest(text="bad tid")
        await self.docker.remove(AGENT_PREFIX + tid)
        if t := self.deadlines.pop(tid, None):
            t.cancel()
        ws = (WORKTREES / tid).resolve()
        if under(ws, WORKTREES) and ws.is_dir() and ws.stat().st_uid != OWNER_UID:
            await self.chown_tree(ws, OWNER_UID, OWNER_GID)
        return web.json_response({"removed": tid})

    async def chown_tree(self, ws: Path, uid: int, gid: int) -> None:
        """Hand a workspace to another uid with a throwaway fixed-spec container (the launcher itself has
        no capabilities). Only the one workspace is mounted, so symlinks inside it cannot reach the host;
        `chown -R -h` changes links themselves and never follows them."""
        name = f"agentbox-chown-{ws.name}"
        spec = {
            "Image": AGENT_IMAGE, "Entrypoint": ["chown"], "Cmd": ["-R", "-h", f"{uid}:{gid}", "/w"],
            "User": "0:0", "NetworkDisabled": True, "Labels": {LABEL: "chown"},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "CapDrop": ["ALL"],
                           "CapAdd": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
                           "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 16,
                           "Binds": [f"{ws}:/w:rw"], **({"Runtime": AGENT_RUNTIME} if AGENT_RUNTIME else {})},
        }
        await self.docker.remove(name)
        await self.docker.req("POST", "/containers/create", spec, params={"name": name})
        await self.docker.req("POST", f"/containers/{name}/start")
        res = await self.docker.req("POST", f"/containers/{name}/wait", ok=(200,))
        _, err = await self.docker.logs(name)
        await self.docker.remove(name)
        if res.get("StatusCode") != 0:
            raise web.HTTPInternalServerError(text=f"chown {ws} -> {uid}:{gid} failed: {err[-300:]}")

    # ---------------------------------------------------------------- backends
    def _vllm_name(self, model_id: str) -> str:
        return VLLM_PREFIX + model_id.replace(".", "-")

    async def _stop_vllm_except(self, keep: str | None, busy: set[str]):
        for r in await self.running():
            if r["role"] != "backend" or r["model"] == keep:
                continue
            if r["model"] in busy:
                raise web.HTTPConflict(text=f"backend {r['model']} has running agents")
            log.info("stopping backend %s", r["name"])
            await self.docker.stop(r["name"], 60)
            await self.docker.remove(r["name"])

    async def start_backend(self, req: web.Request) -> web.Response:
        model = self.reg.models.get(req.match_info["model"])
        if not model or model.backend != "vllm":
            raise web.HTTPBadRequest(text="not a vllm model")
        async with self.lock:
            busy = await self.busy_models()
            if any(self.reg.models[m].backend == "ollama" for m in busy if m in self.reg.models):
                raise web.HTTPConflict(text="ollama agents are running")
            await self._stop_vllm_except(model.id, busy)
            name = self._vllm_name(model.id)
            info = await self.docker.inspect(name)
            if info and info["State"]["Status"] == "running":
                return web.json_response({"state": "running", "name": name})
            if info:
                await self.docker.remove(name)
            hc = {
                "NetworkMode": "host",
                "Binds": [f"{self.reg.models_dir}:/models:ro", f"{self.reg.vllm_cache}:/cache:rw"],
                "DeviceRequests": [{"Driver": "", "Count": -1, "Capabilities": [["gpu"]]}],
                "LogConfig": {"Type": "journald", "Config": {"tag": f"agentbox-{model.id}"}},
                "AutoRemove": True,
            }
            if model.ipc_host:
                hc["IpcMode"] = "host"
            if model.shm_size:
                hc["ShmSize"] = _bytes(model.shm_size)
            if model.memory:
                hc["Memory"] = hc["MemorySwap"] = _bytes(model.memory)
            spec = {"Image": model.image, "Cmd": model.args,
                    "Env": [f"{k}={v}" for k, v in model.env.items()],
                    "Labels": {LABEL: "backend", "agentbox.model": model.id}, "HostConfig": hc}
            if model.entrypoint:
                spec["Entrypoint"] = model.entrypoint
            await self.docker.req("POST", "/containers/create", spec, params={"name": name})
            await self.docker.req("POST", f"/containers/{name}/start")
            log.info("backend %s starting", name)
            return web.json_response({"state": "started", "name": name})

    async def stop_vllm(self, req: web.Request) -> web.Response:
        async with self.lock:
            await self._stop_vllm_except(None, await self.busy_models())
        return web.json_response({"stopped": True})

    async def status(self, req: web.Request) -> web.Response:
        return web.json_response({"containers": await self.running(), "ts": time.time()})


def _bytes(s: str) -> int:
    s = s.strip().lower()
    mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}.get(s[-1])
    return int(float(s[:-1]) * mult) if mult else int(s)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lz = Launcher()
    app = web.Application()
    app.add_routes([
        web.post("/agents", lz.start_agent), web.post("/checks", lz.start_check),
        web.get("/agents/{tid}", lz.get_agent),
        web.delete("/agents/{tid}", lz.delete_agent),
        web.post("/backends/vllm/stop", lz.stop_vllm), web.post("/backends/{model}/start", lz.start_backend),
        web.get("/status", lz.status),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    LAUNCHER_SOCK.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_SOCK.unlink(missing_ok=True)
    await web.UnixSite(runner, str(LAUNCHER_SOCK)).start()
    os.chmod(LAUNCHER_SOCK, 0o660)
    log.info("launcher listening on %s (models: %s)", LAUNCHER_SOCK, ", ".join(lz.reg.models))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
