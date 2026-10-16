"""Phase J: the sandbox-cluster report must stay clearly separate from simulator results and from the user's own live table."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = open(os.path.join(ROOT, "docs", "SANDBOX-CLUSTER.md"), encoding="utf-8").read()


def test_the_report_is_labelled_and_lists_deviations_and_every_bug():
    assert DOC.startswith("# SANDBOX-CLUSTER results")
    assert "These are SANDBOX-CLUSTER results" in DOC and "not** simulator results" in DOC
    assert "the live table in the presentation guide stays empty" in DOC
    for text in ("cgroups v1", "runc", "CAP_SYS_RESOURCE", "registry.k8s.io", "quay.io", "Metrics API",
                 "verify-webhook.sh", "verify-isolation.sh", "upgrade-app.sh", "step 4b"):
        assert text in DOC, text
    assert DOC.count("| 1 |") >= 1 and "| 4 |" in DOC                      # four numbered bugs
    assert "What this does not prove" in DOC


def test_the_live_table_in_the_guide_is_still_the_users_to_fill():
    guide = open(os.path.join(ROOT, "scripts", "make-presentation-guide.py"), encoding="utf-8").read()
    assert "SANDBOX" not in guide.upper().replace("SANDBOX-CLUSTER", "") or "placeholder" in guide.lower()
