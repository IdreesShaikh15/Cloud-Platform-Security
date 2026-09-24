"""Trust arithmetic, weighted evidence scoring and quorum counting."""
import time

import pytest

from resilience.config import QuorumParams
from resilience.evidence import Action, Evidence, ObsType, Vote
from resilience.quorum import VoteBook, should_vote_contain, weighted_score
from resilience.trust import TrustLedger, consensus_trust, linear_step

P = QuorumParams()


def e(origin, obs, conf, target="C"):
    return Evidence(origin=origin, target=target, observation=ObsType(obs), confidence=conf,
                    timestamp=time.time())


# ------------------------------------------------------------------ trust
def test_linear_decay_and_recovery():
    assert linear_step(100, -2.5, 10) == 75
    assert linear_step(10, -5, 10) == 0          # clamped at 0
    assert linear_step(95, 2, 10) == 100         # clamped at 100
    t = TrustLedger(["A"])
    t.decay("A", 2.5, 24)                        # 24 s of contradicted claims
    assert t.get("A") == pytest.approx(40)
    t.recover("A", 0.5, 20)
    assert t.get("A") == pytest.approx(50)
    t.penalize("A", 20)
    assert t.get("A") == pytest.approx(30)


def test_consensus_trust_median_resists_one_liar():
    assert consensus_trust([90, 92, 88, 0]) == pytest.approx(89)
    assert consensus_trust([]) == 100


# ------------------------------------------------------------------ BFT params
def test_bft_quorum_size():
    assert P.n == 4 and P.f == 1 and P.quorum == 3
    assert P.f == (P.n - 1) // 3


# ------------------------------------------------------------------ weighted score
FULL = lambda _s: 100.0  # noqa: E731


def test_single_liar_cannot_reach_threshold_even_claiming_all_types():
    ev = [e("A", t, 0.99) for t in ("NETWORK", "PROCESS", "FILE_INTEGRITY", "AUTH")]
    s = weighted_score(ev, FULL, P)
    assert s.corroborated_types == []
    assert s.total == pytest.approx(0.495)
    assert s.total < P.score_threshold


def test_two_senders_same_type_reach_threshold():
    s = weighted_score([e("A", "NETWORK", 0.8), e("B", "NETWORK", 0.8)], FULL, P)
    assert s.total == pytest.approx(0.8) and s.corroborated_types == ["NETWORK"]


def test_type_diversity_beats_repetition():
    same = weighted_score([e(o, "NETWORK", 0.7) for o in "ABC"], FULL, P).total
    diverse = weighted_score([e(o, t, 0.7) for o in "AB" for t in ("NETWORK", "PROCESS", "FILE_INTEGRITY")],
                             FULL, P).total
    assert diverse > same
    assert diverse == pytest.approx(0.7 + 2 * P.diversity_bonus)


def test_trust_down_weights_sender():
    ev = [e("A", "NETWORK", 0.9), e("B", "NETWORK", 0.9)]
    full = weighted_score(ev, FULL, P).total
    low = weighted_score(ev, lambda s: 20.0 if s == "A" else 100.0, P).total
    assert low == pytest.approx((0.9 * 0.2 + 0.9) / 2)
    assert low < full


def test_same_sender_repeating_counts_once():
    s = weighted_score([e("A", "NETWORK", 0.9) for _ in range(50)], FULL, P)
    assert s.total == pytest.approx(0.45)


def test_vote_rule_requires_local_observation():
    strong = weighted_score([e(o, "NETWORK", 0.9) for o in "ABC"], FULL, P)
    assert should_vote_contain(0.9, strong, P)
    assert not should_vote_contain(0.1, strong, P)       # hearsay only -> no vote
    weak = weighted_score([e("A", "NETWORK", 0.95)], FULL, P)
    assert not should_vote_contain(0.9, weak, P)          # uncorroborated -> no vote


# ------------------------------------------------------------------ vote book
def v(voter, target="C", epoch=1, action=Action.CONTAIN):
    return Vote(voter=voter, target=target, action=action, epoch=epoch)


def test_quorum_requires_three_distinct_eligible_voters():
    book = VoteBook(3)
    everyone = lambda _v: True  # noqa: E731
    book.add(v("A"))
    book.add(v("A"))                       # duplicate voter does not count twice
    book.add(v("B"))
    assert book.new_commits(everyone) == []
    book.add(v("D"))
    commits = book.new_commits(everyone)
    assert len(commits) == 1 and commits[0].voters == ["A", "B", "D"]
    assert book.new_commits(everyone) == []  # committed once only


def test_untrusted_voter_excluded():
    book = VoteBook(3)
    for n in "ABC":
        book.add(v(n))
    assert book.new_commits(lambda n: n != "A") == []
    book.add(v("D"))
    assert book.new_commits(lambda n: n != "A")[0].voters == ["B", "C", "D"]


def test_votes_for_different_epochs_do_not_mix():
    book = VoteBook(3)
    book.add(v("A", epoch=1))
    book.add(v("B", epoch=1))
    book.add(v("C", epoch=2))
    assert book.new_commits(lambda _n: True) == []
