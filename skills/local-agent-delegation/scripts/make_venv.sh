#!/bin/bash
# Build a Python venv for a repo ON THE HOST (network allowed) so a network-less local agent can run its tests.
# Pass the printed path as delegate_task(python_env=...). The agent sees it read-only at /opt/venv.
#   make_venv.sh <repo_path> [extra packages...]
# Installs: the repo itself (editable), pytest, any dependency-groups whose name contains "test" or "dev",
# and common requirements/test files. Re-running reuses the venv. The editable install may write build
# metadata into the repo (e.g. *.egg-info, a generated _version.py) - normally gitignored.
#
# The venv is built INSIDE the agent image (same Python as the executor) at /opt/venv, with the repo mounted at
# /workspace, so paths baked into the venv (editable installs, shebangs) are valid inside the container.
set -euo pipefail
REPO="$(realpath "${1:?usage: make_venv.sh <repo_path> [extra packages...]}")"; shift || true
EXTRA="$*"
NAME="$(basename "$REPO")-$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo nogit)"
VENV="${LOCAL_AGENT_VENVS:-$HOME/.cache/local-agent-venvs}/$NAME"
mkdir -p "$VENV"
uid=$(stat -c %u "$REPO"); gid=$(stat -c %g "$REPO")
docker run --rm --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:size=4g --tmpfs /home/agent:size=1g,uid=$uid,gid=$gid -e HOME=/home/agent -e UV_CACHE_DIR=/tmp/uv \
  -v "$REPO":/workspace -v "$VENV":/opt/venv -w /workspace --entrypoint bash agentbox/agent:local -c '
set -e
[ -x /opt/venv/bin/python ] || uv venv -q --python /usr/bin/python3 /opt/venv
export VIRTUAL_ENV=/opt/venv
uv pip install -q -e . pytest '"$EXTRA"' >/dev/null
for g in $(python3 -c "import tomllib,pathlib;p=pathlib.Path(\"pyproject.toml\");g=tomllib.loads(p.read_text()).get(\"dependency-groups\",{}) if p.exists() else {};print(\" \".join(k for k in g if \"test\" in k or \"dev\" in k))"); do
  uv pip install -q --group "$g" >/dev/null 2>&1 || true
done
for f in requirements/tests.txt requirements/test.txt requirements-test.txt requirements-dev.txt test-requirements.txt; do
  [ -f "$f" ] && uv pip install -q -r "$f" >/dev/null 2>&1 || true
done
/opt/venv/bin/python -m pytest --version >/dev/null
' >&2
echo "$VENV"
