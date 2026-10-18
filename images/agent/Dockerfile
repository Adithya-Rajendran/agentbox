# Hardened coding-agent image: Claude Code (headless) on Chainguard wolfi-base.
# Runs as uid 65532, no package manager at runtime, intended for `--network none` + socket gateway.
# Pin the base by digest in CI; free Chainguard tier only publishes :latest.
ARG BASE=cgr.dev/chainguard/wolfi-base:latest
ARG CLAUDE_CODE_VERSION=2.1.288

FROM ${BASE} AS fetch
ARG CLAUDE_CODE_VERSION
RUN apk add --no-cache nodejs-24 npm
RUN npm install -g --prefix /opt/claude --allow-scripts=@anthropic-ai/claude-code "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && /opt/claude/bin/claude --version

FROM ${BASE}
RUN apk add --no-cache bash coreutils findutils grep sed diffutils patch make build-base \
      git ripgrep jq curl socat nodejs-24 npm python-3.13 uv ca-certificates \
 && rm -rf /sbin/apk /usr/bin/apk /etc/apk /lib/apk /var/cache/apk /usr/share/apk \
 && rm -f /bin/su /usr/bin/su /usr/bin/sudo /usr/bin/doas /usr/bin/newgrp /usr/bin/chsh /usr/bin/chfn /usr/bin/passwd \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null; true
COPY --from=fetch /opt/claude /opt/claude
RUN ln -s /opt/claude/bin/claude /usr/local/bin/claude
COPY shim.sh /usr/local/bin/agent-shim
COPY claude-seed.json /etc/claude-seed.json
# executor guide -> installed as ~/.claude/CLAUDE.md (user memory) by agent-shim on every start
COPY EXECUTOR.md /etc/claude-executor.md
RUN chmod 0755 /usr/local/bin/agent-shim && mkdir -p /workspace /home/agent && chmod 0777 /home/agent
ENV HOME=/home/agent \
    ANTHROPIC_BASE_URL=http://127.0.0.1:4000 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_ERROR_REPORTING=1 \
    IS_SANDBOX=1
USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/agent-shim"]
CMD ["claude", "--version"]
