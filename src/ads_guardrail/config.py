"""Configuration: guardrail caps, policy thresholds, write switch. Loaded from TOML.

Currency amounts in the TOML file are written in dollars for humans and converted to
integer micros here. Percent caps are stored as basis points so the cap math is exact.
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .models import usd


@dataclass(frozen=True)
class ChangeCaps:
    max_change_bp: int  # largest change as basis points of the current value (2000 = 20%)
    max_change_abs_micros: int  # largest change in absolute micros
    floor_micros: int  # never write a value below this

    def __post_init__(self) -> None:
        if not 0 < self.max_change_bp <= 10_000:
            raise ValueError("max_change_pct must be in (0, 100]")
        if self.max_change_abs_micros <= 0 or self.floor_micros < 0:
            raise ValueError("caps must be positive")


@dataclass(frozen=True)
class GuardrailConfig:
    budget: ChangeCaps = ChangeCaps(2000, usd(50), usd(5))
    bid: ChangeCaps = ChangeCaps(1500, usd(10), usd(1))
    # Sum of |budget change| one account may receive in one calendar day (UTC).
    account_daily_change_cap_micros: int = usd(150)
    # Sum of all daily budgets in an account may never be raised above this.
    account_total_budget_cap_micros: dict[str, int] = field(default_factory=dict)
    default_account_total_budget_cap_micros: int = usd(5_000)
    cooldown_hours: int = 72
    max_changes_per_run: int = 8
    # fnmatch patterns on "platform:account:entity". Empty list = nothing is writable.
    allowlist: tuple[str, ...] = ()
    # Changes smaller than this are not worth a write (and a learning reset).
    min_change_micros: int = usd(1)

    def total_cap_for(self, account_id: str) -> int:
        return self.account_total_budget_cap_micros.get(
            account_id, self.default_account_total_budget_cap_micros
        )


@dataclass(frozen=True)
class PolicyConfig:
    lookback_days: int = 7
    min_conversions: float = 10.0
    min_spend_micros: int = usd(100)
    min_days_of_data: int = 5
    min_spend_target_multiple: float = 3.0  # or: spent at least 3x target CPA in the window
    cpa_band: float = 0.15  # act only when CPA is more than 15% better or worse than target
    roas_band: float = 0.15
    min_confidence: float = 0.80
    budget_limited_utilization: float = 0.90  # spend/budget at or above this = budget-limited
    underpacing_utilization: float = 0.60
    # Diminishing returns: conversions scale with spend ** elasticity (used for impact estimates).
    spend_elasticity: float = 0.75


@dataclass(frozen=True)
class KillSwitchConfig:
    file: str = "KILL_SWITCH"
    env_var: str = "ADS_GUARDRAIL_KILL"


@dataclass(frozen=True)
class Config:
    guardrails: GuardrailConfig = GuardrailConfig()
    policy: PolicyConfig = PolicyConfig()
    kill_switch: KillSwitchConfig = KillSwitchConfig()
    writes_enabled: bool = False  # the config half of the two-key write switch

    def fingerprint(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:8]

    def with_writes(self, enabled: bool) -> "Config":
        return replace(self, writes_enabled=enabled)


def _caps(raw: dict[str, Any], default: ChangeCaps) -> ChangeCaps:
    return ChangeCaps(
        max_change_bp=int(round(float(raw.get("max_change_pct", default.max_change_bp / 100)) * 100)),
        max_change_abs_micros=usd(float(raw["max_change_abs"])) if "max_change_abs" in raw else default.max_change_abs_micros,
        floor_micros=usd(float(raw["floor"])) if "floor" in raw else default.floor_micros,
    )


def load_config(path: str | Path) -> Config:
    data = tomllib.loads(Path(path).read_text())
    g = data.get("guardrails", {})
    p = data.get("policy", {})
    k = data.get("kill_switch", {})
    dg = GuardrailConfig()
    dp = PolicyConfig()
    guard = GuardrailConfig(
        budget=_caps(g.get("budget", {}), dg.budget),
        bid=_caps(g.get("bid", {}), dg.bid),
        account_daily_change_cap_micros=usd(float(g.get("account_daily_change_cap", dg.account_daily_change_cap_micros / 1e6))),
        account_total_budget_cap_micros={a: usd(float(v)) for a, v in g.get("account_total_budget_cap", {}).items()},
        default_account_total_budget_cap_micros=usd(float(g.get("default_account_total_budget_cap", dg.default_account_total_budget_cap_micros / 1e6))),
        cooldown_hours=int(g.get("cooldown_hours", dg.cooldown_hours)),
        max_changes_per_run=int(g.get("max_changes_per_run", dg.max_changes_per_run)),
        allowlist=tuple(g.get("allowlist", [])),
        min_change_micros=usd(float(g.get("min_change", dg.min_change_micros / 1e6))),
    )
    policy = PolicyConfig(
        lookback_days=int(p.get("lookback_days", dp.lookback_days)),
        min_conversions=float(p.get("min_conversions", dp.min_conversions)),
        min_spend_micros=usd(float(p.get("min_spend", dp.min_spend_micros / 1e6))),
        min_days_of_data=int(p.get("min_days_of_data", dp.min_days_of_data)),
        min_spend_target_multiple=float(p.get("min_spend_target_multiple", dp.min_spend_target_multiple)),
        cpa_band=float(p.get("cpa_band", dp.cpa_band)),
        roas_band=float(p.get("roas_band", dp.roas_band)),
        min_confidence=float(p.get("min_confidence", dp.min_confidence)),
        budget_limited_utilization=float(p.get("budget_limited_utilization", dp.budget_limited_utilization)),
        underpacing_utilization=float(p.get("underpacing_utilization", dp.underpacing_utilization)),
        spend_elasticity=float(p.get("spend_elasticity", dp.spend_elasticity)),
    )
    ks = KillSwitchConfig(file=str(k.get("file", "KILL_SWITCH")), env_var=str(k.get("env_var", "ADS_GUARDRAIL_KILL")))
    writes = bool(data.get("writes", {}).get("enabled", False))
    return Config(guardrails=guard, policy=policy, kill_switch=ks, writes_enabled=writes)
