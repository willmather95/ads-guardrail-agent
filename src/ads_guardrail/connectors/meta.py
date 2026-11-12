"""Meta Marketing API connector: ad set insights reads, ad set daily_budget / bid_amount writes.

Shapes follow the Graph API: `GET /act_<id>/insights?level=adset&time_increment=1`,
`GET /act_<id>/adsets`, `POST /<adset_id>` with `daily_budget` or `bid_amount` in the
account currency's minor unit (cents for USD). Money strings like "123.45" are converted
to integer micros here.

`MetaAdsAdapter.from_env()` needs META_ACCESS_TOKEN and META_AD_ACCOUNT_ID (act_...).
"""
from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from ..models import ChangeType, DailyMetric, Entity, Platform
from .base import CENT_MICROS, AdapterError
from .http import HttpTransport, UrllibTransport, request_with_retry

API_VERSION = "v23.0"  # bump to the current Graph API version before a live run
BASE_URL = "https://graph.facebook.com"
DEFAULT_CONVERSION_ACTION = "offsite_conversion.fb_pixel_purchase"
BID_STRATEGIES_WITH_AMOUNT = {"COST_CAP", "LOWEST_COST_WITH_BID_CAP"}
INSIGHT_FIELDS = "adset_id,adset_name,spend,impressions,clicks,actions,action_values,date_start,date_stop"
ADSET_FIELDS = "id,name,daily_budget,bid_amount,bid_strategy,effective_status,learning_stage_info"
THROTTLE_PCT = 90


class MetaThrottled(AdapterError):
    def __init__(self, usage: dict[str, Any], wait_minutes: int) -> None:
        super().__init__(f"Meta business use case usage high ({usage}); retry in {wait_minutes} min")
        self.wait_minutes = wait_minutes


def money_to_micros(value: Any) -> int:
    return int(Decimal(str(value or "0")) * 1_000_000)


def cents_to_micros(value: Any) -> int:
    return int(value or 0) * CENT_MICROS


def micros_to_cents(micros: int) -> int:
    if micros % CENT_MICROS:
        raise AdapterError("Meta budgets must be whole cents")
    return micros // CENT_MICROS


def _action_sum(items: list[dict[str, Any]] | None, action_types: set[str]) -> Decimal:
    return sum((Decimal(str(a.get("value", "0"))) for a in items or [] if a.get("action_type") in action_types), Decimal(0))


def parse_adsets(account_id: str, pages: list[dict[str, Any]], targets: dict[str, Any] | None = None) -> list[Entity]:
    targets = targets or {}
    out = []
    for page in pages:
        for a in page.get("data", []):
            if "daily_budget" not in a:
                continue  # lifetime or campaign-level (CBO) budget: not editable at ad set level
            strategy = a.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP")
            bid = cents_to_micros(a["bid_amount"]) if strategy in BID_STRATEGIES_WITH_AMOUNT and a.get("bid_amount") else None
            t = targets.get(str(a["id"]), {})
            out.append(Entity(
                platform=Platform.META,
                account_id=account_id,
                entity_id=str(a["id"]),
                name=a.get("name", ""),
                daily_budget_micros=cents_to_micros(a["daily_budget"]),
                bid_micros=bid,
                learning=(a.get("learning_stage_info") or {}).get("status") == "LEARNING",
                target_cpa_micros=t.get("target_cpa_micros"),
                target_roas=t.get("target_roas"),
                status=a.get("effective_status", "ACTIVE"),
                ref=strategy,
            ))
    return out


def parse_insights(account_id: str, pages: list[dict[str, Any]], action_types: set[str]) -> list[DailyMetric]:
    out = []
    for page in pages:
        for r in page.get("data", []):
            out.append(DailyMetric(
                platform=Platform.META,
                account_id=account_id,
                entity_id=str(r["adset_id"]),
                day=date.fromisoformat(r["date_start"]),
                spend_micros=money_to_micros(r.get("spend")),
                impressions=int(r.get("impressions", 0)),
                clicks=int(r.get("clicks", 0)),
                conversions=float(_action_sum(r.get("actions"), action_types)),
                conversion_value_micros=int(_action_sum(r.get("action_values"), action_types) * 1_000_000),
            ))
    return out


def check_usage(headers: dict[str, str]) -> None:
    raw = headers.get("x-business-use-case-usage") or headers.get("X-Business-Use-Case-Usage")
    if not raw:
        return
    usage = json.loads(raw)
    for entries in usage.values():
        for e in entries:
            peak = max(int(e.get(k, 0)) for k in ("call_count", "total_cputime", "total_time"))
            if peak >= THROTTLE_PCT:
                raise MetaThrottled(e, int(e.get("estimated_time_to_regain_access", 0)))


class MetaAdsAdapter:
    def __init__(self, account_id: str, access_token: str, transport: HttpTransport | None = None,
                 conversion_action_types: set[str] | None = None, targets: dict[str, Any] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.account_id = account_id if account_id.startswith("act_") else f"act_{account_id}"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self.transport = transport or UrllibTransport()
        self.action_types = conversion_action_types or {DEFAULT_CONVERSION_ACTION}
        self.targets = targets or {}
        self._sleep = sleep
        self._entities: dict[str, Entity] = {}

    @classmethod
    def from_env(cls, transport: HttpTransport | None = None) -> "MetaAdsAdapter":
        missing = [k for k in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID") if not os.environ.get(k)]
        if missing:
            raise AdapterError(f"missing env: {', '.join(missing)}")
        return cls(os.environ["META_AD_ACCOUNT_ID"], os.environ["META_ACCESS_TOKEN"], transport=transport)

    def _call(self, method: str, url: str, params: dict[str, str], retry: bool = True) -> Any:
        kw: dict[str, Any] = {"headers": self._headers, "params": params}
        if self._sleep:
            kw["sleep"] = self._sleep
        resp = request_with_retry(self.transport, method, url, max_attempts=4 if retry else 1, **kw)
        check_usage(resp.headers)
        if resp.status != 200:
            raise AdapterError(f"Meta {method} {url} -> HTTP {resp.status}: {resp.body}")
        return resp.body

    def _paged(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        pages = []
        url, p = f"{BASE_URL}/{API_VERSION}/{path}", dict(params)
        for _ in range(200):  # hard stop on runaway pagination
            body = self._call("GET", url, p)
            pages.append(body)
            nxt = (body.get("paging") or {}).get("next")
            if not nxt:
                return pages
            url, p = nxt, {}
        raise AdapterError("pagination did not terminate")

    def list_entities(self) -> list[Entity]:
        pages = self._paged(f"{self.account_id}/adsets", {"fields": ADSET_FIELDS, "limit": "200",
                                                          "effective_status": json.dumps(["ACTIVE"])})
        ents = parse_adsets(self.account_id, pages, self.targets)
        self._entities = {e.key: e for e in ents}
        return ents

    def fetch_metrics(self, start: date, end: date) -> list[DailyMetric]:
        params = {"level": "adset", "fields": INSIGHT_FIELDS, "time_increment": "1", "limit": "500",
                  "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()})}
        return parse_insights(self.account_id, self._paged(f"{self.account_id}/insights", params), self.action_types)

    def _entity(self, key: str) -> Entity:
        if key not in self._entities:
            self.list_entities()
        if key not in self._entities:
            raise AdapterError(f"unknown entity {key}")
        return self._entities[key]

    def read_value(self, entity_key: str, change_type: ChangeType) -> int:
        e = self._entity(entity_key)
        body = self._call("GET", f"{BASE_URL}/{API_VERSION}/{e.entity_id}", {"fields": ADSET_FIELDS})
        if change_type is ChangeType.BUDGET:
            return cents_to_micros(body["daily_budget"])
        if body.get("bid_strategy") not in BID_STRATEGIES_WITH_AMOUNT:
            raise AdapterError(f"{entity_key} has no bid_amount to edit")
        return cents_to_micros(body["bid_amount"])

    def write_value(self, entity_key: str, change_type: ChangeType, new_micros: int) -> int:
        e = self._entity(entity_key)
        field = "daily_budget" if change_type is ChangeType.BUDGET else "bid_amount"
        self._call("POST", f"{BASE_URL}/{API_VERSION}/{e.entity_id}", {field: str(micros_to_cents(new_micros))}, retry=False)
        return self.read_value(entity_key, change_type)

    def quantum_micros(self, entity_key: str, change_type: ChangeType) -> int:
        return CENT_MICROS
