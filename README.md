# agentbox

**Sandboxed local coding agents for Claude Code.** Your frontier model plans; an open-weight model running
on your own GPU does the hands-on work inside a hardened, network-less container, and hands the result
back as a git branch for review.

```
Claude Code (Opus / Fable plans) ──MCP──▶ agentbox ──▶ agent container (Claude Code + local model)
                                                       no network · read-only · no caps · uid 64000
                                                                │ one unix socket
                                                                ▼
                                                  vLLM (on demand) or Ollama on your GPU
```

- **One MCP tool, `delegate_task`.** Give it a spec and a repo path. A local model runs Claude Code
  headless on a scratch clone, and you get back the branch `agent/<id>`, the diff, and the agent's report.
- **Hardened by construction.** Each agent gets:
  - no network interface (`--network none`), so it can't install packages, call home, or exfiltrate code;
  - a read-only root filesystem and zero capabilities;
  - a uid that doesn't exist on the host;
  - a per-task gateway that pins the model and meters tokens.
  
  Only a tiny launcher holds the Docker socket, and it builds every container spec itself.
- **Safe result collection.** agentbox never runs git inside the agent's `.git` (which the agent controls,
  including hooks and `core.fsmonitor`). Results are committed through a trusted shadow git dir.
- **vLLM and Ollama.** Ollama serves models on demand. vLLM models are started when first needed and are
  never swapped out from under a running agent. Only one GPU family is resident at a time.
- **A planner skill.** `skills/local-agent-delegation` teaches the planner to write specs that local models
  actually get right, and to review their work before merging.

## Requirements

- Linux with Docker Engine ≥ 27 and Docker Compose v2 (`gpus:` support)
- An NVIDIA GPU with the NVIDIA Container Toolkit. Developed on an NVIDIA GB10 (128 GB unified
  memory); the shipped model configs target that class of machine.
- [`uv`](https://docs.astral.sh/uv/) for the CLI, and [Claude Code](https://claude.com/claude-code) as the planner

## Quick start

```bash
git clone https://github.com/Adithya-Rajendran/agentbox && cd agentbox
scripts/setup.sh                       # writes .env (ids, paths, fresh MCP token) and config/models.yaml
docker compose --profile build build   # agent image + agentbox image
docker compose up -d                   # ollama, agentbox (MCP), agentbox-launcher
scripts/smoke-test.sh                  # delegates a tiny bug fix, then checks the result and the sandbox
```

Register it with Claude Code:

```bash
claude mcp add --transport http local-agents http://127.0.0.1:8765/mcp --scope user \
  --header "Authorization: Bearer $(grep ^AGENTBOX_MCP_TOKEN= .env | cut -d= -f2)"
ln -s "$PWD/skills/local-agent-delegation" ~/.claude/skills/   # optional: the planner skill
```

Then, in Claude Code: *"Plan the fix, then delegate the implementation to the local model with
delegate_task, and review the branch it returns."*

## Tools

| Tool | What it does |
|---|---|
| `delegate_task(task, repo_path, model?, max_turns, timeout_minutes, python_env?, …)` | Runs the task in a sandbox. Returns the branch, status, report, diff, token usage and timings. |
| `list_models()` | Lists configured models, what's loaded now, and active tasks. |
| `cleanup_delegation(repo_path, branch)` | Removes a task's scratch clone, shadow git dir and branch. |

The same calls are available from the command line: `uv run --script cli.py models | delegate | cleanup`.

## Documentation

See the **[wiki](https://github.com/Adithya-Rajendran/agentbox/wiki)**:
[Architecture](https://github.com/Adithya-Rajendran/agentbox/wiki/Architecture) ·
[Security model](https://github.com/Adithya-Rajendran/agentbox/wiki/Security-Model) ·
[Installation](https://github.com/Adithya-Rajendran/agentbox/wiki/Installation) ·
[Configuration](https://github.com/Adithya-Rajendran/agentbox/wiki/Configuration) ·
[Using with Claude Code](https://github.com/Adithya-Rajendran/agentbox/wiki/Using-with-Claude-Code) ·
[Writing good specs](https://github.com/Adithya-Rajendran/agentbox/wiki/Writing-Good-Specs) ·
[Troubleshooting](https://github.com/Adithya-Rajendran/agentbox/wiki/Troubleshooting)

## Status

agentbox is young and built for a single trusted workstation (everything binds to `127.0.0.1`). Read the
[security model](https://github.com/Adithya-Rajendran/agentbox/wiki/Security-Model) before running it
anywhere else. The launcher holds the Docker socket, which is root-equivalent; it is small and does not
parse model output, but it is the component to audit.
