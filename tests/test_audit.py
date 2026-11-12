import json
import threading

from ads_guardrail.audit import GENESIS, AuditLog


def fill(log, n=5):
    for i in range(n):
        log.append("applied" if i % 2 else "note", run_id="r", proposal_id=f"p{i}", entity_key=f"k{i}",
                   account_id="a", change_type="budget", before_micros=100, after_micros=110)


def test_chain_verifies(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    fill(log)
    ok, msg = log.verify()
    assert ok and "5 records" in msg
    recs = list(log.records())
    assert recs[0]["prev_hash"] == GENESIS
    assert all(recs[i]["prev_hash"] == recs[i - 1]["hash"] for i in range(1, 5))


def _rewrite(path, lines):
    path.write_text("".join(json.dumps(l, sort_keys=True) + "\n" for l in lines))


def test_edit_is_detected(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    fill(log)
    lines = list(log.records())
    lines[2]["after_micros"] = 999  # quietly change history
    _rewrite(log.path, lines)
    ok, msg = log.verify()
    assert not ok and "line 3" in msg


def test_delete_and_reorder_detected(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    fill(log)
    lines = list(log.records())
    _rewrite(log.path, lines[:2] + lines[3:])
    assert not log.verify()[0]
    _rewrite(log.path, [lines[1], lines[0]] + lines[2:])
    assert not log.verify()[0]


def test_concurrent_appends_keep_chain(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    ts = [threading.Thread(target=fill, args=(AuditLog(log.path), 25)) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok, msg = log.verify()
    assert ok, msg
    assert len(list(log.records())) == 100


def test_state_replay(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    a = log.append("applied", run_id="r1", proposal_id="p1", entity_key="k", account_id="acct",
                   change_type="budget", before_micros=100_000_000, after_micros=120_000_000)
    log.append("run_finished", run_id="r1")
    st = log.state()
    assert "p1" in st.live_proposals and "r1" in st.finished_runs
    assert st.movement("acct", a["at"][:10]) == 20_000_000
    log.append("rolled_back", run_id="r1", proposal_id="p1", entity_key="k", account_id="acct",
               change_type="budget", before_micros=120_000_000, after_micros=100_000_000, reverses_seq=a["seq"])
    st = log.state()
    assert "p1" not in st.live_proposals and ("r1", a["seq"]) in st.rolled_back
