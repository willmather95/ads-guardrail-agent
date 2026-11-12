"""Run every rule over every entity for one as-of date."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from ..config import PolicyConfig
from ..connectors.base import AdsReader
from ..models import DailyMetric, Entity, Proposal
from .rules import Hold, evaluate_entity


def evaluate(entities: list[Entity], metrics: list[DailyMetric], cfg: PolicyConfig, run_id: str,
             as_of: date) -> tuple[list[Proposal], list[Hold]]:
    """`as_of` is the run date. The window is the `lookback_days` full days before it."""
    start = as_of - timedelta(days=cfg.lookback_days)
    end = as_of - timedelta(days=1)
    by_key: dict[str, list[DailyMetric]] = defaultdict(list)
    for m in metrics:
        if start <= m.day <= end:
            by_key[m.key].append(m)
    proposals: list[Proposal] = []
    holds: list[Hold] = []
    for e in sorted(entities, key=lambda x: x.key):
        out = evaluate_entity(e, by_key.get(e.key, []), cfg, run_id, as_of)
        (proposals if isinstance(out, Proposal) else holds).append(out)  # type: ignore[arg-type]
    return proposals, holds


def read_window(reader: AdsReader, cfg: PolicyConfig, as_of: date) -> tuple[list[Entity], list[DailyMetric]]:
    entities = reader.list_entities()
    metrics = reader.fetch_metrics(as_of - timedelta(days=cfg.lookback_days), as_of - timedelta(days=1))
    return entities, metrics
