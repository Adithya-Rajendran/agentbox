# agentbox: MCP server + launcher (same image, different entrypoints). Chainguard wolfi-base, non-root,
# no package manager at runtime. The launcher is the only container that gets the Docker socket.
ARG BASE=cgr.dev/chainguard/wolfi-base:latest
FROM ${BASE}
RUN apk add --no-cache python-3.13 uv git ca-certificates
COPY requirements.lock /tmp/requirements.lock
RUN uv venv -q --python /usr/bin/python3 /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install -q --no-cache -r /tmp/requirements.lock \
 && apk del --no-cache uv \
 && rm -rf /sbin/apk /usr/bin/apk /etc/apk /lib/apk /var/cache/apk /usr/share/apk /tmp/requirements.lock \
 && rm -f /bin/su /usr/bin/su /usr/bin/sudo /usr/bin/doas /usr/bin/newgrp /usr/bin/chsh /usr/bin/chfn /usr/bin/passwd \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null; true
COPY agentbox /opt/agentbox/agentbox
COPY config /etc/agentbox-config
ENV PATH=/opt/venv/bin:/usr/bin:/bin PYTHONPATH=/opt/agentbox PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/tmp GIT_CONFIG_NOSYSTEM=1
USER 65532:65532
ENTRYPOINT ["python3", "-m"]
CMD ["agentbox.server"]
