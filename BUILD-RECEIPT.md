# BUILD RECEIPT: ads-guardrail-agent

Built Nov 12, 2026 for willmather95. Local commits only. Not pushed, no GitHub repo created, `gh` never run.

## What works (verified on this box)

- **Policy engine** (`src/ads_guardrail/policy/`): 6 rule paths (learning-phase hold, min-data hold, scale winner, underpacing bid up, bid down / cut loser, within band). Each proposal has a plain-English reason, expected daily impact and a confidence from a Gamma posterior on Poisson conversions (pure Python, checked against closed forms).
- **12 guardrails** (`guardrails.py`), all in integer micros: allowlist, shared-budget block, idempotency, 72h cooldown, one change per entity per run, max changes per run, per-change % and $ cap, floor, account daily cap, account total cap, cent rounding toward the old value, minimum change. A second `hard_check` runs right before every write.
- **Two-key writes:** `--apply` (or `apply=True`) AND `[writes] enabled = true`. Dry run is the default everywhere.
- **Kill switch** (file or env), read live before every write and during rollback. A halted run resumes without double writes.
- **Append-only JSONL audit** with a SHA-256 hash chain, file lock and fsync. `rollback RUN_ID` restores exact before values, detects conflicts, supports `--force` and is idempotent.
- **Connectors:** Google Ads (GAQL searchStream, campaignBudgets:mutate, tCPA update masks, shared-budget refusal, retry on reads only) and Meta Marketing API (adset insights with pagination, daily_budget / bid_amount in cents, usage-header throttle). Both tested against a fake transport. FixtureAdapter parses the synthetic fixtures with the same parsers.
- **TRUTH:** DuckDB warehouse (`ads_daily`, `ads_entities`, `ads_load_meta`), idempotent loads, in the marketing-warehouse style.
- **HOME:** `ads-guardrail tick` with scheduler exit codes (0 / 1 / 3 halted / 4 refused), plus `examples/crontab.txt` and `examples/github-actions-daily.yml`.
- **Python API** for channel-loop: `propose`, `apply`, `rollback`, `external_proposal`.
- **Demo:** `make demo` (or `python -m ads_guardrail demo`) runs a SIMULATED 14-day period. It shows per-change caps clamping, a +$5,000/day request from another agent cut to +$50, the kill switch halting day 9 after 1 write, a no-op re-run, then an exact rollback of day 14 and of all 14 runs. Output is deterministic and matches the committed `docs/demo-summary.json` byte for byte (checked from a fresh clone).
- **Hero:** `docs/hero.png` (3200x1800) rendered by `scripts/render_hero.py` with headless Chrome from the demo's own summary. World-class-charts tokens.

## Tests

```
.venv/bin/python -m pytest
..................................................................       [100%]
```

This includes a Hypothesis property test (600 examples) that no decision exceeds any cap, and a multi-day end-to-end property test that re-checks the audit log independently. Other tests cover the kill switch, rollback exactness, dry run never writing, the audit chain and tampering, the connectors, the warehouse, fixture determinism and the CLI. CI: `.github/workflows/ci.yml` runs the tests and the demo on Python 3.11, 3.12 and 3.13. No secrets.

From a fresh clone: demo 2.5s, tests 8.6s, dependency install about 5s with a warm cache. One install on this box took 28 minutes of wall time with only 25s of CPU. That was the sandbox network stalling while pip fetched build tools, not the repo.

Note: `make` is not installed on this box. I ran the Makefile's commands directly. The Makefile is included for users.

## Scrub results

- `gitleaks dir . --no-banner --redact`: no leaks found
- `gitleaks git . --no-banner --redact`: no leaks found
- `DRY=1 bash publish-private.sh . "x"`: scrub grep printed **clean** (DRY=1 stopped before any push)
- The first scrub run caught two false positives: synthetic money values that matched the phone-prefix pattern. I fixed this by picking fixture seed 20261102, which generates no such values, rather than editing data by hand. A test now guards it.
- No client names, no real IDs (Google customer `1112223333` and Meta `act_900000000000001` are synthetic), no real keys (`.env.example` has placeholders only), no /home paths.

## Final commit

HEAD before this receipt: `5b156c4162afb4f8a78f2ecac719d22f4da6e24c`. This receipt is committed on top of it. Run `git rev-parse HEAD` for the final hash.

## Simulated or untested

- All fixture data is SYNTHETIC. All demo numbers, the hero and README Results are SIMULATED (market simulator plus a fake in-memory account).
- The simulator assumes a move over 20% restarts learning. That is labeled as a rule of thumb, not a platform fact.
- The live Google Ads and Meta adapters were **never run against a real account**. `API_VERSION` constants (Google `v21`, Meta `v23.0`) must be checked before any live run. Google OAuth refresh is out of scope.
- README links to `github.com/willmather95/channel-loop`, which is being built separately and isn't published yet.

## Paid-spend ask

None. Nothing here needs paid APIs. Google Ads and Meta API access is free. A live pilot would need Will's yes, his own credentials, a dry-run week, and then a narrow allowlist. Ad spend would stay within the configured caps (demo config: $150/day per account of movement).
