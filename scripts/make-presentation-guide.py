#!/usr/bin/env python3
"""Build docs/presentation-guide.pdf: a plain-English, healthcare-focused guide for a non-expert audience.

    python3 scripts/make-presentation-guide.py

Needs reportlab and Pillow. Uses only text written here, numbers copied from docs/RESULTS.md (simulator results) and the committed
screenshots in docs/img/ (dashboard-pF-*). The live-verified table is an EMPTY placeholder for the presenter's own runs: nothing is
claimed to be verified on a real cluster.
"""
import io
import os

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, Image as RLImage, KeepTogether, PageBreak, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT, "docs", "img")
OUT = os.environ.get("GUIDE_OUT") or os.path.join(ROOT, "docs", "presentation-guide.pdf")
W, H = A4
M = 18 * mm

ss = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica", fontSize=10.5, leading=14.5, spaceAfter=5, alignment=TA_LEFT)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.5, leading=11, textColor=colors.HexColor("#444444"))
CELL = ParagraphStyle("cell", parent=BODY, fontSize=9, leading=11.5, spaceAfter=0)
CELLB = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold")
H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=18, leading=22, spaceBefore=4, spaceAfter=8, textColor=colors.HexColor("#0b3d5c"))
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, leading=16, spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#0b3d5c"))
BUL = ParagraphStyle("bul", parent=BODY, leftIndent=12, bulletIndent=2, spaceAfter=2)
TITLE = ParagraphStyle("title", parent=H1, fontSize=26, leading=31, spaceAfter=10)


def P(t, st=BODY, **kw):
    return Paragraph(t, st, **kw)


def bullets(items):
    return [Paragraph(i, BUL, bulletText="•") for i in items]


def box(text, bg="#eef5fa", border="#0b3d5c"):
    t = Table([[Paragraph(text, BODY)]], colWidths=[W - 2 * M])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)), ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor(border)),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def table(rows, widths, head=True):
    data = [[Paragraph(str(c), CELLB if (head and i == 0) else CELL) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if head else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if head:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfeaf2")))
    t.setStyle(TableStyle(st))
    return t


def shot(name, caption, crop_h=None, width=None):
    path = os.path.join(IMG, name)
    im = Image.open(path).convert("RGB")
    if crop_h and im.height > crop_h:
        im = im.crop((0, 0, im.width, crop_h))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    w = width or (W - 2 * M)
    img = RLImage(buf, width=w, height=w * im.height / im.width)
    img.hAlign = "CENTER"
    return KeepTogether([img, Paragraph(caption, SMALL), Spacer(1, 6)])


def footer(c, d):
    c.saveState()
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawString(M, 10 * mm, "Presentation guide. Results are SIMULATOR results unless marked LIVE-VERIFIED. Patient data is synthetic.")
    c.drawRightString(W - M, 10 * mm, f"page {d.page}")
    c.restoreState()


story = []
a = story.append

# ------------------------------------------------------------------ cover
a(Spacer(1, 30 * mm))
a(P("Protecting hospital cloud systems when the protectors themselves can be attacked", TITLE))
a(P("A plain-English guide to the Distributed Cyber-Resilience Platform, for a presentation to a non-expert audience", ParagraphStyle("sub", parent=BODY, fontSize=13, leading=17)))
a(Spacer(1, 8 * mm))
a(box("<b>What to remember in one minute.</b> A clinic's patient portal, login service, health-record service, database, laboratory-results service and medication service run in the cloud. "
      "If one of them is taken over, someone must cut it off quickly, replace it with a clean copy, check the copy, and bring it back carefully. "
      "Normally one central program decides. If that program is fooled, hacked or stops, the whole clinic is exposed. "
      "This project replaces the single decision-maker with <b>four identical security agents</b> that must <b>agree, three out of four,</b> before anything is cut off. "
      "One liar or one failure cannot cause harm or stop protection."))
a(Spacer(1, 6 * mm))
a(box("<b>Honest status.</b> The agents, the signatures, the 3-of-4 agreement and the recovery pipeline are real code. They were tested many times against a "
      "<b>simulator</b> (section 6) and then run once on a <b>test Kubernetes cluster that the author built in a cloud sandbox</b> (section 7, labelled SANDBOX-CLUSTER and kept apart from the simulator numbers). "
      "<b>Nothing has yet been verified by the presenter on their own cluster</b>; the live-verified table in section 8 is empty on purpose and is for the presenter to fill with "
      "their own runs. All patient data is <b>synthetic</b>: 200 invented patients from the Synthea generator, never real people (section 5).", bg="#fff6e0", border="#b07800"))
a(PageBreak())

# ------------------------------------------------------------------ 1 problem
a(P("1. The problem, in plain English", H1))
a(P("Healthcare organisations run applications in the cloud: a portal where patients and staff look at records, a service that checks who may log in, a service "
    "that serves the health records, a database that stores them, and services for laboratory results and medications. Attackers go after exactly these. When one is compromised, three things matter:"))
a(P("<b>Speed</b>: cut the compromised system off before it leaks or spreads.", BUL, bulletText="•"))
a(P("<b>Safety</b>: never cut off a healthy system by mistake, because clinicians then cannot see records.", BUL, bulletText="•"))
a(P("<b>Care in coming back</b>: replace it with a clean copy, prove the copy is clean, and give access back step by step, not all at once.", BUL, bulletText="•"))
a(P("The usual design has one central controller making these decisions. That controller becomes the single weak point: if it is tricked into accusing a healthy service, "
    "it cuts it off; if it crashes, nothing is protected. In the simulator, a lying central controller cut off a healthy service <b>every time (20 of 20 trials)</b>, "
    "and with the controller down a real attack was <b>never</b> contained (0 of 20)."))

# ------------------------------------------------------------------ 2 what
a(P("2. What I built", H1))
a(P("<b>Six healthcare workloads (A to F)</b> are watched by <b>four identical security agents (A to D)</b>. Every agent watches every workload and votes on every one; the agents are <b>identical peers</b>: no agent is the boss, and the rule stays <b>3 of 4</b>."))
a(table([["Workload", "Name", "What it does for patients and staff", "If it is isolated"],
         ["A", "Patient Portal", "shows patient records", "no other service depends on it; the portal itself is unavailable"],
         ["B", "Login &amp; Access Service", "signs people in and checks their access", "the Patient Portal and the EHR API cannot work"],
         ["C", "Electronic Health Records (EHR) API", "serves patient records", "the Patient Portal cannot show patient records"],
         ["D", "Clinical Database", "stores patient records", "the EHR API, the laboratory and medication services cannot serve records, so the portal cannot show them"],
         ["E", "Laboratory Results Service", "serves laboratory results", "the Patient Portal cannot show laboratory results"],
         ["F", "Medication Service", "serves medication lists", "the Patient Portal cannot show medications"]],
        [14 * mm, 42 * mm, 50 * mm, W - 2 * M - 106 * mm]))
a(Spacer(1, 4))
a(P("The dashboard shows this same table, and works out the last column from which service calls which (checked against the application code), so nothing is typed in by hand. "
    "The suggested recovery order shown beside it is only a suggestion for a human operator; it does not change what the agents do, and there are no invented clinical urgency scores.", SMALL))
a(P("How a decision is made", H2))
for i, t in enumerate([
        "<b>See.</b> Each agent watches its workload and its neighbours: <i>Network Activity</i> (unexpected connections), <i>Running Processes</i> (programs that should not be there), "
        "<i>File Changes</i> (files that differ from the known-good copy) and <i>Failed Logins</i>.",
        "<b>Share evidence.</b> Agents send each other signed evidence. A forged or tampered message is rejected and the sender loses trust.",
        "<b>Agree (3 of 4).</b> Isolating a workload needs at least three agents to sign a vote. A missing vote is never counted as approval.",
        "<b>Isolation.</b> The workload is cut off from the network (a Kubernetes network policy), and a read-only copy of the evidence is saved first.",
        "<b>Replace and check.</b> A clean copy is started from the known-good image. The <b>Recovery Check</b> tests that it is healthy, that its files match, that it behaves normally and that it is really new.",
        "<b>Return in steps.</b> Limited Access, Monitored Access, Confirmed by Peers, then <b>Normal Access Restored</b>, each step only when the evidence supports it. If recovery keeps failing, the workload stays isolated and a person is asked to step in."], 1):
    a(Paragraph(t, BUL, bulletText=f"{i}."))

# ------------------------------------------------------------------ 3 two pages
a(PageBreak())
a(P("3. Two pages that are never mixed: Demo Mode and Live Monitoring", H1))
a(P("The dashboard has a toggle at the top. <b>Demo Mode</b> shows the simulator and has the scenario buttons. <b>Live Monitoring</b> shows only the real cluster and has no buttons at all. "
    "The two pages share no data, and the server (not just the page) refuses to show simulated data on the live page. If the cluster cannot be reached the live page says so instead of showing anything else."))
a(shot("dashboard-pK-01-demo-simple.png", "<b>Demo Mode</b> (simulator), in the new <b>Simple view</b> (the default). Amber banner: simulated data, no real healthcare workload is attacked. One top bar holds the banner, the Demo/Live toggle, the 'synthetic data only' label and the Simple/Detailed switch; below it the counters and the six workload cards.", width=(W - 2 * M) * 0.95))
a(shot("dashboard-pK-02-live-not-connected.png", "<b>Live Monitoring with no cluster reachable</b> (real behaviour of the current build): it says NOT CONNECTED once, with the reason, and shows nothing from the simulator. When connected it shows a green banner with the cluster name, how many of the four agents answer and how fresh the data is."))
a(box("<b>Placeholder: Live Monitoring, connected, on your own cluster.</b> A screenshot of the connected live page from <b>your</b> run can only be taken on your cluster. Presenter: paste your own screenshot here after your run (see docs/LIVE_DEMO.md). None is included because none has been taken by the presenter; the author's sandbox-cluster screenshots are in section 7 and are labelled as such.", bg="#fff6e0", border="#b07800"))

# ------------------------------------------------------------------ 4 what you see
a(PageBreak())
a(P("4. Reading the dashboard", H1))
a(shot("dashboard-pK-03-services-and-impact.png", "The Services table: what each of the six services calls and what stops working if it is isolated, worked out from the application code. In Simple view the technical boxes are hidden; <b>Detailed</b> shows them all.", width=(W - 2 * M) * 0.95))
a(shot("dashboard-pK-04-detailed-view.png", "The same page in the <b>Detailed</b> view: every explanation box and technical fold is shown.", width=(W - 2 * M) * 0.95))
a(table([["You see", "It means"],
         ["Workload trust (0-100)", "how much the agents trust this workload right now; falls while something looks wrong, restarts at 0 on isolation, must climb back before each step of access is restored"],
         ["Agent trust (0-100)", "how much the other agents trust this agent's evidence; falls only if its claims are contradicted"],
         ["Evidence confidence (0 to 1)", "how sure one agent is about one kind of signal on one workload; not a trust score"],
         ["Recovery Check", "pass or fail, not a score"],
         ["Agreement Reached (3 of 4)", "at least three agents signed; the only way anything is isolated"]], [48 * mm, W - 2 * M - 48 * mm]))
a(P("These are four separate things on purpose: there is no single combined 'security score'. Each agent is shown in exactly one of five states: <b>Agrees, Disagrees, Unavailable, Not yet responded, Message failed verification</b>. Only a counted, signed vote is 'Agrees'.", SMALL))
a(shot("dashboard-pK-05-security-alert-story.png", "A <b>Security Alert</b> in the Simple view: it starts with a plain-English story (what happened, who agreed, how it ended) and shows each reading as a sentence; votes, trust changes and raw logs are in the Detailed view.", width=(W - 2 * M) * 0.95))
a(shot("dashboard-pK-06-performance-key-cards.png", "<b>Performance Results</b>: five large key-result cards (the numbers are read out of the results text); the full twelve lines, graphs and tables are one click away. Labelled as simulator results.", width=(W - 2 * M) * 0.95))
a(shot("dashboard-pK-07-walkthrough.png", "The <b>guided walkthrough</b> (Demo Mode only): eleven steps that run a real simulator scenario; a step that has not been reached yet says so instead of showing results.", width=(W - 2 * M) * 0.95))

# ------------------------------------------------------------------ 5 data
a(PageBreak())
a(P("5. Realistic data and live traffic (Synthea)", H1))
a(box("<b>Every patient is invented.</b> The 200 patients come from <b>Synthea</b>, the open-source synthetic patient generator (Apache-2.0, by MITRE): 491 conditions, 2,359 laboratory results and 494 medications. "
      "Social security numbers, addresses and other identifiers were left out on purpose. Every service answer says <font face='Courier'>source: Synthea, synthetic: true</font>. No real patient data is used anywhere."))
a(Spacer(1, 4))
a(P("How it flows, in real time", H2))
for t in ["The <b>Clinical Database</b> holds the dataset; the <b>EHR API</b>, <b>Laboratory Results</b> and <b>Medication</b> services read from it over the network, each after a token check with the Login service, and the <b>Patient Portal</b> calls them.",
          "A <b>client</b> runs three simulated clinicians who log in, look up patients, open records, lab results and medications every one to three seconds (and make a mistyped login now and then). That is continuous, realistic traffic on the real workloads.",
          "The four agents poll every workload once a second and count what happens: <b>requests per second, outbound connections, data sent, failed logins</b>. The Live Monitoring page draws a small chart for each of these for each service, labelled LIVE_CLUSTER, with the age of the data. Nothing simulated can appear there.",
          "<b>The agents watch behaviour, never records.</b> The measurements have no field that can hold a patient record, and the agents' Kubernetes permissions cannot read the application's data. The dataset file is also fingerprinted, so a changed file is detected like any tampered program file."]:
    a(P(t, BUL, bulletText="•"))
a(shot("dashboard-pK-08-live-charts-sandbox.png", "<b>SANDBOX-CLUSTER screenshot</b> (the author's test cluster, not the presenter's): the Live Monitoring charts, one row per service, four charts each, telemetry age 0 s. The clinicians' traffic is visible as requests per second and data sent; outbound connections and failed logins stay flat.", width=(W - 2 * M) * 0.98))
a(P("Why not real patient data? Privacy law and ethics approval would be needed, and a platform that demonstrates attacks and isolation must never put a real person's record at risk. Synthea keeps the structure (clinical codes, lab values, medication lists) without a real person. Limits: it is a model, the sample is small and static, and it is not for clinical conclusions (docs/DATASET.md).", SMALL))

# ------------------------------------------------------------------ 5 simulator results
a(PageBreak())
a(P("6. Results measured in the simulator", H1))
a(box("<b>These are SIMULATOR results.</b> The telemetry and the Kubernetes API are simulated; the agents, signatures, 3-of-4 agreement, certificates and the recovery pipeline are the real code. "
      "Times such as '1.2 s' say nothing about real Kubernetes, where a pod takes tens of seconds to start. What carries over is <i>who</i> gets isolated, who does not, and what survives a failure. "
      "20 trials per case unless stated; everything is reproducible with <font face='Courier'>python3 scripts/evaluate.py</font> (docs/RESULTS.md).", bg="#fff6e0", border="#b07800"))
a(Spacer(1, 4))
a(table([["Question", "What the simulator showed"],
         ["Does the full cycle work?", "80 of 80 scenario trials went through detection, isolation, recovery and return; a genuine attack was isolated in about 1.2 s and back at full access in about 18.5 s of simulator time."],
         ["One lying agent?", "never isolated a healthy workload (0 of 20); the lying agent was marked suspect after about 22 s and ignored after about 25 s."],
         ["A lying or crashed central controller (the alternative design)?", "a lying controller isolated a healthy workload 20 of 20 times; with the controller crashed a real attack was not contained (0 of 20); with one agent crashed it was (20 of 20)."],
         ["Cost of the safety?", "containment takes about 1.5 s instead of 0.5 s."],
         ["Where does it break?", "3 of 4 compromised agents isolate a healthy workload (20 of 20); 2 compromised agents plus one honest agent seeing a weak blip also do (20 of 20); 2 silent agents mean nothing is isolated (0 of 20). One compromised agent never broke anything."],
         ["A compromised agent that refuses to vote ('obstruct')?", "the other three contained and recovered the workload in 20 of 20 trials with exactly 3 signers; its vote was never counted (0 of 20). It was <b>not penalised</b>: the platform has no rule for a missing vote."],
         ["False alarms?", "in a mixed run no genuine attack was missed (0 of 30) but 50% of benign traffic spikes were isolated, all of them seen by 3 or more agents: a threshold detector cannot tell a unanimous harmless spike from an attack."],
         ["Optional 6-second re-check (off by default)?", "false isolations 12 of 20 down to 9 of 20: every short blip filtered (3 of 11 down to 0 of 11), none of the 9 long bursts. It delays a network-only attack from about 1.0 s to 7.0 s, and an attacker sending 3 s bursts every 5 s is <b>never</b> contained with it on (0 of 20, against 20 of 20 with it off)."],
         ["Targeted investigation?", "did <b>not</b> reduce false isolations in my mix (4 of 20 both ways); it made slow-burn attacks isolate about 4.2 s sooner and turned silent ambiguity into a human-review flag."]],
        [52 * mm, W - 2 * M - 52 * mm]))

# ------------------------------------------------------------------ 7 sandbox cluster
a(PageBreak())
a(P("7. Results from a test cluster in the author's sandbox (SANDBOX-CLUSTER)", H1))
a(box("<b>SANDBOX-CLUSTER results.</b> These come from a real 4-node Kubernetes cluster (Minikube, Calico network policies) that the author built in a cloud sandbox on 2026-10-16. They are <b>not</b> simulator results "
      "and they are <b>not</b> the presenter's own live-verified results (section 8 stays empty). The sandbox differs from a laptop in ways listed in docs/SANDBOX-CLUSTER.md section 2 (for example Kubernetes 1.34.12 and no Metrics API).", bg="#e9f3e9", border="#1f6f43"))
a(Spacer(1, 4))
a(table([["Check", "Result on the sandbox cluster"],
         ["Deployment", "six workloads, four agents, dashboard and two admission-webhook replicas all running, using the project's own scripts"],
         ["Known-good file check (verify-baseline)", "all six workloads match the manifest (five files each, including the dataset), before and after every incident"],
         ["Agent permissions (verify-rbac)", "27 of 27 checks pass for each of the six workloads"],
         ["Admission webhook (verify-webhook)", "7 of 7 steps pass, and 8 of 8 with the webhook switched off: an agent's isolation request is still refused when it is not backed by a 3-of-4 certificate"],
         ["Network isolation (verify-isolation)", "16 of 16 checks pass on two workloads with real Calico enforcement: user traffic blocked, agents still reach the workload"],
         ["Controlled tripwire test", "the real agents detected a harmless one-line change in the Laboratory Results Service and in the Medication Service, agreed (4 of 4), isolated, replaced and checked the workload, and restored access in four steps: about 82 seconds from the change to normal access, almost all of it Kubernetes' 30-second pod shutdown wait and the four 10-second stages"],
         ["The Synthea data and clinician traffic", "an 11-minute observation: every workload stayed healthy, nothing was isolated, at most 1 outbound connection (the limit is 8), at most 39 KiB/s sent (the limit is 256), no failed-login alarm"],
         ["Bugs found and fixed", "four deployment bugs were found only because it ran on a real cluster (two script bugs in the verification scripts, one in the upgrade path, one in the webhook order); each was fixed and has a test"]],
        [52 * mm, W - 2 * M - 52 * mm]))
a(Spacer(1, 4))
a(shot("dashboard-pK-09-live-overview-sandbox.png", "<b>SANDBOX-CLUSTER screenshot</b>: Live Monitoring connected to the author's test cluster: green banner, cluster name and Kubernetes version, 4 of 4 agents reachable, six workload cards.", width=(W - 2 * M) * 0.95))
a(P("What this does not prove: behaviour on Windows/WSL and Docker Desktop, the presenter's Minikube version, kubectl top figures, and anything about a production hospital cluster (docs/SANDBOX-CLUSTER.md section 6).", SMALL))


# ------------------------------------------------------------------ 6 live verified
a(PageBreak())
a(P("8. Results verified on the real cluster", H1))
a(box("<b>LIVE-VERIFIED results: none yet.</b> This table is a placeholder for the presenter to fill with their own runs (docs/LIVE_DEMO.md, section 9). "
      "Nothing in it has been verified. Do not describe a simulator result as a live result.", bg="#fdeaea", border="#a02020"))
a(Spacer(1, 4))
a(table([["Test (runbook step)", "Date", "Result", "Evidence (screenshot or log)"],
         ["Pre-flight: verify-baseline, verify-isolation", "not run", "", ""],
         ["Healthy state, 4 of 4 agents", "not run", "", ""],
         ["Agent goes offline, 3 of 4 carry on, agent rejoins", "not run", "", ""],
         ["Tripwire test: real detection, isolation, recovery, return", "not run", "", ""],
         ["Compromised agent (false accusation)", "not run", "", ""],
         ["Forgery rejected", "not run", "", ""],
         ["Obstruct plus tripwire", "not run", "", ""],
         ["Centralized comparison", "not run", "", ""]],
        [70 * mm, 20 * mm, 30 * mm, W - 2 * M - 120 * mm]))

# ------------------------------------------------------------------ 7 demo plan
a(PageBreak())
a(P("9. A suggested demonstration (about 10 minutes)", H1))
a(P("<b>In Demo Mode (safe, repeatable):</b>", BODY))
a(table([["Scenario in the picker", "What to say"],
         ["Patient records being copied out of the EHR API", "three of four agents agree, isolate it, a clean copy is checked, access returns in steps"],
         ["A security agent falsely accuses the Login &amp; Access Service", "one liar cannot reach three votes; the liar is marked suspect"],
         ["A security agent goes offline, then the EHR API is attacked", "three agents are still enough"],
         ["A compromised agent refuses to vote while the EHR API is attacked", "the other three still act; the dashboard shows the refusing agent as 'Disagrees' with its reason"]],
        [70 * mm, W - 2 * M - 70 * mm]))
a(Spacer(1, 4))
a(P("<b>On the real cluster (only if you have run and verified it first):</b> the healthy state, deleting one agent pod, and the <b>tripwire test</b>: a single harmless line appended to a designated, unused marker file in one workload, "
    "which the real agents detect and handle with the real pipeline. Exact commands, what to point at, and how to reset are in <font face='Courier'>docs/LIVE_DEMO.md</font>."))
a(box("<b>The one exception to 'attacks only in the simulator'.</b> The tripwire is not an attack: it is a harmless, operator-initiated change to one file that nothing reads, made with <font face='Courier'>python -c</font> "
      "(not a shell), refused for anything but the six healthcare workloads and for any other cluster. It triggers real isolation of a synthetic-data workload by design. It is documented as a controlled validation event with restore steps."))
a(P("What the project deliberately does <b>not</b> contain: attack code, real patient data, or any way for the simulator's controls to reach a real cluster (the server refuses it, and tests prove it).", BODY))

# ------------------------------------------------------------------ 8 limits
a(P("10. Limitations, stated plainly", H1))
for t in [
        "<b>Verified only on one test cluster.</b> Real pod start times, Calico enforcement and the admission webhook were exercised on the author\'s sandbox cluster (section 7), not on the presenter\'s laptop, not on Windows/WSL and not on a production hospital system.",
        "<b>Simulated telemetry in the measured results.</b> The numbers in section 6 come from invented signals; real traffic will differ, and the false-alarm rates depend on my choice of benign mix. On the sandbox cluster, healthy Synthea-based traffic raised no alarm.",
        "<b>Telemetry is reported by the workload itself.</b> A fully compromised workload could lie about itself, and all four agents read the same source.",
        "<b>Three or more compromised agents</b> can isolate a healthy workload; two silent or obstructing agents stop containment. The design tolerates one bad agent, not two.",
        "<b>A sustained harmless spike seen by everyone looks like an attack</b> to a threshold detector; the optional re-check helps with short spikes and can be evaded by short bursts.",
        "<b>A missing vote costs the agent nothing</b> (no trust rule for it).",
        "<b>Agents keep history in temporary storage:</b> a deleted agent pod rejoins with empty history.",
        "<b>Six workloads only</b>, watched by four agents. Not a compliance claim: the HIPAA Security Rule categories in docs/HEALTHCARE.md are a mapping for discussion, with gaps listed."]:
    a(P(t, BUL, bulletText="•"))

# ------------------------------------------------------------------ 9 Q&A
a(PageBreak())
a(P("11. Questions the faculty may ask, with honest answers", H1))
QA = [
    ("Why four agents and three of four?", "Four agents can tolerate one that is wrong or down and still reach three. One bad agent can neither cause an isolation (it cannot get three votes) nor block one (the other three suffice). It is a deliberate, small choice; docs/RESULTS.md shows exactly where it stops working."),
    ("What if two agents are compromised?", "Then it breaks, and the results say so: two compromised agents plus one honest agent that sees a weak blip can isolate a healthy workload, and two silent agents mean nothing is isolated. The claim is tolerance of one failure, not two."),
    ("Has this been tested on a real cluster?", "On one test cluster, yes: I built a real 4-node Kubernetes cluster in a cloud sandbox and ran the deployment, the verification scripts, the harmless tripwire test and an 11-minute observation with realistic Synthea traffic (section 7, labelled SANDBOX-CLUSTER). The measured numbers in section 6 are simulator results (real agents and cryptography, simulated telemetry and Kubernetes). I have not yet verified anything on my own laptop cluster, so the live-verified table in section 8 is empty until I run it."),
    ("Why not one smart central controller?", "It is a single point of failure and of compromise. In my simulation a lying controller isolated a healthy service 20 of 20 times and a crashed one never contained a real attack; the distributed design did neither. The price is slower containment (about 1.5 s against 0.5 s in the simulator)."),
    ("How does it recognise an attack?", "Four signals measured by every agent independently: unexpected connections or traffic, unexpected programs, files that differ from the known-good copy, and bursts of failed logins. Weak or disputed evidence triggers a short investigation first."),
    ("What about false alarms?", "They exist. In my stress mix 50% of harmless spikes that three or more agents saw were isolated. An optional 6-second re-check lowers that (12 of 20 to 9 of 20) but delays some real attacks by about 6 s and can be evaded by short bursts, so it is off by default."),
    ("What does isolating a workload do to patients and staff?", "It cuts the workload off the network, so services that depend on it stop working: for example isolating the EHR API means the Patient Portal cannot show records. The dashboard shows this impact in advance. It is deliberate: a compromised system must not keep running."),
    ("How do you know a recovered workload is clean?", "A Recovery Check: healthy, files match the known-good copy, behaves normally, and it is a new instance. If it fails it is retried a limited number of times, then marked 'needs human attention' and stays isolated; it is never shown as healthy."),
    ("Can a compromised agent forge another agent's messages?", "Not undetected: messages are signed and agents use mutual TLS, so forged ones are rejected and the sender loses trust. A compromised agent can still lie with its own genuine identity; that is what the 3-of-4 rule handles."),
    ("Is this HIPAA compliant?", "No claim is made. docs/HEALTHCARE.md maps what the platform does to HIPAA Security Rule categories (access control, audit, integrity, contingency planning, transmission security) and lists the gaps; compliance needs much more than this project."),
    ("Is any patient data real?", "No. Every patient comes from Synthea, an open-source generator of invented patients, and is labelled synthetic on every page and in every service answer. The security agents watch behaviour (connections, programs, files, logins) and never read patient records."),
    ("Is the 'tripwire' an attack?", "No. It is one harmless line appended to an unused marker file, run by the operator on purpose, so the real agents have something genuine to detect. It is a narrow, documented exception, with restore steps."),
    ("What would you do next?", "Measure on my own laptop cluster and a larger one, tune thresholds on real traffic, add more workloads, add stronger sensors, and evaluate with realistic (still synthetic) workloads. These are future work, not done."),
]
for q, ans in QA:
    a(KeepTogether([P(f"<b>Q. {q}</b>", BODY), P(ans, ParagraphStyle("ans", parent=BODY, leftIndent=10)), Spacer(1, 2)]))

# ------------------------------------------------------------------ glossary
a(P("12. Glossary", H1))
a(table([["Term", "Plain meaning"],
         ["Workload", "one application service running in the cloud (portal, login, records API, database)"],
         ["Agent", "a small security program next to a workload; four identical peers"],
         ["Evidence", "a signed report from one agent about what it sees"],
         ["Agreement Reached (3 of 4)", "at least three agents signed a vote; also called a quorum"],
         ["Isolation", "cutting a workload off the network (reversible)"],
         ["Recovery Check", "tests that a replacement is healthy, matches the known-good copy and behaves normally"],
         ["Normal Access Restored", "the last step of the staged return, after limited, monitored and peer-confirmed access"],
         ["Simulator", "the test world with simulated telemetry and Kubernetes, used for all measured results"],
         ["Synthetic data", "made-up patient records (from the Synthea generator); no real person is represented"],
         ["SANDBOX-CLUSTER", "results from the author's own test Kubernetes cluster in a cloud sandbox; not simulator results, not the presenter's live results"]], [48 * mm, W - 2 * M - 48 * mm]))
a(Spacer(1, 6))
a(P("Where to find things: <font face='Courier'>docs/RESULTS.md</font> (all numbers and graphs), <font face='Courier'>docs/HEALTHCARE.md</font> (requirements and safeguards), "
    "<font face='Courier'>docs/DASHBOARD.md</font>, <font face='Courier'>docs/LIVE_DEMO.md</font> (runbook), <font face='Courier'>docs/PROGRESS.md</font> (what was done, tests, limits).", SMALL))


def build():
    doc = BaseDocTemplate(OUT, pagesize=A4, leftMargin=M, rightMargin=M, topMargin=16 * mm, bottomMargin=18 * mm,
                          title="Distributed Cyber-Resilience Platform - presentation guide", author="")
    doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(M, 18 * mm, W - 2 * M, H - 34 * mm, id="f")], onPage=footer)])
    doc.build(story)
    print("wrote", os.path.relpath(OUT, ROOT), os.path.getsize(OUT) // 1024, "KB")


if __name__ == "__main__":
    build()
