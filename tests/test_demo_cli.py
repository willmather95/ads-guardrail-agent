import json
import os
from datetime import date

import pytest

from ads_guardrail import Config, FixtureAdapter, apply, propose
from ads_guardrail.cli import EXIT_HALTED, EXIT_OK, EXIT_REFUSED, main
from ads_guardrail.demo import run_demo
from conftest import NOW

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def summary(tmp_path_factory):
    return run_demo(tmp_path_factory.mktemp("demo") / "out", os.path.join(ROOT, "config", "demo.toml"), quiet=True)


def test_demo_shows_caps_kill_switch_and_exact_rollback(summary):
    t = summary["totals"]
    assert t["proposed_budget_move"] > 5 * t["capped_budget_move"]  # caps held most of the asks back
    assert t["capped_budget_move"] >= t["applied_budget_move"] > 0
    rows = {r["day"]: r for r in summary["days_rows"]}
    assert rows["2026-11-09"]["halted"] and rows["2026-11-09"]["applied"] == 1
    assert not rows["2026-11-10"]["halted"] and rows["2026-11-10"]["applied"] > 0
    assert any("already_complete=True" in n for n in rows["2026-11-10"]["notes"])
    assert rows["2026-11-03"]["proposed_budget_move"] > 5000  # the rogue +$5,000 ask
    assert summary["rollback"]["last_run_exact"] and summary["rollback"]["all_runs_exact"]
    assert summary["rollback"]["conflicts"] == 0
    assert summary["audit"]["ok"] and summary["audit"]["cap_breaches"] == 0
    for r in summary["days_rows"]:
        for acct, cap in summary["caps"]["account_total_caps"].items():
            assert r["total_budget"][acct] <= cap


def test_rogue_request_is_cut_to_per_change_cap(summary, tmp_path):
    out = tmp_path
    run_demo(out, os.path.join(ROOT, "config", "demo.toml"), quiet=True)
    plan = json.loads((out / "plans" / "2026-11-03.json").read_text())
    rogue = next(d for d in plan["decisions"] if d["proposal"]["rule"].startswith("external"))
    assert rogue["proposal"]["proposed_micros"] - rogue["proposal"]["before_micros"] == 5_000_000_000
    assert rogue["verdict"] == "clamped"
    assert rogue["final_delta_micros"] == 50_000_000  # cut to the $50 per-change cap, not blocked
    assert any("per-change cap" in n for n in rogue["notes"])
    rr = next(e for e in summary["events"] if e["kind"] == "rogue_result")
    assert rr["asked"] == 5000 and rr["allowed"] == 50


def test_public_api_for_channel_loop():
    a = FixtureAdapter()
    plan = propose(a, Config(), as_of=date(2026, 11, 1), now=NOW)
    assert plan.proposals and all(d.verdict.value == "blocked" for d in plan.decisions)  # empty allowlist = no writes
    assert apply(plan, a, Config(), audit=None, now=NOW).mode == "dry_run"  # type: ignore[arg-type]


def test_cli_dry_run_refuse_and_kill(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADS_GUARDRAIL_KILL", raising=False)
    demo_cfg = os.path.join(ROOT, "config", "demo.toml")
    ex_cfg = os.path.join(ROOT, "config", "guardrail.example.toml")
    assert main(["--config", ex_cfg, "run", "--as-of", "2026-11-01"]) == EXIT_OK
    assert "DRY RUN" in capsys.readouterr().out
    assert not (tmp_path / ".ads-guardrail" / "audit.jsonl").exists()
    assert main(["--config", ex_cfg, "run", "--as-of", "2026-11-01", "--apply"]) == EXIT_REFUSED
    assert main(["--config", demo_cfg, "kill", "on"]) == EXIT_OK
    assert main(["--config", demo_cfg, "run", "--as-of", "2026-11-01", "--apply"]) == EXIT_HALTED
    assert main(["--config", demo_cfg, "kill", "off"]) == EXIT_OK
    assert main(["--config", demo_cfg, "run", "--as-of", "2026-11-01", "--apply"]) == EXIT_OK
    out = capsys.readouterr().out
    assert '"applied": 5' in out
    assert main(["audit", "verify"]) == EXIT_OK
    run_id = [l for l in out.splitlines() if l.startswith("run-")][-1].split(":")[0]
    assert main(["--config", demo_cfg, "rollback", run_id, "--apply"]) == EXIT_OK
    assert "5 restored" in capsys.readouterr().out
