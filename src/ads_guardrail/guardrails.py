"""Guardrails: turn proposals into decisions that can never exceed a cap.

Caps, in the order they are applied:

1. allowlist            entity must match a pattern, or it is blocked
2. shared budget        Google shared budgets are blocked (one edit moves many campaigns)
3. already live         a proposal id that is already applied is blocked (idempotent re-runs)
4. cooldown             no second write to an entity inside `cooldown_hours`
5. one per entity       at most one change per entity per run
6. max changes per run  lowest-priority extras are blocked
7. per-change cap       |change| <= min(pct of current value, absolute amount)
8. floor                never below the floor; a cut at the floor is blocked
9. account daily cap    sum of |budget change| per account per UTC day
10. account total cap   sum of daily budgets in an account never rises above its ceiling
11. platform step       round toward the old value to whole cents (never rounds past a cap)
12. minimum change      if what is left is too small to matter, block it

All math is integer micros. The same checks run again inside the executor right before
each write (`hard_check`), against a fresh read of the live value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from typing import Callable

from .audit import AuditState
from .config import ChangeCaps, GuardrailConfig
from .models import ChangeType, Decision, Entity, Proposal, Verdict, fmt_usd


class GuardrailViolation(RuntimeError):
    pass


def caps_for(cfg: GuardrailConfig, change_type: ChangeType) -> ChangeCaps:
    return cfg.budget if change_type is ChangeType.BUDGET else cfg.bid


def per_change_limit(caps: ChangeCaps, before: int) -> int:
    return min(before * caps.max_change_bp // 10_000, caps.max_change_abs_micros)


def quantize_toward(before: int, final: int, quantum: int) -> int:
    """Round `final` to a multiple of `quantum`, toward `before` (so magnitude only shrinks)."""
    if quantum <= 1 or final % quantum == 0:
        return final
    if final > before:
        q = (final // quantum) * quantum
        return q if q >= before else before
    q = -((-final) // quantum) * quantum
    return q if q <= before else before


def priority(p: Proposal) -> float:
    return p.confidence * abs(p.expected_impact.get("delta_spend_per_day", 0.0) or p.delta_micros / 1e6)


def allowed(cfg: GuardrailConfig, key: str) -> bool:
    return any(fnmatchcase(key, pat) for pat in cfg.allowlist)


@dataclass
class _RunTally:
    movement: dict[str, int]
    pending_increase: dict[str, int]
    touched: set[str]
    writable: int = 0


def decide(proposals: list[Proposal], entities: list[Entity], cfg: GuardrailConfig, state: AuditState,
           now: datetime, quantum: Callable[[str, ChangeType], int] = lambda k, c: 10_000) -> list[Decision]:
    ents = {e.key: e for e in entities}
    day = now.date().isoformat()
    account_total: dict[str, int] = {}
    for e in entities:
        account_total[e.account_id] = account_total.get(e.account_id, 0) + e.daily_budget_micros
    tally = _RunTally(movement={}, pending_increase={}, touched=set())
    decisions: list[Decision] = []

    for p in sorted(proposals, key=lambda x: (-priority(x), x.proposal_id)):
        notes: list[str] = []

        def block(why: str) -> None:
            decisions.append(Decision(p, Verdict.BLOCKED, p.before_micros, tuple(notes + [why])))

        e = ents.get(p.entity_key)
        if e is None:
            block("entity no longer exists")
            continue
        if not allowed(cfg, p.entity_key):
            block("not on the allowlist")
            continue
        if p.change_type is ChangeType.BUDGET and e.budget_shared:
            block("shared budget: editing it would move other campaigns")
            continue
        if p.proposal_id in state.live_proposals:
            block("already applied (idempotent re-run)")
            continue
        last = state.last_write_at.get(p.entity_key)
        if last is not None and now - last < timedelta(hours=cfg.cooldown_hours):
            left = timedelta(hours=cfg.cooldown_hours) - (now - last)
            block(f"cooldown: last change {int((now - last).total_seconds() // 3600)}h ago, "
                  f"{int(left.total_seconds() // 3600)}h left of {cfg.cooldown_hours}h")
            continue
        if p.entity_key in tally.touched:
            block("one change per entity per run")
            continue
        if tally.writable >= cfg.max_changes_per_run:
            block(f"max {cfg.max_changes_per_run} changes per run reached")
            continue

        caps = caps_for(cfg, p.change_type)
        before = p.before_micros
        delta = p.delta_micros
        lim = per_change_limit(caps, before)
        if abs(delta) > lim:
            delta = lim if delta > 0 else -lim
            notes.append(f"per-change cap {caps.max_change_bp / 100:g}% / {fmt_usd(caps.max_change_abs_micros)} "
                         f"limits it to {fmt_usd(lim)}")
        if delta < 0 and before + delta < caps.floor_micros:
            new_delta = caps.floor_micros - before
            if new_delta >= 0:
                block(f"already at the floor of {fmt_usd(caps.floor_micros)}")
                continue
            delta = new_delta
            notes.append(f"floor {fmt_usd(caps.floor_micros)}")

        acct = p.account_id
        if p.change_type is ChangeType.BUDGET:
            used = state.movement(acct, day) + tally.movement.get(acct, 0)
            remaining = cfg.account_daily_change_cap_micros - used
            if remaining <= 0:
                block(f"account daily change cap {fmt_usd(cfg.account_daily_change_cap_micros)} used up")
                continue
            if abs(delta) > remaining:
                delta = remaining if delta > 0 else -remaining
                notes.append(f"account daily cap leaves {fmt_usd(remaining)}")
            if delta > 0:
                ceiling = cfg.total_cap_for(acct)
                headroom = ceiling - account_total.get(acct, 0) - tally.pending_increase.get(acct, 0)
                if headroom <= 0:
                    block(f"account total budget is at its ceiling of {fmt_usd(ceiling)}")
                    continue
                if delta > headroom:
                    delta = headroom
                    notes.append(f"account ceiling {fmt_usd(ceiling)} leaves {fmt_usd(headroom)}")

        q = quantum(p.entity_key, p.change_type)
        final = quantize_toward(before, before + delta, q)
        if final != before + delta and final != p.proposed_micros:
            notes.append(f"rounded to the platform step of {fmt_usd(q)}")
        delta = final - before
        if abs(delta) < cfg.min_change_micros:
            block(f"change of {fmt_usd(delta)} is below the {fmt_usd(cfg.min_change_micros)} minimum after caps")
            continue

        verdict = Verdict.APPROVED if final == p.proposed_micros else Verdict.CLAMPED
        decisions.append(Decision(p, verdict, final, tuple(notes)))
        tally.touched.add(p.entity_key)
        tally.writable += 1
        if p.change_type is ChangeType.BUDGET:
            tally.movement[acct] = tally.movement.get(acct, 0) + abs(delta)
            if delta > 0:
                tally.pending_increase[acct] = tally.pending_increase.get(acct, 0) + delta

    order = {p.proposal_id: i for i, p in enumerate(proposals)}
    decisions.sort(key=lambda d: order.get(d.proposal.proposal_id, 0))
    return decisions


def hard_check(d: Decision, live_before: int, cfg: GuardrailConfig, state: AuditState, now: datetime,
               live_account_total: int, run_movement: int) -> None:
    """Last line of defence, run right before a write against live values. Raises on any breach."""
    p = d.proposal
    caps = caps_for(cfg, p.change_type)
    delta = d.final_micros - live_before
    if not allowed(cfg, p.entity_key):
        raise GuardrailViolation("not on the allowlist")
    if abs(delta) > per_change_limit(caps, live_before):
        raise GuardrailViolation("per-change cap exceeded")
    if delta < 0 and d.final_micros < caps.floor_micros:
        raise GuardrailViolation("below floor")
    if p.change_type is ChangeType.BUDGET:
        used = state.movement(p.account_id, now.date().isoformat()) + run_movement
        if used + abs(delta) > cfg.account_daily_change_cap_micros:
            raise GuardrailViolation("account daily change cap exceeded")
        if delta > 0 and live_account_total + delta > cfg.total_cap_for(p.account_id):
            raise GuardrailViolation("account total budget cap exceeded")
