"""Real connector shapes, exercised against a fake transport. No network."""
from __future__ import annotations

import json
from datetime import date

import pytest

from ads_guardrail.connectors import google_ads, meta
from ads_guardrail.connectors.base import AdapterError, RoutedAdapter
from ads_guardrail.connectors.fixture import FIXTURE_DIR
from ads_guardrail.connectors.http import HttpResponse
from ads_guardrail.models import ChangeType, Platform


class FakeTransport:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, *, headers=None, params=None, json_body=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "params": params or {}, "json": json_body})
        return self.handler(method, url, params or {}, json_body)


def load(rel):
    return json.loads((FIXTURE_DIR / rel).read_text())


# ---------------- Google Ads ----------------

def google_server(state):
    entities = load("google_ads/campaigns.searchStream.json")
    metrics = load("google_ads/metrics.searchStream.json")

    def handler(method, url, params, body):
        if url.endswith("googleAds:searchStream"):
            if "segments.date" in body["query"]:
                return HttpResponse(200, metrics)
            for row in entities[0]["results"]:  # reflect writes
                rn = row["campaignBudget"]["resourceName"]
                if rn in state:
                    row["campaignBudget"]["amountMicros"] = str(state[rn])
            return HttpResponse(200, entities)
        if url.endswith("campaignBudgets:mutate"):
            op = body["operations"][0]
            state[op["update"]["resourceName"]] = int(op["update"]["amountMicros"])
            return HttpResponse(200, {"results": [{"resourceName": op["update"]["resourceName"]}]})
        if url.endswith("campaigns:mutate"):
            return HttpResponse(200, {"results": [{}]})
        return HttpResponse(404, {})
    return handler


def g_adapter(state=None, handler=None):
    t = FakeTransport(handler or google_server(state if state is not None else {}))
    return google_ads.GoogleAdsAdapter("111-222-3333", "DEV_TOKEN_PLACEHOLDER", "ACCESS_TOKEN_PLACEHOLDER",
                                       login_customer_id="999-000-1111", transport=t, sleep=lambda s: None), t


def test_gaql_queries():
    q = google_ads.metrics_query(date(2026, 10, 1), date(2026, 10, 7))
    assert "FROM campaign" in q and "segments.date BETWEEN '2026-10-01' AND '2026-10-07'" in q
    assert "metrics.cost_micros" in q
    with pytest.raises(ValueError):
        google_ads.metrics_query(date(2026, 10, 7), date(2026, 10, 1))


def test_google_reads_parse_and_headers():
    a, t = g_adapter()
    ents = a.list_entities()
    assert len(ents) == 10 and all(e.platform is Platform.GOOGLE_ADS for e in ents)
    call = t.calls[0]
    assert call["url"] == f"https://googleads.googleapis.com/{google_ads.API_VERSION}/customers/1112223333/googleAds:searchStream"
    assert call["headers"]["developer-token"] == "DEV_TOKEN_PLACEHOLDER"
    assert call["headers"]["login-customer-id"] == "9990001111"
    shared = [e for e in ents if e.budget_shared]
    learning = [e for e in ents if e.learning]
    assert len(shared) == 1 and len(learning) == 1
    brand = next(e for e in ents if e.name == "Search | Brand")
    assert brand.bid_micros == 25_000_000  # targetCpa.targetCpaMicros
    ms = a.fetch_metrics(date(2026, 9, 2), date(2026, 10, 31))
    assert len(ms) == 600 and all(isinstance(m.spend_micros, int) for m in ms)


def test_google_budget_write_shape_and_readback():
    state = {}
    a, t = g_adapter(state)
    e = next(x for x in a.list_entities() if x.name == "PMax | All Services")
    after = a.write_value(e.key, ChangeType.BUDGET, 450_000_000)
    assert after == 450_000_000
    mut = next(c for c in t.calls if c["url"].endswith("campaignBudgets:mutate"))
    assert mut["json"] == {"operations": [{"updateMask": "amount_micros", "update": {
        "resourceName": e.ref.split("|")[0], "amountMicros": "450000000"}}]}


def test_google_tcpa_body_and_refusals():
    body = google_ads.tcpa_mutate_body("1", "2", "MAXIMIZE_CONVERSIONS", 80_000_000)
    op = body["operations"][0]
    assert op["updateMask"] == "maximize_conversions.target_cpa_micros"
    assert op["update"]["maximizeConversions"]["targetCpaMicros"] == "80000000"
    with pytest.raises(AdapterError):
        google_ads.tcpa_mutate_body("1", "2", "MAXIMIZE_CLICKS", 1)
    with pytest.raises(AdapterError):
        google_ads.budget_mutate_body("x", 1_234_567)  # not a whole cent
    a, _ = g_adapter()
    shared = next(e for e in a.list_entities() if e.budget_shared)
    with pytest.raises(AdapterError, match="shared budget"):
        a.write_value(shared.key, ChangeType.BUDGET, 110_000_000)


def test_google_reads_retry_but_mutates_do_not():
    n = {"read": 0, "write": 0}

    def flaky(method, url, params, body):
        if url.endswith("searchStream"):
            n["read"] += 1
            return HttpResponse(503, {}) if n["read"] < 3 else HttpResponse(200, load("google_ads/campaigns.searchStream.json"))
        n["write"] += 1
        return HttpResponse(503, {})
    a, _ = g_adapter(handler=flaky)
    ents = a.list_entities()
    assert n["read"] == 3
    with pytest.raises(AdapterError):
        a.write_value(ents[1].key, ChangeType.BUDGET, 300_000_000)
    assert n["write"] == 1


def test_google_from_env_needs_credentials(monkeypatch):
    for k in ("GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_ACCESS_TOKEN", "GOOGLE_ADS_CUSTOMER_ID"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(AdapterError, match="missing env"):
        google_ads.GoogleAdsAdapter.from_env()


# ---------------- Meta ----------------

def meta_server(state, usage=None):
    adsets = load("meta/adsets.pages.json")[0]["data"]
    pages = load("meta/insights.pages.json")

    def handler(method, url, params, body):
        hdr = {"x-business-use-case-usage": json.dumps(usage)} if usage else {}
        if url.endswith("/adsets"):
            return HttpResponse(200, {"data": adsets, "paging": {}}, hdr)
        if "/insights" in url:
            idx = 1 if "after=" in url else 0
            return HttpResponse(200, pages[idx], hdr)
        aid = url.rsplit("/", 1)[1]
        a = next(x for x in adsets if x["id"] == aid)
        if method == "POST":
            for k, v in params.items():
                a[k] = v
            state.append((aid, dict(params)))
            return HttpResponse(200, {"success": True}, hdr)
        return HttpResponse(200, a, hdr)
    return handler


def m_adapter(state=None, usage=None):
    t = FakeTransport(meta_server(state if state is not None else [], usage))
    return meta.MetaAdsAdapter("900000000000001", "TOKEN_PLACEHOLDER", transport=t, sleep=lambda s: None), t


def test_meta_insights_paginate_and_parse():
    a, t = m_adapter()
    ms = a.fetch_metrics(date(2026, 9, 2), date(2026, 10, 31))
    assert len(ms) == 600
    first = t.calls[0]
    assert first["params"]["level"] == "adset" and first["params"]["time_increment"] == "1"
    assert json.loads(first["params"]["time_range"]) == {"since": "2026-09-02", "until": "2026-10-31"}
    assert len([c for c in t.calls if "insights" in c["url"]]) == 2  # followed paging.next
    assert first["headers"]["Authorization"].startswith("Bearer ")
    assert sum(m.conversions for m in ms) > 0 and sum(m.conversion_value_micros for m in ms) > 0


def test_meta_actions_only_count_conversion_type():
    page = [{"data": [{"adset_id": "1", "date_start": "2026-10-01", "spend": "10.50", "impressions": "100", "clicks": "5",
                       "actions": [{"action_type": "link_click", "value": "5"},
                                   {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "2"}],
                       "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "120.00"}]}]}]
    m = meta.parse_insights("act_1", page, {"offsite_conversion.fb_pixel_purchase"})[0]
    assert m.spend_micros == 10_500_000 and m.conversions == 2 and m.conversion_value_micros == 120_000_000


def test_meta_budget_write_in_cents():
    writes = []
    a, _ = m_adapter(writes)
    e = next(x for x in a.list_entities() if x.name.startswith("Prospecting | Broad"))
    assert e.daily_budget_micros == 300_000_000
    assert a.write_value(e.key, ChangeType.BUDGET, 360_000_000) == 360_000_000
    assert writes == [(e.entity_id, {"daily_budget": "36000"})]
    cc = next(x for x in a.list_entities() if x.ref == "COST_CAP")
    a.write_value(cc.key, ChangeType.BID, 23_000_000)
    assert writes[-1] == (cc.entity_id, {"bid_amount": "2300"})
    with pytest.raises(AdapterError):
        meta.micros_to_cents(1)


def test_meta_throttle_header_stops_calls():
    usage = {"900000000000001": [{"type": "ADS_MANAGEMENT", "call_count": 95, "total_cputime": 10,
                                  "total_time": 10, "estimated_time_to_regain_access": 7}]}
    a, _ = m_adapter(usage=usage)
    with pytest.raises(meta.MetaThrottled) as exc:
        a.list_entities()
    assert exc.value.wait_minutes == 7


def test_routed_adapter_sends_writes_to_the_right_platform():
    gstate, mwrites = {}, []
    g, _ = g_adapter(gstate)
    m, _ = m_adapter(mwrites)
    r = RoutedAdapter(g, m)
    ents = r.list_entities()
    assert len(ents) == 20
    me = next(e for e in ents if e.platform is Platform.META)
    r.write_value(me.key, ChangeType.BUDGET, me.daily_budget_micros + 10_000)
    assert mwrites and not gstate
