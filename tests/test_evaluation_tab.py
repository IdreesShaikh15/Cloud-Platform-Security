"""Part 4a: the Evaluation tab shows results/ and docs/RESULTS.md exactly as written, labelled as simulator results."""
import importlib.util
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_MD = open(os.path.join(ROOT, "docs", "RESULTS.md"), encoding="utf-8").read()
FIGS = ["fig1_timing_boxplots.png", "fig2_trust_trajectory.png", "fig3_distributed_vs_centralized.png", "fig4_investigation_on_off.png",
        "fig5_fault_tolerance_boundary.png", "fig6_agent_crash.png", "fig7_overhead.png", "fig8_false_positive_negative.png"]


def load_dash(monkeypatch, **env):
    for k in ("SIM_CONTROL_URL", "SIM_CONTROL_TOKEN", "BASELINE_URL", "DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE", "EVAL_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATUS_URLS", ",".join(f"http://127.0.0.1:{9 + i}/status" for i in range(4)))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("dash_eval_mod", os.path.join(ROOT, "dashboard", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def serve():
    servers = []

    def start(mod):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for s in servers:
        s.shutdown()


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


# ------------------------------------------------------------------ the Markdown subset
def test_markdown_blocks(monkeypatch):
    d = load_dash(monkeypatch)
    text = ["Intro paragraph", "continues here.", "", "* bullet one", "  continued", "* bullet two", "", "1. first", "2. second", "",
            "| A | B |", "|---|---|", "| 1 | x<br>y |", "", "![alt text](../results/fig1_timing_boxplots.png)", "", "```bash", "echo hi", "```",
            "", "> quoted", "", "<!-- BEGIN:x -->", "last"]
    blocks = d.parse_markdown_blocks(text)
    assert [b["type"] for b in blocks] == ["p", "bullets", "numbered", "table", "image", "code", "quote", "p"]
    assert blocks[0]["text"] == "Intro paragraph continues here." and blocks[1]["items"] == ["bullet one continued", "bullet two"]
    assert blocks[3] == {"type": "table", "head": ["A", "B"], "rows": [["1", "x<br>y"]]}           # separator row dropped, <br> kept
    assert blocks[4] == {"type": "image", "alt": "alt text", "name": "fig1_timing_boxplots.png"}   # path reduced to a file name


# ------------------------------------------------------------------ the real evaluation
def test_the_real_evaluation_is_complete_and_labelled_simulator(monkeypatch):
    d = load_dash(monkeypatch)
    v = d.evaluation_view()
    assert v["available"] and v["data_source"] == "SIMULATOR" and v["notice"].startswith("SIMULATOR RESULTS")
    assert "Nothing here was measured on a real cluster" in v["notice"]
    sec = {s["id"]: s for s in v["sections"]}
    assert len(sec["0"]["blocks"][0]["items"]) == 10                                # the ten-line summary
    exps = sec["4"]["subsections"]
    assert [e["id"] for e in exps] == [f"4.{i}" for i in range(1, 9)]
    for e in exps:
        kinds = [b["type"] for b in e["blocks"]]
        assert "image" in kinds and "table" in kinds and "p" in kinds, e["id"]       # graph + summary table + plain-English explanation
        para = " ".join(b["text"] for b in e["blocks"] if b["type"] == "p")
        assert "How to read it" in para or "How to read it, and why to be careful" in para, e["id"]
    assert len([b for b in exps[7]["blocks"] if b["type"] == "table"]) == 2         # 4.8 has the mixed table and the strata table
    assert len(sec["5"]["blocks"][0]["items"]) >= 6                                  # the limitations
    assert v["meta"]["trials"] == 760 and "0 errors" in v["meta"]["errors_text"] and len(v["meta"]["figures"]) == 8


def test_nothing_on_the_tab_is_invented_every_table_cell_is_in_the_document(monkeypatch):
    d = load_dash(monkeypatch)
    v = d.evaluation_view()
    cells = 0
    for s in v["sections"]:
        for blocks in [s["blocks"]] + [x["blocks"] for x in s["subsections"]]:
            for b in blocks:
                if b["type"] == "table":
                    for c in b["head"] + [x for r in b["rows"] for x in r]:
                        cells += 1
                        assert c in RESULTS_MD, c
                if b["type"] in ("p", "quote"):
                    assert b["text"].split(" ")[0] in RESULTS_MD
    assert cells > 200
    # and the numbers the page leads with are the evaluation's own
    assert "760" in RESULTS_MD and "20/20" in RESULTS_MD


def test_every_graph_is_served_as_a_png(monkeypatch, serve):
    url = serve(load_dash(monkeypatch))
    for f in FIGS:
        code, ctype, body = fetch(f"{url}/api/eval/figure/{f}")
        assert code == 200 and ctype == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n", f
    code, _, body = fetch(f"{url}/api/evaluation")
    assert code == 200 and b'"available": true' in body


@pytest.mark.parametrize("name", ["../README.md", "..%2fREADME.md", "%2e%2e/README.md", "/etc/passwd", "fig1_timing_boxplots.png/../x",
                                  "fig1_timing_boxplots.png%00.txt", "summary.md", "nope.png", "fig1%20timing.png", "RESULTS.md", ""])
def test_figure_endpoint_serves_nothing_else(monkeypatch, serve, name):
    url = serve(load_dash(monkeypatch))
    assert fetch(f"{url}/api/eval/figure/{name}")[0] == 404
    assert load_dash(monkeypatch).eval_figure(name) is None or name == ""


def test_evaluation_needs_the_dashboard_login_when_a_token_is_set(monkeypatch, serve):
    url = serve(load_dash(monkeypatch, DASHBOARD_TOKEN="x" * 24))
    for path in ("/api/evaluation", "/api/eval/figure/fig1_timing_boxplots.png"):
        assert fetch(url + path)[0] == 401


def test_a_deployment_without_the_evaluation_says_so_and_still_labels_it_simulator(monkeypatch):
    d = load_dash(monkeypatch)
    monkeypatch.setattr(d, "eval_locations", lambda: {})
    v = d.evaluation_view()
    assert v["available"] is False and v["data_source"] == "SIMULATOR" and "scripts/evaluate.py" in v["message"]
    assert d.eval_figure("fig1_timing_boxplots.png") is None


def test_unreadable_files_are_reported_not_crashed_on(monkeypatch, tmp_path):
    d = load_dash(monkeypatch, EVAL_DIR=str(tmp_path))
    (tmp_path / "RESULTS.md").write_bytes(b"\xff\xfe\x00bad")
    v = d.evaluation_view()
    assert v["available"] is False and "could not be read" in v["message"]


def test_a_custom_evaluation_directory_is_used(monkeypatch, tmp_path):
    (tmp_path / "RESULTS.md").write_text("# T\n\n## 4. Results\n\n### 4.1 Only one\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n![g](x/fig9_extra.png)\n\ntext\n")
    (tmp_path / "fig9_extra.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    d = load_dash(monkeypatch, EVAL_DIR=str(tmp_path))
    v = d.evaluation_view()
    assert v["available"] and [e["id"] for e in v["sections"][0]["subsections"]] == ["4.1"] and v["meta"]["figures"] == ["fig9_extra.png"]
    assert d.eval_figure("fig9_extra.png").startswith(b"\x89PNG") and d.eval_figure("fig1_timing_boxplots.png") is None


# ------------------------------------------------------------------ the image build context
def test_the_dashboard_build_context_contains_the_evaluation_and_nothing_secret():
    with tempfile.TemporaryDirectory() as dest:
        subprocess.run(["bash", os.path.join(ROOT, "scripts", "stage-dashboard.sh"), dest], check=True, capture_output=True)
        names = sorted(os.listdir(dest))
        assert names == ["Dockerfile", "evaluation", "index.html", "server.py"]
        ev = sorted(os.listdir(os.path.join(dest, "evaluation")))
        assert ev == sorted(["RESULTS.md", "summary.md"] + FIGS)
        assert "COPY evaluation/" in open(os.path.join(dest, "Dockerfile")).read()
        # run the dashboard exactly as the image would: from the staged directory, with no repository around it
        env = dict(os.environ, STATUS_URLS=",".join(f"http://127.0.0.1:{9 + i}/status" for i in range(4)))
        code = ("import importlib.util,sys; s=importlib.util.spec_from_file_location('d','server.py'); m=importlib.util.module_from_spec(s); "
                "s.loader.exec_module(m); v=m.evaluation_view(); assert v['available'] and len(m.eval_figure('fig1_timing_boxplots.png'))>1000; print('ok')")
        r = subprocess.run(["python3", "-c", code], cwd=dest, env=env, capture_output=True, text=True)
        assert r.returncode == 0 and "ok" in r.stdout, r.stderr
    build = open(os.path.join(ROOT, "scripts", "build-images.sh")).read()
    assert "stage-dashboard.sh" in build and '"$ROOT/dashboard"' not in build


# ------------------------------------------------------------------ the tab in a browser
def test_evaluation_tab_in_a_browser(monkeypatch, serve):
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists("/opt/pw-browsers/chromium"):
        pytest.skip("chromium not available")
    d = load_dash(monkeypatch)
    # the page only shows its tabs while its agents answer, so give the (never started) collector one simulated status each
    tg = {n: {"phase": "HEALTHY", "stage": "FULL", "workload": n, "epoch": 0, "last_decision": None, "detections": [], "workload_trust": 100,
              "attempt": 1, "attention": None, "display_state": "HEALTHY"} for n in "ABCD"}
    d.COLLECTOR.status = {n: {"node": n, "mode": "distributed", "time": 1.0, "data_source": "SIMULATOR", "targets": tg,
                              "agent_trust": {x: 100 for x in "ABCD"}, "decisions": [], "rejections": [], "pending_votes": {},
                              "event_boot": "b", "event_seq": 0, "events": [], "counters": None,
                              "investigations": {"enabled": True, "active": [], "recent": [], "watch": {}}} for n in "ABCD"}
    url = serve(d)
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        pg = b.new_page(viewport={"width": 1300, "height": 900})
        errs, reqs = [], []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("request", lambda r: reqs.append(r.url))
        pg.goto(url + "/")
        pg.wait_for_selector("#counters")
        assert not any("/api/evaluation" in u for u in reqs)                      # loaded only when the tab is opened
        pg.click("#tab-evaluation")
        pg.wait_for_selector("#evalbox .evcard img")
        pg.wait_for_function("[...document.querySelectorAll('#evalbox img')].every(i => i.complete && i.naturalWidth > 0)")
        text = pg.inner_text("#evalbox")
        assert "SIMULATOR RESULTS" in text and "Nothing here was measured on a real cluster" in text
        assert pg.locator("#evalbox img").count() == 8 and pg.locator("#evalbox table").count() >= 12
        for want in ("4.1 Detection, isolation", "4.8 False positives", "760 seeded trials", "0 errors", "Honest limitations", "How to read it"):
            assert want in text, want
        assert pg.locator("#evalbox .evbanner").is_visible()
        assert pg.evaluate("location.hash") == "#evaluation"
        assert "<script" not in pg.inner_html("#evalbox").lower()
        assert errs == [], errs
        b.close()
