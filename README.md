# Agent Compass

[English](README.md) | [简体中文](README.zh-CN.md)

Choose how your coding agent works. Agent Compass first checks whether the project warrants a framework, selects one of four supported options by user need, verifies the result, and records honest readiness state.

## Frameworks in v0.6.0

| Need | Framework |
|---|---|
| On-demand debugging, review, design, and TDD | Matt Pocock Skills |
| Reviewable specifications before implementation | OpenSpec |
| Cross-session project context and task continuity | Trellis |
| Strict planning, implementation, and validation for a complex task | Superpowers |

Spec Kit, BMAD, Compound Engineering, and Ponytail were removed because their user value overlapped too heavily with the retained choices. Agent Compass still detects their legacy traces and refuses to install over them.

## Install Agent Compass

The former Skill and slash-command name are retired. There is no compatibility alias; use only `agent-compass` and `/agent-compass`. Legacy artifacts are detected and must be handled explicitly before installation.


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
这个项目是否需要长期维护或严格工程流程？

1. 否，只是短期、低风险或一次性任务
2. 是，或者我还不确定
```

Short, low-risk work defaults to no framework. Otherwise it asks:

```text
你最想解决哪类问题？

1. 由我主导，按需调用调试、评审和 TDD Skills
2. 每次变更先形成可评审的规格，再开始实现
3. 跨会话接续项目规范、任务进度和设计决策
4. 让单次复杂任务遵循严格的规划、实现和验证流程
5. 仍然不安装任何框架
```

For choices 1–4 it then asks whether AI should default to the smallest necessary change.

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

Installs six selected project Skills and records `pending`. Run `setup-matt-pocock-skills`, then finalize:

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py matt \
  --project-root . \
  --harness codex \
  --finalize \
  --yes
```

### OpenSpec

Uses a resolved exact `@fission-ai/openspec` npm version, initializes selected hosts, and verifies the OpenSpec directories and generated Skills.

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

Uses the official Codex plugin when Codex is the only selected host. Other hosts default to project-local Skills. The project-local mode is explicitly marked as a compatibility fallback and does not claim official plugin hooks.

## Read-only health check

Diagnose the recorded setup without modifying project or host state:

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py \
  --project-root . \
  --doctor
```

The command returns 0 only for a recorded and verified `ready` setup. Missing state, legacy Trellis readiness, incomplete gates, or failed verification return 1.

## Safety properties

- one primary framework per repository
- no automatic uninstall or migration
- legacy framework conflict detection
- symbolic-link and path-escape rejection
- atomic writes for Agent-Compass-managed files
- rollback of Agent-Compass-managed files on failure
- exact version or revision recording
- post-install verification before `ready`
- multi-host verification
- phased Trellis readiness without false success
- read-only health diagnosis

State is stored in `.agent-compass.json` schema 6. Schema 5 remains readable for compatibility.

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
