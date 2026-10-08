"""The real monitoring path: HttpTelemetrySource -> the real mock healthcare app
(apps/src/app.py, run as a subprocess) -> Detector. The simulator replaces all of this
with FakeTelemetry, so before this test the HTTP path was only ever exercised live.

Only the file signal is checked for "clean": inside a test sandbox /proc shows every process
and every socket of the machine, not one container, so process and network readings depend
on the environment.
Nothing is attacked or modified: a wrong baseline stands in for a tampered file.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

from resilience.config import ClusterConfig, NodeSpec
from resilience.detection import Detector
from resilience.evidence import ObsType
from resilience.monitoring import HttpTelemetrySource

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "apps", "src")

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc/net"), reason="telemetry reads /proc (Linux)")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def app_url():
    port = _free_port()
    env = dict(os.environ, ROLE="database", PORT=str(port), APP_ROOT=SRC, POD_IP="127.0.0.1")
    proc = subprocess.Popen([sys.executable, os.path.join(SRC, "app.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=SRC)
    import urllib.request
    end = time.time() + 15
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("mock app did not start")
    yield f"http://127.0.0.1:{port}"
    proc.kill()
    proc.wait()


def _cfg(url):
    dead = f"http://127.0.0.1:{_free_port()}"
    nodes = {n: NodeSpec(n, f"agent-{n.lower()}", "x:1", "x", w, url if n == "D" else dead)
             for n, w in zip("ABCD", ("patient-portal", "auth-service", "records-api", "database"))}
    return ClusterConfig(nodes=nodes)


def _baseline():
    sys.path.insert(0, SRC)
    from telemetry import hash_tree
    return hash_tree(SRC)


def test_http_source_reads_health_and_real_telemetry(app_url):
    snaps = HttpTelemetrySource(_cfg(app_url), timeout_s=2.0).collect()
    d = snaps["D"]
    assert d.reachable and d.healthy and d.instance_id and d.pod_ip == "127.0.0.1"
    assert d.file_hashes == _baseline() and "app.py" in d.file_hashes
    assert any(p["comm"].startswith("python") for p in d.processes)
    assert not snaps["A"].reachable and not snaps["A"].healthy      # unreachable workload is explicit


def test_clean_app_raises_no_file_or_auth_detection(app_url):
    src = HttpTelemetrySource(_cfg(app_url), timeout_s=2.0)
    det = Detector(_baseline(), ["python", "python3"])
    out = {}
    for i in range(2):
        out = det.analyze(src.collect(), time.time())
        time.sleep(0.3)
    kinds = {x.observation for x in out["D"]}
    assert ObsType.FILE_INTEGRITY not in kinds and ObsType.AUTH not in kinds


def test_hash_mismatch_is_detected_through_the_real_http_path(app_url):
    wrong = dict(_baseline())
    wrong["app.py"] = "0" * 64              # stands in for "this file was modified"
    out = Detector(wrong, ["python", "python3"]).analyze(
        HttpTelemetrySource(_cfg(app_url), timeout_s=2.0).collect(), time.time())
    fi = [x for x in out["D"] if x.observation == ObsType.FILE_INTEGRITY]
    assert fi and fi[0].confidence == 0.95 and "app.py" in fi[0].summary
