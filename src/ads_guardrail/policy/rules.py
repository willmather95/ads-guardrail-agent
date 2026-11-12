"""Policy rules. Each rule reads one entity's window stats and returns a Proposal or a hold.

The policy is allowed to want big moves. It is the guardrails' job to cut them down. That
split is on purpose: the policy says what the data suggests, the guardrails say what is
safe, and the audit log keeps both so a human can see how much the caps held back.

Budget sizing uses marginal CPA. If conversions grow like spend ** e (e < 1, diminishing
returns), the next dollar costs avg_CPA / e. The budget multiplier that brings the marginal
CPA to target is m = (e * r) ** (1 / (1 - e)), where r = target_CPA / CPA (or ROAS / target).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..config import PolicyConfig
from ..models import MICROS, ChangeType, DailyMetric, Entity, Proposal, fmt_usd, stable_id
from ..stats import prob_cpa_below, prob_roas_above

MAX_POLICY_BUDGET_MULT = 3.0
MIN_POLICY_BUDGET_MULT = 0.3


@dataclass(frozen=True)
class WindowStats:
    days: int
    spend_micros: int
    conversions: float
    value_micros: int
    budget_micros: int

    @property
    def cpa(self) -> float | None:
        return self.spend_micros / MICROS / self.conversions if self.conversions else None

    @property
    def roas(self) -> float | None:
        return self.value_micros / self.spend_micros if self.spend_micros else None

    @property
    def utilization(self) -> float:
        if not self.days or not self.budget_micros:
            return 0.0
        return self.spend_micros / self.days / self.budget_micros

    @property
    def spend_per_day(self) -> float:
        return self.spend_micros / MICROS / self.days if self.days else 0.0

    @property
    def conv_per_day(self) -> float:
        return self.conversions / self.days if self.days else 0.0


def window_stats(entity: Entity, metrics: list[DailyMetric]) -> WindowStats:
    return WindowStats(
        days=len({m.day for m in metrics}),
        spend_micros=sum(m.spend_micros for m in metrics),
        conversions=sum(m.conversions for m in metrics),
        value_micros=sum(m.conversion_value_micros for m in metrics),
        budget_micros=entity.daily_budget_micros,
    )


@dataclass(frozen=True)
class Hold:
    entity_key: str
    rule: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"entity_key": self.entity_key, "rule": self.rule, "reason": self.reason}


def budget_multiplier(r: float, elasticity: float) -> float:
    m = (elasticity * r) ** (1.0 / (1.0 - elasticity))
    return max(MIN_POLICY_BUDGET_MULT, min(MAX_POLICY_BUDGET_MULT, m))


def project(stats: WindowStats, spend_mult: float, elasticity: float, aov: float | None) -> dict[str, float]:
    """Expected daily impact if spend scales by spend_mult (diminishing returns model)."""
    base_spend, base_conv = stats.spend_per_day, stats.conv_per_day
    new_spend = base_spend * spend_mult
    new_conv = base_conv * spend_mult ** elasticity
    out = {
        "spend_per_day": round(base_spend, 2),
        "conv_per_day": round(base_conv, 3),
        "delta_spend_per_day": round(new_spend - base_spend, 2),
        "delta_conv_per_day": round(new_conv - base_conv, 3),
        "projected_cpa": round(new_spend / new_conv, 2) if new_conv else 0.0,
    }
    if aov:
        out["delta_value_per_day"] = round((new_conv - base_conv) * aov, 2)
        out["projected_roas"] = round(new_conv * aov / new_spend, 3) if new_spend else 0.0
    return out


def evaluate_entity(entity: Entity, metrics: list[DailyMetric], cfg: PolicyConfig, run_id: str,
                    as_of: date) -> Proposal | Hold:
    key = entity.key
    if entity.status not in ("ENABLED", "ACTIVE"):
        return Hold(key, "status", f"status is {entity.status}")
    if entity.learning:
        return Hold(key, "learning_phase", "still in the platform's learning phase; changes would restart it")
    if entity.target_cpa_micros is None and entity.target_roas is None:
        return Hold(key, "no_target", "no CPA or ROAS target set, so there is nothing to judge against")

    s = window_stats(entity, metrics)
    if s.days < cfg.min_days_of_data:
        return Hold(key, "min_data", f"only {s.days} days of data (need {cfg.min_days_of_data})")
    # Enough evidence = enough conversions, OR (CPA accounts) spend of several target CPAs.
    # The second path lets a clearly broken campaign be cut before it buys 10 expensive leads.
    spent_targets = (s.spend_micros / entity.target_cpa_micros) if entity.target_cpa_micros else 0.0
    if s.spend_micros < cfg.min_spend_micros or (
            s.conversions < cfg.min_conversions and spent_targets < cfg.min_spend_target_multiple):
        need = f"{cfg.min_conversions:g} conversions"
        if entity.target_cpa_micros:
            need += f" or {cfg.min_spend_target_multiple:g}x target CPA in spend"
        return Hold(key, "min_data", f"{s.conversions:g} conversions and {fmt_usd(s.spend_micros)} spend in "
                    f"{s.days} days; need {need} (and at least {fmt_usd(cfg.min_spend_micros)})")

    spend = s.spend_micros / MICROS
    aov = s.value_micros / MICROS / s.conversions if (entity.target_roas and s.conversions) else None
    if entity.target_roas is not None:
        roas = s.roas or 0.0
        r = roas / entity.target_roas
        p_good = prob_roas_above(spend, s.conversions, s.value_micros / MICROS, entity.target_roas)
        perf = f"ROAS {roas:.2f} vs target {entity.target_roas:.2f}"
        band = cfg.roas_band
    else:
        tgt = entity.target_cpa_micros / MICROS  # type: ignore[operator]
        cpa = s.cpa or float("inf")
        r = tgt / cpa
        p_good = prob_cpa_below(spend, s.conversions, tgt)
        perf = f"CPA ${cpa:,.2f} vs target ${tgt:,.2f}"
        band = cfg.cpa_band
    p_bad = 1.0 - p_good
    util = s.utilization
    e = cfg.spend_elasticity
    win = f"last {s.days} days: {perf}, {s.conversions:g} conversions, spent {util:.0%} of budget"

    def make(change: ChangeType, before: int, proposed: int, rule: str, why: str, conf: float,
             spend_mult: float) -> Proposal:
        pid = stable_id(as_of.isoformat(), key, change.value)
        return Proposal(
            proposal_id=pid, run_id=run_id, entity_key=key, platform=entity.platform,
            account_id=entity.account_id, entity_id=entity.entity_id, entity_name=entity.name,
            change_type=change, before_micros=before, proposed_micros=proposed, rule=rule,
            reason=f"{why} ({win}).", confidence=round(conf, 4),
            expected_impact=project(s, spend_mult, e, aov),
        )

    if r >= 1 + band:
        if p_good < cfg.min_confidence:
            return Hold(key, "low_confidence", f"looks efficient but confidence {p_good:.0%} is below {cfg.min_confidence:.0%}")
        if util >= cfg.budget_limited_utilization:
            m = budget_multiplier(r, e)
            if m <= 1.01:
                return Hold(key, "at_efficient_scale", "efficient, but more budget would push the next dollar past target")
            proposed = int(entity.daily_budget_micros * m)
            return make(ChangeType.BUDGET, entity.daily_budget_micros, proposed, "scale_winner",
                        f"Raise budget: beating target and limited by budget. Marginal-CPA sizing says x{m:.2f}", p_good, m)
        if util <= cfg.underpacing_utilization and entity.bid_micros:
            m = min(1.0 + (r - 1.0) / 2.0, 1.5)
            proposed = int(entity.bid_micros * m)
            return make(ChangeType.BID, entity.bid_micros, proposed, "underpacing_bid_up",
                        "Raise bid: beating target but the bid limits delivery, so budget goes unspent", p_good,
                        min(m, 1.0 / max(util, 0.01)))
        return Hold(key, "not_constrained", f"efficient, but not limited by budget ({util:.0%} used) or bid")

    if r <= 1 - band:
        if p_bad < cfg.min_confidence:
            return Hold(key, "low_confidence", f"looks inefficient but confidence {p_bad:.0%} is below {cfg.min_confidence:.0%}")
        if entity.bid_micros:
            m = max(r, 0.6)
            proposed = int(entity.bid_micros * m)
            return make(ChangeType.BID, entity.bid_micros, proposed, "over_target_bid_down",
                        "Lower bid: missing target, so bid closer to what a conversion is worth", p_bad, m)
        m = budget_multiplier(r, e)
        proposed = int(entity.daily_budget_micros * m)
        return make(ChangeType.BUDGET, entity.daily_budget_micros, proposed, "cut_loser",
                    f"Cut budget: missing target. Marginal-CPA sizing says x{m:.2f}", p_bad, m)

    return Hold(key, "within_band", f"within {band:.0%} of target; leave it alone")

