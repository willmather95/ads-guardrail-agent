from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ads_guardrail import Config, GuardrailConfig, InMemoryAdAccount
from ads_guardrail.models import DailyMetric, Entity, Platform, usd

AS_OF = date(2026, 11, 1)
NOW = datetime(2026, 11, 1, 6, tzinfo=timezone.utc)


def entity(eid="1", budget=100.0, bid=None, target_cpa=50.0, target_roas=None, learning=False,
           platform=Platform.GOOGLE_ADS, account="acct", shared=False) -> Entity:
    return Entity(platform=platform, account_id=account, entity_id=eid, name=f"E{eid}",
                  daily_budget_micros=usd(budget), bid_micros=usd(bid) if bid else None, learning=learning,
                  budget_shared=shared, target_cpa_micros=usd(target_cpa) if target_cpa else None,
                  target_roas=target_roas)


def metrics_for(e: Entity, days=7, spend=100.0, conv=2.0, value=0.0, end=AS_OF - timedelta(days=1)):
    return [DailyMetric(e.platform, e.account_id, e.entity_id, end - timedelta(days=i), usd(spend), 1000, 50,
                        conv, usd(value)) for i in range(days)]


def open_config(**g) -> Config:
    base = dict(allowlist=("*",))
    base.update(g)
    return Config(guardrails=GuardrailConfig(**base), writes_enabled=True)


@pytest.fixture
def account():
    """Three CPA campaigns: a budget-limited winner, a loser, and an underpacing winner with a bid."""
    w = entity("w", budget=200, target_cpa=60)
    l = entity("l", budget=150, target_cpa=60)
    u = entity("u", budget=150, bid=50, target_cpa=60)
    ms = metrics_for(w, spend=196, conv=6) + metrics_for(l, spend=148, conv=1.2) + metrics_for(u, spend=70, conv=2.3)
    return InMemoryAdAccount([w, l, u], ms)
