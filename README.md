# Agent Compass

[English](README.md) | [简体中文](README.zh-CN.md)

Choose how your coding agent works. Agent Compass asks one plain-language question, installs one of four supported frameworks, verifies the result, and records the project state.

## What remains in v0.5.0

| Need | Framework |
|---|---|
| You lead; AI helps when asked | Matt Pocock Skills |
| AI proposes a plan before coding | OpenSpec |
| AI remembers project rules over time | Trellis |
| AI plans and completes the whole task | Superpowers |

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
你希望 AI 主要怎么工作？

1. 我来主导，AI 按需帮我
2. AI 先给方案，我确认后再做
3. AI 长期记住这个项目的规则
4. AI 自己规划并完成整个任务
5. 不安装任何框架
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

Uses a resolved exact `@mindfoldhq/trellis` npm version, initializes all requested hosts, and verifies both core and platform files. It does not install Trellis globally.

### Superpowers

Uses the official Codex plugin when Codex is the only selected host. Other hosts default to project-local Skills. The project-local mode is explicitly marked as a compatibility fallback and does not claim official plugin hooks.

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
- `ready` and `pending` state distinction

State is stored in `.agent-compass.json` schema 5.

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
