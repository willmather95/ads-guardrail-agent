"""A fake, writable ad account held in memory. The demo and tests write here, never to a platform."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from ..models import ChangeType, DailyMetric, Entity
from .base import CENT_MICROS, AdapterError


@dataclass
class WriteRecord:
    entity_key: str
    change_type: ChangeType
    before_micros: int
    after_micros: int


class InMemoryAdAccount:
    """Holds entity state and metric history. Implements the full AdsAdapter interface."""

    def __init__(self, entities: list[Entity], metrics: list[DailyMetric] | None = None) -> None:
        self._entities: dict[str, Entity] = {e.key: e for e in entities}
        self._metrics: list[DailyMetric] = list(metrics or [])
        self.writes: list[WriteRecord] = []
        self.fail_on_write: set[str] = set()  # entity keys whose writes raise (fault injection)

    # --- reads -----------------------------------------------------------------------------
    def list_entities(self) -> list[Entity]:
        return list(self._entities.values())

    def fetch_metrics(self, start: date, end: date) -> list[DailyMetric]:
        return [m for m in self._metrics if start <= m.day <= end]

    def entity(self, key: str) -> Entity:
        try:
            return self._entities[key]
        except KeyError as exc:
            raise AdapterError(f"unknown entity {key}") from exc

    def snapshot(self) -> dict[str, tuple[int, int | None]]:
        """(budget, bid) per entity. Used to prove rollback restores the exact prior state."""
        return {k: (e.daily_budget_micros, e.bid_micros) for k, e in sorted(self._entities.items())}

    # --- writes ----------------------------------------------------------------------------
    def read_value(self, entity_key: str, change_type: ChangeType) -> int:
        e = self.entity(entity_key)
        if change_type is ChangeType.BUDGET:
            return e.daily_budget_micros
        if e.bid_micros is None:
            raise AdapterError(f"{entity_key} has no bid to edit")
        return e.bid_micros

    def write_value(self, entity_key: str, change_type: ChangeType, new_micros: int) -> int:
        if entity_key in self.fail_on_write:
            raise AdapterError(f"injected write failure for {entity_key}")
        if new_micros <= 0 or new_micros % CENT_MICROS:
            raise AdapterError("value must be a positive whole number of cents")
        before = self.read_value(entity_key, change_type)
        e = self.entity(entity_key)
        if change_type is ChangeType.BUDGET:
            e = replace(e, daily_budget_micros=new_micros)
        else:
            e = replace(e, bid_micros=new_micros)
        self._entities[entity_key] = e
        self.writes.append(WriteRecord(entity_key, change_type, before, new_micros))
        return self.read_value(entity_key, change_type)

    def quantum_micros(self, entity_key: str, change_type: ChangeType) -> int:
        return CENT_MICROS

    # --- simulation hooks --------------------------------------------------------------------
    def set_learning(self, entity_key: str, learning: bool) -> None:
        self._entities[entity_key] = replace(self.entity(entity_key), learning=learning)

    def append_metrics(self, metrics: list[DailyMetric]) -> None:
        self._metrics.extend(metrics)
