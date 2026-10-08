"""Dashboard server: the four agents' copies of one investigation merge into one row."""
import importlib.util
import os

import pytest

from test_investigation import Cluster, started
from resilience.investigation import AMBIGUOUS, FALSE_POSITIVE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def dash(monkeypatch):
    monkeypatch.setenv("STATUS_URLS", "http://x:1/status,http://x:2/status,http://x:3/status,http://x:4/status")
    spec = importlib.util.spec_from_file_location("dash_server_inv", os.path.join(ROOT, "dashboard", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def live(cl):
    return {n: a.status() for n, a in cl.agents.items()}


def test_active_investigation_is_one_row_with_question_and_time_left(dash):
    cl = Cluster()
    inv = started(cl)
    view = dash.investigations_view(live(cl))
    assert view["enabled"] and len(view["rows"]) == 1
    row = view["rows"][0]
    assert row["id"] == inv.id and row["active"] and row["target"] == "C" and row["workload"] == "records-api"
    assert 0 < row["time_left_s"] <= 10 and "still present" in row["question"]
    assert set(row["per_agent"]) == {"A", "B", "C", "D"}, "every agent's copy shows up under one id"
    assert row["outcome"] is None and row["outcome_text"] == "in progress"


def test_closed_outcome_and_human_review_flag_are_reported(dash):
    cl = Cluster()
    cl.world.inject_connections("C", lambda t: 40, observers={"A", "B"})
    inv = started(cl, kind_evidence=("A", "B"))
    cl.sample_all(8)
    cl.poll(inv)
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    for a in cl.agents.values():
        a.inv.tick(cl.now)
    view = dash.investigations_view(live(cl))
    row = view["rows"][0]
    assert not row["active"] and row["outcome"] == AMBIGUOUS and "human review" in row["outcome_text"]
    assert view["watch"] and view["watch"][0]["review_needed"] and view["watch"][0]["target"] == "C"
    assert sorted(view["watch"][0]["agents"]) == ["A", "B", "C", "D"]


def test_false_positive_has_no_watch_and_disabled_hides_the_panel(dash):
    cl = Cluster()
    inv = started(cl, kind_evidence=("A", "B"))
    cl.sample_all(8)
    cl.poll(inv)
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    for a in cl.agents.values():
        a.inv.tick(cl.now)
    view = dash.investigations_view(live(cl))
    assert view["rows"][0]["outcome"] == FALSE_POSITIVE and view["watch"] == []
    off = Cluster(enabled=False)
    assert dash.investigations_view(live(off)) == {"enabled": False, "rows": [], "watch": []}
