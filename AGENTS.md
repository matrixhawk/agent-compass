# Repository Guidelines

## Scope

Agent Compass is a reusable coding-agent Skill. Its runtime is the Python 3.10+ standard-library bootstrapper at `skills/agent-compass/scripts/compass_bootstrap.py`; keep runtime dependencies at zero.

## Sources of truth

- `skills/agent-compass/scripts/compass_bootstrap.py`: behavior, version, state schema, and safety checks.
- `tests/test_agent_compass.py` and `.github/workflows/ci.yml`: regression contract and supported CI matrix.
- `skills/agent-compass/SKILL.md`: agent-facing workflow contract.
- `README.md` and `README.zh-CN.md`: human-facing documentation; keep them semantically aligned.

## Invariants

- Allow exactly one primary framework: Matt Skills, OpenSpec, Trellis, or Superpowers.
- Never report `ready` until every machine-verifiable and required user-confirmed gate passes.
- Raise every `BootstrapError` with an English variant, or add its static message to `ERROR_MESSAGE_ENGLISH`; a regression test enforces this. Keep the failing command's output tail in command errors.
- Keep `--doctor` read-only and `--dry-run` non-mutating with status `not_installed`.
- Preserve symlink/path-escape rejection, atomic managed writes, repository locking, the post-lock conflict check, and fail-closed plugin detection.
- Pin npm packages to exact semver and source installs to exact revisions; only `superpowers@openai-curated` is the official Codex integration.
- Never automatically uninstall or migrate conflicting or legacy frameworks.

## Change workflow

- Make the smallest scoped change and add or update regression tests for behavior changes.
- Keep user-visible behavior synchronized across the bootstrapper, Skill contract, and both READMEs.
- When changing compatibility, update `VERSION`, schema handling, documentation, and release metadata together.
- Run `python3 -m py_compile skills/agent-compass/scripts/compass_bootstrap.py`, `python3 -m unittest discover -s tests -v`, and `git diff --check`.
- Do not commit generated caches or local `.agent-compass.json` / `.agent-compass.lock` files.
- When publication is requested, push directly to `main`; do not open a PR unless the owner asks for one.
