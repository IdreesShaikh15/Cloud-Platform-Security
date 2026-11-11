#!/usr/bin/env python3
"""Generates Fig. 1 and Fig. 2 (SVG source).  Render to PDF/PNG with render.js.
Labels follow docs/PAPER-SECTION-III.md; nothing here is data, it is a drawing."""
import sys
from xml.sax.saxutils import escape

FONT = "Arial, Helvetica, FreeSans, 'Liberation Sans', sans-serif"
HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        'font-family="' + FONT + '">\n<title>{t}</title>\n<rect width="100%" height="100%" fill="#fff"/>\n'
        '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#000"/></marker></defs>\n')


class S:
    def __init__(s, w, h, title):
        s.w, s.h, s.o = w, h, [HEAD.format(w=w, h=h, t=escape(title))]
        s.late = []                      # drawn last (solid connectors with white halo)

    def rect(s, x, y, w, h, fill="#fff", stroke="#000", sw=1.2, dash=None, rx=0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        s.o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def text(s, x, y, t, size=13, bold=False, italic=False, anchor="middle", rot=None, fill="#000"):
        a = f' transform="rotate({rot} {x} {y})"' if rot is not None else ""
        st = (' font-weight="bold"' if bold else "") + (' font-style="italic"' if italic else "")
        s.o.append(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" fill="{fill}"{st}{a}>{escape(t)}</text>')

    def lines(s, cx, cy, ls, size=13, bold=False, lh=None):
        lh = lh or size * 1.25
        y0 = cy - lh * (len(ls) - 1) / 2 + size * 0.35
        for i, t in enumerate(ls):
            s.text(cx, round(y0 + i * lh, 1), t, size, bold)

    def box(s, x, y, w, h, ls, size=13, bold=False, **kw):
        s.rect(x, y, w, h, **kw); s.lines(x + w / 2, y + h / 2, ls, size, bold)

    def path(s, pts, arrow=True, both=False, dash=None, sw=1.2, halo=True):
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        da = f' stroke-dasharray="{dash}"' if dash else ""
        mk = (' marker-end="url(#a)"' if arrow else "") + (' marker-start="url(#a)"' if both else "")
        if halo:
            s.late.append(f'<path d="{d}" fill="none" stroke="#fff" stroke-width="{sw + 4}"/>')
        s.late.append(f'<path d="{d}" fill="none" stroke="#000" stroke-width="{sw}"{da}{mk}/>')

    def diamond(s, cx, cy, w, h, ls, size=13):
        s.o.append(f'<polygon points="{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}" fill="#fff" stroke="#000" stroke-width="1.2"/>')
        s.lines(cx, cy, ls, size)

    def save(s, fn):
        open(fn, "w").write("\n".join(s.o + s.late) + "\n</svg>\n")


def fig1():
    s = S(1040, 818, "Fig. 1 Overall architecture")
    X0, X1 = 46, 984
    # layers
    L = [(5, 150, ["Healthcare", "Workload Layer"]),
         (160, 482, ["Distributed Security Agent", "and Decision Layer"]),
         (655, 148, ["Containment and", "Recovery Layer"])]
    for y, h, t in L:
        s.rect(5, y, 990, h, fill="#fafafa", sw=1.4)
        s.rect(5, y, 34, h, fill="#e9e9e9", sw=1.4)
        s.text(22, y + h / 2, "", 1)
        for i, line in enumerate(t):
            s.text(15 + i * 14, y + h / 2, line, 12, True, rot=-90)
    # workloads
    s.rect(50, 15, 935, 105, fill="#fff", dash="5,3")
    s.text(58, 31, "Containerized Healthcare Application Environment", 12, True, anchor="start")
    s.late.append('<path d="M700,30 L722,30" stroke="#000" stroke-width="1.2" fill="none"/>')
    s.text(728, 34, "co-located agent monitors workload", 11, anchor="start")
    s.late.append('<path d="M700,46 L722,46" stroke="#000" stroke-width="1.2" stroke-dasharray="4,3" fill="none"/>')
    s.text(728, 50, "all four agents monitor E and F", 11, anchor="start")
    wc = []
    for i, n in enumerate("ABCDEF"):
        x = 65 + i * 153
        s.box(x, 58, 135, 40, [f"Workload {n}"], 13, True)
        wc.append(x + 67.5)
    # agents
    AY, AH, AW, GAP = 226, 176, 218, 22
    ac = []
    for i, n in enumerate("ABCD"):
        x = X0 + i * (AW + GAP)
        s.rect(x, AY, AW, AH)
        s.text(x + AW / 2, AY + 20, f"Agent {n}", 14, True)
        for j, lab in enumerate(["Workload Monitoring", "Rule-Based Detection", "Evidence Generation",
                                 "Ed25519 Signing", "Local Trust Assessment"]):
            s.box(x + 14, AY + 32 + j * 28, AW - 28, 23, [lab], 12, rx=2, fill="#f2f2f2", sw=0.9)
        ac.append(x)
    # solid monitoring links (halo, drawn last)
    s.path([(wc[0], 98), (wc[0], AY)])                                   # A -> Agent A
    s.path([(wc[1], 98), (wc[1], 172), (ac[1] + 40, 172), (ac[1] + 40, AY)])   # B -> Agent B
    s.path([(wc[2], 98), (wc[2], 186), (ac[2] + 40, 186), (ac[2] + 40, AY)])   # C -> Agent C
    s.path([(wc[3], 98), (wc[3], 200), (ac[3] + 40, 200), (ac[3] + 40, AY)])   # D -> Agent D
    # dashed links E, F -> all four agents (bus)
    by = 212
    drops = [ac[0] + 24, ac[1] + 24 + 120, ac[2] + 24 + 120, ac[3] + 24 + 120]
    drops = [ac[0] + 24, ac[1] + 150, ac[2] + 150, ac[3] + 170]
    s.o.append(f'<path d="M{drops[0]},{by} L{drops[3]},{by}" fill="none" stroke="#000" stroke-width="1.2" stroke-dasharray="4,3"/>')
    for e in (wc[4], wc[5]):
        s.o.append(f'<path d="M{e},98 L{e},{by}" fill="none" stroke="#000" stroke-width="1.2" stroke-dasharray="4,3"/>')
    for d in drops:
        s.o.append(f'<path d="M{d},{by} L{d},{AY}" fill="none" stroke="#000" stroke-width="1.2" stroke-dasharray="4,3" marker-end="url(#a)"/>')
    # peer-to-peer mesh below agents
    yb = AY + AH
    off = lambda i, j: ac[i] + AW / 2 + (j - i) * 14
    lvl = {(0, 1): yb + 20, (1, 2): yb + 20, (2, 3): yb + 20, (0, 2): yb + 38, (1, 3): yb + 54, (0, 3): yb + 70}
    for (i, j), y in lvl.items():
        s.path([(off(i, j), yb), (off(i, j), y), (off(j, i), y), (off(j, i), yb)], both=True, sw=1.1)
    s.text(515, yb + 94, "Peer-to-peer gRPC + mTLS links between all agents", 12.5, True)
    s.text(515, yb + 111, "Ed25519-signed evidence, votes and certificate shares", 12, italic=True)
    # decision chain
    dy = 540
    s.rect(X0, dy - 12, X1 - X0, 108, fill="#fff", dash="5,3")
    s.text(515, dy + 80, "Independent local trust and decision state at each agent", 12, italic=True)
    bw, gp = 250, 94
    xs = [X0 + 10 + k * (bw + gp - 20) for k in range(3)]
    xs = [60, 60 + bw + 60, 60 + 2 * (bw + 60)]
    names = [["Observer-Specific", "Trust Management"], ["Trust-Weighted", "Evidence Correlation"],
             ["Three-of-Four Quorum Authorization"]]
    for k, x in enumerate(xs):
        if k < 2:
            s.box(x, dy, bw, 56, names[k], 13, True)
        else:
            s.rect(x, dy, bw, 56)
            s.text(x + bw / 2, dy + 24, names[k][0], 13, True)
            s.text(x + bw / 2, dy + 43, "3-of-4 Application-Level Authorization", 11.5)
        if k:
            s.path([(xs[k - 1] + bw, dy + 28), (x, dy + 28)])
    # containment and recovery
    cy, ch, cw, cg = 690, 62, 172, 20
    boxes = [["Forensic Snapshot", "Initiation"], ["Kubernetes NetworkPolicy", "Isolation"],
             ["Known-Good Container", "Image Replacement"], ["Recovery", "Validation"],
             ["Controlled Workload", "Reintegration"]]
    cx = []
    for k, b in enumerate(boxes):
        x = X0 + k * (cw + cg)
        s.box(x, cy, cw, ch, b, 12.5, True)
        cx.append(x + cw / 2)
        if k and k != 1:
            s.path([(x - cg, cy + ch / 2), (x, cy + ch / 2)])
    pad = 4
    s.rect(X0 - pad, cy - 7, 2 * cw + cg + 2 * pad, ch + 14, fill="none", dash="4,3", sw=1.0)
    s.text(X0, cy + ch + 20, "started concurrently", 11, italic=True, anchor="start")
    mid = X0 + cw + cg / 2
    qx = xs[2] + bw - 40
    s.path([(qx, dy + 56), (qx, 672), (mid, 672), (mid, cy - 7)])
    s.text((qx + mid) / 2, 666, "authorized protective action", 11, italic=True)
    # feedback to workload layer
    fx = 1016
    s.path([(cx[4], cy + ch), (cx[4], 784), (fx, 784), (fx, 70), (985, 70)])
    s.text(1029, 430, "Recovered workload re-enters the application environment", 11, italic=True, rot=-90)
    s.save("fig1_architecture.svg")


def fig2():
    s = S(1350, 1530, "Fig. 2 Containment and recovery workflow")
    cx, bw, bh = 420, 480, 52
    ys = [24, 112, 200, 288, 376]
    names = ["Suspicious Workload Detected", "Generate Security Evidence",
             "Authenticate Evidence Using Ed25519", "Trust-Weighted Evidence Correlation",
             "Three-of-Four Quorum Authorization"]
    for y, n in zip(ys, names):
        s.box(cx - bw / 2, y, bw, bh, [n], 15, True)
    for a, b in zip(ys[:-1], ys[1:]):
        s.path([(cx, a + bh), (cx, b)], halo=False)
    d1y = 548
    s.path([(cx, ys[-1] + bh), (cx, d1y - 80)], halo=False)
    s.diamond(cx, d1y, 540, 160, ["Are at least three eligible", "agents authorizing", "the same action?"], 14)
    s.path([(cx + 270, d1y), (800, d1y)], halo=False); s.text(cx + 285, d1y - 8, "NO", 13, True, anchor="start")
    s.box(800, d1y - 30, 360, 60, ["Protective Action Withheld"], 15, True)
    # --- parallel: forensic snapshot initiation || NetworkPolicy isolation
    s.text(cx + 12, d1y + 102, "YES", 13, True, anchor="start")
    yf, py = 668, 696
    lx, rx_ = cx - 125, cx + 125            # centres of the two parallel boxes
    s.path([(cx, d1y + 80), (cx, yf)], arrow=False, halo=False)
    s.path([(lx, yf), (rx_, yf)], arrow=False, halo=False)
    s.path([(lx, yf), (lx, py)], halo=False); s.path([(rx_, yf), (rx_, py)], halo=False)
    pw = 232
    s.box(lx - pw / 2, py, pw, bh, ["Start Forensic Snapshot"], 14, True)
    s.box(rx_ - pw / 2, py, pw, bh, ["Apply NetworkPolicy", "Isolation"], 14, True)
    s.text(rx_ + pw / 2 + 14, py + 14, "started concurrently after quorum", 12, italic=True, anchor="start")
    yj, rep = py + bh + 30, py + bh + 60
    s.path([(lx, py + bh), (lx, yj)], arrow=False, halo=False)
    s.path([(rx_, py + bh), (rx_, yj)], arrow=False, halo=False)
    s.path([(lx, yj), (rx_, yj)], arrow=False, halo=False)
    s.path([(cx, yj), (cx, rep)], halo=False)
    s.text(rx_ + pw / 2 + 14, py + bh + 22, "replacement waits for confirmed isolation", 12, italic=True, anchor="start")
    s.text(rx_ + pw / 2 + 14, py + bh + 38, "and snapshot completion (snapshot wait capped at 15 s;", 12, italic=True, anchor="start")
    s.text(rx_ + pw / 2 + 14, py + bh + 54, "capture then continues in the background)", 12, italic=True, anchor="start")
    s.box(cx - bw / 2, rep, bw, bh, ["Replace Workload", "Using Known-Good Container Image"], 15, True)
    val = rep + 88
    s.path([(cx, rep + bh), (cx, val)], halo=False)
    s.box(cx - bw / 2, val, bw, bh, ["Perform Recovery Validation"], 15, True)
    d2y = val + bh + 34 + 65
    s.path([(cx, val + bh), (cx, d2y - 65)], halo=False)
    s.diamond(cx, d2y, 420, 130, ["Validation Passed?", "(Three-of-Four Quorum)"], 15)
    # retry branch
    rx, rw = 800, 380
    s.path([(cx + 210, d2y), (rx, d2y)], halo=False); s.text(cx + 222, d2y - 8, "NO", 13, True, anchor="start")
    s.text(cx + 222, d2y + 16, "failure persists > 90 s", 11, italic=True, anchor="start")
    s.box(rx, d2y - 36, rw, 72, ["Quorum-Authorized Retry Recovery", "(Maximum Three Retries)"], 15, True)
    ry = rep + bh / 2
    s.path([(rx + rw / 2, d2y - 36), (rx + rw / 2, ry), (cx + bw / 2, ry)], halo=False)
    s.text(rx + rw / 2 + 10, (ry + d2y - 36) / 2, "retry: replace again", 12, italic=True, anchor="start")
    s.path([(rx + rw / 2, d2y + 36), (rx + rw / 2, d2y + 96)], halo=False)
    s.text(rx + rw / 2 + 10, d2y + 72, "retries exhausted", 12, italic=True, anchor="start")
    s.box(rx, d2y + 96, rw, 52, ["Needs Attention"], 15, True)
    # reintegration
    ytop = d2y + 190
    s.path([(cx, d2y + 65), (cx, d2y + 98), (155, d2y + 98), (155, ytop)], halo=False)
    s.text(cx + 12, d2y + 86, "YES", 13, True, anchor="start")
    s.text(185, ytop - 12, "Staged reintegration: each stage is entered only after its own three-of-four quorum", 14, True, anchor="start")
    stg = [("QUARANTINE", 0), ("RESTRICTED", 20), ("MONITORED", 40), ("PEER_VALIDATED", 60), ("FULL", 80)]
    sw_, sg = 215, 48
    for k, (n, m) in enumerate(stg):
        x = 40 + k * (sw_ + sg)
        s.rect(x, ytop, sw_, 84)
        s.text(x + sw_ / 2, ytop + 34, n, 15, True)
        s.text(x + sw_ / 2, ytop + 58, f"Workload trust to enter: {m}", 12.5)
        if k:
            s.path([(x - sg, ytop + 42), (x, ytop + 42)], halo=False)
            s.text(x - sg / 2, ytop + 34, "3-of-4", 11, italic=True)
    s.text(675, ytop + 114, "Workload Trust Required to Enter: 0, 20, 40, 60, 80", 13, True)
    s.text(675, ytop + 134, "Each transition: separate three-of-four quorum, at least 10 s in the current stage, local anomaly confidence below 0.3.", 12.5, italic=True)
    hx = 40 + 4 * (sw_ + sg) + sw_ / 2
    s.path([(hx, ytop + 84), (hx, ytop + 160)], halo=False)
    s.rect(hx - 150, ytop + 160, 300, 66)
    s.text(hx, ytop + 186, "Healthy Workload Operation", 15, True)
    s.text(hx, ytop + 208, "after the cluster confirms the isolation policy is removed", 11, italic=True)
    s.h = ytop + 245
    s.o[0] = s.o[0].replace('height="1530"', f'height="{s.h}"').replace("0 0 1350 1530", f"0 0 1350 {s.h}")
    s.save("fig2_workflow.svg")


def fig3():
    s = S(1040, 610, "Fig. Experimental testbed")
    s.box(220, 12, 600, 58, ["Agent code (identical in both environments)"], 14, True, fill="#f2f2f2")
    s.text(520, 58, "signing, gRPC + mTLS, trust, voting, certificates, admission logic", 12, italic=True)
    # simulator panel
    s.rect(15, 105, 490, 355, fill="#fafafa", sw=1.4)
    s.text(260, 128, "Simulator", 15, True)
    s.box(40, 150, 440, 56, ["Synthetic world", "generates telemetry and injected attacks"], 13, True)
    s.box(40, 250, 440, 56, ["Real agents A-D", "run in-process with real signatures and gRPC/mTLS"], 13, True)
    s.box(40, 350, 440, 56, ["FakeBackend", "simulated Kubernetes API and pod replacement"], 13, True)
    s.path([(260, 206), (260, 250)]); s.text(272, 232, "telemetry", 11, italic=True, anchor="start")
    s.path([(260, 306), (260, 350)]); s.text(272, 332, "actions", 11, italic=True, anchor="start")
    s.text(260, 435, "20 seeded runs per scenario, 1,140 trials in total; shorter timers", 12, italic=True)
    # sandbox panel
    s.rect(535, 105, 490, 355, fill="#fafafa", sw=1.4)
    s.text(780, 128, "Kubernetes sandbox", 15, True)
    s.text(780, 146, "Minikube, 4 nodes, Calico, Kubernetes 1.34.12", 12)
    s.box(555, 165, 450, 62, ["Namespace healthcare", "workloads A-F and a traffic client (Synthea data)"], 13, True)
    s.box(555, 270, 450, 62, ["Namespace resilience", "agents A-D, dashboard, two webhook replicas"], 13, True)
    s.box(555, 375, 450, 56, ["Kubernetes API (K8sBackend)", "NetworkPolicy, Deployment updates"], 13, True)
    s.path([(780, 270), (780, 227)]); s.text(792, 253, "monitor", 11, italic=True, anchor="start")
    s.path([(780, 332), (780, 375)]); s.text(792, 357, "certificate-checked actions", 11, italic=True, anchor="start")
    s.text(780, 448, "harmless tripwire only; single runs", 12, italic=True)
    # shared code arrows
    s.path([(300, 70), (300, 105), (260, 105), (260, 250)], halo=False, arrow=False) if False else None
    s.path([(330, 70), (330, 88), (260, 88), (260, 105)], halo=False)
    s.path([(710, 70), (710, 88), (780, 88), (780, 105)], halo=False)
    # own cluster
    s.rect(15, 490, 1010, 100, dash="6,4")
    s.text(520, 520, "Authors' own four-node cluster (Kubernetes 1.37.0)", 14, True)
    s.text(520, 545, "dashboard and normal-operation monitoring screenshots only; no attack was run here", 12.5)
    s.text(520, 566, "Results from the three environments are reported separately.", 12.5, italic=True)
    s.save("fig3_testbed.svg")


if __name__ == "__main__":
    fig1(); fig2(); fig3()
