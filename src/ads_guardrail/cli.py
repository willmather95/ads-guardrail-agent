"""Command line: `ads-guardrail <command>` (or `python -m ads_guardrail`).

  demo                       SIMULATED 14-day run on SYNTHETIC fixtures (no credentials)
  run    [--apply]           propose + apply one day. Dry run unless --apply AND config allows
  tick   [--apply]           the scheduler entrypoint (HOME): `run` for today, exit codes for cron
  rollback RUN_ID [--apply]  reverse every live change from one run
  audit verify | show        check the hash chain / print recent lines
  kill on | off | status     the kill switch file
  warehouse load             copy fixture (or live) metrics into the DuckDB warehouse (TRUTH)
  fixtures                   regenerate the synthetic fixtures
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import warehouse
from .audit import AuditLog
from .config import Config, load_config
from .connectors.base import AdsAdapter, RoutedAdapter
from .connectors.fixture import FixtureAdapter
from .executor import WritesNotEnabled, apply, propose, replay_audit, rollback
from .killswitch import KillSwitch
from .models import ChangeType, Decision, fmt_usd

EXIT_OK, EXIT_ERROR, EXIT_HALTED, EXIT_REFUSED = 0, 1, 3, 4


def _fmt(d: Decision) -> str:
    p = d.proposal
    unit = "budget/day" if p.change_type is ChangeType.BUDGET else "bid"
    head = f"[{d.verdict.value:8}] {p.entity_name[:34]:34} {unit:10} {fmt_usd(p.before_micros):>10} -> "
    head += f"{fmt_usd(p.proposed_micros):>10} wanted, {fmt_usd(d.final_micros):>10} allowed  conf {p.confidence:.2f}"
    lines = [head, f"             why: {p.reason}"]
    if d.notes:
        lines.append(f"             caps: {'; '.join(d.notes)}")
    return "\n".join(lines)


def _source(name: str, state_dir: Path) -> AdsAdapter:
    if name == "fixture":
        acct = FixtureAdapter()
        replay_audit(acct, AuditLog(state_dir / "audit.jsonl"))  # fixture state persists across ticks
        return acct
    if name in ("google", "meta", "both"):
        from .connectors.google_ads import GoogleAdsAdapter  # noqa: PLC0415
        from .connectors.meta import MetaAdsAdapter  # noqa: PLC0415

        parts: list[AdsAdapter] = []
        if name in ("google", "both"):
            parts.append(GoogleAdsAdapter.from_env())
        if name in ("meta", "both"):
            parts.append(MetaAdsAdapter.from_env())
        return parts[0] if len(parts) == 1 else RoutedAdapter(*parts)
    raise SystemExit(f"unknown source {name}")


def _config(path: str | None) -> Config:
    if path:
        return load_config(path)
    for cand in ("guardrail.toml", "config/guardrail.toml"):
        if Path(cand).exists():
            return load_config(cand)
    example = Path(__file__).resolve().parents[2] / "config" / "guardrail.example.toml"
    return load_config(example) if example.exists() else Config()


def cmd_run(a: argparse.Namespace) -> int:
    cfg = _config(a.config)
    state = Path(a.state_dir)
    audit = AuditLog(state / "audit.jsonl")
    adapter = _source(a.source, state)
    as_of = date.fromisoformat(a.as_of) if a.as_of else datetime.now(timezone.utc).date()
    now = datetime.now(timezone.utc) if not a.as_of else datetime.combine(as_of, datetime.min.time(), timezone.utc) + timedelta(hours=6)
    entities = metrics = None
    if a.warehouse and warehouse.has_truth(a.warehouse):
        entities = adapter.list_entities()
        metrics = warehouse.WarehouseReader(a.warehouse).fetch_metrics(
            as_of - timedelta(days=cfg.policy.lookback_days), as_of - timedelta(days=1))
        print(f"TRUTH: metrics from warehouse {a.warehouse}")
    plan = propose(adapter, cfg, as_of=as_of, audit=audit, now=now, entities=entities, metrics=metrics)
    print(f"{plan.run_id}: {len(plan.proposals)} proposals, {len(plan.holds)} holds, source={a.source}")
    for d in plan.decisions:
        print(_fmt(d))
    if a.verbose:
        for h in plan.holds:
            print(f"[hold    ] {h['entity_key']}: {h['rule']}: {h['reason']}")
    plans = state / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{plan.run_id}.json").write_text(json.dumps(plan.to_dict(), indent=1, default=str))
    ks = KillSwitch.from_config(cfg.kill_switch)
    try:
        res = apply(plan, adapter, cfg, audit, apply=a.apply, now=now, kill_switch=ks)
    except WritesNotEnabled as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    print(json.dumps(res.summary()))
    if res.mode == "dry_run":
        print(f"DRY RUN: {len(res.would_apply)} changes would be written. Nothing was written.")
    if res.halted:
        print(f"HALTED: {res.halt_reason}")
        return EXIT_HALTED
    return EXIT_OK


def cmd_rollback(a: argparse.Namespace) -> int:
    cfg = _config(a.config)
    state = Path(a.state_dir)
    audit = AuditLog(state / "audit.jsonl")
    adapter = _source(a.source, state)
    try:
        res = rollback(a.run_id, adapter, cfg, audit, apply=a.apply, force=a.force,
                       kill_switch=KillSwitch.from_config(cfg.kill_switch))
    except WritesNotEnabled as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    for r in res.targets:
        print(f"{'restore' if a.apply else 'would restore'} {r['entity_key']} {r['change_type']}: "
              f"{fmt_usd(r['after_micros'])} -> {fmt_usd(r['before_micros'])}")
    print(f"{res.mode}: {len(res.targets)} targets, {len(res.restored)} restored, {len(res.conflicts)} conflicts")
    return EXIT_HALTED if res.halted else EXIT_OK


def cmd_audit(a: argparse.Namespace) -> int:
    audit = AuditLog(Path(a.state_dir) / "audit.jsonl")
    if a.action == "verify":
        ok, msg = audit.verify()
        print(("OK: " if ok else "BROKEN: ") + msg)
        return EXIT_OK if ok else EXIT_ERROR
    recs = list(audit.records())[-a.n:]
    for r in recs:
        brief = {k: r[k] for k in ("seq", "event", "at", "run_id", "entity_key", "before_micros", "after_micros") if k in r}
        print(json.dumps(brief))
    return EXIT_OK


def cmd_kill(a: argparse.Namespace) -> int:
    ks = KillSwitch.from_config(_config(a.config).kill_switch)
    if a.action == "on":
        ks.engage(a.note or "engaged from CLI")
    elif a.action == "off":
        ks.release()
    r = ks.reason()
    print(f"kill switch: {'ENGAGED (' + r + ')' if r else 'off'}")
    return EXIT_OK


def cmd_warehouse(a: argparse.Namespace) -> int:
    adapter = _source(a.source, Path(a.state_dir))
    end = date.fromisoformat(a.end) if a.end else date(2026, 10, 31)
    start = end - timedelta(days=a.days - 1)
    n = warehouse.load(a.path, adapter, start, end)
    print(f"loaded {n} rows ({start} to {end}) into {a.path}")
    return EXIT_OK


def cmd_demo(a: argparse.Namespace) -> int:
    from .demo import run_demo  # noqa: PLC0415

    s = run_demo(a.out, a.config)
    good = s["rollback"]["all_runs_exact"] and s["audit"]["ok"] and s["audit"]["cap_breaches"] == 0
    return EXIT_OK if good else EXIT_ERROR


def cmd_fixtures(a: argparse.Namespace) -> int:
    from .fixturegen import DEFAULT_DIR, generate  # noqa: PLC0415

    out = Path(a.out) if a.out else DEFAULT_DIR
    for rel in generate(out):
        print(f"wrote {out / rel}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ads-guardrail", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="TOML config (default: guardrail.toml, then config/guardrail.example.toml)")
    ap.add_argument("--state-dir", default=os.environ.get("ADS_GUARDRAIL_STATE", ".ads-guardrail"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def src(p: argparse.ArgumentParser) -> None:
        p.add_argument("--source", default=os.environ.get("ADS_GUARDRAIL_SOURCE", "fixture"),
                       choices=["fixture", "google", "meta", "both"])

    for name in ("run", "tick"):
        p = sub.add_parser(name)
        src(p)
        p.add_argument("--as-of", help="run date YYYY-MM-DD (default: today UTC; fixture data ends 2026-10-31)")
        p.add_argument("--apply", action="store_true", help="write (also needs [writes] enabled = true)")
        p.add_argument("--warehouse", default=os.environ.get("ADS_GUARDRAIL_WAREHOUSE"), help="DuckDB file for TRUTH")
        p.add_argument("-v", "--verbose", action="store_true")
        p.set_defaults(fn=cmd_run)
    p = sub.add_parser("rollback")
    src(p)
    p.add_argument("run_id")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--force", action="store_true", help="restore even if the value changed since our write")
    p.set_defaults(fn=cmd_rollback)
    p = sub.add_parser("audit")
    p.add_argument("action", choices=["verify", "show"])
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("kill")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--note")
    p.set_defaults(fn=cmd_kill)
    p = sub.add_parser("warehouse")
    src(p)
    p.add_argument("action", choices=["load"])
    p.add_argument("--path", default="warehouse.duckdb")
    p.add_argument("--end")
    p.add_argument("--days", type=int, default=60)
    p.set_defaults(fn=cmd_warehouse)
    p = sub.add_parser("demo")
    p.add_argument("--out", default="demo/out")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("fixtures")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_fixtures)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    sys.exit(main())
