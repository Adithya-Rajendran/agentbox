"""Shared config for the agentbox MCP server and the privileged launcher.

Everything path-like is a HOST path: both containers mount the directories they need at the same
path as on the host, so a path means the same thing to the MCP server, the launcher and the Docker
daemon (which resolves bind-mount sources on the host).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TID_RE = re.compile(r"^[0-9a-z][0-9a-z-]{0,63}$")


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# All runtime state lives under one host dir (worktrees, gateway sockets, shadow git dirs, logs).
DATA = env_path("AGENTBOX_DATA", "/var/lib/agentbox")
WORKTREES = DATA / "worktrees"      # scratch clones the agents work in; the launcher only mounts paths here
SOCKETS = DATA / "sockets"          # SOCKETS/<tid>/llm.sock, bind-mounted into that agent at /run/gw
SHADOWS = DATA / "git"              # trusted per-task git dirs the agent never sees
LOG = DATA / "logs" / "delegations.jsonl"
# Allowlist for repo paths the planner may pass (e.g. its current folder). NOTHING is mounted from here:
# each request mounts only the one repo, briefly, into a short-lived git-helper container.
REPOS_ROOT = env_path("AGENTBOX_REPOS_ROOT", "/home")
# Prebuilt Python envs may live under these roots (also allowlists; verified per request by a helper).
VENV_ROOTS = [p for p in (env_path("AGENTBOX_VENVS", "/var/lib/agentbox-venvs"), REPOS_ROOT)]
LAUNCHER_SOCK = env_path("AGENTBOX_LAUNCHER_SOCK", "/run/agentbox/launcher.sock")
MODELS_FILE = env_path("AGENTBOX_MODELS", "/etc/agentbox-config/models.yaml")
EXCLUDES = env_path("AGENTBOX_EXCLUDES", "/etc/agentbox-config/agent-gitignore")
AGENT_IMAGE = os.environ.get("AGENTBOX_AGENT_IMAGE", "agentbox/agent:local")
OLLAMA_URL = os.environ.get("AGENTBOX_OLLAMA_URL", "http://127.0.0.1:11434")
# Agents run as a uid/gid that need not exist on the host (default 64000): if a sandbox were ever
# escaped, that identity owns nothing. The launcher hands the workspace to it for the run and back after.
AGENT_UID = int(os.environ.get("AGENTBOX_AGENT_UID", "64000"))
AGENT_GID = int(os.environ.get("AGENTBOX_AGENT_GID", "64000"))
# The identity agentbox itself runs as (owns DATA and the repos).
OWNER_UID = int(os.environ.get("AGENTBOX_UID", str(os.getuid())))
OWNER_GID = int(os.environ.get("AGENTBOX_GID", str(os.getgid())))

LABEL = "agentbox.role"          # agent | backend
VLLM_PREFIX = "agentbox-vllm-"   # container name prefix for vLLM backends
AGENT_PREFIX = "agentbox-agent-"


# Remote/external endpoints, reached only through the llm-bridge container (which holds the provider keys
# and is the only component that talks to them). No launcher, no GPU arbitration.
EXTERNAL = ("openai", "anthropic")
BRIDGE_URL = os.environ.get("AGENTBOX_BRIDGE_URL", "http://127.0.0.1:8790")
BRIDGE_TOKEN = os.environ.get("AGENTBOX_BRIDGE_TOKEN", "")


@dataclass
class Model:
    id: str
    backend: str                  # vllm | ollama (local)  |  openai | anthropic (via llm-bridge)
    description: str = ""
    max_agents: int = 4
    token_budget: int | None = None   # per-task input+output tokens (default: 20M local, 3M remote)
    # vllm
    port: int | None = None
    image: str | None = None
    entrypoint: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    memory: str | None = None
    shm_size: str | None = None
    ipc_host: bool = False
    load_timeout_s: int = 1800
    # ollama
    ollama_model: str | None = None
    # openai: the OpenAI API or any OpenAI-compatible /chat/completions server (translated by llm-bridge)
    # anthropic: the Anthropic API or any /v1/messages endpoint (passed through by llm-bridge)
    endpoint: str | None = None           # openai: base URL incl. /v1; anthropic: base URL without /v1
    model_name: str | None = None         # model name sent upstream
    api_key_env: str | None = None        # env var (in agentbox's environment) holding the key
    max_output_tokens: int | None = None  # clamp max_tokens for servers with small output limits
    token_param: str = "max_tokens"       # OpenAI's newer models want "max_completion_tokens"
    drop_params: list[str] = field(default_factory=list)   # e.g. [temperature, top_p] for reasoning models
    extra_body: dict = field(default_factory=dict)   # merged into every upstream request

    @property
    def external(self) -> bool:
        return self.backend in EXTERNAL

    @property
    def served_name(self) -> str:
        # external models are addressed by their agentbox id; llm-bridge maps that to model_name upstream
        return self.ollama_model if self.backend == "ollama" else self.id

    @property
    def base_url(self) -> str:
        if self.external:
            return BRIDGE_URL
        return OLLAMA_URL if self.backend == "ollama" else f"http://127.0.0.1:{self.port}"

    @property
    def budget(self) -> int:
        return self.token_budget or (3_000_000 if self.external else 20_000_000)

    @property
    def upstream_url(self) -> str:
        return (self.endpoint or "").rstrip("/")

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


@dataclass
class Registry:
    models: dict[str, Model]
    default_model: str
    models_dir: str
    vllm_cache: str


def load_registry(path: Path = MODELS_FILE) -> Registry:
    raw = yaml.safe_load(path.read_text())
    models = {}
    for mid, m in (raw.get("models") or {}).items():
        if not TID_RE.match(mid.replace(".", "-")):
            raise ValueError(f"bad model id {mid!r}")
        m = dict(m)
        if isinstance(m.get("args"), str):
            m["args"] = m["args"].split()
        if m.get("backend") not in ("vllm", "ollama", *EXTERNAL):
            raise ValueError(f"{mid}: backend must be vllm, ollama, openai or anthropic")
        if m["backend"] in EXTERNAL and not (m.get("endpoint") and m.get("model_name")):
            raise ValueError(f"{mid}: {m['backend']} models need endpoint and model_name")
        if m["backend"] == "vllm" and not (m.get("port") and m.get("image")):
            raise ValueError(f"{mid}: vllm models need port and image")
        if m["backend"] == "ollama" and not m.get("ollama_model"):
            raise ValueError(f"{mid}: ollama models need ollama_model")
        m["env"] = {str(k): str(v) for k, v in (m.get("env") or {}).items()}
        models[mid] = Model(id=mid, **m)
    p = raw.get("paths") or {}
    return Registry(models, raw.get("default_model") or next(iter(models)),
                    p.get("models_dir", ""), p.get("vllm_cache", ""))


def host_path_ok(p: str, roots: list[Path]) -> bool:
    """A host path string we may hand to Docker: absolute, already normalized (no '..', '//', trailing '/'),
    no control characters, and inside one of `roots`. Symlinks are NOT resolved here (that would be the
    caller's view, not the host's); the git helper verifies ownership of whatever Docker actually mounts."""
    if not p or len(p) > 4096 or not p.startswith("/") or os.path.normpath(p) != p:
        return False
    if any(ord(c) < 32 for c in p) or ":" in p or "," in p:      # ':' / ',' would confuse bind syntax
        return False
    return any(p == str(r) or p.startswith(str(r).rstrip("/") + "/") for r in roots)


def under(child: Path, root: Path) -> bool:
    """True if `child` (already resolved) is strictly inside `root` (resolved)."""
    try:
        child.relative_to(root.resolve())
        return child != root.resolve()
    except ValueError:
        return False
