#!/bin/bash
# Render every docs/diagrams/*.excalidraw to .svg using Excalidraw's own exporter (headless Chrome),
# run locally from the Kroki Excalidraw companion image - nothing is sent to a third party.
#   docs/diagrams/render.sh
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=yuzutech/kroki-excalidraw:0.33.0
NAME=agentbox-excalidraw-render
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -p 127.0.0.1:18004:8004 "$IMAGE" >/dev/null
trap 'docker rm -f "$NAME" >/dev/null' EXIT
for _ in $(seq 60); do curl -s -o /dev/null -X POST --data-binary '{"type":"excalidraw","elements":[]}' \
  http://127.0.0.1:18004/ && break; sleep 1; done
for f in *.excalidraw; do
  curl -sf -X POST -H "Content-Type: text/plain" --data-binary "@$f" http://127.0.0.1:18004/ -o "${f%.excalidraw}.svg"
  echo "rendered ${f%.excalidraw}.svg"
done
