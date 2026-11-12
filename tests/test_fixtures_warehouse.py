import json
from datetime import date

from ads_guardrail import FixtureAdapter
from ads_guardrail import warehouse
from ads_guardrail.connectors.fixture import FIXTURE_DIR
from ads_guardrail.fixturegen import generate


def test_fixtures_are_labeled_and_sized():
    a = FixtureAdapter()
    assert a.manifest["_synthetic"] is True and "SYNTHETIC" in a.manifest["label"]
    ents = a.list_entities()
    assert len(ents) == 20 and len({e.account_id for e in ents}) == 2
    ms = a.fetch_metrics(date(2000, 1, 1), date(2100, 1, 1))
    assert len({m.day for m in ms}) == 60 and len(ms) == 1200


def test_committed_fixtures_match_generator(tmp_path):
    written = generate(tmp_path)
    for rel in written:
        assert (tmp_path / rel).read_text() == (FIXTURE_DIR / rel).read_text(), rel


def test_warehouse_round_trip_and_idempotent_load(tmp_path):
    a = FixtureAdapter()
    db = tmp_path / "w.duckdb"
    assert not warehouse.has_truth(db)
    n1 = warehouse.load(db, a, date(2026, 9, 2), date(2026, 10, 31))
    n2 = warehouse.load(db, a, date(2026, 10, 25), date(2026, 10, 31))  # reload a slice
    assert n1 == 1200 and n2 == 140 and warehouse.has_truth(db)
    r = warehouse.WarehouseReader(db)
    src = sorted(a.fetch_metrics(date(2026, 9, 2), date(2026, 10, 31)), key=lambda m: (m.key, m.day))
    got = sorted(r.fetch_metrics(date(2026, 9, 2), date(2026, 10, 31)), key=lambda m: (m.key, m.day))
    assert got == src  # exact, no double counting
    assert {e.key: e.daily_budget_micros for e in r.list_entities()} == {e.key: e.daily_budget_micros for e in a.list_entities()}
    con = warehouse.connect(db, read_only=True)
    meta = con.execute("SELECT platform, row_count FROM ads_load_meta ORDER BY 1").fetchall()
    con.close()
    assert meta == [("google_ads", 600), ("meta", 600)]


def test_synthetic_data_never_looks_like_a_phone_number():
    """The publish scrub flags one phone area-code prefix. Keep generated data clear of it."""
    import re
    from pathlib import Path
    pat = re.compile("8" + r"47[-. )]")  # split so this file does not match itself
    files = list(FIXTURE_DIR.rglob("*.json")) + [Path(__file__).resolve().parents[1] / "docs" / "demo-summary.json"]
    assert not [str(p) for p in files if pat.search(p.read_text())]
