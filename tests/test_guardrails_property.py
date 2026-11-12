"""Property-based: whatever the policy (or another agent) asks for, no cap is ever exceeded."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from fnmatch import fnmatchcase

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ads_guardrail import AuditLog, InMemoryAdAccount, apply
from ads_guardrail.audit import AuditState
from ads_guardrail.config import ChangeCaps, Config, GuardrailConfig
from ads_guardrail.demo import audit_breaches
from ads_guardrail.guardrails import decide, per_change_limit
from ads_guardrail.models import ChangeType, Entity, Plan, Platform, Proposal, Verdict, stable_id

NOW = datetime(2026, 11, 1, 6, tzinfo=timezone.utc)
CENT = 10_000

cents = st.integers(min_value=1, max_value=200_000).map(lambda c: c * CENT)  # $0.01 .. $2,000


@st.composite
def caps(draw):
    return ChangeCaps(draw(st.integers(1, 10_000)), draw(st.integers(1, 500_000)) * CENT, draw(st.integers(0, 5_000)) * CENT)


@st.composite
def worlds(draw):
    n = draw(st.integers(1, 12))
    accounts = ["a1", "a2"]
    ents = []
    for i in range(n):
        has_bid = draw(st.booleans())
        ents.append(Entity(platform=Platform.GOOGLE_ADS, account_id=draw(st.sampled_from(accounts)),
                           entity_id=f"e{i}", name=f"e{i}", daily_budget_micros=draw(cents),
                           bid_micros=draw(cents) if has_bid else None, budget_shared=draw(st.booleans()),
                           target_cpa_micros=50_000_000))
    props = []
    for e in ents:
        for ct in (ChangeType.BUDGET, ChangeType.BID):
            before = e.daily_budget_micros if ct is ChangeType.BUDGET else e.bid_micros
            if before is None or not draw(st.booleans()):
                continue
            # anything from -100% to +100x, including absurd requests
            proposed = draw(st.one_of(st.integers(0, before * 100), st.integers(0, 10**12)))
            props.append(Proposal(stable_id(e.key, ct.value), "run", e.key, e.platform, e.account_id, e.entity_id,
                                  e.name, ct, before, proposed, "prop", "test", draw(st.floats(0, 1)), {}))
    allow = tuple(f"google_ads:*:e{i}" for i in range(n) if draw(st.booleans()))
    cfg = GuardrailConfig(
        budget=draw(caps()), bid=draw(caps()),
        account_daily_change_cap_micros=draw(st.integers(1, 100_000)) * CENT,
        account_total_budget_cap_micros={"a1": draw(st.integers(1, 3_000_000)) * CENT},
        default_account_total_budget_cap_micros=draw(st.integers(1, 3_000_000)) * CENT,
        cooldown_hours=draw(st.integers(0, 200)), max_changes_per_run=draw(st.integers(0, 15)),
        allowlist=allow, min_change_micros=draw(st.integers(0, 1000)) * CENT,
    )
    state = AuditState()
    for e in ents:
        if draw(st.booleans()):
            state.last_write_at[e.key] = NOW - timedelta(hours=draw(st.integers(0, 300)))
    for a in accounts:
        state.movement_by_account_day[(a, NOW.date().isoformat())] = draw(st.integers(0, 120_000)) * CENT
    return ents, props, cfg, state


@settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(worlds())
def test_no_decision_ever_exceeds_a_cap(world):
    ents, props, cfg, state = world
    decisions = decide(props, ents, cfg, state, NOW)
    assert len(decisions) == len(props)
    by_key = {e.key: e for e in ents}
    writable = [d for d in decisions if d.writable]
    assert len(writable) <= cfg.max_changes_per_run
    assert len({d.proposal.entity_key for d in writable}) == len(writable)  # one per entity per run

    moved: dict[str, int] = {}
    raised: dict[str, int] = {}
    for d in decisions:
        p = d.proposal
        if d.verdict is Verdict.BLOCKED:
            assert d.final_micros == p.before_micros
            continue
        delta = d.final_delta_micros
        c = cfg.budget if p.change_type is ChangeType.BUDGET else cfg.bid
        assert abs(delta) <= per_change_limit(c, p.before_micros)              # per-change % and $
        assert abs(delta) <= abs(p.delta_micros)                                # never more than asked
        assert delta == 0 or (delta > 0) == (p.delta_micros > 0)                # never flips direction
        assert d.final_micros % CENT == 0                                       # platform step
        if delta < 0:
            assert d.final_micros >= c.floor_micros                             # floor
        if d.writable:
            assert abs(delta) >= cfg.min_change_micros
            assert any(fnmatchcase(p.entity_key, pat) for pat in cfg.allowlist)  # allowlist
            last = state.last_write_at.get(p.entity_key)
            assert last is None or NOW - last >= timedelta(hours=cfg.cooldown_hours)  # cooldown
            if p.change_type is ChangeType.BUDGET:
                assert not by_key[p.entity_key].budget_shared
                moved[p.account_id] = moved.get(p.account_id, 0) + abs(delta)
                if delta > 0:
                    raised[p.account_id] = raised.get(p.account_id, 0) + delta
    for acct, mv in moved.items():
        assert state.movement(acct, NOW.date().isoformat()) + mv <= cfg.account_daily_change_cap_micros
    for acct, up in raised.items():
        total = sum(e.daily_budget_micros for e in ents if e.account_id == acct)
        assert total + up <= cfg.total_cap_for(acct)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(worlds(), st.integers(1, 6))
def test_end_to_end_multi_day_writes_respect_caps(tmp_path_factory, world, days):
    """Apply absurd requests day after day to a fake account; replay the audit log independently."""
    ents, props, gcfg, _ = world
    cfg = Config(guardrails=gcfg, writes_enabled=True)
    acct = InMemoryAdAccount(ents)
    audit = AuditLog(tmp_path_factory.mktemp("a") / "audit.jsonl")
    from ads_guardrail.killswitch import KillSwitch
    ks = KillSwitch(file=tmp_path_factory.mktemp("k") / "KILL", env_var="X", env={})
    for i in range(days):
        now = NOW + timedelta(days=i)
        live = {e.key: e for e in acct.list_entities()}
        todays = []
        for p in props:
            e = live[p.entity_key]
            before = e.daily_budget_micros if p.change_type is ChangeType.BUDGET else e.bid_micros
            todays.append(Proposal(stable_id(i, p.proposal_id), f"run-{i}", p.entity_key, p.platform, p.account_id,
                                   p.entity_id, p.entity_name, p.change_type, before,
                                   max(0, before + (p.proposed_micros - p.before_micros)), "prop", "t", p.confidence, {}))
        apply(Plan(f"run-{i}", date(2026, 11, 1) + timedelta(days=i), todays, [], []), acct, cfg, audit,
              apply=True, now=now, kill_switch=ks)
    assert audit.verify()[0]
    assert audit_breaches(audit, cfg) == []
    for a in {e.account_id for e in ents}:
        start = sum(e.daily_budget_micros for e in ents if e.account_id == a)
        now_total = sum(e.daily_budget_micros for e in acct.list_entities() if e.account_id == a)
        assert now_total <= max(start, cfg.guardrails.total_cap_for(a))
