"""Google Ads connector: GAQL reads via searchStream, budget and tCPA writes via mutate.

Uses the Google Ads REST interface (same resources and field names as the official
`google-ads` Python client). Responses are parsed from their JSON form (camelCase, int64
as strings), which is also the shape of the recorded fixtures in `fixtures/`, so the demo
and the tests run the exact parser a live run would.

Nothing here runs without credentials. `GoogleAdsAdapter.from_env()` needs:
GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_ACCESS_TOKEN (OAuth, refreshed outside this repo),
GOOGLE_ADS_CUSTOMER_ID and optionally GOOGLE_ADS_LOGIN_CUSTOMER_ID.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Iterable

from ..models import ChangeType, DailyMetric, Entity, Platform
from .base import CENT_MICROS, AdapterError
from .http import HttpTransport, UrllibTransport, request_with_retry

API_VERSION = "v21"  # bump to the current supported version before a live run
BASE_URL = "https://googleads.googleapis.com"

# Bidding strategies whose target CPA we know how to edit, and where the field lives.
TCPA_FIELDS = {
    "TARGET_CPA": ("targetCpa", "target_cpa.target_cpa_micros"),
    "MAXIMIZE_CONVERSIONS": ("maximizeConversions", "maximize_conversions.target_cpa_micros"),
}
LEARNING_STATUSES = {"LEARNING_NEW", "LEARNING_SETTING_CHANGE", "LEARNING_BUDGET_CHANGE",
                     "LEARNING_COMPOSITION_CHANGE", "LEARNING_CONVERSION_TYPE_CHANGE",
                     "LEARNING_CONVERSION_SETTING_CHANGE"}


def entity_query() -> str:
    return (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.bidding_strategy_type, "
        "campaign.bidding_strategy_system_status, campaign.target_cpa.target_cpa_micros, "
        "campaign.maximize_conversions.target_cpa_micros, campaign.maximize_conversion_value.target_roas, "
        "campaign_budget.resource_name, campaign_budget.amount_micros, campaign_budget.explicitly_shared "
        "FROM campaign WHERE campaign.status = 'ENABLED'"
    )


def metrics_query(start: date, end: date) -> str:
    if end < start:
        raise ValueError("end before start")
    return (
        "SELECT campaign.id, segments.date, metrics.cost_micros, metrics.impressions, "
        "metrics.clicks, metrics.conversions, metrics.conversions_value "
        "FROM campaign WHERE campaign.status = 'ENABLED' "
        f"AND segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )


def _i(v: Any) -> int:
    return int(v) if v not in (None, "") else 0


def _iter_rows(stream: Any) -> Iterable[dict[str, Any]]:
    """searchStream returns a JSON array of batches, each with `results`."""
    batches = stream if isinstance(stream, list) else [stream]
    for batch in batches:
        yield from batch.get("results", [])


def parse_entities(customer_id: str, stream: Any, targets: dict[str, Any] | None = None) -> list[Entity]:
    targets = targets or {}
    out = []
    for row in _iter_rows(stream):
        c, b = row["campaign"], row.get("campaignBudget", {})
        strategy = c.get("biddingStrategyType", "")
        bid = None
        if strategy in TCPA_FIELDS:
            bid = _i(c.get(TCPA_FIELDS[strategy][0], {}).get("targetCpaMicros")) or None
        t = targets.get(str(c["id"]), {})
        out.append(Entity(
            platform=Platform.GOOGLE_ADS,
            account_id=customer_id,
            entity_id=str(c["id"]),
            name=c.get("name", ""),
            daily_budget_micros=_i(b.get("amountMicros")),
            bid_micros=bid,
            learning=c.get("biddingStrategySystemStatus", "") in LEARNING_STATUSES,
            budget_shared=bool(b.get("explicitlyShared", False)),
            target_cpa_micros=t.get("target_cpa_micros") or bid,
            target_roas=t.get("target_roas") or c.get("maximizeConversionValue", {}).get("targetRoas"),
            status=c.get("status", "ENABLED"),
            ref=f"{b.get('resourceName', '')}|{strategy}",
        ))
    return out


def parse_metrics(customer_id: str, stream: Any) -> list[DailyMetric]:
    out = []
    for row in _iter_rows(stream):
        m = row.get("metrics", {})
        out.append(DailyMetric(
            platform=Platform.GOOGLE_ADS,
            account_id=customer_id,
            entity_id=str(row["campaign"]["id"]),
            day=date.fromisoformat(row["segments"]["date"]),
            spend_micros=_i(m.get("costMicros")),
            impressions=_i(m.get("impressions")),
            clicks=_i(m.get("clicks")),
            conversions=float(m.get("conversions", 0) or 0),
            conversion_value_micros=int(round(float(m.get("conversionsValue", 0) or 0) * 1_000_000)),
        ))
    return out


def budget_mutate_body(budget_resource: str, new_micros: int) -> dict[str, Any]:
    if new_micros % CENT_MICROS:
        raise AdapterError("Google budgets must be a multiple of 10,000 micros")
    return {"operations": [{"updateMask": "amount_micros",
                            "update": {"resourceName": budget_resource, "amountMicros": str(new_micros)}}]}


def tcpa_mutate_body(customer_id: str, campaign_id: str, strategy: str, new_micros: int) -> dict[str, Any]:
    if strategy not in TCPA_FIELDS:
        raise AdapterError(f"bidding strategy {strategy!r} has no editable target CPA")
    obj, mask = TCPA_FIELDS[strategy]
    return {"operations": [{"updateMask": mask, "update": {
        "resourceName": f"customers/{customer_id}/campaigns/{campaign_id}",
        obj: {"targetCpaMicros": str(new_micros)}}}]}


class GoogleAdsAdapter:
    def __init__(self, customer_id: str, developer_token: str, access_token: str,
                 login_customer_id: str | None = None, transport: HttpTransport | None = None,
                 targets: dict[str, Any] | None = None, sleep=None) -> None:
        self.customer_id = customer_id.replace("-", "")
        self._headers = {"developer-token": developer_token, "Authorization": f"Bearer {access_token}"}
        if login_customer_id:
            self._headers["login-customer-id"] = login_customer_id.replace("-", "")
        self.transport = transport or UrllibTransport()
        self.targets = targets or {}
        self._sleep = sleep
        self._entities: dict[str, Entity] = {}

    @classmethod
    def from_env(cls, transport: HttpTransport | None = None) -> "GoogleAdsAdapter":
        need = ["GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_ACCESS_TOKEN", "GOOGLE_ADS_CUSTOMER_ID"]
        missing = [k for k in need if not os.environ.get(k)]
        if missing:
            raise AdapterError(f"missing env: {', '.join(missing)}")
        return cls(os.environ["GOOGLE_ADS_CUSTOMER_ID"], os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
                   os.environ["GOOGLE_ADS_ACCESS_TOKEN"], os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
                   transport=transport)

    def _url(self, path: str) -> str:
        return f"{BASE_URL}/{API_VERSION}/customers/{self.customer_id}/{path}"

    def _post(self, path: str, body: dict[str, Any], retry: bool = True) -> Any:
        kw = {"headers": self._headers, "json_body": body}
        if self._sleep:
            kw["sleep"] = self._sleep
        resp = request_with_retry(self.transport, "POST", self._url(path), max_attempts=4 if retry else 1, **kw)
        if resp.status != 200:
            raise AdapterError(f"Google Ads {path} -> HTTP {resp.status}: {resp.body}")
        return resp.body

    def search_stream(self, query: str) -> Any:
        return self._post("googleAds:searchStream", {"query": query})

    def list_entities(self) -> list[Entity]:
        ents = parse_entities(self.customer_id, self.search_stream(entity_query()), self.targets)
        self._entities = {e.key: e for e in ents}
        return ents

    def fetch_metrics(self, start: date, end: date) -> list[DailyMetric]:
        return parse_metrics(self.customer_id, self.search_stream(metrics_query(start, end)))

    def _entity(self, key: str) -> Entity:
        if key not in self._entities:
            self.list_entities()
        if key not in self._entities:
            raise AdapterError(f"unknown entity {key}")
        return self._entities[key]

    def read_value(self, entity_key: str, change_type: ChangeType) -> int:
        self._entities.pop(entity_key, None)  # force a fresh read
        e = self._entity(entity_key)
        if change_type is ChangeType.BUDGET:
            return e.daily_budget_micros
        if e.bid_micros is None:
            raise AdapterError(f"{entity_key} has no editable target CPA")
        return e.bid_micros

    def write_value(self, entity_key: str, change_type: ChangeType, new_micros: int) -> int:
        e = self._entity(entity_key)
        budget_resource, strategy = e.ref.split("|", 1)
        if change_type is ChangeType.BUDGET:
            if e.budget_shared:
                raise AdapterError("refusing to edit a shared budget (it moves other campaigns too)")
            # Mutates are not retried: a timeout after success must not be applied twice blindly.
            self._post("campaignBudgets:mutate", budget_mutate_body(budget_resource, new_micros), retry=False)
        else:
            self._post("campaigns:mutate", tcpa_mutate_body(self.customer_id, e.entity_id, strategy, new_micros), retry=False)
        return self.read_value(entity_key, change_type)

    def quantum_micros(self, entity_key: str, change_type: ChangeType) -> int:
        return CENT_MICROS
