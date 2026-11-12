from datetime import timedelta

from ads_guardrail.config import PolicyConfig
from ads_guardrail.models import ChangeType, Proposal
from ads_guardrail.policy import budget_multiplier, evaluate
from ads_guardrail.policy.rules import Hold

from conftest import AS_OF, entity, metrics_for

CFG = PolicyConfig()


def run(e, ms):
    props, holds = evaluate([e], ms, CFG, "run-x", AS_OF)
    return (props + holds)[0]


def test_budget_limited_winner_gets_budget_up():
    e = entity(budget=200, target_cpa=60)
    out = run(e, metrics_for(e, spend=196, conv=6))  # CPA $32.67 vs $60, 98% utilization
    assert isinstance(out, Proposal)
    assert out.change_type is ChangeType.BUDGET and out.proposed_micros > out.before_micros
    assert out.rule == "scale_winner" and out.confidence >= 0.8
    assert "CPA $32.67 vs target $60.00" in out.reason
    assert out.expected_impact["delta_conv_per_day"] > 0


def test_loser_without_bid_gets_budget_cut():
    e = entity(budget=150, target_cpa=60)
    out = run(e, metrics_for(e, spend=148, conv=1.2))
    assert isinstance(out, Proposal) and out.rule == "cut_loser"
    assert out.proposed_micros < out.before_micros


def test_loser_with_bid_gets_bid_cut():
    e = entity(budget=150, bid=80, target_cpa=60)
    out = run(e, metrics_for(e, spend=148, conv=1.2))
    assert out.change_type is ChangeType.BID and out.proposed_micros < out.before_micros


def test_underpacing_winner_gets_bid_up():
    e = entity(budget=150, bid=50, target_cpa=60)
    out = run(e, metrics_for(e, spend=70, conv=2.3))
    assert out.rule == "underpacing_bid_up" and out.proposed_micros > out.before_micros


def test_learning_phase_is_protected():
    e = entity(budget=200, target_cpa=60, learning=True)
    out = run(e, metrics_for(e, spend=196, conv=6))
    assert isinstance(out, Hold) and out.rule == "learning_phase"


def test_min_data_holds():
    e = entity(budget=200, target_cpa=60)
    assert run(e, metrics_for(e, days=3, spend=196, conv=6)).rule == "min_data"
    e2 = entity(budget=40, target_cpa=60)
    # 7 x $20 = $140 spend (2.3x target CPA), 3.5 conversions: not enough evidence either way
    assert run(e2, metrics_for(e2, spend=20, conv=0.5)).rule == "min_data"


def test_spend_multiple_unlocks_a_cut_before_ten_conversions():
    e = entity(budget=300, target_cpa=100)
    out = run(e, metrics_for(e, spend=290, conv=1.0))  # 7 conv, $2,030 spend = 20x target
    assert isinstance(out, Proposal) and out.rule == "cut_loser"


def test_within_band_holds():
    e = entity(budget=200, target_cpa=60)
    assert run(e, metrics_for(e, spend=196, conv=3.3)).rule == "within_band"


def test_roas_rules():
    e = entity(budget=100, target_cpa=None, target_roas=2.0)
    win = run(e, metrics_for(e, spend=98, conv=4, value=400))
    assert isinstance(win, Proposal) and win.change_type is ChangeType.BUDGET and win.proposed_micros > win.before_micros
    lose = run(e, metrics_for(e, spend=98, conv=2, value=100))
    assert isinstance(lose, Proposal) and lose.proposed_micros < lose.before_micros


def test_no_target_holds():
    e = entity(target_cpa=None)
    assert run(e, metrics_for(e)).rule == "no_target"


def test_budget_multiplier_is_bounded_and_monotone():
    vals = [budget_multiplier(r, 0.75) for r in (0.2, 0.6, 1.0, 1.4, 2.0, 5.0)]
    assert vals == sorted(vals)
    assert min(vals) >= 0.3 and max(vals) <= 3.0
    # at r = 1/e the marginal CPA is exactly target -> no change
    assert abs(budget_multiplier(1 / 0.75, 0.75) - 1.0) < 1e-9


def test_proposal_ids_are_stable_per_day_entity_type():
    e = entity(budget=200, target_cpa=60)
    a = run(e, metrics_for(e, spend=196, conv=6))
    b = run(e, metrics_for(e, spend=196, conv=6))
    assert a.proposal_id == b.proposal_id
    c, _ = evaluate([e], metrics_for(e, spend=196, conv=6, end=AS_OF), CFG, "run-y", AS_OF + timedelta(days=1))
    assert c[0].proposal_id != a.proposal_id
