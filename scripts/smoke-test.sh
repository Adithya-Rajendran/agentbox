#!/bin/bash
# End-to-end smoke test: delegate a tiny bug fix to a model and verify the result AND the sandbox.
#   scripts/smoke-test.sh [model]        (default: nemotron-3.5-lightning, the Ollama model)
# Checks: task succeeds with the expected diff; agent container has no network, read-only rootfs, no caps,
# no-new-privileges, runs as AGENTBOX_AGENT_UID; only its workspace + gateway socket are mounted; and the
# returned branch passes verification via run_checks inside the sandbox (never on the host).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
MODEL="${1:-nemotron-3.5-lightning}"
REPO="$AGENTBOX_REPOS_ROOT/.agentbox-smoke-repo"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rm -rf "$REPO"; mkdir -p "$REPO/tests"
cat > "$REPO/calc.py" <<'PY'
def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)
PY
cat > "$REPO/tests/test_calc.py" <<'PY'
from calc import mean


def test_mean():
    assert mean([1, 2, 3]) == 2
PY
git -C "$REPO" init -q && git -C "$REPO" add -A && git -C "$REPO" -c user.name=smoke -c user.email=smoke@localhost commit -qm init
cat > "$TMP/task.md" <<'EOF'
GOAL: calc.mean([]) raises ZeroDivisionError; change it to raise ValueError("mean of empty list").
WHERE: calc.py::mean.
REQUIRED: mean([]) raises ValueError("mean of empty list"); mean([1, 2, 3]) still returns 2;
add test_mean_empty to tests/test_calc.py.
VERIFY: python3 -c "import calc; print(calc.mean([1,2,3]))"   (pytest may be unavailable)
CONSTRAINTS: minimal change, do not modify existing tests, do not commit.
EOF

# capture the agent container's runtime config while it runs
( for _ in $(seq 1800); do
    n=$(docker ps --format '{{.Names}}' | grep -m1 '^agentbox-agent-' || true)
    if [ -n "$n" ]; then
      docker inspect "$n" --format 'net={{.HostConfig.NetworkMode}} ro={{.HostConfig.ReadonlyRootfs}} caps={{.HostConfig.CapDrop}}+{{.HostConfig.CapAdd}} sec={{.HostConfig.SecurityOpt}} user={{.Config.User}} mounts={{len .Mounts}} runtime={{.HostConfig.Runtime}}' > "$TMP/inspect"
      # interfaces from /proc/net/dev (works under runc and gVisor, which has no /sys/class/net)
      docker exec "$n" sh -c 'awk -F: "NR>2{gsub(/ /,\"\",\$1); printf \"%s \", \$1}" /proc/net/dev; id -u' > "$TMP/inside" 2>&1 || true
      exit 0
    fi
    sleep 1
  done ) &
WATCH=$!

echo "delegating to $MODEL (first use may pull/load the model)..."
uv run -q --script cli.py delegate --repo "$REPO" --task-file "$TMP/task.md" --model "$MODEL" \
  --timeout-min 15 --max-turns 30 > "$TMP/out.json"
wait $WATCH || true
BRANCH="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('branch',''))" "$TMP/out.json")"
echo "verifying the branch with run_checks (inside the sandbox)..."
uv run -q --script cli.py check --repo "$REPO" --branch "$BRANCH" --timeout-min 5 --command \
  'python3 -c "import calc; assert calc.mean([1, 2, 3]) == 2
try:
    calc.mean([]); raise SystemExit(\"no ValueError\")
except ValueError as e:
    assert str(e) == \"mean of empty list\", e
print(\"CHECK-OK\")" && echo "uid=$(id -u) net=[$(awk -F: "NR>2{gsub(/ /,\"\",\$1); printf \"%s \", \$1}" /proc/net/dev)]"' > "$TMP/check.json" || true
python3 - "$TMP" "$AGENTBOX_AGENT_UID" "${AGENTBOX_AGENT_RUNTIME:-}" <<'PY'
import json, sys, pathlib
tmp, agent_uid, want_rt = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
r = json.loads((tmp / "out.json").read_text())
ins = (tmp / "inspect").read_text().strip() if (tmp / "inspect").exists() else ""
inside = (tmp / "inside").read_text().split() if (tmp / "inside").exists() else []
checks = {
    "task succeeded": r.get("status") == "success",
    "diff raises ValueError": 'raise ValueError("mean of empty list")' in r.get("diff", ""),
    "branch returned": str(r.get("branch", "")).startswith("agent/"),
    "network none": "net=none" in ins,
    "read-only rootfs": "ro=true" in ins,
    "all caps dropped, none added": "caps=[ALL]+[]" in ins,
    "no-new-privileges": "no-new-privileges:true" in ins,
    f"runs as uid {agent_uid}": f"user={agent_uid}:" in ins and inside[-1:] == [agent_uid],
    "only lo interface": inside[:-1] == ["lo"],
    "exactly 2 mounts (workspace, gateway)": "mounts=2" in ins,
}
chk = json.loads((tmp / "check.json").read_text() or "{}") if (tmp / "check.json").exists() else {}
checks["run_checks passes in sandbox"] = chk.get("passed") is True and "CHECK-OK" in chk.get("stdout_tail", "")
checks[f"run_checks sandbox: uid {agent_uid}, only lo"] = f"uid={agent_uid} net=[lo ]" in chk.get("stdout_tail", "")
rt = ins.split("runtime=")[-1].strip() if "runtime=" in ins else ""
if want_rt:
    checks[f"sandbox runtime is {want_rt}"] = rt == want_rt
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'}  {k}")
print(f"  ({r.get('wall_seconds')}s agent, {r.get('wait_seconds')}s wait, usage {r.get('usage')})")
if not want_rt:
    print("  NOTE  sandboxes use runc (shared host kernel); gVisor is recommended: set AGENTBOX_AGENT_RUNTIME=runsc")
if not all(checks.values()):
    print(json.dumps(r, indent=1)[:3000]); sys.exit(1)
print("SMOKE TEST PASSED")
PY
uv run -q --script cli.py cleanup --repo "$REPO" --branch "$BRANCH" >/dev/null
rm -rf "$REPO"
