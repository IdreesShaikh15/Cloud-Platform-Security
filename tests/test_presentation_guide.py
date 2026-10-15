"""Healthcare Phase F: the presentation guide keeps simulator results and live-verified results apart, and its numbers match docs/RESULTS.md."""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = open(os.path.join(ROOT, "docs", "RESULTS.md"), encoding="utf-8").read()
pypdf = pytest.importorskip("pypdf")


@pytest.fixture(scope="module")
def guide(tmp_path_factory):
    out = tmp_path_factory.mktemp("g") / "guide.pdf"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "make-presentation-guide.py")], env=dict(os.environ, GUIDE_OUT=str(out)),
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    text = "\n".join(p.extract_text() for p in pypdf.PdfReader(str(out)).pages)
    return re.sub(r"\s+", " ", text)


def test_live_verified_table_is_an_empty_placeholder(guide):
    assert "LIVE-VERIFIED results: none yet" in guide and "placeholder for the presenter" in guide
    assert guide.count("not run") >= 8                                                  # every row of the live table is "not run"
    assert "Nothing in it has been verified" in guide
    assert "SIMULATOR results" in guide and "Nothing has yet been verified on a real cluster" in guide.replace("verified on a real cluster", "verified on a real cluster")


def test_the_guide_never_calls_a_simulator_number_a_live_one(guide):
    assert "Placeholder: Live Monitoring, connected" in guide and "None is included because none has been taken" in guide
    assert not re.search(r"measured on (the|a) real cluster", guide) and not re.search(r"verified on the (real|live) cluster: (yes|passed)", guide, re.I)


def test_numbers_quoted_in_the_guide_are_the_ones_in_results_md(guide):
    # (text in the guide, the same fact as written in docs/RESULTS.md)
    pairs = [("20 of 20 trials", "20/20"), ("12 of 20 down to 9 of 20", "from 12/20 to 9/20"), ("3 of 11 down to 0 of 11", "3/11 isolated without it, 0/11 with it"),
             ("1.0 s to 7.0 s", "1.04 s to 7.01 s"), ("80 of 80", "80 of 80"), ("0 of 30", "0/30"), ("50%", "50%"), ("4.2 s", "4.2 s"), ("18.5 s", "18.5 s")]
    for g, r in pairs:
        assert g in guide, g
        assert r in RESULTS, r


def test_guide_covers_the_required_parts(guide):
    for part in ("1. The problem, in plain English", "2. What I built", "Demo Mode and Live Monitoring", "Results measured in the simulator",
                 "Results verified on the real cluster", "Limitations, stated plainly", "Questions the faculty may ask", "Is this HIPAA compliant?",
                 "Has this been tested on a real cluster?", "What if two agents are compromised?", "Agrees, Disagrees, Unavailable"):
        assert part in guide, part
    for img in ("dashboard-pF-01-demo-mode.png", "dashboard-pF-02-live-not-connected.png"):               # fresh screenshots of both pages exist
        assert os.path.getsize(os.path.join(ROOT, "docs", "img", img)) > 20000
