# Agent Compass

[English](README.md) | [简体中文](README.zh-CN.md)

Choose how your coding agent works. Agent Compass first checks whether the project warrants a framework, selects one of four supported options by user need, verifies the result, and records honest readiness state.

## Frameworks in v0.6.1

| Need | Framework |
|---|---|
| On-demand debugging, review, design, and TDD | Matt Pocock Skills |
| Reviewable specifications before implementation | OpenSpec |
| Cross-session project context and task continuity | Trellis |
| Strict planning, implementation, and validation for a complex task | Superpowers |

Spec Kit, BMAD, Compound Engineering, and Ponytail were removed because their user value overlapped too heavily with the retained choices. Agent Compass still detects their legacy traces and refuses to install over them.

## Install Agent Compass

The former Skill and slash-command name are retired. There is no compatibility alias; use only `agent-compass` and `/agent-compass`. Legacy artifacts are detected and must be handled explicitly before installation.

The bootstrapper requires Python 3.10 or newer. Project-Skills and Trellis installations require Node.js 18 or newer; OpenSpec requires Node.js 20.19 or newer.

From the extracted directory:

```bash
npx skills@1.5.9 add /path/to/agent-compass \
  --skill agent-compass \
  --agent codex \
  --copy \
  --yes
```

Replace `codex` with `claude-code`, `cursor`, or `opencode` as needed.

## Use

```text
/agent-compass
```

Agent Compass asks:

```text
Does this task meet all three conditions: one-off, low-risk,
and no need for a strict engineering workflow?

1. Yes, all three conditions apply
2. No, at least one does not apply, or I am unsure
```

Only answer 1 skips framework selection. A complex or higher-risk one-off task can still select Superpowers. Otherwise it asks:

```text
What is the primary need?

1. Agent-led, on-demand debugging, review, and TDD skills
2. Reviewable specifications before each implementation
3. Project rules, task progress, and decisions across sessions
4. A strict plan, implementation, and verification flow for one complex task
5. Do not install a framework
```

For choices 1–4 it then asks whether AI should default to the smallest necessary change. The questionnaire, operation summary, completion phase, and doctor output follow the locale; use `--language zh` or `--language en` to override it.

Direct selection is also supported:

```text
/agent-compass matt
/agent-compass openspec --minimal
/agent-compass trellis --harness codex,cursor
/agent-compass superpowers --integration project-skills
/agent-compass none
```

## Minimal mode

`--minimal` is not a fifth framework. It writes a small managed rule to `AGENTS.md` or `CLAUDE.md` requiring the smallest correct change while preserving correctness, lifecycle management, synchronization, resource ownership, diagnostics, security, compatibility, and tests.

It is safe to combine with any retained framework and is idempotent.

## Installation behavior

### Matt Pocock Skills

Resolves one upstream commit, checks out that exact revision in a temporary directory, installs only six selected project Skills, and rewrites `skills-lock.json` with the remote source and commit. It then records `pending`. Run `setup-matt-pocock-skills`, then finalize:

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py matt \
  --project-root . \
  --harness codex \
  --finalize \
  --yes
```

### OpenSpec

Uses a resolved exact `@fission-ai/openspec` npm version, initializes selected hosts, and verifies real `openspec-*/SKILL.md` outputs with checksums. Dist-tags and version ranges are rejected when a version is supplied.

### Trellis

Uses a resolved exact `@mindfoldhq/trellis` npm version, initializes requested hosts, and verifies core and platform files. It does not install Trellis globally.

Trellis file installation is not treated as full readiness. Agent Compass records one of:

- `installed`: files exist, but a readiness gate is unknown
- `activation_pending`: host activation or Hook approval remains
- `bootstrap_pending`: `00-bootstrap-guidelines` still needs to produce project-specific specs
- `ready`: installation, activation, and bootstrap are complete

For Codex, enable hooks and approve the Trellis Hook through `/hooks`. After completing activation and the bootstrap workflow, finalize explicitly:

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py trellis \
  --project-root . \
  --harness codex \
  --finalize \
  --confirm-trellis-activation \
  --confirm-trellis-bootstrap \
  --yes
```

### Superpowers

Uses the `superpowers@openai-curated` Codex plugin when Codex is the only selected host; a same-named plugin from a custom marketplace is not accepted as the official integration. Other hosts default to a whitelist of project-local Skills installed from one exact upstream commit with matching lock provenance. The project-local mode is explicitly marked as a compatibility fallback and does not claim official plugin hooks.

When official mode is explicitly chosen for a non-Codex host, Agent Compass records `pending` until the host installation is complete and explicitly confirmed:

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py superpowers \
  --project-root . \
  --harness cursor \
  --integration official \
  --finalize \
  --confirm-superpowers-installation \
  --yes
```

This confirmation is recorded as user-attested because the host state cannot be machine-verified by Agent Compass.

## Read-only health check

Diagnose the recorded setup without modifying project or host state:

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py \
  --project-root . \
  --doctor \
  --language en
```

The command returns 0 only for a structurally valid, conflict-free, recorded `ready` setup whose current artifacts, checksums, plugin identities, and managed rules match state. Missing or legacy state, a repository lock, mixed frameworks, tampering, incomplete gates, or failed verification return 1. `--language` is the only non-diagnostic behavior option accepted with `--doctor`; it changes output language but never state.

Every error on the diagnosis path follows `--language`, including the embedded failure cause. When a downstream command fails, the error carries the tail of that command's own output, so the root cause is not swallowed.

A failed Codex plugin inventory stays fail-closed: the diagnosis never claims the repository is clean. It reports that a framework installed as a plugin cannot be ruled out, includes the cause, and still returns 1.

## Safety properties

- one primary framework per repository
- no automatic uninstall or migration
- legacy framework conflict detection
- recursive symbolic-link and path-escape rejection before upstream installers run
- atomic writes for Agent-Compass-managed files
- rollback of Agent-Compass-managed files on failure
- exact semver, pinned source checkout/lock revision, plugin identity, and artifact checksum recording
- post-install verification before `ready`
- multi-host verification
- per-host readiness that cannot be bypassed by finalizing a host subset
- phased Trellis readiness without false success
- fail-closed Codex plugin conflict detection
- repository mutation lock plus a second conflict check inside the lock
- dry runs that report `not_installed`, never write state, and never claim successful installation
- read-only, tamper-detecting health diagnosis

State is stored in `.agent-compass.json` schema 7. Schemas 5 and 6 remain readable only for migration and must be finalized into schema 7 before health checks can report `ready`.

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

Run a dry run:

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py openspec \
  --project-root . \
  --harness codex \
  --minimal \
  --yes \
  --dry-run
```
