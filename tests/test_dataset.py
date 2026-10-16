"""Item 3: the Synthea dataset, the services that serve it, the client's traffic, the file fingerprints and the Live telemetry charts."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "apps", "src"))
DATA = os.path.join(ROOT, "apps", "src", "data", "synthea-sample.json")
CHROMIUM = "/opt/pw-browsers/chromium"


def load():
    return json.load(open(DATA, encoding="utf-8"))


# ------------------------------------------------------------------ the data
def test_the_dataset_is_synthea_output_and_labelled_as_such():
    d = load()
    m = d["meta"]
    assert m["source"] == "Synthea" and m["synthetic"] is True and "Apache-2.0" in m["generator"] and m["seed"] == 20261016
    assert "No real person" in m["note"] and m["patients_in_file"] == len(d["patients"]) == 200
    mrns = [p["mrn"] for p in d["patients"]]
    assert len(set(mrns)) == 200 and all(x.startswith("SYN-") for x in mrns)
    assert all(p["labs"] for p in d["patients"])
    assert sum(len(p["medications"]) for p in d["patients"]) > 100 and sum(len(p["conditions"]) for p in d["patients"]) > 100


def test_nothing_that_looks_like_an_identifier_is_kept():
    import re
    text = open(DATA, encoding="utf-8").read().lower()
    for word in ("ssn", "passport", "drivers", "address", "\"lat\"", "\"lon\"", "income", "healthcare_expenses"):
        assert (word in text) is False, word                                       # (a bool, so a failure never prints the whole file)
    assert re.search(r"\b\d{3}-\d{2}-\d{4}\b", text) is None                      # no social-security-number-shaped value
    assert set(load()["patients"][0]) == {"mrn", "name", "dob", "gender", "city", "state", "conditions", "labs", "medications"}


def test_the_converter_is_documented_deterministic_and_the_committed_file_names_its_source():
    src = open(os.path.join(ROOT, "scripts", "gen-synthea-dataset.py"), encoding="utf-8").read()
    assert "ALL DATA IS SYNTHETIC" in src and "--seed" in src and "Apache-2.0" in src
    assert open(DATA, "rb").read().endswith(b"\n")


# ------------------------------------------------------------------ fingerprints: the data file is part of the known-good manifest
def test_the_data_file_is_in_the_known_good_fingerprints():
    from telemetry import hash_tree
    h = hash_tree(os.path.join(ROOT, "apps", "src"))
    assert "data/synthea-sample.json" in h and "app.py" in h and "static/TRIPWIRE-TEST.txt" in h
    gen = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen-baseline-hashes.py")], capture_output=True, text=True, cwd=ROOT)
    assert gen.returncode == 0 and "data/synthea-sample.json" in gen.stdout


# ------------------------------------------------------------------ the services, as real processes
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def stack():
    roles = ["auth-service", "database", "records-api", "lab-results", "medication-service", "patient-portal", "client"]
    ports = {r: free_port() for r in roles}
    u = lambda r: f"http://127.0.0.1:{ports[r]}"   # noqa: E731
    env = dict(os.environ, AUTH_URL=u("auth-service"), DATABASE_URL=u("database"), RECORDS_URL=u("records-api"), LAB_URL=u("lab-results"),
               MEDICATION_URL=u("medication-service"), PORTAL_URL=u("patient-portal"), APP_ROOT=os.path.join(ROOT, "apps", "src"))
    procs = [subprocess.Popen([sys.executable, os.path.join(ROOT, "apps", "src", "app.py")], env=dict(env, ROLE=r, PORT=str(ports[r])),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for r in roles]

    def get(role, path):
        return json.loads(urllib.request.urlopen(u(role) + path, timeout=8).read())
    for _ in range(60):
        try:
            get("patient-portal", "/api/patients?limit=1")
            break
        except Exception:
            time.sleep(0.3)
    time.sleep(12)                                  # let the client's clinicians work for a while
    yield get, u
    for p in procs:
        p.terminate()


def test_lookups_return_real_synthea_records_with_labels(stack):
    get, _ = stack
    page = get("patient-portal", "/api/patients?limit=5")
    assert page["total"] == 200 and len(page["patients"]) == 5 and page["source"] == "Synthea" and page["synthetic"] is True
    mrn = next(p["mrn"] for p in get("patient-portal", "/api/patients?limit=50")["patients"])
    rec = get("patient-portal", f"/api/patient?mrn={mrn}")
    labs = get("patient-portal", f"/api/labs?mrn={mrn}")
    meds = get("patient-portal", f"/api/medications?mrn={mrn}")
    assert rec["record"]["mrn"] == mrn and rec["source"] == "Synthea" and labs["source"] == "Synthea" and meds["source"] == "Synthea"
    truth = next(p for p in load()["patients"] if p["mrn"] == mrn)
    assert [l["test"] for l in labs["results"]] == [l["test"] for l in truth["labs"][:12]]            # served unchanged from the dataset
    assert [m["description"] for m in meds["medications"]] == [m["description"] for m in truth["medications"][:12]]


def test_answers_are_bounded_and_unknown_patients_are_refused(stack):
    get, u = stack
    assert len(get("patient-portal", "/api/patients?limit=100000")["patients"]) <= 50
    assert len(urllib.request.urlopen(u("patient-portal") + "/api/patients?limit=100000").read()) < 30_000
    with pytest.raises(Exception):
        get("records-api", "/records")                                            # no token: refused
    r = None
    try:
        get("patient-portal", "/api/patient?mrn=SYN-DOESNOTEXIST")
    except urllib.error.HTTPError as e:
        r = e.code
    assert r in (503, 404)


def test_the_client_generates_continuous_clinician_traffic_without_errors(stack):
    get, _ = stack
    st = get("client", "/stats")
    t = st["traffic"]
    assert t["clinicians"] == 3 and t["requests"] >= 15 and t["errors"] == 0, t
    assert {"login", "patient-list", "record", "labs"} <= set(t["by_kind"]), t
    assert st["availability"] == 1.0                                              # the availability probe still measures the same thing


def test_telemetry_counts_requests_but_carries_no_patient_data(stack):
    get, _ = stack
    for role in ("database", "patient-portal", "lab-results"):
        tel = get(role, "/_telemetry")
        assert isinstance(tel["requests_total"], int) and tel["requests_total"] > 0
        blob = json.dumps(tel).lower()
        names = [p["name"].lower() for p in load()["patients"][:60]]
        assert not any(n in blob for n in names) and "syn-" not in blob and "loinc" not in blob


def test_the_request_rate_is_realistic_and_answers_are_small(stack):
    """A realistic clinic: some requests per second, small answers (the agents flag > 256 KB/s sent; one page is under 10 KB)."""
    get, _ = stack
    a = {r: get(r, "/_telemetry") for r in ("patient-portal", "database")}
    time.sleep(10)
    b = {r: get(r, "/_telemetry") for r in ("patient-portal", "database")}
    for r in a:
        # (the number of established connections is NOT asserted here: on one test machine all services share a single network namespace, so the count
        # is global. It is checked per pod on the real cluster, docs/SANDBOX-CLUSTER.md section 8.)
        rps = (b[r]["requests_total"] - a[r]["requests_total"]) / 10
        assert 0.5 <= rps <= 20, (r, rps)                                          # a realistic clinic, not a flood and not idle


# ------------------------------------------------------------------ the agents' view: counts only
def test_the_agent_turns_the_request_counter_into_a_rate_and_tolerates_its_absence():
    from resilience.detection import Detector
    from resilience.monitoring import Snapshot
    det = Detector({}, ["python"])
    s1 = Snapshot.from_telemetry("C", 100.0, True, {"instance_id": "i", "requests_total": 100, "tx_bytes": 0})
    s2 = Snapshot.from_telemetry("C", 102.0, True, {"instance_id": "i", "requests_total": 110, "tx_bytes": 1000})
    det.network(s1, 1.0)
    det.network(s2, 1.0)
    m = det.measurements["C"]["network"]
    assert m["requests_total"] == 110 and m["requests_per_s"] == 5.0 and m["confidence"] == 0.0           # display only: no effect on the decision
    old = Snapshot.from_telemetry("D", 1.0, True, {})
    det.network(old, 1.0)
    assert det.measurements["D"]["network"]["requests_per_s"] is None


# ------------------------------------------------------------------ the Live charts
def serve_live(r, with_obs=True):
    r.serve("live", "LIVE_CLUSTER", telemetry={"age_s": 1.2, "oldest_age_s": 2.0, "reachable": 4, "total": 4})
    for f in r.fakes["live"]:
        f.data["observations"] = {n: {"reachable": True, "network": {"outbound_connections": 2 + i, "tx_rate_Bps": 5000.0 * (i + 1), "requests_per_s": 1.5 * (i + 1),
                                                                    "confidence": 0}, "auth": {"failures_in_window": i % 2}}
                                  for i, n in enumerate("ABCD")} if with_obs else {}


def test_series_are_served_for_the_live_channel_only(monkeypatch):
    import test_two_pages as T
    r = T.Rig(monkeypatch)
    r.serve("live", "LIVE_CLUSTER", telemetry={"age_s": 1.2, "oldest_age_s": 2.0})
    serve_live(r)
    r.serve("demo", "SIMULATOR")
    for f in r.fakes["demo"]:
        f.data["observations"] = {"C": {"network": {"requests_per_s": 99.0}}}
    for _ in range(3):
        r.poll()
        time.sleep(0.05)
    code, live = r.get("/api/series?channel=live")
    assert code == 200 and live["available"] and live["data_source"] == "LIVE_CLUSTER" and live["age_s"] == 1.2
    assert sorted(live["points"]) == ["A", "B", "C", "D"] and len(live["points"]["B"]) >= 3
    assert live["points"]["B"][-1]["rps"] == 3.0 and live["points"]["B"][-1]["tx_Bps"] == 10000.0
    code, demo = r.get("/api/series?channel=demo")
    assert demo["available"] is False and "points" not in demo                     # a simulated reading can never be drawn on the Live page


@pytest.mark.skipif(not os.path.exists(CHROMIUM), reason="chromium not available")
def test_the_live_page_draws_charts_labelled_live_cluster_and_the_demo_page_does_not(monkeypatch):
    from playwright.sync_api import sync_playwright
    import test_two_pages as T
    r = T.Rig(monkeypatch)
    serve_live(r)
    r.serve("demo", "SIMULATOR")
    for _ in range(4):
        r.poll()
        time.sleep(0.05)
    r.d.COLLECTOR_THREAD = None
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        try:
            pg = b.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(r.url + "/?mode=live")
            pg.wait_for_selector("#livecharts:not([hidden])", timeout=20000)
            txt = pg.inner_text("#livecharts")
            assert "LIVE_CLUSTER" in txt and "telemetry age" in txt and "Requests per second" in txt and "Failed logins" in txt
            assert pg.locator("#livecharts svg").count() == 16                              # 4 workloads x 4 charts
            assert "never read patient records" in txt and errs == []
            pg2 = b.new_page()
            pg2.goto(r.url + "/?mode=demo")
            pg2.wait_for_timeout(3000)
            assert pg2.locator("#livecharts").is_hidden()
        finally:
            b.close()
