"""FixtureAdapter: the recorded SYNTHETIC accounts, parsed by the real connector parsers,
loaded into a writable in-memory account. Zero credentials, zero network."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..simulator import Curve
from . import google_ads, meta
from .memory import InMemoryAdAccount

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load(p: Path) -> Any:
    return json.loads(p.read_text())


class FixtureAdapter(InMemoryAdAccount):
    def __init__(self, fixture_dir: str | Path | None = None) -> None:
        d = Path(fixture_dir) if fixture_dir else FIXTURE_DIR
        self.manifest = _load(d / "manifest.json")
        if not self.manifest.get("_synthetic"):
            raise ValueError("fixture manifest must be labeled _synthetic")
        accounts = {a["platform"]: a["account_id"] for a in self.manifest["accounts"]}
        g_id, m_id = accounts["google_ads"], accounts["meta"]
        g_targets = _load(d / "google_ads/targets.json")["targets"]
        m_targets = _load(d / "meta/targets.json")["targets"]
        entities = (
            google_ads.parse_entities(g_id, _load(d / "google_ads/campaigns.searchStream.json"), g_targets)
            + meta.parse_adsets(m_id, _load(d / "meta/adsets.pages.json"), m_targets)
        )
        metrics = (
            google_ads.parse_metrics(g_id, _load(d / "google_ads/metrics.searchStream.json"))
            + meta.parse_insights(m_id, _load(d / "meta/insights.pages.json"), {meta.DEFAULT_CONVERSION_ACTION})
        )
        super().__init__(entities, metrics)
        sim = _load(d / "sim/hidden_curves.json")
        self.curves = {k: Curve.from_dict(v) for k, v in sim["curves"].items()}
        self.learning_days: dict[str, int] = dict(sim.get("learning_days", {}))
