"""agentbox launcher - the ONLY component with the Docker socket.

It exposes a tiny API on a unix socket to the (unprivileged) MCP server and builds every container
spec itself. Clients choose *which* task/model to run, never *how*: workspace and gateway paths are
derived from the task id, the hardening flags are fixed here, the command must be `claude ...`, and
only a short allowlist of environment variables is passed through. A compromised MCP server can
therefore start sandboxed agents and the configured model servers - nothing privileged.

  POST /agents          {tid, model, cmd[], env{}, venv?, timeout_s}  -> start a sandboxed agent
  POST /checks          {tid, command, venv?, timeout_s}  -> run a command in the same sandbox, no gateway
  POST /repo/snapshot   {tid, path, ref?}   -> copy one commit of the planner's repo into the shadow git dir
  POST /repo/deliver    {tid, path, branch} -> fetch agent/<id> from the shadow into the planner's repo
  POST /repo/delete-branch {path, branch}   -> delete an agent/<id> branch
  (repo operations run in short-lived git-helper containers that mount only that one repo)
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

import re
import stat as statmod
import uuid

from .common import (AGENT_GID, AGENT_IMAGE, AGENT_PREFIX, AGENT_UID, DATA, LABEL, LAUNCHER_SOCK, OWNER_GID,
                     OWNER_UID, REPOS_ROOT, SHADOWS, SOCKETS, TID_RE, VENV_ROOTS, VLLM_PREFIX, WORKTREES,
                     host_path_ok, load_registry, under)

AGENT_BRANCH_RE = re.compile(r"^agent/[0-9a-z][0-9a-z-]{0,63}$")
WORKTREE_GITDIR_RE = re.compile(r"^(/.+/\.git)/worktrees/[^/]+$")

# Git-helper scripts (run with sh -ec in a short-lived container as the agentbox uid, network none).
# /src or /dst is the ONE repo mounted for this request; /shadow is agentbox's trusted bare repo.
PROBE = """
o=$(stat -c %u /src); [ "$o" = "$(id -u)" ] || { echo "not owned by the agentbox user (owner uid $o)" >&2; exit 3; }
if [ -d /src/.git ]; then echo repo
elif [ -f /src/.git ]; then sed -n 's/^gitdir: //p' /src/.git | head -1
else echo none; fi
"""
SNAPSHOT = """
cd /src
git rev-parse --git-dir >/dev/null
sha=$(git rev-parse --verify --quiet "$1^{commit}") || { echo "not a commit in this repo: $1" >&2; exit 5; }
git init -q --bare /shadow
git --git-dir=/shadow fetch -q --no-tags /src "$sha:refs/heads/base"
echo "$sha"
"""
DELIVER = """
cd /dst
o=$(stat -c %u /dst); [ "$o" = "$(id -u)" ] || { echo "repo not owned by the agentbox user" >&2; exit 3; }
git fetch -q --no-tags /shadow "refs/heads/$1:refs/heads/$1"
"""
DELETE_BRANCH = """
cd /dst
o=$(stat -c %u /dst); [ "$o" = "$(id -u)" ] || { echo "repo not owned by the agentbox user" >&2; exit 3; }
git branch -D "$1"
"""
VENV_CHECK = """
o=$(stat -c %u /v); [ "$o" = "$(id -u)" ] || { echo "venv not owned by the agentbox user" >&2; exit 3; }
[ -x /v/bin/python ] || { echo "not a venv (no bin/python)" >&2; exit 4; }
"""

log = logging.getLogger("agentbox.launcher")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
MAX_TIMEOUT_S = 4 * 3600
ENV_ALLOW = {"ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "AGENT_NO_GUIDE", "MAX_THINKING_TOKENS"}
AGENT_RUNTIME = os.environ.get("AGENTBOX_AGENT_RUNTIME") or None   # runsc (gVisor) recommended; None = runc
GVISOR_DOCS = "https://github.com/Adithya-Rajendran/agentbox/wiki/Run-Agents-under-gVisor"
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
    @staticmethod
    def _real_dir(p: Path) -> os.stat_result:
        """lstat every component from DATA down: a symlink anywhere (e.g. worktrees -> /) is refused, so the
        path the Docker daemon mounts on the host is the directory checked here."""
        rel = p.relative_to(DATA)
        cur = DATA
        for part in (None, *rel.parts):
            cur = cur if part is None else cur / part
            st = os.lstat(cur)
            if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISDIR(st.st_mode):
                raise web.HTTPBadRequest(text=f"refusing non-directory or symlink: {cur}")
        return st

    def _workspace(self, tid: str) -> Path:
        if not TID_RE.match(tid):
            raise web.HTTPBadRequest(text="bad tid")
        ws = WORKTREES / tid
        try:
            st = self._real_dir(ws)
        except FileNotFoundError:
            raise web.HTTPBadRequest(text="workspace missing") from None
        if (st.st_uid, st.st_gid) != (OWNER_UID, OWNER_GID):
            raise web.HTTPBadRequest(text="workspace must be owned by the agentbox user")
        return ws

    async def _venv(self, b: dict) -> tuple[list[str], list[str]]:
        if not b.get("venv"):
            return [], []
        venv = str(b["venv"])
        if not host_path_ok(venv, VENV_ROOTS):
            raise web.HTTPBadRequest(text=f"venv must be a normalized absolute path under one of {VENV_ROOTS}")
        code, _, err = await self._helper(VENV_CHECK, [], [(venv, "/v", True)])
        if code != 0:
            raise web.HTTPBadRequest(text=f"venv {venv}: {err.strip()[-300:]}")
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
            # NetworkMode none (below) gives a private namespace with only lo. Not NetworkDisabled: under gVisor
            # that removes lo too, which breaks the agent's 127.0.0.1 relay and tests that use localhost.
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
        sockdir = SOCKETS / tid
        try:
            self._real_dir(sockdir)
            sock_ok = statmod.S_ISSOCK(os.lstat(sockdir / "llm.sock").st_mode)
        except FileNotFoundError:
            sock_ok = False
        if not sock_ok:
            raise web.HTTPBadRequest(text="gateway socket missing")
        vbinds, venv_env = await self._venv(b)
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
        vbinds, venv_env = await self._venv(b)
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
        ws = WORKTREES / tid
        try:
            st = self._real_dir(ws)
        except (FileNotFoundError, web.HTTPBadRequest):
            st = None
        if st is not None and st.st_uid != OWNER_UID:
            await self.chown_tree(ws, OWNER_UID, OWNER_GID)
        return web.json_response({"removed": tid})

    async def chown_tree(self, ws: Path, uid: int, gid: int) -> None:
        """Hand a workspace to another uid with a throwaway fixed-spec root container (the launcher itself has
        no capabilities). It first checks that the mounted directory is the exact inode validated here (so a
        path swapped after the check is refused), then `chown -R -h` (never follows symlinks) and gives the
        new owner read/write on everything (an agent leaving mode-000 files must not break collection)."""
        st = self._real_dir(ws)
        expect = f"{st.st_dev}:{st.st_ino}"
        name = f"agentbox-chown-{ws.name}-{uuid.uuid4().hex[:6]}"
        script = ('[ "$(stat -c %d:%i /w)" = "$1" ] || { echo "workspace changed under us" >&2; exit 9; }; '
                  'chown -R -h "$2" /w && chmod -R u+rwX /w')
        spec = {
            "Image": AGENT_IMAGE, "Entrypoint": ["sh", "-ec", script, "chown"], "Cmd": [expect, f"{uid}:{gid}"],
            "User": "0:0", "Labels": {LABEL: "chown"},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "CapDrop": ["ALL"],
                           "CapAdd": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
                           "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 16,
                           # runc: this fixed command executes no untrusted code (chown -h never follows
                           # links), and gVisor's own threads would not fit its tiny pids limit.
                           "Runtime": "runc",
                           "Binds": [f"{ws}:/w:rw"]},
        }
        code, _, err = await self._run_fixed(name, spec, timeout_s=600)
        if code != 0:
            raise web.HTTPInternalServerError(text=f"chown {ws} -> {uid}:{gid} failed: {err[-300:]}")

    async def _run_fixed(self, name: str, spec: dict, timeout_s: int) -> tuple[int, str, str]:
        """Create, run, wait for and remove a short-lived container; returns (exit code, stdout, stderr)."""
        spec.setdefault("HostConfig", {}).setdefault("LogConfig", {"Type": "json-file",
                                                                    "Config": {"max-size": "2m"}})
        await self.docker.remove(name)
        await self.docker.req("POST", "/containers/create", spec, params={"name": name})
        try:
            await self.docker.req("POST", f"/containers/{name}/start")
            try:
                res = await asyncio.wait_for(
                    self.docker.req("POST", f"/containers/{name}/wait", ok=(200,)), timeout_s)
            except asyncio.TimeoutError:
                await self.docker.req("POST", f"/containers/{name}/kill", ok=(204, 404, 409))
                return 124, "", f"timed out after {timeout_s}s"
            out, err = await self.docker.logs(name)
            return res.get("StatusCode", 1), out, err
        finally:
            await self.docker.remove(name)

    async def _helper(self, script: str, args: list[str], mounts: list[tuple[str, str, bool]],
                      timeout_s: int = 300) -> tuple[int, str, str]:
        """Run a fixed git/stat script in a short-lived container as the agentbox user: no network, no
        capabilities, read-only root. `mounts` = [(host path, container path, read_only)] - exactly what this
        one operation needs. Mounts (not Binds) so a missing host path is an error, never created as root."""
        spec = {
            "Image": AGENT_IMAGE, "Entrypoint": ["sh", "-ec", script, "helper"], "Cmd": args,
            "User": f"{OWNER_UID}:{OWNER_GID}", "WorkingDir": "/",
            "Env": ["HOME=/tmp", "GIT_CONFIG_NOSYSTEM=1", "GIT_TERMINAL_PROMPT=0"],
            "Labels": {LABEL: "git"},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "CapDrop": ["ALL"],
                           "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 64,
                           "Memory": 1 << 30, "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=64m"},
                           "Runtime": "runc",
                           "Mounts": [{"Type": "bind", "Source": src, "Target": dst, "ReadOnly": ro,
                                       "BindOptions": {"Propagation": "rprivate"}} for src, dst, ro in mounts]},
        }
        try:
            return await self._run_fixed(f"agentbox-git-{uuid.uuid4().hex[:10]}", spec, timeout_s)
        except web.HTTPBadGateway as e:   # e.g. "bind source path does not exist"
            return 2, "", e.text

    # ---------------------------------------------------------------- repos (per request, one repo at a time)
    async def _repo_mounts(self, path: str, target: str, rw: bool) -> tuple[str, list[tuple[str, str, bool]]]:
        """Find the repo for `path` (the planner's current folder, or any folder inside the repo) and return
        (repo root, mounts) with the root at `target`. Walks up at most to the allowlist root. Linked worktrees
        (.git is a file "gitdir: <main>/.git/worktrees/<name>") also get their common .git dir, mounted at its
        own host path so the absolute link still resolves."""
        if not host_path_ok(path, [REPOS_ROOT]):
            raise web.HTTPBadRequest(text=f"repo_path must be a normalized absolute path inside {REPOS_ROOT}")
        cur = path
        while host_path_ok(cur, [REPOS_ROOT]):
            code, out, err = await self._helper(PROBE, [], [(cur, "/src", True)], timeout_s=60)
            if code != 0:
                if "does not exist" in err or "invalid mount config" in err:
                    raise web.HTTPBadRequest(text=f"{cur}: folder does not exist")
                raise web.HTTPBadRequest(text=f"{cur}: {err.strip()[-300:] or 'not accessible'}")
            kind = out.strip()
            if kind == "repo":
                return cur, [(cur, target, not rw)]
            if kind != "none":
                m = WORKTREE_GITDIR_RE.match(kind)
                if not (m and host_path_ok(m.group(1), [REPOS_ROOT])):
                    raise web.HTTPBadRequest(text=f"{cur}: unsupported .git link {kind!r}")
                return cur, [(cur, target, not rw), (m.group(1), m.group(1), not rw)]
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        raise web.HTTPBadRequest(text=f"{path} is not inside a git repository under {REPOS_ROOT}")

    def _shadow(self, tid: str) -> Path:
        if not TID_RE.match(tid):
            raise web.HTTPBadRequest(text="bad tid")
        sh = SHADOWS / f"{tid}.git"
        self._real_dir(sh)
        return sh

    async def repo_snapshot(self, req: web.Request) -> web.Response:
        """Fetch one commit (default HEAD) of the planner's repo into agentbox's shadow git dir."""
        b = await req.json()
        ref = str(b.get("ref") or "HEAD")
        if ref.startswith("-") or len(ref) > 200 or any(ord(c) < 32 for c in ref):
            raise web.HTTPBadRequest(text="bad ref")
        shadow = self._shadow(str(b.get("tid", "")))
        top, mounts = await self._repo_mounts(str(b.get("path", "")), "/src", rw=False)
        code, out, err = await self._helper(SNAPSHOT, [ref], mounts + [(str(shadow), "/shadow", False)])
        if code != 0:
            raise web.HTTPBadRequest(text=f"snapshot failed: {err.strip()[-400:]}")
        return web.json_response({"repo": top, "sha": out.strip().splitlines()[-1]})

    async def repo_deliver(self, req: web.Request) -> web.Response:
        """Fetch branch agent/<id> from the shadow into the planner's repo (the only write to it)."""
        b = await req.json()
        branch = str(b.get("branch", ""))
        if not AGENT_BRANCH_RE.match(branch):
            raise web.HTTPBadRequest(text="branch must be agent/<id>")
        shadow = self._shadow(str(b.get("tid", "")))
        top, mounts = await self._repo_mounts(str(b.get("path", "")), "/dst", rw=True)
        code, _, err = await self._helper(DELIVER, [branch], mounts + [(str(shadow), "/shadow", True)])
        if code != 0:
            raise web.HTTPBadRequest(text=f"deliver failed: {err.strip()[-400:]}")
        return web.json_response({"repo": top, "branch": branch})

    async def repo_delete_branch(self, req: web.Request) -> web.Response:
        b = await req.json()
        branch = str(b.get("branch", ""))
        if not AGENT_BRANCH_RE.match(branch):
            raise web.HTTPBadRequest(text="only agent/<id> branches can be deleted")
        top, mounts = await self._repo_mounts(str(b.get("path", "")), "/dst", rw=True)
        code, _, err = await self._helper(DELETE_BRANCH, [branch], mounts)
        return web.json_response({"repo": top, "deleted": code == 0, "error": err.strip()[-300:]})

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

    async def check_runtime(self) -> None:
        """Fail fast on a sandbox runtime that would break agents; recommend gVisor when none is set."""
        runtimes = (await self.docker.req("GET", "/info")).get("Runtimes") or {}
        if not AGENT_RUNTIME:
            log.warning("sandboxes use the default runtime (runc, shared host kernel). gVisor is recommended: "
                        "install runsc and set AGENTBOX_AGENT_RUNTIME=runsc - %s", GVISOR_DOCS)
            return
        if AGENT_RUNTIME not in runtimes:
            raise SystemExit(f"AGENTBOX_AGENT_RUNTIME={AGENT_RUNTIME} is not a Docker runtime here "
                             f"(have: {', '.join(runtimes)}). Install it or unset the variable. {GVISOR_DOCS}")
        args = runtimes[AGENT_RUNTIME].get("runtimeArgs") or []
        if AGENT_RUNTIME == "runsc" and not any(a in ("--host-uds=open", "--host-uds=all") for a in args):
            raise SystemExit("runsc is registered without --host-uds=open, so agents could not reach their model "
                             "socket. Fix: sudo runsc install -- --host-uds=open && sudo systemctl reload docker")
        log.info("sandbox runtime: %s %s", AGENT_RUNTIME, " ".join(args))

    async def status(self, req: web.Request) -> web.Response:
        return web.json_response({"containers": await self.running(), "ts": time.time(),
                                  "sandbox_runtime": AGENT_RUNTIME or "runc (default; gVisor recommended)"})


def _bytes(s: str) -> int:
    s = s.strip().lower()
    mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}.get(s[-1])
    return int(float(s[:-1]) * mult) if mult else int(s)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lz = Launcher()
    await lz.check_runtime()
    app = web.Application()
    app.add_routes([
        web.post("/agents", lz.start_agent), web.post("/checks", lz.start_check),
        web.post("/repo/snapshot", lz.repo_snapshot), web.post("/repo/deliver", lz.repo_deliver),
        web.post("/repo/delete-branch", lz.repo_delete_branch),
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
