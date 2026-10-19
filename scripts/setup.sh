#!/bin/bash
# One-time setup: write .env (if missing) with detected ids and a fresh MCP token, and create the data
# directories as the current user (Docker would create missing bind-mount sources as root).
set -euo pipefail
cd "$(dirname "$0")/.."
GVISOR_DOCS=https://github.com/Adithya-Rajendran/agentbox/wiki/Run-Agents-under-gVisor
# gVisor status: "ready" (runsc registered with --host-uds=open), "needs-uds", or "absent"
gvisor_status() {
  docker info --format '{{json .Runtimes}}' 2>/dev/null | python3 -c '
import json, sys
r = json.load(sys.stdin).get("runsc")
args = (r or {}).get("runtimeArgs") or []
print("absent" if r is None else "ready" if any(a in ("--host-uds=open", "--host-uds=all") for a in args) else "needs-uds")' \
    2>/dev/null || echo absent
}
GVISOR="$(gvisor_status)"
if [ ! -f .env ]; then
  DATA="${AGENTBOX_DATA:-$HOME/.local/share/agentbox}"
  sed -e "s|^AGENTBOX_UID=.*|AGENTBOX_UID=$(id -u)|" \
      -e "s|^AGENTBOX_GID=.*|AGENTBOX_GID=$(id -g)|" \
      -e "s|^DOCKER_GID=.*|DOCKER_GID=$(getent group docker | cut -d: -f3)|" \
      -e "s|^AGENTBOX_DATA=.*|AGENTBOX_DATA=$DATA|" \
      -e "s|^AGENTBOX_REPOS_ROOT=.*|AGENTBOX_REPOS_ROOT=${AGENTBOX_REPOS_ROOT:-$HOME/Projects}|" \
      -e "s|^AGENTBOX_VENVS=.*|AGENTBOX_VENVS=${AGENTBOX_VENVS:-$HOME/.cache/local-agent-venvs}|" \
      -e "s|^OLLAMA_MODELS_DIR=.*|OLLAMA_MODELS_DIR=${OLLAMA_MODELS_DIR:-$DATA/ollama}|" \
      -e "s|^AGENTBOX_MCP_TOKEN=.*|AGENTBOX_MCP_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')|" \
      -e "s|^AGENTBOX_BRIDGE_TOKEN=.*|AGENTBOX_BRIDGE_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')|" \
      .env.example > .env
  if [ "$GVISOR" = ready ]; then
    sed -i 's|^# AGENTBOX_AGENT_RUNTIME=runsc|AGENTBOX_AGENT_RUNTIME=runsc|' .env
    echo "gVisor (runsc) detected: agents will run under it (AGENTBOX_AGENT_RUNTIME=runsc)"
  fi
  chmod 600 .env
  echo "wrote .env"
fi
grep -q '^AGENTBOX_BRIDGE_TOKEN=' .env || echo "AGENTBOX_BRIDGE_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" >> .env
if [ ! -f secrets.env ]; then
  cp secrets.env.example secrets.env && chmod 600 secrets.env
  echo "wrote secrets.env (provider API keys for llm-bridge; leave empty for local-only use)"
fi
[ -f config/models.yaml ] || { cp config/models.example.yaml config/models.yaml; echo "wrote config/models.yaml (edit paths/models for your machine)"; }
set -a; . ./.env; set +a
if getent passwd "$AGENTBOX_AGENT_UID" >/dev/null; then
  echo "warning: agent uid $AGENTBOX_AGENT_UID exists on this host ($(getent passwd "$AGENTBOX_AGENT_UID" | cut -d: -f1)); pick an unused one" >&2
fi
mkdir -p "$AGENTBOX_DATA"/{run,worktrees,sockets,git,logs} "$OLLAMA_MODELS_DIR" "$AGENTBOX_VENVS"
# only the agentbox user may list or enter the state dirs (agents reach their own dirs via bind mounts)
chmod 700 "$AGENTBOX_DATA" "$AGENTBOX_DATA"/{run,worktrees,sockets,git,logs}
echo "data dirs ready under $AGENTBOX_DATA"
case "${AGENTBOX_AGENT_RUNTIME:-}:$GVISOR" in
  runsc:ready) echo "sandbox runtime: gVisor (runsc)" ;;
  runsc:needs-uds) echo "error: runsc lacks --host-uds=open; run: sudo runsc install -- --host-uds=open && sudo systemctl reload docker" >&2 ;;
  runsc:absent) echo "error: AGENTBOX_AGENT_RUNTIME=runsc but runsc is not a Docker runtime; see $GVISOR_DOCS" >&2 ;;
  :ready) echo "recommended: gVisor is installed - set AGENTBOX_AGENT_RUNTIME=runsc in .env, then docker compose up -d" ;;
  :needs-uds) echo "recommended: enable gVisor - sudo runsc install -- --host-uds=open && sudo systemctl reload docker, then set AGENTBOX_AGENT_RUNTIME=runsc in .env" ;;
  *) echo "recommended (optional): run agents under gVisor for a stronger sandbox - $GVISOR_DOCS" ;;
esac
echo "next: docker compose --profile build build && docker compose up -d"
