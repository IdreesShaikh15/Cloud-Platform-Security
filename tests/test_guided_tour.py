"""Phase H: the guided healthcare demonstration (Demo Mode only), driven by the real simulator's recorded state."""
import os
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
CHROMIUM = "/opt/pw-browsers/chromium"


def test_the_walkthrough_exists_only_behind_the_simulator_controls():
    assert 'id="tour"' in HTML and "#tour.on { display:block; }" in HTML
    # shown only when the backend says the simulator controls are allowed, and removed for Live Monitoring
    assert 'document.getElementById("tour").classList.toggle("on", !!st.enabled)' in HTML
    assert 'if (CH !== "demo") { box.classList.remove("on"); document.getElementById("tour").classList.remove("on"); return; }' in HTML
    assert "body.noconn #tour" in HTML


def test_steps_are_the_eleven_stages_in_order_and_do_not_advance_on_a_timer():
    ids = ["healthy", "suspicious", "evidence", "investigation", "votes", "agreement", "isolation", "recovery", "checks", "staged", "results"]
    block = HTML[HTML.index("const TOUR_STEPS"):HTML.index("function tourModel")]
    assert [x for x in ids if f'["{x}"' in block] == ids and block.index('["healthy"') < block.index('["results"')
    tour = HTML[HTML.index("// ---------------------------------------------------------------- guided demonstration"):HTML.index("// ---------------------------------------------------------------- dependencies and the impact")]
    assert "setInterval" not in tour and "setTimeout(() => tour" not in tour           # only the user (or the 'jump to newest' box) moves it
    assert "not reached yet" in tour and "Nothing is shown here because this has not happened yet" in tour


def test_the_walkthrough_only_starts_the_fixed_catalogue_scenario():
    tour = HTML[HTML.index("document.getElementById(\"tourstart\").onclick"):HTML.index("// ---------------------------------------------------------------- dependencies and the impact")]
    assert 'simPost("api/sim/start", {scenario: "app-compromise"})' in tour and tour.count("api/sim/start") == 1


@pytest.fixture()
def demo():
    if not os.path.exists(CHROMIUM):
        pytest.skip("chromium not available")
    pytest.importorskip("playwright.sync_api")
    import socket
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "sim", "demo_server.py"), "--dashboard", "--dashboard-port", str(port),
                          "--control-port", "50371", "--base-port", "52151"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT, start_new_session=True)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/channels", timeout=2).read()
                break
            except Exception:
                time.sleep(1)
        yield f"http://127.0.0.1:{port}"
    finally:
        import signal                              # the demo server starts the dashboard as a child: stop the whole group
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                break
            try:
                p.wait(10)
                break
            except Exception:
                continue


def test_walkthrough_against_the_real_simulator_end_to_end(demo):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        try:
            pg = b.new_page()
            pg.goto(demo + "/?mode=demo")
            pg.wait_for_selector("#tour.on", timeout=30000)
            pg.wait_for_function("Object.keys((lastState||{}).nodes||{}).length>0", timeout=30000)
            assert "Press" in pg.inner_text("#tourbody") and "Start the walkthrough" in pg.inner_text("#tourbody")
            pg.click("#tourstart")
            pg.wait_for_selector("#tourbody .card2", timeout=90000)
            # right at the start, a step that has not happened must say so, and show no facts
            pg.click('[data-step="10"]')
            early = pg.inner_text("#tourbody .card2")
            assert "not reached yet" in early and "What the simulator recorded" not in early
            # going back and forward never changes what has happened
            pg.click("#tourback")
            assert "10. Staged return" in pg.inner_text("#tourbody .card2")
            pg.wait_for_function("document.querySelector('#tourstat').innerText.indexOf('running')<0", timeout=150000)
            pg.wait_for_timeout(3500)
            facts = {}
            for i in range(11):
                pg.click(f'[data-step="{i}"]')
                facts[i] = pg.inner_text("#tourbody .card2")
            assert all("recorded by the agents" in facts[i] for i in range(11)), {i: f[:80] for i, f in facts.items() if "recorded by the agents" not in f}
            assert "6 of 6 workloads were healthy" in facts[0]
            assert "Network Activity" in facts[1] and "agents A, B, C, D" in facts[1]
            assert "of 4 agents signed" in facts[5] and "At least 3 signatures are required" in facts[5]
            assert "Limited Access → Monitored Access → Confirmed by Peers → Normal Access Restored" in facts[9]
            assert "Other workloads isolated by mistake: none" in facts[10]
            pg.click("#tourstop")
            assert "Start the walkthrough" in pg.inner_text("#tourbody")
        finally:
            b.close()
