# agentbox: local coding agents behind an MCP server

A frontier planner (Claude Code with Opus/Fable) hands well-specified coding tasks to **local models**
(vLLM or Ollama on the GB10). Each task runs Claude Code in a hardened, network-less container on a
scratch clone of the repo, and the result comes back as a git branch `agent/<id>` for review.

```
Claude Code (planner)                      host 127.0.0.1 only
   │  MCP streamable HTTP + bearer token
   ▼
agentbox ─────── unprivileged container: MCP server, per-task gateways, trusted git collection
   │ unix socket ($AGENTBOX_DATA/run/launcher.sock)
   ▼
agentbox-launcher ── the ONLY container with docker.sock; builds every container spec itself
   │ Docker API
   ├─► agent container per task   --network none, read-only, no caps, non-root, tini, pids/mem/cpu caps
   │      └─ only way out: $AGENTBOX_DATA/sockets/<tid>/llm.sock ──► that task's gateway
   └─► vLLM backend containers (on demand, from config/models.yaml)
ollama ── always-on service, non-root, read-only; models pulled on first use
```

## Security model

| Layer | What it enforces |
|---|---|
| Agent container | `NetworkMode none` (only `lo`), read-only rootfs, `CapDrop ALL`, `no-new-privileges`, runs as **uid 64000, a uid that does not exist on the host** (if a sandbox were escaped, that identity owns nothing; the launcher hands the workspace to it for the run and back afterwards with a fixed-spec `chown` container), private IPC, tini, 512 pids / 8 GiB / 4 CPUs, tmpfs `/tmp` and `$HOME`, no Docker socket, no host paths except its own workspace, its own gateway socket and (optionally) a read-only venv. Image has no package manager, `su`/`sudo`, or setuid binaries. Optional gVisor: set `AGENTBOX_AGENT_RUNTIME=runsc`. |
| Per-task gateway | One unix socket per task, mounted into that agent only (agents can't reach each other). Accepts only `POST /v1/messages` and `/v1/messages/count_tokens` with the task's one-time token; **pins the model** (the agent can't choose another); meters tokens and enforces a per-task budget; never forwards the agent's credentials or arbitrary headers. |
| Launcher | Clients pick *which* task and model to run, never *how*: workspace and socket paths are derived from the task id and validated, the command must be `claude …`, only an env allowlist passes through, venvs must live under configured roots, and a hard deadline kills each agent even if the MCP server dies. |
| MCP server | Unprivileged (uid 1000, read-only rootfs, no caps), localhost-only, bearer-token auth. Only repos under `AGENTBOX_REPOS_ROOT` are accepted. |
| Result collection | The agent's `.git` is agent-controlled (hooks, `core.fsmonitor` and config are all writable inside the sandbox). agentbox **never runs git in it**. It uses a trusted *shadow* git dir the agent never sees, plus a temporary index over the workspace files, commits the result there, and fetches the branch into your repo. |

Residual risks:
- The launcher holds `docker.sock`, which is root-equivalent; keeping its attack surface tiny is the mitigation.
- The model servers run with GPU access on the host network, bound to 127.0.0.1.
- The MCP server uses host networking so it can reach them.

## Setup

```bash
scripts/setup.sh                              # writes .env (ids, paths, fresh MCP token), creates data dirs
docker compose --profile build build          # agent image + agentbox image
docker compose up -d                          # ollama, agentbox, agentbox-launcher
scripts/smoke-test.sh                         # delegates a tiny fix; checks result + sandbox (10 checks)
```

`.env` settings (see `.env.example`):
- `AGENTBOX_DATA`: holds worktrees, gateway sockets, shadow git dirs and logs.
- `AGENTBOX_REPOS_ROOT`: the repos `delegate_task` may work on.
- `AGENTBOX_VENVS`: prebuilt venvs.
- `OLLAMA_MODELS_DIR`.
- `AGENTBOX_AGENT_UID`/`GID`: the agent identity. It should *not* exist on the host; setup warns if it does.
- Optional: `AGENTBOX_AGENT_RUNTIME=runsc` (gVisor).

Register with Claude Code (user scope):

```bash
claude mcp add --transport http local-agents http://127.0.0.1:8765/mcp --scope user \
  --header "Authorization: Bearer $(grep ^AGENTBOX_MCP_TOKEN= .env | cut -d= -f2)"
```

Long tasks: set `MCP_TOOL_TIMEOUT=7200000` in the environment Claude Code runs in.

**Skill.** `skills/local-agent-delegation` teaches the planner how to write specs a local executor
gets right, and how to review the result. Link it where Claude Code finds skills, e.g.
`ln -s "$PWD/skills/local-agent-delegation" ~/.claude/skills/`. Use `scripts/make_venv.sh` (in the skill)
to build a test venv for `python_env`.

## Tools

- `delegate_task(task, repo_path, model?, max_turns=60, timeout_minutes=30, python_env?, use_executor_guide=true, preamble=true)`
  returns the branch, status, the agent's report, the diff stat and diff, token usage, `wait_seconds`
  (backend load or queue) and `wall_seconds` (the agent run).
- `list_models()`: configured models, what's loaded, and active tasks.
- `cleanup_delegation(repo_path, branch)`: removes the scratch clone, the shadow git dir and the branch.

Command line (scripts and benchmarks use the same path):

```bash
uv run --script cli.py models
uv run --script cli.py delegate --repo ~/Projects/foo --task-file spec.md --model nemotron-3.5-lightning
```

## Models (`config/models.yaml`)

- **vllm**: the launcher starts a dedicated container on first use; loading takes about 5–7 min.
  - One vLLM model is resident at a time.
  - A model is never swapped out while agents are using it; other requests wait.
- **ollama**: served by the `ollama` service and pulled on first use.
- **One GPU family at a time.**
  - Starting a vLLM model first unloads Ollama models.
  - Using an Ollama model stops the resident vLLM container.
  - Either switch waits while agents on the other side are still running.
  - gb10-memguard remains the backstop.
- Edit the file and run `docker compose restart agentbox agentbox-launcher` to apply.
- The shipped `config/models.yaml` is tuned for an NVIDIA GB10 (128 GB unified memory). The Bolt entry
  expects a locally built `self-hosted-ai/vllm-bolt:local` image; adapt the entries to your hardware.

Logs:
- delegations: `$AGENTBOX_DATA/logs/delegations.jsonl`
- vLLM backends: `journalctl -t agentbox-<model>`
- launcher: `docker logs agentbox-launcher`
