"""Append-only JSONL audit log with a SHA-256 hash chain. It doubles as the rollback log.

Every line holds `prev_hash` and `hash = sha256(canonical JSON of the line without hash)`.
Editing, deleting or reordering any line breaks `verify()`. Appends take an exclusive file
lock and fsync, so two schedulers on one box cannot interleave lines.

The log is also the source of truth for guardrail state: cooldowns, today's movement per
account, which proposals are already live, and which runs finished.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS = "0" * 64


class AuditError(RuntimeError):
    pass


def _canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


@dataclass
class AuditState:
    """What the log says has happened. Built by replaying every line."""

    last_write_at: dict[str, datetime] = field(default_factory=dict)  # entity_key -> last write time
    movement_by_account_day: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    live_proposals: set[str] = field(default_factory=set)  # applied and not rolled back
    finished_runs: set[str] = field(default_factory=set)
    applied: list[dict[str, Any]] = field(default_factory=list)
    rolled_back: set[tuple[str, int]] = field(default_factory=set)  # (run_id, seq of applied line)

    def movement(self, account_id: str, day: str) -> int:
        return self.movement_by_account_day.get((account_id, day), 0)


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # --- reading ------------------------------------------------------------------------
    def records(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open() as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise AuditError(f"line {n} is not valid JSON") from exc

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        count = 0
        for i, rec in enumerate(self.records()):
            if rec.get("seq") != i:
                return False, f"line {i + 1}: seq {rec.get('seq')} != {i}"
            if rec.get("prev_hash") != prev:
                return False, f"line {i + 1}: prev_hash does not match line {i}"
            if rec.get("hash") != _digest(rec):
                return False, f"line {i + 1}: hash mismatch (line was edited)"
            prev = rec["hash"]
            count += 1
        return True, f"{count} records, chain intact"

    def state(self) -> AuditState:
        st = AuditState()
        for rec in self.records():
            ev = rec["event"]
            if ev in ("applied", "rolled_back"):
                at = datetime.fromisoformat(rec["at"])
                key = rec["entity_key"]
                if key not in st.last_write_at or at > st.last_write_at[key]:
                    st.last_write_at[key] = at
            if ev == "applied":
                if rec["change_type"] == "budget":
                    day = rec["at"][:10]
                    st.movement_by_account_day[(rec["account_id"], day)] += abs(rec["after_micros"] - rec["before_micros"])
                st.live_proposals.add(rec["proposal_id"])
                st.applied.append(rec)
            elif ev == "rolled_back":
                st.live_proposals.discard(rec["proposal_id"])
                st.rolled_back.add((rec["run_id"], rec["reverses_seq"]))
            elif ev == "run_finished":
                st.finished_runs.add(rec["run_id"])
        return st

    # --- writing ------------------------------------------------------------------------
    def append(self, event: str, *, at: datetime | None = None, **fields: Any) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            seq, prev = 0, GENESIS
            last = self._last_line()
            if last is not None:
                seq, prev = last["seq"] + 1, last["hash"]
            rec: dict[str, Any] = {
                "seq": seq,
                "event": event,
                "at": (at or datetime.now(timezone.utc)).isoformat(),
                **fields,
                "prev_hash": prev,
            }
            rec["hash"] = _digest(rec)
            os.write(fd, (json.dumps(rec, sort_keys=True, default=str) + "\n").encode())
            os.fsync(fd)
            return rec
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _last_line(self) -> dict[str, Any] | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf = b""
            while pos > 0:
                step = min(4096, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                lines = buf.strip().split(b"\n")
                if len(lines) > 1 or pos == 0:
                    return json.loads(lines[-1])
        return None
