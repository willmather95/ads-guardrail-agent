"""ads-guardrail-agent: propose Google Ads and Meta bid and budget changes, write them only under hard caps.

Small public API (used by channel-loop):

    from ads_guardrail import propose, apply, rollback, Config, AuditLog, FixtureAdapter
    plan = propose(adapter, config, as_of=date(2026, 11, 1), audit=audit)
    result = apply(plan, adapter, config, audit, apply=False)   # dry run unless apply=True AND config allows
"""
from .audit import AuditLog, AuditState
from .config import ChangeCaps, Config, GuardrailConfig, KillSwitchConfig, PolicyConfig, load_config
from .connectors.base import AdsAdapter, AdsReader, AdsWriter, RoutedAdapter
from .connectors.fixture import FixtureAdapter
from .connectors.memory import InMemoryAdAccount
from .executor import ApplyResult, RollbackResult, WritesNotEnabled, apply, propose, rollback, run_id_for
from .guardrails import GuardrailViolation
from .killswitch import KillSwitch, KillSwitchEngaged
from .models import ChangeType, DailyMetric, Decision, Entity, Plan, Platform, Proposal, Verdict

__version__ = "0.1.0"

__all__ = [
    "AdsAdapter", "AdsReader", "AdsWriter", "ApplyResult", "AuditLog", "AuditState", "ChangeCaps", "ChangeType",
    "Config", "DailyMetric", "Decision", "Entity", "FixtureAdapter", "GuardrailConfig", "GuardrailViolation",
    "InMemoryAdAccount", "KillSwitch", "KillSwitchConfig", "KillSwitchEngaged", "Plan", "Platform", "PolicyConfig",
    "Proposal", "RollbackResult", "RoutedAdapter", "Verdict", "WritesNotEnabled", "apply", "load_config", "propose",
    "rollback", "run_id_for",
]
