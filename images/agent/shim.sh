#!/bin/bash
# Bridge the container's loopback to the gateway's unix socket(s), seed Claude Code's
# config into the (tmpfs) HOME, then exec the real command. The container has no network
# interface besides lo, so this socket is its only way out.
set -euo pipefail
SOCK="${GW_SOCKET:-/run/gw/llm.sock}"
[ -S "$SOCK" ] || { echo "agent-shim: gateway socket $SOCK missing" >&2; exit 70; }
socat TCP-LISTEN:4000,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:"$SOCK" &
[ -f "$HOME/.claude.json" ] || cp /etc/claude-seed.json "$HOME/.claude.json"
# executor discipline guide, loaded by Claude Code as user memory (AGENT_NO_GUIDE=1 disables, for A/B tests)
if [ -z "${AGENT_NO_GUIDE:-}" ] && [ -f /etc/claude-executor.md ]; then
  mkdir -p "$HOME/.claude" && cp /etc/claude-executor.md "$HOME/.claude/CLAUDE.md"
fi
for _ in $(seq 50); do (exec 3<>/dev/tcp/127.0.0.1/4000) 2>/dev/null && break; sleep 0.1; done
exec "$@"
