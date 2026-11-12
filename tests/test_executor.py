"""Dry run never writes, two-key apply, kill switch, idempotency, drift, rollback exactness."""
from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from ads_guardrail import (AuditLog, ChangeType, InMemoryAdAccount, KillSwitch, WritesNotEnabled, apply,
                           propose, rollback)
from ads_guardrail.connectors.base import AdapterError

from conftest import AS_OF, NOW, open_config


class NoWrites(InMemoryAdAccount):
    def write_value(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("dry run called write_value")


def ks(tmp_path, env=None):
    return KillSwitch(file=tmp_path / "KILL_SWITCH", env_var="ADS_GUARDRAIL_KILL", env=env if env is not None else {})


def test_dry_run_never_writes(tmp_path, account):
    spy = NoWrites(account.list_entities(), account.fetch_metrics(date(2026, 1, 1), date(2027, 1, 1)))
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    plan = propose(spy, cfg, as_of=AS_OF, audit=audit, now=NOW)
    assert any(d.writable for d in plan.decisions)
    res = apply(plan, spy, cfg, audit, now=NOW)  # apply defaults to False
    assert res.mode == "dry_run" and res.applied == [] and len(res.would_apply) > 0
    assert not audit.path.exists()


def test_apply_needs_config_flag_too(tmp_path, account):
    cfg = open_config().with_writes(False)
    audit = AuditLog(tmp_path / "audit.jsonl")
    plan = propose(account, cfg, as_of=AS_OF, now=NOW)
    before = account.snapshot()
    with pytest.raises(WritesNotEnabled):
        apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path))
    assert account.snapshot() == before and account.writes == [] and not audit.path.exists()


def test_apply_writes_and_logs_before_after(tmp_path, account):
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    plan = propose(account, cfg, as_of=AS_OF, audit=audit, now=NOW)
    res = apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path))
    assert len(res.applied) == 3 == len(account.writes)
    for rec, w in zip(res.applied, account.writes):
        assert (rec["before_micros"], rec["after_micros"]) == (w.before_micros, w.after_micros)
        assert rec["proposal_id"] and rec["reason"]
    assert audit.verify()[0]


def test_kill_switch_env_blocks_everything(tmp_path, account):
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    plan = propose(account, cfg, as_of=AS_OF, now=NOW)
    res = apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path, {"ADS_GUARDRAIL_KILL": "1"}))
    assert res.halted and account.writes == []
    assert [r["event"] for r in audit.records()] == ["halted"]


def test_kill_switch_reads_real_environ_live(tmp_path, account, monkeypatch):
    k = KillSwitch(file=tmp_path / "K", env_var="ADS_GUARDRAIL_KILL_TEST")
    assert k.reason() is None
    monkeypatch.setenv("ADS_GUARDRAIL_KILL_TEST", "true")
    assert "ADS_GUARDRAIL_KILL_TEST" in k.reason()
    monkeypatch.setenv("ADS_GUARDRAIL_KILL_TEST", "0")
    assert k.reason() is None


class TripAfter(InMemoryAdAccount):
    """Engages the kill switch file after N writes (an operator hitting stop mid-run)."""

    def __init__(self, src: InMemoryAdAccount, switch: KillSwitch, n: int):
        super().__init__(src.list_entities(), src.fetch_metrics(date(2026, 1, 1), date(2027, 1, 1)))
        self.switch, self.n = switch, n

    def write_value(self, key, ct, v):
        out = super().write_value(key, ct, v)
        if len(self.writes) == self.n:
            self.switch.engage("stop")
        return out


def test_kill_switch_file_halts_mid_run_then_resumes_without_double_writes(tmp_path, account):
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    k = ks(tmp_path)
    acct = TripAfter(account, k, 1)
    plan = propose(acct, cfg, as_of=AS_OF, now=NOW)
    res = apply(plan, acct, cfg, audit, apply=True, now=NOW, kill_switch=k)
    assert res.halted and len(acct.writes) == 1  # checked before every write
    # still engaged: a re-run writes nothing
    assert apply(plan, acct, cfg, audit, apply=True, now=NOW, kill_switch=k).halted
    assert len(acct.writes) == 1
    k.release()
    res2 = apply(plan, acct, cfg, audit, apply=True, now=NOW, kill_switch=k)
    assert not res2.halted and len(res2.applied) == 2 and len(acct.writes) == 3
    keys = [w.entity_key for w in acct.writes]
    assert len(set(keys)) == 3  # the first change was not applied twice
    assert apply(plan, acct, cfg, audit, apply=True, now=NOW, kill_switch=k).already_complete


def test_rerun_is_idempotent(tmp_path, account):
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    plan = propose(account, cfg, as_of=AS_OF, audit=audit, now=NOW)
    apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path))
    snap = account.snapshot()
    again = apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path))
    assert again.already_complete and account.snapshot() == snap
    # a fresh plan for the same day: cooldown and "already applied" block everything
    plan2 = propose(account, cfg, as_of=AS_OF, audit=audit, now=NOW)
    assert not any(d.writable for d in plan2.decisions)


def test_drift_since_proposal_is_skipped(tmp_path, account):
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    plan = propose(account, cfg, as_of=AS_OF, now=NOW)
    target = next(d for d in plan.decisions if d.writable).proposal
    account.write_value(target.entity_key, target.change_type, target.before_micros + 10_000)  # someone else edits
    res = apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path))
    assert any(s["entity_key"] == target.entity_key for s in res.skipped)
    assert "skipped_drift" in [r["event"] for r in audit.records()]


def test_write_failure_is_logged_and_run_continues(tmp_path, account):
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    plan = propose(account, cfg, as_of=AS_OF, now=NOW)
    bad = next(d for d in plan.decisions if d.writable).proposal.entity_key
    account.fail_on_write.add(bad)
    res = apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path))
    assert len(res.applied) == 2 and "write_failed" in [r["event"] for r in audit.records()]


def test_rollback_is_exact_across_runs(tmp_path, account):
    from conftest import metrics_for
    for e in account.list_entities():
        spend, conv = {"w": (196, 6), "l": (148, 1.2), "u": (70, 2.3)}[e.entity_id]
        account.append_metrics(metrics_for(e, days=4, spend=spend, conv=conv, end=AS_OF + timedelta(days=3)))
    cfg = open_config(cooldown_hours=0)
    audit = AuditLog(tmp_path / "audit.jsonl")
    k = ks(tmp_path)
    snaps, runs = [], []
    for i in range(4):
        now = NOW + timedelta(days=i)
        snaps.append(account.snapshot())
        plan = propose(account, cfg, as_of=AS_OF + timedelta(days=i), audit=audit, now=now)
        apply(plan, account, cfg, audit, apply=True, now=now, kill_switch=k)
        runs.append(plan.run_id)
    assert account.snapshot() != snaps[0]
    assert len({w.entity_key for w in account.writes}) == 3 and len(account.writes) >= 5  # several runs wrote
    dry = rollback(runs[0], account, cfg, audit, now=NOW + timedelta(days=5))
    assert dry.mode == "dry_run" and dry.targets and dry.restored == []
    for i in reversed(range(4)):
        r = rollback(runs[i], account, cfg, audit, apply=True, now=NOW + timedelta(days=5), kill_switch=k)
        assert r.conflicts == []
        assert account.snapshot() == snaps[i]  # to the micro
    again = rollback(runs[0], account, cfg, audit, apply=True, now=NOW + timedelta(days=5), kill_switch=k)
    assert again.targets == []  # idempotent
    assert audit.verify()[0]


def test_rollback_detects_conflict_and_force_overrides(tmp_path, account):
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    k = ks(tmp_path)
    start = account.snapshot()
    plan = propose(account, cfg, as_of=AS_OF, now=NOW)
    res = apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=k)
    first = res.applied[0]
    account.write_value(first["entity_key"], ChangeType(first["change_type"]), first["after_micros"] + 10_000)
    r = rollback(plan.run_id, account, cfg, audit, apply=True, now=NOW, kill_switch=k)
    assert len(r.conflicts) == 1 and len(r.restored) == 2
    r2 = rollback(plan.run_id, account, cfg, audit, apply=True, now=NOW, kill_switch=k, force=True)
    assert len(r2.restored) == 1 and account.snapshot() == start


def test_rollback_respects_kill_switch_and_two_keys(tmp_path, account):
    cfg = open_config()
    audit = AuditLog(tmp_path / "audit.jsonl")
    plan = propose(account, cfg, as_of=AS_OF, now=NOW)
    apply(plan, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path))
    snap = account.snapshot()
    r = rollback(plan.run_id, account, cfg, audit, apply=True, now=NOW, kill_switch=ks(tmp_path, {"ADS_GUARDRAIL_KILL": "yes"}))
    assert r.halted and account.snapshot() == snap
    with pytest.raises(WritesNotEnabled):
        rollback(plan.run_id, account, cfg.with_writes(False), audit, apply=True, now=NOW, kill_switch=ks(tmp_path))


def test_naive_datetime_rejected(account):
    from datetime import datetime
    with pytest.raises(ValueError):
        propose(account, open_config(), as_of=AS_OF, now=datetime(2026, 11, 1))


def test_invalid_write_values_refused(account):
    key = account.list_entities()[0].key
    with pytest.raises(AdapterError):
        account.write_value(key, ChangeType.BUDGET, 12_345)  # not whole cents
