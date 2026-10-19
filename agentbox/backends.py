"""MCP-side clients: the launcher (unix socket) and the model servers (Ollama API, vLLM /health)."""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from .common import BRIDGE_TOKEN, BRIDGE_URL, LAUNCHER_SOCK, OLLAMA_URL, Model, Registry

log = logging.getLogger("agentbox.backends")


class LauncherError(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"launcher {status}: {text}")
        self.status = status


class LauncherClient:
    _s: aiohttp.ClientSession | None = None

    @property
    def s(self) -> aiohttp.ClientSession:  # created lazily, inside the server's event loop
        if self._s is None:
            self._s = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(LAUNCHER_SOCK)),
                                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
        return self._s

    async def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        async with self.s.request(method, f"http://launcher{path}", json=body, params=params) as r:
            if r.status >= 400:
                raise LauncherError(r.status, await r.text())
            return await r.json()


class Backends:
    """Makes sure the requested model is being served, honouring one-GPU-family-at-a-time."""

    def __init__(self, reg: Registry, launcher: LauncherClient):
        self.reg, self.launcher = reg, launcher
        self._http: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5))
        return self._http

    async def status(self) -> dict:
        st = await self.launcher.call("GET", "/status")
        try:
            async with self.http.get(f"{OLLAMA_URL}/api/ps", timeout=aiohttp.ClientTimeout(total=5)) as r:
                ollama = [m["name"] for m in (await r.json()).get("models", [])]
        except Exception as e:  # noqa: BLE001
            ollama = f"unreachable: {e}"
        return {"containers": st["containers"], "ollama_loaded": ollama}

    async def _ollama_unload_all(self) -> None:
        try:
            async with self.http.get(f"{OLLAMA_URL}/api/ps") as r:
                loaded = [m["name"] for m in (await r.json()).get("models", [])]
            for name in loaded:
                log.info("unloading ollama model %s", name)
                async with self.http.post(f"{OLLAMA_URL}/api/generate", json={"model": name, "keep_alive": 0}) as r:
                    await r.read()
        except aiohttp.ClientError as e:
            log.warning("ollama unload skipped: %s", e)

    async def _healthy(self, m: Model) -> bool:
        try:
            async with self.http.get(f"{m.base_url}/health", timeout=aiohttp.ClientTimeout(total=5)) as r:
                return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def ensure(self, m: Model, wait_s: float, note) -> None:
        """Block until `m` is servable. Waits (up to wait_s) while the other family has running agents."""
        if m.external:   # OpenAI / OpenAI-compatible: nothing to start, just check it answers
            await self._check_openai(m, note)
            return
        deadline = time.time() + wait_s
        while True:
            try:
                async with self.lock:
                    if m.backend == "vllm":
                        await self._ensure_vllm(m, deadline, note)
                    else:
                        await self._ensure_ollama(m, note)
                return
            except LauncherError as e:
                if e.status != 409 or time.time() > deadline:
                    raise
                note(f"waiting: {e}")
                await asyncio.sleep(20)

    async def _ensure_vllm(self, m: Model, deadline: float, note) -> None:
        if await self._healthy(m):
            await self.launcher.call("POST", f"/backends/{m.id}/start")   # also stops any stray other vLLM
            return
        st = await self.launcher.call("GET", "/status")
        busy_ollama = [c for c in st["containers"] if c["role"] == "agent" and c["state"] == "running"
                       and self.reg.models.get(c["model"], m).backend == "ollama"]
        if busy_ollama:
            raise LauncherError(409, f"{len(busy_ollama)} Ollama agent(s) running")
        await self._ollama_unload_all()
        r = await self.launcher.call("POST", f"/backends/{m.id}/start")
        note(f"vLLM {m.id}: {r['state']} (load can take several minutes)")
        t0 = time.time()
        while time.time() < min(deadline, t0 + m.load_timeout_s):
            if await self._healthy(m):
                note(f"vLLM {m.id} healthy after {time.time() - t0:.0f}s")
                return
            st = await self.launcher.call("GET", "/status")
            if not any(c["role"] == "backend" and c["model"] == m.id for c in st["containers"]):
                raise RuntimeError(f"vLLM {m.id} exited during load; see `journalctl -t agentbox-{m.id}`")
            await asyncio.sleep(10)
        raise RuntimeError(f"vLLM {m.id} not healthy after {time.time() - t0:.0f}s")

    async def _check_openai(self, m: Model, note) -> None:
        """Remote model: ask llm-bridge whether its key is configured and the upstream answers."""
        try:
            async with self.http.get(f"{BRIDGE_URL}/check/{m.id}", timeout=aiohttp.ClientTimeout(total=30),
                                     headers={"authorization": f"Bearer {BRIDGE_TOKEN}"}) as r:
                res = await r.json(content_type=None) if r.status == 200 else {"ok": False,
                                                                               "error": f"bridge {r.status}"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise RuntimeError(f"llm-bridge unreachable at {BRIDGE_URL}: {e}") from e
        if not res.get("ok"):
            raise RuntimeError(f"{m.id}: {res.get('error')}")
        note(f"{m.id}: {m.backend} upstream {m.upstream_url} reachable via llm-bridge")

    async def _ensure_ollama(self, m: Model, note) -> None:
        await self.launcher.call("POST", "/backends/vllm/stop")      # 409 while vLLM agents are running
        async with self.http.post(f"{OLLAMA_URL}/api/show", json={"model": m.ollama_model}) as r:
            present = r.status == 200
        if not present:
            note(f"pulling {m.ollama_model} into Ollama (first use)")
            async with self.http.post(f"{OLLAMA_URL}/api/pull",
                                      json={"model": m.ollama_model, "stream": False}) as r:
                body = await r.text()
                if r.status != 200:
                    raise RuntimeError(f"ollama pull {m.ollama_model} failed: {body[:300]}")
