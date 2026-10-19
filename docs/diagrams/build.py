#!/usr/bin/env python3
"""Generate the Excalidraw diagrams used by the README and the wiki.

    python3 docs/diagrams/build.py          # writes docs/diagrams/*.excalidraw
    docs/diagrams/render.sh                 # renders each to .svg with a local Excalidraw renderer

The .excalidraw files are the editable sources: open one on https://excalidraw.com (File > Open), edit,
save it back here, and re-run render.sh. This script only exists so the first versions are reproducible.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent
FONT, MONO = 5, 3            # Excalifont (hand-drawn), Cascadia (code)

# palette (Excalidraw defaults)
INK, MUTED = "#1e1e1e", "#757575"
BLUE, GREEN, AMBER, RED, PURPLE, TEAL = "#1971c2", "#2f9e44", "#f08c00", "#e03131", "#6741d9", "#0c8599"
F_BLUE, F_GREEN, F_ORANGE, F_RED, F_PURPLE, F_YELLOW, F_TEAL, F_GREY = (
    "#a5d8ff", "#b2f2bb", "#ffd8a8", "#ffc9c9", "#d0bfff", "#fff3bf", "#c3fae8", "#e9ecef")
Z_BLUE, Z_GREEN, Z_RED, Z_ORANGE, Z_PURPLE = "#dbe4ff", "#d3f9d8", "#ffe3e3", "#fff4e6", "#f3f0ff"


class Scene:
    def __init__(self):
        self.els: list[dict] = []
        self._n = 0
        self.rng = random.Random(7)

    def _base(self, typ: str, x, y, w, h, **kw) -> dict:
        self._n += 1
        el = {"type": typ, "id": kw.pop("id", f"{typ[:2]}{self._n}"), "x": x, "y": y, "width": w, "height": h,
              "angle": 0, "strokeColor": INK, "backgroundColor": "transparent", "fillStyle": "solid",
              "strokeWidth": 2, "strokeStyle": "solid", "roughness": 1, "opacity": 100, "groupIds": [],
              "frameId": None, "roundness": None, "seed": self.rng.randint(1, 2**31),
              "version": 1, "versionNonce": self.rng.randint(1, 2**31), "isDeleted": False,
              "boundElements": [], "updated": 1, "link": None, "locked": False}
        el.update(kw)
        self.els.append(el)
        return el

    def _text_el(self, text, cx, cy, size, color, family, container=None, align="center"):
        lines = text.split("\n")
        w = max(len(l) for l in lines) * size * (0.6 if family == MONO else 0.55)
        h = len(lines) * size * 1.25
        x = cx - w / 2 if align == "center" else cx
        return self._base("text", x, cy - h / 2, w, h, strokeColor=color, text=text, originalText=text,
                          fontSize=size, fontFamily=family, textAlign=align, verticalAlign="middle",
                          containerId=container, autoResize=True, lineHeight=1.25, boundElements=None)

    def box(self, x, y, w, h, text="", fill=F_BLUE, stroke=INK, size=18, family=FONT, dashed=False,
            shape="rectangle", color=INK, rounded=True):
        el = self._base(shape, x, y, w, h, backgroundColor=fill, strokeColor=stroke,
                        strokeStyle="dashed" if dashed else "solid",
                        roundness={"type": 3} if rounded and shape == "rectangle" else (
                            {"type": 2} if shape != "rectangle" else None))
        if text:
            t = self._text_el(text, x + w / 2, y + h / 2, size, color, family, container=el["id"])
            el["boundElements"].append({"type": "text", "id": t["id"]})
        return el

    def zone(self, x, y, w, h, title, fill, stroke, size=18, color=None):
        self._base("rectangle", x, y, w, h, backgroundColor=fill, strokeColor=stroke, strokeWidth=1,
                   strokeStyle="dashed", roundness={"type": 3}, opacity=60)
        self.text(title, x + 14, y + 18, size=size, color=color or stroke, align="left")

    def text(self, text, x, y, size=16, color=INK, family=FONT, align="center"):
        """x,y = centre (align=center) or left edge / vertical centre of first line (align=left)."""
        lines = text.split("\n")
        cy = y + (len(lines) - 1) * size * 1.25 / 2 if align == "left" else y
        return self._text_el(text, x, cy, size, color, family, align=align)

    def arrow(self, pts, label="", color=INK, dashed=False, size=15, both=False, start=None, end=None,
              label_at=0.5, label_dy=-14, width=2, label_dx=0):
        x0, y0 = pts[0]
        rel = [[px - x0, py - y0] for px, py in pts]
        xs, ys = [p[0] for p in rel], [p[1] for p in rel]
        el = self._base("arrow", x0, y0, max(xs) - min(xs), max(ys) - min(ys), points=rel, strokeColor=color,
                        strokeStyle="dashed" if dashed else "solid", strokeWidth=width,
                        roundness=None if len(pts) > 2 else {"type": 2}, lastCommittedPoint=None,
                        startArrowhead="arrow" if both else None,
                        endArrowhead="arrow", startBinding=None, endBinding=None, elbowed=False)
        for key, b in (("startBinding", start), ("endBinding", end)):
            if b:
                el[key] = {"elementId": b["id"], "focus": 0, "gap": 4}
                b["boundElements"].append({"type": "arrow", "id": el["id"]})
        if label:
            # free-standing label near the chosen point of the polyline (kept readable above the line)
            seg = max(1, len(pts) - 1)
            i = min(int(label_at * seg), seg - 1)
            f = label_at * seg - i
            mx = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f
            my = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f
            self.text(label, mx + label_dx, my + label_dy, size=size, color=color if color != INK else MUTED)
        return el

    def save(self, name: str):
        doc = {"type": "excalidraw", "version": 2, "source": "https://github.com/Adithya-Rajendran/agentbox",
               "elements": self.els, "appState": {"viewBackgroundColor": "#ffffff", "exportBackground": True,
                                                  "exportWithDarkMode": False, "gridSize": None},
               "files": {}}
        (OUT / f"{name}.excalidraw").write_text(json.dumps(doc, indent=1))
        print("wrote", name)


# ---------------------------------------------------------------------------------------------------------------
def architecture():
    s = Scene()
    s.text("agentbox: components", 640, 24, size=28)
    planner = s.box(20, 300, 200, 100, "Claude Code\n(planner: Opus / Fable)", F_YELLOW, AMBER, size=17)
    repos = s.box(20, 560, 200, 80, "your repos", F_TEAL, TEAL)
    s.zone(270, 70, 1010, 690, "host  (everything binds to 127.0.0.1)", Z_BLUE, BLUE)
    ab = s.box(330, 260, 290, 150, "agentbox\nMCP server · per-task gateways\nshadow-git result collection\nunprivileged", F_PURPLE, PURPLE)
    la = s.box(330, 560, 290, 110, "agentbox-launcher\nonly holder of docker.sock\nnetwork: none", F_ORANGE, AMBER)
    s.zone(700, 470, 330, 270, "sandboxes (one per task)", Z_RED, RED, size=16)
    ag = s.box(730, 510, 270, 110, "agent container\nClaude Code + local model\nno network · ro · uid 64000", F_RED, RED, size=16)
    ck = s.box(730, 640, 270, 80, "check container (run_checks)\nsame sandbox, no socket at all", F_GREY, MUTED, size=15)
    ol = s.box(700, 110, 240, 80, "Ollama\n:11434 (always on)", F_GREEN, GREEN)
    vl = s.box(700, 225, 240, 90, "vLLM backends\nstarted on demand\nby the launcher", F_GREEN, GREEN, size=16)
    br = s.box(1000, 230, 250, 110, "llm-bridge\nonly holder of API keys\ntranslate / pass-through", F_BLUE, BLUE, size=16)
    cloud = s.box(1000, 90, 250, 90, "OpenAI · Anthropic ·\nOpenAI-compatible APIs", "#ffffff", BLUE, shape="ellipse", size=16)
    s.arrow([(220, 350), (330, 350)], "MCP\n+ token", start=planner, end=ab, label_dy=-26, size=14)
    s.arrow([(120, 560), (120, 470), (330, 395)], "clone HEAD /\nfetch agent/<id>", start=repos, end=ab, both=True,
            color=TEAL, label_at=0.25, label_dx=72, label_dy=0, size=14)
    s.arrow([(475, 410), (475, 560)], "launcher.sock\n(fixed specs only)", start=ab, end=la, label_dx=80, label_dy=0, size=14)
    s.arrow([(620, 600), (730, 565)], "create · wait\nremove", start=la, end=ag, color=AMBER, label_dy=-36, size=14)
    s.arrow([(620, 640), (730, 680)], start=la, end=ck, color=AMBER)
    s.arrow([(975, 510), (975, 445), (560, 445), (560, 410)], "llm.sock: the agent's only way out", start=ag,
            end=ab, color=RED, dashed=True, label_at=0.5, label_dy=-14, size=15)
    s.arrow([(620, 300), (700, 150)], start=ab, end=ol, color=GREEN)
    s.arrow([(620, 320), (700, 270)], start=ab, end=vl, color=GREEN)
    s.arrow([(620, 350), (1000, 300)], "remote models", start=ab, end=br, color=BLUE, label_at=0.72, label_dy=14, size=14)
    s.arrow([(1125, 230), (1125, 180)], start=br, end=cloud, color=BLUE)
    s.save("architecture")


def data_path():
    s = Scene()
    s.text("How a network-less agent reaches a model", 600, 24, size=28)
    s.zone(30, 70, 520, 470, "agent container: network namespace with only lo", Z_RED, RED, size=17)
    cc = s.box(80, 120, 420, 80, "Claude Code\nANTHROPIC_BASE_URL=http://127.0.0.1:4000", F_GREY, INK, size=15)
    so = s.box(80, 260, 420, 70, "socat  127.0.0.1:4000  ->  /run/gw/llm.sock", F_YELLOW, AMBER, size=16, family=MONO)
    sk = s.box(200, 400, 180, 70, "llm.sock\n(a file)", F_ORANGE, AMBER, shape="ellipse", size=16)
    s.arrow([(290, 200), (290, 260)], "TCP on its own loopback", start=cc, end=so, label_dx=95, label_dy=0, size=14)
    s.arrow([(290, 330), (290, 400)], "unix socket", start=so, end=sk, label_dx=55, label_dy=0, size=14)
    s.text("curl 127.0.0.1:11434  ->  connection refused\n(this 127.0.0.1 is the container's own)", 290, 505,
           size=14, color=RED)
    s.zone(640, 70, 560, 470, "agentbox container: host network", Z_PURPLE, PURPLE, size=17)
    gw = s.box(690, 300, 460, 170, "per-task gateway\nchecks the task's one-time token\npins the model · strips server-side tools\nmeters tokens · enforces the budget",
               F_PURPLE, PURPLE, size=16)
    s.arrow([(380, 435), (690, 400)], "bind mount of\nsockets/<tid>/", start=sk, end=gw, color=AMBER, label_dy=-30)
    ol = s.box(690, 120, 140, 70, "Ollama\n:11434", F_GREEN, GREEN, size=16)
    vl = s.box(850, 120, 140, 70, "vLLM\n:9110", F_GREEN, GREEN, size=16)
    br = s.box(1010, 120, 140, 70, "llm-bridge\n:8790", F_BLUE, BLUE, size=16)
    for b, x in ((ol, 760), (vl, 920), (br, 1080)):
        s.arrow([(x, 300), (x, 190)], start=gw, end=b, color=GREEN if b is not br else BLUE)
    s.text("plain HTTP on the host's 127.0.0.1", 920, 240, size=14, color=MUTED)
    s.save("data-path")


def lifecycle():
    s = Scene()
    s.text("Life of a delegate_task call", 600, 22, size=28)
    cols = [("Claude Code\n(planner)", F_YELLOW, AMBER), ("agentbox", F_PURPLE, PURPLE),
            ("launcher", F_ORANGE, AMBER), ("sandbox", F_RED, RED), ("model server", F_GREEN, GREEN)]
    xs = [110, 400, 690, 980, 1270]
    for (name, fill, stroke), x in zip(cols, xs):
        s.box(x - 85, 60, 170, 64, name, fill, stroke, size=17)
        s._base("line", x, 124, 0, 820, points=[[0, 0], [0, 820]], strokeColor="#adb5bd", strokeWidth=1,
                strokeStyle="dashed", lastCommittedPoint=None, startArrowhead=None, endArrowhead=None)
    y = 170
    P, A, L, S, M = xs

    def msg(a, b, text, color=INK, dashed=False, dy=56):
        nonlocal y
        s.arrow([(a, y), (b, y)], text, color=color, dashed=dashed, size=14, label_dy=-14)
        y += dy

    def note(x, text, w=230, fill=F_YELLOW, dy=74):
        nonlocal y
        s.box(x - w / 2, y - 22, w, 52, text, fill, MUTED, size=14)
        y += dy

    msg(P, A, "1  delegate_task(spec, repo)")
    msg(A, M, "2  make sure the model is up (load / unload)", GREEN)
    note(A, "3  shadow git dir + scratch clone\nper-task gateway socket", 260, F_PURPLE)
    msg(A, L, "4  run agent (task id, model)", AMBER)
    msg(L, S, "5  chown -> uid 64000, start", AMBER)
    msg(S, A, "6  /v1/messages over llm.sock  (many turns)", RED, dashed=True)
    msg(A, M, "   ... forwarded to the pinned model", GREEN, dashed=True)
    msg(S, L, "7  exits (or deadline kill)", MUTED, dashed=True)
    msg(A, L, "8  remove, hand workspace back", AMBER)
    note(A, "9  collect via shadow git\n-> branch agent/<id> in your repo", 280, F_TEAL)
    msg(A, P, "10  branch, diff, report  (untrusted data)", PURPLE, dashed=True)
    msg(P, A, "11  run_checks(branch, tests)")
    msg(A, S, "12  same sandbox, no socket: run the tests", AMBER)
    msg(A, P, "13  passed / failed + output tail", PURPLE, dashed=True)
    note(P, "14  review, then git merge\nor re-delegate", 220, F_YELLOW)
    s.save("lifecycle")


def trust():
    s = Scene()
    s.text("Trust boundaries", 630, 24, size=28)
    s.zone(30, 70, 340, 330, "you (trusted)", Z_GREEN, GREEN)
    pl = s.box(60, 130, 280, 90, "Claude Code planner\nreviews; never runs\nagent code on the host", F_YELLOW, AMBER, size=16)
    rp = s.box(60, 280, 280, 80, "your repos\nonly receive a branch", F_TEAL, TEAL, size=16)
    s.zone(410, 70, 480, 330, "unprivileged infrastructure", Z_BLUE, BLUE)
    ab = s.box(440, 120, 240, 100, "agentbox\nno caps · read-only\nparses model output", F_PURPLE, PURPLE, size=16)
    br = s.box(650, 270, 220, 100, "llm-bridge\nholds API keys\nonly remote egress", F_BLUE, BLUE, size=16)
    s.zone(930, 70, 300, 170, "privileged (root-equivalent)", Z_ORANGE, AMBER, size=16)
    la = s.box(950, 115, 260, 100, "launcher: docker.sock\nnever sees model output\nbuilds every spec itself", F_ORANGE, AMBER, size=15)
    s.zone(440, 480, 790, 220, "untrusted: the model and the code it writes", Z_RED, RED)
    ag = s.box(470, 530, 300, 120, "agent sandbox\nno network · uid 64000 · ro\nno caps · seccomp · AppArmor", F_RED, RED, size=16)
    ck = s.box(900, 530, 300, 120, "check sandbox\nruns tests on agent/<id>\nno network, no socket", F_RED, RED, size=16)
    s.arrow([(340, 170), (440, 170)], "MCP + token", start=pl, end=ab, label_dy=-14, size=14)
    s.arrow([(440, 205), (340, 315)], start=ab, end=rp, color=PURPLE, dashed=True)
    s.text("branch +\nuntrusted report", 478, 262, size=14, color=PURPLE)
    s.arrow([(680, 165), (950, 165)], "fixed API", start=ab, end=la, color=AMBER, label_dy=-14, size=14)
    s.arrow([(640, 220), (720, 270)], start=ab, end=br, color=BLUE)
    s.arrow([(1080, 215), (1080, 530)], "starts", start=la, end=ck, color=AMBER, label_dx=30, label_dy=0, size=14)
    s.arrow([(1000, 215), (740, 530)], start=la, end=ag, color=AMBER)
    s.arrow([(560, 530), (560, 220)], start=ag, end=ab, color=RED, dashed=True)
    s.text("Messages API only:\ntoken · pinned model\nno server-side tools", 640, 435, size=14, color=RED)
    s.text("Only what crosses a boundary is shown. The agent's single channel is its gateway socket;\n"
           "its results come back as data that you review, and test inside the sandbox.", 630, 740, size=15,
           color=MUTED)
    s.save("trust-boundaries")


def collection():
    s = Scene()
    s.text("Collecting results without trusting the agent's .git", 610, 24, size=26)
    rp = s.box(30, 120, 220, 90, "your repo\nHEAD = base", F_TEAL, TEAL)
    sh = s.box(370, 120, 250, 90, "shadow git dir (bare)\nnever mounted anywhere", F_GREEN, GREEN, size=16)
    s.arrow([(250, 165), (370, 165)], "fetch HEAD", start=rp, end=sh, label_dy=-14, size=14)
    s.zone(680, 80, 510, 290, "workspace (mounted into the sandbox)", Z_RED, RED, size=16)
    wt = s.box(720, 130, 200, 90, "files\n(the agent edits these)", F_YELLOW, AMBER, size=16)
    ag = s.box(950, 130, 210, 90, "agent's .git\nhooks · fsmonitor · config\nagent-controlled", F_GREY, MUTED, size=15)
    s.arrow([(620, 150), (720, 150)], "clone", start=sh, end=wt, label_dy=-14, size=14)
    s.text("never executed by agentbox", 1055, 250, size=15, color=RED)
    col = s.box(300, 400, 760, 120, "collect (in agentbox):  git --git-dir=<shadow> --work-tree=<workspace>\n"
                "read-tree base -> add -A (temp index) -> write-tree -> commit-tree\n"
                "core.hooksPath=/dev/null · core.fsmonitor=false · trusted excludes", F_PURPLE, PURPLE, size=14,
                family=MONO)
    s.arrow([(820, 220), (820, 400)], "reads files only", start=wt, end=col, color=AMBER, label_dx=-70, label_dy=0, size=14)
    s.arrow([(495, 210), (495, 400)], "trusted config", start=sh, end=col, color=GREEN, label_dx=-65, label_dy=0, size=14)
    br = s.box(30, 420, 220, 80, "branch agent/<id>\nfetched into your repo", F_TEAL, TEAL, size=16)
    s.arrow([(300, 460), (250, 460)], start=col, end=br, color=TEAL)
    s.text("Tested: an agent planted pre-commit / post-commit / post-index-change hooks, core.hooksPath and\n"
           "core.fsmonitor. Nothing ran outside the sandbox.", 610, 580, size=15, color=MUTED)
    s.save("result-collection")


def remote():
    s = Scene()
    s.text("Remote models through llm-bridge", 610, 24, size=28)
    ag = s.box(30, 150, 200, 90, "agent sandbox\n(no network)", F_RED, RED)
    gw = s.box(300, 120, 260, 150, "gateway (agentbox)\nstrip server-side tools,\nMCP servers, betas\nmeter + budget", F_PURPLE, PURPLE, size=16)
    br = s.box(640, 100, 300, 190, "llm-bridge\nkeys from secrets.env\nopenai: Messages <-> Chat\nCompletions (+ streaming)\nanthropic: pass-through", F_BLUE, BLUE, size=16)
    s.arrow([(230, 195), (300, 195)], "Messages", start=ag, end=gw, label_dy=-14, size=14)
    s.arrow([(560, 195), (640, 195)], "+ bridge\ntoken", start=gw, end=br, label_dy=-30, size=14)
    o1 = s.box(1020, 70, 190, 70, "OpenAI API", "#ffffff", BLUE, shape="ellipse", size=16)
    o2 = s.box(1020, 160, 190, 70, "OpenAI-compatible\n(llama.cpp, SGLang ...)", "#ffffff", BLUE, shape="ellipse", size=14)
    o3 = s.box(1020, 250, 190, 70, "Anthropic API", "#ffffff", BLUE, shape="ellipse", size=16)
    for o, y in ((o1, 105), (o2, 195), (o3, 285)):
        s.arrow([(940, 195), (1020, y)], start=br, end=o, color=BLUE)
    s.zone(30, 350, 1180, 230, "translation (openai backend)", Z_BLUE, BLUE, size=16)
    rows = [("system (string or blocks)", "messages[0] role=system"),
            ("assistant tool_use {id, name, input}", "assistant tool_calls [{id, function{name, arguments}}]"),
            ("user tool_result {tool_use_id, content}", "role=tool {tool_call_id, content}"),
            ("tools [{name, input_schema}]", "tools [{type: function, function{name, parameters}}]"),
            ("SSE message_start / content_block_* / message_delta", "chat.completion.chunk deltas + usage chunk")]
    for i, (a, b) in enumerate(rows):
        yy = 410 + i * 34
        s.text(a, 70, yy, size=14, family=MONO, align="left")
        s.text("<->", 610, yy, size=14, color=BLUE)
        s.text(b, 660, yy, size=14, family=MONO, align="left")
    s.save("remote-models")


def arbitration():
    s = Scene()
    s.text("GPU arbitration: one family at a time", 520, 24, size=26)
    ol = s.box(20, 110, 290, 120, "Ollama family\nmodels load on demand\none loaded at a time", F_GREEN, GREEN, size=16)
    vl = s.box(750, 110, 290, 120, "vLLM family\none container at a time\n(minutes to load)", F_GREEN, GREEN, size=16)
    s.arrow([(310, 150), (750, 150)], "vLLM requested: unload Ollama models, start container", color=AMBER, size=14)
    s.arrow([(750, 195), (310, 195)], "Ollama requested: stop idle vLLM containers", color=AMBER, size=14,
            label_dy=18)
    s.box(260, 290, 540, 100, "If agents on the other side are still running, the switch waits\n"
          "(retry every 20 s, up to an hour) - a model is never pulled out\nfrom under a running agent. "
          "Shows up as wait_seconds.", F_YELLOW, AMBER, size=15)
    s.text("Remote models (llm-bridge) use no GPU and never wait. Host memory guards (OOM killer, earlyoom)\n"
           "remain the backstop.", 530, 440, size=14, color=MUTED)
    s.save("gpu-arbitration")


if __name__ == "__main__":
    for f in (architecture, data_path, lifecycle, trust, collection, remote, arbitration):
        f()
