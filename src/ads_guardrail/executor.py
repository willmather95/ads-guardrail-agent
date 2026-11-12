"""propose(), apply() and rollback(): the public write path.

Two keys to write: `apply=True` from the caller AND `writes_enabled = true` in config.
Anything less is a dry run, and a dry run never calls a write method and never touches the
audit log. Before every single write the executor:

1. checks the kill switch (file or env), and halts the whole run if it is engaged,
2. re-reads the live value and skips the change if it drifted from what was proposed,
3. re-runs the hard cap check against live values,
4. writes, reads the value back, and appends a hash-chained audit line with before/after.

Runs are idempotent. The run id is the as-of date plus the config fingerprint. A finished
run is a no-op when re-run, and a run halted by the kill switch resumes where it stopped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .audit import AuditLog, AuditState
from .config import Config
from .connectors.base import AdsAdapter, AdsReader
from .guardrails import GuardrailViolation, decide, hard_check
from .killswitch import KillSwitch
from .models import ChangeType, Decision, Plan, Proposal, stable_id
from .policy import evaluate, read_window


class WritesNotEnabled(RuntimeError):
    pass


@dataclass
class ApplyResult:
    run_id: str
    mode: str  # "dry_run" | "apply"
    decisions: list[Decision] = field(default_factory=list)
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None
    already_complete: bool = False

    @property
    def would_apply(self) -> list[Decision]:
        return [d for d in self.decisions if d.writable]

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "mode": self.mode, "decisions": len(self.decisions),
            "writable": len(self.would_apply), "applied": len(self.applied), "skipped": len(self.skipped),
            "halted": self.halted, "halt_reason": self.halt_reason, "already_complete": self.already_complete,
        }


@dataclass
class RollbackResult:
    run_id: str
    mode: str
    targets: list[dict[str, Any]] = field(default_factory=list)
    restored: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None


def _utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    return now.astimezone(timezone.utc)


def run_id_for(as_of: date, config: Config) -> str:
    return f"run-{as_of.isoformat()}-{config.fingerprint()}"


def propose(reader: AdsReader, config: Config, *, as_of: date, audit: AuditLog | None = None,
            now: datetime | None = None, entities=None, metrics=None) -> Plan:
    """Read performance, run the policy, and pre-check every proposal against the guardrails.

    Pass `entities`/`metrics` to use a warehouse (TRUTH) instead of the reader's own reads.
    Never writes anything.
    """
    now = _utc(now)
    rid = run_id_for(as_of, config)
    if entities is None or metrics is None:
        entities, metrics = read_window(reader, config.policy, as_of)
    proposals, holds = evaluate(entities, metrics, config.policy, rid, as_of)
    state = audit.state() if audit else AuditState()
    quantum = getattr(reader, "quantum_micros", lambda k, c: 10_000)
    decisions = decide(proposals, entities, config.guardrails, state, now, quantum)
    return Plan(run_id=rid, as_of=as_of, proposals=proposals, decisions=decisions,
                holds=[h.to_dict() for h in holds])


def apply(plan: Plan, writer: AdsAdapter, config: Config, audit: AuditLog, *, apply: bool = False,
          now: datetime | None = None, kill_switch: KillSwitch | None = None) -> ApplyResult:
    """Apply a plan under every guardrail. Dry run unless apply=True AND config.writes_enabled."""
    now = _utc(now)
    if not apply:
        return ApplyResult(plan.run_id, "dry_run", decisions=list(plan.decisions))
    if not config.writes_enabled:
        raise WritesNotEnabled("--apply was given but [writes] enabled = false in config; nothing written")

    ks = kill_switch or KillSwitch.from_config(config.kill_switch)
    state = audit.state()
    res = ApplyResult(plan.run_id, "apply")
    if plan.run_id in state.finished_runs:
        res.already_complete = True
        return res
    reason = ks.reason()
    if reason:
        audit.append("halted", at=now, run_id=plan.run_id, reason=reason, before_any_write=True)
        res.halted, res.halt_reason = True, reason
        return res

    entities = writer.list_entities()
    res.decisions = decide(plan.proposals, entities, config.guardrails, state, now, writer.quantum_micros)
    audit.append("run_started", at=now, run_id=plan.run_id, as_of=plan.as_of.isoformat(),
                 config=config.fingerprint(), proposals=len(plan.proposals), writable=len(res.would_apply))
    for d in res.decisions:
        if not d.writable:
            audit.append("blocked", at=now, run_id=plan.run_id, proposal_id=d.proposal.proposal_id,
                         entity_key=d.proposal.entity_key, change_type=d.proposal.change_type.value,
                         notes=list(d.notes))

    totals: dict[str, int] = {}
    for e in entities:
        totals[e.account_id] = totals.get(e.account_id, 0) + e.daily_budget_micros
    run_movement: dict[str, int] = {}

    for d in res.would_apply:
        p = d.proposal
        reason = ks.reason()  # checked before EVERY write
        if reason:
            audit.append("halted", at=now, run_id=plan.run_id, reason=reason, next_proposal=p.proposal_id)
            res.halted, res.halt_reason = True, reason
            break
        base = dict(run_id=plan.run_id, proposal_id=p.proposal_id, entity_key=p.entity_key,
                    account_id=p.account_id, change_type=p.change_type.value)
        live = writer.read_value(p.entity_key, p.change_type)
        if live != p.before_micros:
            audit.append("skipped_drift", at=now, **base, expected_micros=p.before_micros, live_micros=live)
            res.skipped.append({**base, "why": "live value drifted since the proposal"})
            continue
        try:
            hard_check(d, live, config.guardrails, state, now, totals.get(p.account_id, 0),
                       run_movement.get(p.account_id, 0))
        except GuardrailViolation as exc:
            audit.append("blocked_at_write", at=now, **base, why=str(exc))
            res.skipped.append({**base, "why": str(exc)})
            continue
        try:
            after = writer.write_value(p.entity_key, p.change_type, d.final_micros)
        except Exception as exc:  # noqa: BLE001 (record any adapter failure, keep going)
            audit.append("write_failed", at=now, **base, error=str(exc)[:500])
            res.skipped.append({**base, "why": f"write failed: {exc}"})
            continue
        rec = audit.append("applied", **base, at=now, before_micros=live, after_micros=after,
                           requested_micros=d.final_micros, proposed_micros=p.proposed_micros,
                           verdict=d.verdict.value, rule=p.rule, reason=p.reason, confidence=p.confidence,
                           notes=list(d.notes))
        res.applied.append(rec)
        if p.change_type is ChangeType.BUDGET:
            run_movement[p.account_id] = run_movement.get(p.account_id, 0) + abs(after - live)
            totals[p.account_id] = totals.get(p.account_id, 0) + (after - live)

    if not res.halted:
        audit.append("run_finished", at=now, run_id=plan.run_id, applied=len(res.applied), skipped=len(res.skipped))
    return res


def rollback(run_id: str, writer: AdsAdapter, config: Config, audit: AuditLog, *, apply: bool = False,
             now: datetime | None = None, kill_switch: KillSwitch | None = None, force: bool = False) -> RollbackResult:
    """Reverse every live change from one run, newest first, restoring exact before values.

    If a value was changed by someone else since our write, that entity is reported as a
    conflict and left alone (unless force=True). Re-running a finished rollback is a no-op.
    """
    now = _utc(now)
    state = audit.state()
    targets = [r for r in state.applied if r["run_id"] == run_id and (run_id, r["seq"]) not in state.rolled_back]
    targets.sort(key=lambda r: r["seq"], reverse=True)
    res = RollbackResult(run_id, "apply" if apply else "dry_run", targets=targets)
    if not apply or not targets:
        return res
    if not config.writes_enabled:
        raise WritesNotEnabled("rollback --apply needs [writes] enabled = true in config")
    ks = kill_switch or KillSwitch.from_config(config.kill_switch)
    audit.append("rollback_started", at=now, run_id=run_id, targets=len(targets))
    for r in targets:
        reason = ks.reason()
        if reason:
            audit.append("halted", at=now, run_id=run_id, reason=reason, during="rollback")
            res.halted, res.halt_reason = True, reason
            break
        ct = ChangeType(r["change_type"])
        live = writer.read_value(r["entity_key"], ct)
        base = dict(run_id=run_id, proposal_id=r["proposal_id"], entity_key=r["entity_key"],
                    account_id=r["account_id"], change_type=r["change_type"], reverses_seq=r["seq"])
        if live != r["after_micros"] and not force:
            audit.append("rollback_conflict", at=now, **base, expected_micros=r["after_micros"], live_micros=live)
            res.conflicts.append({**base, "live_micros": live, "expected_micros": r["after_micros"]})
            continue
        restored = writer.write_value(r["entity_key"], ct, r["before_micros"])
        rec = audit.append("rolled_back", at=now, **base, before_micros=live, after_micros=restored)
        res.restored.append(rec)
    if not res.halted:
        audit.append("rollback_finished", at=now, run_id=run_id, restored=len(res.restored), conflicts=len(res.conflicts))
    return res


def default_paths(state_dir: str | Path) -> tuple[Path, Path]:
    d = Path(state_dir)
    return d / "audit.jsonl", d / "plans"


def external_proposal(entity, change_type: ChangeType, proposed_micros: int, *, reason: str, as_of: date,
                      config: Config, source: str = "external", confidence: float = 0.5) -> Proposal:
    """Wrap a change requested by another agent (or a person) so it goes through the same caps."""

    before = entity.daily_budget_micros if change_type is ChangeType.BUDGET else entity.bid_micros
    if before is None:
        raise ValueError(f"{entity.key} has no {change_type.value} to change")
    return Proposal(
        proposal_id=stable_id(as_of.isoformat(), entity.key, change_type.value, source),
        run_id=run_id_for(as_of, config), entity_key=entity.key, platform=entity.platform,
        account_id=entity.account_id, entity_id=entity.entity_id, entity_name=entity.name,
        change_type=change_type, before_micros=before, proposed_micros=proposed_micros,
        rule=f"external:{source}", reason=reason, confidence=confidence, expected_impact={},
    )


def replay_audit(account, audit: AuditLog) -> int:
    """Re-apply logged writes to a fresh in-memory account (fixture mode keeps state across ticks)."""
    n = 0
    for rec in audit.records():
        if rec["event"] in ("applied", "rolled_back"):
            account.write_value(rec["entity_key"], ChangeType(rec["change_type"]), rec["after_micros"])
            n += 1
    account.writes.clear()
    return n
