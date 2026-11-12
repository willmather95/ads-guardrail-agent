"""DuckDB warehouse layer (TRUTH). Schema follows the marketing-warehouse conventions:
one local DuckDB file, a daily fact table keyed by date and entity, a load-meta table, and
idempotent loads (delete the date range, then insert).

Tables
  ads_daily      platform, account_id, entity_id, entity_name, date, spend, impressions,
                 clicks, conversions, conversion_value, pulled_at
  ads_entities   latest settings snapshot per entity (budget, bid, learning, targets)
  ads_load_meta  per account: min/max date, row count, last load

If the warehouse file has `ads_daily` rows, the agent reads metrics from it instead of
calling the ad APIs. Live entity settings (budgets, bids) still come from the platform,
because the warehouse snapshot can be hours old and writes must start from the live value.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from .connectors.base import AdsReader
from .models import MICROS, DailyMetric, Entity, Platform

DDL = """
CREATE TABLE IF NOT EXISTS ads_daily (
  platform VARCHAR NOT NULL,
  account_id VARCHAR NOT NULL,
  entity_id VARCHAR NOT NULL,
  entity_name VARCHAR,
  date DATE NOT NULL,
  spend DOUBLE NOT NULL,
  impressions BIGINT NOT NULL,
  clicks BIGINT NOT NULL,
  conversions DOUBLE NOT NULL,
  conversion_value DOUBLE NOT NULL,
  pulled_at TIMESTAMP NOT NULL,
  PRIMARY KEY (platform, account_id, entity_id, date)
);
CREATE TABLE IF NOT EXISTS ads_entities (
  platform VARCHAR NOT NULL,
  account_id VARCHAR NOT NULL,
  entity_id VARCHAR NOT NULL,
  name VARCHAR,
  daily_budget DOUBLE NOT NULL,
  bid DOUBLE,
  learning BOOLEAN NOT NULL,
  budget_shared BOOLEAN NOT NULL,
  target_cpa DOUBLE,
  target_roas DOUBLE,
  status VARCHAR,
  ref VARCHAR,
  snapshot_at TIMESTAMP NOT NULL,
  PRIMARY KEY (platform, account_id, entity_id)
);
CREATE TABLE IF NOT EXISTS ads_load_meta (
  platform VARCHAR NOT NULL,
  account_id VARCHAR NOT NULL,
  min_date DATE,
  max_date DATE,
  row_count BIGINT,
  last_pulled_at TIMESTAMP,
  notes VARCHAR,
  PRIMARY KEY (platform, account_id)
);
"""


def _duckdb():
    try:
        import duckdb  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pip install duckdb to use the warehouse") from exc
    return duckdb


def _m(v: float | None) -> int | None:
    return None if v is None else int(round(v * 100)) * 10_000


def connect(path: str | Path, read_only: bool = False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return _duckdb().connect(str(path), read_only=read_only)


def init_schema(path: str | Path) -> None:
    con = connect(path)
    try:
        con.execute(DDL)
    finally:
        con.close()


def load(path: str | Path, reader: AdsReader, start: date, end: date, now: datetime | None = None) -> int:
    """Copy metrics for [start, end] and an entity snapshot from `reader` into the warehouse."""
    now = now or datetime.now(timezone.utc)
    init_schema(path)
    entities = reader.list_entities()
    names = {e.key: e.name for e in entities}
    metrics = reader.fetch_metrics(start, end)
    con = connect(path)
    try:
        con.execute("BEGIN")
        accounts = {(e.platform.value, e.account_id) for e in entities} | {(m.platform.value, m.account_id) for m in metrics}
        for plat, acct in accounts:
            con.execute("DELETE FROM ads_daily WHERE platform=? AND account_id=? AND date BETWEEN ? AND ?",
                        [plat, acct, start, end])
        con.executemany(
            "INSERT INTO ads_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [[m.platform.value, m.account_id, m.entity_id, names.get(m.key), m.day, m.spend_micros / MICROS,
              m.impressions, m.clicks, m.conversions, m.conversion_value_micros / MICROS, now.replace(tzinfo=None)]
             for m in metrics])
        con.execute("DELETE FROM ads_entities")
        con.executemany(
            "INSERT INTO ads_entities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [[e.platform.value, e.account_id, e.entity_id, e.name, e.daily_budget_micros / MICROS,
              None if e.bid_micros is None else e.bid_micros / MICROS, e.learning, e.budget_shared,
              None if e.target_cpa_micros is None else e.target_cpa_micros / MICROS, e.target_roas,
              e.status, e.ref, now.replace(tzinfo=None)] for e in entities])
        for plat, acct in accounts:
            con.execute("DELETE FROM ads_load_meta WHERE platform=? AND account_id=?", [plat, acct])
            con.execute(
                "INSERT INTO ads_load_meta SELECT platform, account_id, min(date), max(date), count(*), ?, ? "
                "FROM ads_daily WHERE platform=? AND account_id=? GROUP BY 1, 2",
                [now.replace(tzinfo=None), "loaded by ads-guardrail-agent", plat, acct])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return len(metrics)


def has_truth(path: str | Path) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    con = connect(p, read_only=True)
    try:
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'ads_daily'").fetchone()[0]
        return bool(exists) and con.execute("SELECT count(*) FROM ads_daily").fetchone()[0] > 0
    finally:
        con.close()


class WarehouseReader:
    """AdsReader backed by the DuckDB file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def list_entities(self) -> list[Entity]:
        con = connect(self.path, read_only=True)
        try:
            rows = con.execute("SELECT platform, account_id, entity_id, name, daily_budget, bid, learning, "
                               "budget_shared, target_cpa, target_roas, status, ref FROM ads_entities "
                               "ORDER BY 1, 2, 3").fetchall()
        finally:
            con.close()
        return [Entity(platform=Platform(r[0]), account_id=r[1], entity_id=r[2], name=r[3] or "",
                       daily_budget_micros=_m(r[4]) or 0, bid_micros=_m(r[5]), learning=bool(r[6]),
                       budget_shared=bool(r[7]), target_cpa_micros=_m(r[8]), target_roas=r[9],
                       status=r[10] or "ENABLED", ref=r[11] or "") for r in rows]

    def fetch_metrics(self, start: date, end: date) -> list[DailyMetric]:
        con = connect(self.path, read_only=True)
        try:
            rows = con.execute("SELECT platform, account_id, entity_id, date, spend, impressions, clicks, "
                               "conversions, conversion_value FROM ads_daily WHERE date BETWEEN ? AND ? "
                               "ORDER BY 1, 2, 3, 4", [start, end]).fetchall()
        finally:
            con.close()
        return [DailyMetric(platform=Platform(r[0]), account_id=r[1], entity_id=r[2], day=r[3],
                            spend_micros=_m(r[4]) or 0, impressions=int(r[5]), clicks=int(r[6]),
                            conversions=float(r[7]), conversion_value_micros=_m(r[8]) or 0) for r in rows]
