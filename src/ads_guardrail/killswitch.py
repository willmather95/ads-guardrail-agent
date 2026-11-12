"""Kill switch: a file or an environment variable. Checked before every single write."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .config import KillSwitchConfig

_OFF = {"", "0", "false", "no", "off"}


class KillSwitchEngaged(RuntimeError):
    pass


@dataclass
class KillSwitch:
    file: Path
    env_var: str
    env: Mapping[str, str] | None = None  # None = read os.environ live on every check

    @classmethod
    def from_config(cls, cfg: KillSwitchConfig, base_dir: str | Path = ".") -> "KillSwitch":
        p = Path(cfg.file)
        return cls(file=p if p.is_absolute() else Path(base_dir) / p, env_var=cfg.env_var)

    def reason(self) -> str | None:
        env = os.environ if self.env is None else self.env
        val = env.get(self.env_var, "")
        if val.strip().lower() not in _OFF:
            return f"env {self.env_var}={val!r}"
        if self.file.exists():
            note = self.file.read_text(errors="replace").strip()[:200]
            return f"file {self.file.name} present" + (f": {note}" if note else "")
        return None

    def check(self) -> None:
        r = self.reason()
        if r:
            raise KillSwitchEngaged(f"kill switch engaged ({r}); all writes halted")

    def engage(self, note: str = "engaged") -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(note + "\n")

    def release(self) -> None:
        self.file.unlink(missing_ok=True)
