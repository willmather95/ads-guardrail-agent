"""`make demo`: a SIMULATED 14-day run on the SYNTHETIC fixture accounts.

Each morning (06:00 UTC) the agent reads yesterday's numbers from the DuckDB warehouse,
proposes changes, and applies them under the caps to an in-memory fake account. Then the
simulator plays out the day with the new budgets. Along the way it shows:

* day 3: another agent asks for +$5,000/day on one campaign; the per-change cap cuts it
* day 9: an operator engages the kill switch after the first write; the rest of the run halts
* day 10: the switch is released; a re-run of an already finished run is a no-op
* end:  roll back day 14 alone, then every run newest first, and prove each restore is exact

Nothing here touches a real ad platform.
"""
from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from . import warehouse
from .audit import AuditLog
from .config import Config, load_config
from .connectors.fixture import FixtureAdapter
from .executor import apply, external_proposal, propose, rollback
from .killswitch import KillSwitch
from .models import MICROS, ChangeType, Plan, Verdict
from .simulator import MarketSimulator

START = date(2026, 11, 1)
DAYS = 14
KILL_DAY = 9
ROGUE_DAY = 3
REPO_ROOT = Path(__file__).resolve().parents[2]


class TripwireAccount:
    """Wraps the fake account. After N writes on a chosen day it engages the kill switch,
    the way an operator would `touch KILL_SWITCH` mid-run."""

    def __init__(self, inner: FixtureAdapter, ks: KillSwitch) -> None:
        self.inner, self.ks = inner, ks
        self.trip_after: int | None = None
        self._count = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def write_value(self, key, change_type, new_micros):
        out = self.inner.write_value(key, change_type, new_micros)
        self._count += 1
        if self.trip_after is not None and self._count >= self.trip_after:
            self.ks.engage("operator: pause all writes (demo)")
            self.trip_after = None
        return out

    def arm(self, after_writes: int) -> None:
        self._count, self.trip_after = 0, after_writes


@dataclass
class DayRow:
    day: str
    run_id: str
    proposals: int = 0
    approved: int = 0
    clamped: int = 0
    blocked: int = 0
    applied: int = 0
    halted: bool = False
    proposed_budget_move: float = 0.0  # sum |proposed budget change|, $/day
    capped_budget_move: float = 0.0  # sum |change the caps allow|
    applied_budget_move: float = 0.0  # sum |change actually written|
    proposed_bid_move: float = 0.0
    applied_bid_move: float = 0.0
    total_budget: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _dollars(m: int) -> float:
    return round(m / MICROS, 2)


def _tally(row: DayRow, plan: Plan, decisions, applied_recs) -> None:
    row.proposals = len(plan.proposals)
    for d in decisions:
        p = d.proposal
        if p.change_type is ChangeType.BUDGET:
            row.proposed_budget_move += abs(p.delta_micros) / MICROS
            if d.writable:
                row.capped_budget_move += abs(d.final_delta_micros) / MICROS
        else:
            row.proposed_bid_move += abs(p.delta_micros) / MICROS
        row.approved += d.verdict is Verdict.APPROVED
        row.clamped += d.verdict is Verdict.CLAMPED
        row.blocked += d.verdict is Verdict.BLOCKED
    for r in applied_recs:
        mv = abs(r["after_micros"] - r["before_micros"]) / MICROS
        if r["change_type"] == "budget":
            row.applied_budget_move += mv
        else:
            row.applied_bid_move += mv
    row.applied = len(applied_recs)
    for k in ("proposed_budget_move", "capped_budget_move", "applied_budget_move", "proposed_bid_move", "applied_bid_move"):
        setattr(row, k, round(getattr(row, k), 2))


def run_demo(out_dir: str | Path = "demo/out", config_path: str | Path | None = None, quiet: bool = False) -> dict[str, Any]:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    (out / "plans").mkdir(parents=True)
    say = (lambda *a: None) if quiet else print
    config: Config = load_config(config_path or REPO_ROOT / "config" / "demo.toml")
    ks = KillSwitch(file=out / "KILL_SWITCH", env_var=config.kill_switch.env_var, env={})
    audit = AuditLog(out / "audit.jsonl")
    fake = FixtureAdapter()
    account = TripwireAccount(fake, ks)
    sim = MarketSimulator(fake, fake.curves, seed=11, learning_days=fake.learning_days)
    wh = out / "warehouse.duckdb"
    hist_start = date.fromisoformat(fake.manifest["history_start"])
    n = warehouse.load(wh, fake, hist_start, START - timedelta(days=1), now=datetime(2026, 11, 1, 5, tzinfo=timezone.utc))
    reader = warehouse.WarehouseReader(wh)
    initial = fake.snapshot()
    say(f"SIMULATED demo on SYNTHETIC fixtures. Warehouse loaded: {n} rows, {hist_start} to {START - timedelta(days=1)}.")
    say(f"{'day':10} {'props':>5} {'ok':>3} {'clamp':>5} {'block':>5} {'wrote':>5}  budget $ proposed / capped / applied")

    rows: list[DayRow] = []
    snapshots: dict[str, dict] = {}
    run_ids: list[str] = []
    events: list[dict[str, str]] = []
    for i in range(DAYS):
        day = START + timedelta(days=i)
        now = datetime.combine(day, time(6, 0), tzinfo=timezone.utc)
        if i > 0:  # load yesterday's simulated numbers into the warehouse (TRUTH)
            warehouse.load(wh, fake, day - timedelta(days=1), day - timedelta(days=1), now=now)
        entities = fake.list_entities()  # live settings come from the account
        metrics = reader.fetch_metrics(day - timedelta(days=config.policy.lookback_days), day - timedelta(days=1))
        plan = propose(fake, config, as_of=day, audit=audit, now=now, entities=entities, metrics=metrics)
        row = DayRow(day=day.isoformat(), run_id=plan.run_id)
        if i + 1 == ROGUE_DAY:
            target = next(e for e in entities if e.name.startswith("ASC | Full catalog"))
            rogue = external_proposal(target, ChangeType.BUDGET, target.daily_budget_micros + 5_000 * MICROS,
                                      reason="Upstream agent: 'ASC is winning, add $5,000/day'", as_of=day,
                                      config=config, source="upstream-agent", confidence=0.99)
            plan.proposals.append(rogue)
            row.notes.append("rogue +$5,000/day request from another agent")
            events.append({"day": row.day, "kind": "rogue", "text": "Another agent asks for +$5,000/day"})
        if i + 1 == KILL_DAY:
            account.arm(after_writes=1)
            events.append({"day": row.day, "kind": "kill", "text": "Kill switch engaged after 1 write"})
        if i + 1 == KILL_DAY + 1:
            ks.release()
            first = run_ids[0]
            again = apply(Plan(first, START, [], [], []), account, config, audit, apply=True, now=now, kill_switch=ks)
            row.notes.append(f"kill switch released; re-run of {first}: already_complete={again.already_complete}")
            events.append({"day": row.day, "kind": "release", "text": "Released; finished runs re-run as no-ops"})
        snapshots[plan.run_id] = fake.snapshot()
        res = apply(plan, account, config, audit, apply=True, now=now, kill_switch=ks)
        if res.decisions:
            plan.decisions = res.decisions  # what the guardrails decided at write time
        _tally(row, plan, res.decisions, res.applied)
        for d in res.decisions:
            if d.proposal.rule.startswith("external"):
                events.append({"day": row.day, "kind": "rogue_result", "verdict": d.verdict.value,
                               "asked": _dollars(d.proposal.delta_micros), "allowed": _dollars(d.final_delta_micros),
                               "notes": list(d.notes)})
        if res.halted:
            row.halted = True
            row.notes.append(f"halted: {res.halt_reason}")
        (out / "plans" / f"{row.day}.json").write_text(json.dumps(
            {**plan.to_dict(), "result": res.summary(), "applied": res.applied}, indent=1, default=str))
        totals: dict[str, int] = {}
        for e in fake.list_entities():
            totals[e.account_id] = totals.get(e.account_id, 0) + e.daily_budget_micros
        row.total_budget = {k: _dollars(v) for k, v in totals.items()}
        rows.append(row)
        run_ids.append(plan.run_id)
        say(f"{row.day:10} {row.proposals:5} {row.approved:3} {row.clamped:5} {row.blocked:5} {row.applied:5}  "
            f"{row.proposed_budget_move:9,.2f} / {row.capped_budget_move:8,.2f} / {row.applied_budget_move:8,.2f}"
            + ("   KILL SWITCH: halted" if row.halted else ""))
        sim.step(day)  # the day plays out with the new settings

    # ---- rollback: last run alone, then everything newest first ----
    end_state = fake.snapshot()
    last = run_ids[-1]
    rb = rollback(last, account, config, audit, apply=True, now=datetime(2026, 11, 15, 6, tzinfo=timezone.utc), kill_switch=ks)
    exact_last = fake.snapshot() == snapshots[last]
    say(f"rollback {last}: restored {len(rb.restored)} changes; state equals pre-run snapshot: {exact_last}")
    restored_all = len(rb.restored)
    conflicts = len(rb.conflicts)
    for rid in reversed(run_ids[:-1]):
        r = rollback(rid, account, config, audit, apply=True, now=datetime(2026, 11, 15, 6, 5, tzinfo=timezone.utc), kill_switch=ks)
        restored_all += len(r.restored)
        conflicts += len(r.conflicts)
    exact_all = fake.snapshot() == initial
    say(f"rollback all {len(run_ids)} runs: restored {restored_all} changes, {conflicts} conflicts; "
        f"every budget and bid equals the fixture start: {exact_all}")
    ok, msg = audit.verify()
    say(f"audit chain: {msg}")

    # ---- caps never exceeded (checked from the audit log, independently of the engine) ----
    breaches = audit_breaches(audit, config)
    say(f"cap breaches found by independent audit replay: {len(breaches)}")

    totals_row = {
        "proposed_budget_move": round(sum(r.proposed_budget_move for r in rows), 2),
        "capped_budget_move": round(sum(r.capped_budget_move for r in rows), 2),
        "applied_budget_move": round(sum(r.applied_budget_move for r in rows), 2),
        "proposals": sum(r.proposals for r in rows),
        "approved": sum(r.approved for r in rows),
        "clamped": sum(r.clamped for r in rows),
        "blocked": sum(r.blocked for r in rows),
        "applied": sum(r.applied for r in rows),
    }
    summary = {
        "_label": "SIMULATED 14-day run on SYNTHETIC fixtures. Fake in-memory ad account. Not real results.",
        "start": START.isoformat(), "days": DAYS, "config": str(Path(config_path or 'config/demo.toml').name),
        "config_fingerprint": config.fingerprint(),
        "caps": {
            "budget_per_change": f"{config.guardrails.budget.max_change_bp / 100:g}% or ${config.guardrails.budget.max_change_abs_micros / MICROS:,.0f}",
            "bid_per_change": f"{config.guardrails.bid.max_change_bp / 100:g}% or ${config.guardrails.bid.max_change_abs_micros / MICROS:,.0f}",
            "account_daily_change_cap": _dollars(config.guardrails.account_daily_change_cap_micros),
            "account_total_caps": {k: _dollars(v) for k, v in config.guardrails.account_total_budget_cap_micros.items()},
            "cooldown_hours": config.guardrails.cooldown_hours,
            "max_changes_per_run": config.guardrails.max_changes_per_run,
        },
        "totals": totals_row,
        "events": events,
        "days_rows": [r.__dict__ for r in rows],
        "rollback": {"last_run": last, "last_run_restored": len(rb.restored), "last_run_exact": exact_last,
                     "all_runs_restored": restored_all, "conflicts": conflicts, "all_runs_exact": exact_all},
        "audit": {"ok": ok, "message": msg, "cap_breaches": len(breaches)},
        "end_state_changed_entities": sum(1 for k in end_state if end_state[k] != initial[k]),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    with (out / "timeline.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "proposals", "approved", "clamped", "blocked", "applied", "halted",
                    "proposed_budget_move", "capped_budget_move", "applied_budget_move", "notes"])
        for r in rows:
            w.writerow([r.day, r.proposals, r.approved, r.clamped, r.blocked, r.applied, r.halted,
                        r.proposed_budget_move, r.capped_budget_move, r.applied_budget_move, "; ".join(r.notes)])
    say(f"wrote {out}/summary.json, timeline.csv, audit.jsonl, plans/, warehouse.duckdb")
    return summary


def audit_breaches(audit: AuditLog, config: Config) -> list[str]:
    """Replay the audit log and re-check every applied write against the caps, from scratch."""
    g = config.guardrails
    out: list[str] = []
    movement: dict[tuple[str, str], int] = {}
    last_at: dict[str, datetime] = {}
    from fnmatch import fnmatchcase  # noqa: PLC0415

    for r in audit.records():
        if r["event"] == "rolled_back":
            last_at[r["entity_key"]] = datetime.fromisoformat(r["at"])
        if r["event"] != "applied":
            continue
        caps = g.budget if r["change_type"] == "budget" else g.bid
        before, after = r["before_micros"], r["after_micros"]
        lim = min(before * caps.max_change_bp // 10_000, caps.max_change_abs_micros)
        if abs(after - before) > lim:
            out.append(f"seq {r['seq']}: per-change cap")
        if after < before and after < caps.floor_micros:
            out.append(f"seq {r['seq']}: floor")
        if not any(fnmatchcase(r["entity_key"], p) for p in g.allowlist):
            out.append(f"seq {r['seq']}: allowlist")
        at = datetime.fromisoformat(r["at"])
        prev = last_at.get(r["entity_key"])
        if prev is not None and at - prev < timedelta(hours=g.cooldown_hours):
            out.append(f"seq {r['seq']}: cooldown")
        last_at[r["entity_key"]] = at
        if r["change_type"] == "budget":
            k = (r["account_id"], r["at"][:10])
            movement[k] = movement.get(k, 0) + abs(after - before)
            if movement[k] > g.account_daily_change_cap_micros:
                out.append(f"seq {r['seq']}: account daily cap")
    return out


if __name__ == "__main__":
    run_demo()
