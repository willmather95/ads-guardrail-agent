"""Core data types.

Money is always an integer number of *micros* (1 USD = 1_000_000 micros), the same unit
Google Ads uses. Meta reports and accepts budgets in minor units (cents); the Meta adapter
converts at the edge. Integer money means caps are checked with exact arithmetic, never
with floats.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any

MICROS = 1_000_000


def usd(amount: float) -> int:
    """Convert a currency amount to integer micros (rounded to the nearest micro)."""
    return int(round(amount * MICROS))


def fmt_usd(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    return f"{sign}${abs(micros) / MICROS:,.2f}"


class Platform(str, Enum):
    GOOGLE_ADS = "google_ads"
    META = "meta"


class ChangeType(str, Enum):
    BUDGET = "budget"  # daily budget (Google campaign budget, Meta ad set daily_budget)
    BID = "bid"  # Google target CPA, Meta ad set bid_amount (cost cap)


class Verdict(str, Enum):
    APPROVED = "approved"  # within every cap as proposed
    CLAMPED = "clamped"  # cut down to the largest change the caps allow
    BLOCKED = "blocked"  # not allowed at all


@dataclass(frozen=True)
class Entity:
    """Something whose budget or bid we may change: a Google campaign or a Meta ad set."""

    platform: Platform
    account_id: str
    entity_id: str
    name: str
    daily_budget_micros: int
    bid_micros: int | None = None  # tCPA (Google) or cost-cap bid (Meta); None = no bid control
    learning: bool = False  # platform reports the entity is still in its learning phase
    budget_shared: bool = False  # Google shared budgets are not safe to edit per campaign
    target_cpa_micros: int | None = None
    target_roas: float | None = None
    status: str = "ENABLED"
    ref: str = ""  # platform handle for writes, e.g. a Google campaign budget resource name

    @property
    def key(self) -> str:
        return f"{self.platform.value}:{self.account_id}:{self.entity_id}"


@dataclass(frozen=True)
class DailyMetric:
    platform: Platform
    account_id: str
    entity_id: str
    day: date
    spend_micros: int
    impressions: int
    clicks: int
    conversions: float
    conversion_value_micros: int

    @property
    def key(self) -> str:
        return f"{self.platform.value}:{self.account_id}:{self.entity_id}"


@dataclass(frozen=True)
class Proposal:
    """What the policy engine wants to do, before any guardrail runs."""

    proposal_id: str
    run_id: str
    entity_key: str
    platform: Platform
    account_id: str
    entity_id: str
    entity_name: str
    change_type: ChangeType
    before_micros: int
    proposed_micros: int
    rule: str
    reason: str
    confidence: float
    expected_impact: dict[str, float]

    @property
    def delta_micros(self) -> int:
        return self.proposed_micros - self.before_micros

    @property
    def delta_pct(self) -> float:
        return 0.0 if self.before_micros == 0 else self.delta_micros / self.before_micros

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["platform"] = self.platform.value
        d["change_type"] = self.change_type.value
        return d


@dataclass(frozen=True)
class Decision:
    """A proposal after the guardrails: the final value we are allowed to write, and why."""

    proposal: Proposal
    verdict: Verdict
    final_micros: int  # equals before_micros when blocked
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def final_delta_micros(self) -> int:
        return self.final_micros - self.proposal.before_micros

    @property
    def writable(self) -> bool:
        return self.verdict is not Verdict.BLOCKED and self.final_delta_micros != 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal": self.proposal.to_dict(),
            "verdict": self.verdict.value,
            "final_micros": self.final_micros,
            "final_delta_micros": self.final_delta_micros,
            "notes": list(self.notes),
        }


@dataclass
class Plan:
    run_id: str
    as_of: date
    proposals: list[Proposal]
    decisions: list[Decision]
    holds: list[dict[str, str]]  # entities the policy looked at and chose not to touch, with why

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of": self.as_of.isoformat(),
            "decisions": [d.to_dict() for d in self.decisions],
            "holds": self.holds,
        }


def stable_id(*parts: object, n: int = 12) -> str:
    raw = json.dumps([str(p) for p in parts], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:n]
