"""The adapter interface every connector implements.

Reads (TRUTH) and writes (HANDS) are split so a warehouse can supply the numbers while a
platform adapter does the writing. A write always returns the value read back from the
platform afterwards, so the audit log records what is actually live, not what we asked for.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from ..models import ChangeType, DailyMetric, Entity

# Google Ads budgets must be a multiple of 10,000 micros (one cent). Meta takes integer cents.
CENT_MICROS = 10_000


class AdapterError(RuntimeError):
    pass


@runtime_checkable
class AdsReader(Protocol):
    def list_entities(self) -> list[Entity]: ...

    def fetch_metrics(self, start: date, end: date) -> list[DailyMetric]: ...


@runtime_checkable
class AdsWriter(Protocol):
    def read_value(self, entity_key: str, change_type: ChangeType) -> int:
        """Fresh read of the live value right before a write (drift check)."""
        ...

    def write_value(self, entity_key: str, change_type: ChangeType, new_micros: int) -> int:
        """Write and return the value read back afterwards."""
        ...

    def quantum_micros(self, entity_key: str, change_type: ChangeType) -> int:
        """Smallest step the platform accepts. Final values are rounded toward the old value."""
        ...


@runtime_checkable
class AdsAdapter(AdsReader, AdsWriter, Protocol):
    pass


class RoutedAdapter:
    """Combine several adapters (e.g. Google + Meta) behind one interface, routed by platform."""

    def __init__(self, *adapters: AdsAdapter) -> None:
        self._adapters = list(adapters)
        self._route: dict[str, AdsAdapter] = {}

    def list_entities(self) -> list[Entity]:
        out: list[Entity] = []
        for a in self._adapters:
            for e in a.list_entities():
                self._route[e.key] = a
                out.append(e)
        return out

    def fetch_metrics(self, start: date, end: date) -> list[DailyMetric]:
        return [m for a in self._adapters for m in a.fetch_metrics(start, end)]

    def _for(self, key: str) -> AdsAdapter:
        if key not in self._route:
            self.list_entities()
        try:
            return self._route[key]
        except KeyError as exc:
            raise AdapterError(f"unknown entity {key}") from exc

    def read_value(self, entity_key: str, change_type: ChangeType) -> int:
        return self._for(entity_key).read_value(entity_key, change_type)

    def write_value(self, entity_key: str, change_type: ChangeType, new_micros: int) -> int:
        return self._for(entity_key).write_value(entity_key, change_type, new_micros)

    def quantum_micros(self, entity_key: str, change_type: ChangeType) -> int:
        return self._for(entity_key).quantum_micros(entity_key, change_type)
