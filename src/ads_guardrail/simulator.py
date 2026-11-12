"""SIMULATED market used by the fixture generator and the 14-day demo.

Each entity has a hidden response curve the policy never sees:

* Delivery: the auction absorbs at most `demand_cap` per day at the reference bid. A higher
  bid opens more demand (linear in bid). Spend = min(budget, demand) with pacing noise.
* Efficiency: average CPA rises with spend, `CPA = base_cpa * (spend / ref_spend) ** k`
  (diminishing returns), a little with bid, and 25% while in a learning phase.
* Counts: conversions are Poisson draws; order values are log-normal around `aov`.
* Learning: a budget or bid move over 20% restarts learning for 3 days. This is a stated
  simulator assumption (a common rule of thumb), not a documented platform threshold.

All numbers are synthetic. Nothing here is a real account.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date

from .connectors.memory import InMemoryAdAccount
from .models import MICROS, DailyMetric, Entity

LEARNING_RESET_THRESHOLD = 0.20
LEARNING_DAYS = 3


@dataclass(frozen=True)
class Curve:
    demand_cap: float  # dollars/day the auction will absorb at bid_ref
    ref_spend: float
    base_cpa: float
    k: float = 0.35
    bid_ref: float | None = None
    aov: float | None = None  # average order value (ecommerce); None = lead gen
    cpc: float = 3.0
    ctr: float = 0.04
    weekend: float = 1.08  # CPA multiplier on Sat/Sun

    @classmethod
    def from_dict(cls, d: dict) -> "Curve":
        return cls(**d)


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 40:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def simulate_entity_day(rng: random.Random, e: Entity, c: Curve, day: date) -> DailyMetric:
    budget = e.daily_budget_micros / MICROS
    bid_mult = (e.bid_micros / MICROS / c.bid_ref) if (e.bid_micros and c.bid_ref) else 1.0
    demand = c.demand_cap * bid_mult * rng.uniform(0.92, 1.06)
    spend = min(budget * rng.uniform(0.94, 1.0), demand)
    cpa = c.base_cpa * (max(spend, 1.0) / c.ref_spend) ** c.k * bid_mult ** 0.3
    if day.weekday() >= 5:
        cpa *= c.weekend
    if e.learning:
        cpa *= 1.25
    conv = poisson(rng, spend / cpa)
    value = 0.0
    if c.aov:
        value = sum(c.aov * math.exp(rng.gauss(-0.08, 0.4)) for _ in range(conv))
    clicks = max(conv, int(spend / (c.cpc * rng.uniform(0.9, 1.1))))
    impressions = int(clicks / (c.ctr * rng.uniform(0.85, 1.15))) if clicks else 0
    return DailyMetric(
        platform=e.platform, account_id=e.account_id, entity_id=e.entity_id, day=day,
        spend_micros=int(round(spend * 100)) * 10_000, impressions=impressions, clicks=clicks,
        conversions=float(conv), conversion_value_micros=int(round(value * 100)) * 10_000,
    )


class MarketSimulator:
    """Advances an InMemoryAdAccount one day at a time and tracks learning resets."""

    def __init__(self, account: InMemoryAdAccount, curves: dict[str, Curve], seed: int = 7,
                 learning_days: dict[str, int] | None = None) -> None:
        self.account = account
        self.curves = curves
        self.rng = random.Random(seed)
        self.learning_left: dict[str, int] = dict(learning_days or {})
        self._seen_writes = len(account.writes)

    def _register_new_writes(self) -> None:
        for w in self.account.writes[self._seen_writes:]:
            if change_is_learning_reset(w.before_micros, w.after_micros):
                self.learning_left[w.entity_key] = LEARNING_DAYS
        self._seen_writes = len(self.account.writes)

    def step(self, day: date) -> list[DailyMetric]:
        self._register_new_writes()
        out = []
        for e in self.account.list_entities():
            left = self.learning_left.get(e.key, 0)
            self.account.set_learning(e.key, left > 0)
            e = self.account.entity(e.key)
            out.append(simulate_entity_day(self.rng, e, self.curves[e.key], day))
            if left > 0:
                self.learning_left[e.key] = left - 1
        self.account.append_metrics(out)
        # Learning state as the platform would report it the next morning.
        for e in self.account.list_entities():
            self.account.set_learning(e.key, self.learning_left.get(e.key, 0) > 0)
        return out


def change_is_learning_reset(before: int, after: int) -> bool:
    return before > 0 and abs(after - before) / before > LEARNING_RESET_THRESHOLD
