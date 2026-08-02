---
name: agent-compass
description: Safely choose, install, initialize, and verify one of four AI coding frameworks for the current repository. Use only when the user explicitly invokes /agent-compass or asks to install Matt Pocock Skills, OpenSpec, Trellis, or Superpowers.
argument-hint: "[auto|matt|openspec|trellis|superpowers|none] [--harness <host>] [--integration auto|official|project-skills] [--minimal] [--yes]"
disable-model-invocation: true
---

# Agent Compass

Expose one simple command that installs exactly one primary framework. Keep framework jargon out of the automatic selection flow, but clearly report installation scope, limitations, and whether initialization is `ready` or `pending`.

## Invocation

```text
/agent-compass
/agent-compass matt
/agent-compass openspec --minimal
/agent-compass trellis --harness codex,cursor
/agent-compass superpowers --integration project-skills
/agent-compass none
```

Alias:

```text
open-spec  → openspec
mattpocock → matt
```

On hosts that invoke Skills with `$`, use `$agent-compass` with the same arguments.

## Automatic selection

When no framework is supplied, ask exactly:

```text
你希望 AI 主要怎么工作？

1. 我来主导，AI 按需帮我
2. AI 先给方案，我确认后再做
3. AI 长期记住这个项目的规则
4. AI 自己规划并完成整个任务
5. 不安装任何框架
```

Internal mapping:

| Answer | Framework |
|---|---|
| 我来主导，AI 按需帮我 | Matt Pocock Skills |
| AI 先给方案，我确认后再做 | OpenSpec |
| AI 长期记住这个项目的规则 | Trellis |
| AI 自己规划并完成整个任务 | Superpowers |
| 不安装任何框架 | None |

After answers 1–4, ask:

```text
是否默认要求 AI 只做最小必要修改？
```

This maps to `--minimal`. It is a small repository rule, not another framework. Do not ask it after `none`.

When the user directly supplies a framework name, skip selection questions. Enable minimal behavior only when `--minimal` is supplied.

## Supported hosts

```text
codex
claude-code
cursor
opencode
```

`--harness` may be repeated or comma-separated. If absent, prefer the current host or inspect project markers. Ask one host-selection question only when detection is ambiguous.

## Framework boundaries

### Matt Pocock Skills

Use project-local Skills. Install only:

```text
setup-matt-pocock-skills
diagnosing-bugs
code-review
codebase-design
tdd
handoff
```

Installation initially becomes `pending`. In the same slash-command workflow:

1. Invoke `setup-matt-pocock-skills`.
2. Complete its repository-specific questions.
3. Run the bootstrapper with `matt --finalize --yes`.
4. Report success only after the state becomes `ready`.

### OpenSpec

Use official project initialization through an exact resolved npm version. Verify:

- `openspec/`
- `openspec/specs/`
- `openspec/changes/`
- generated `openspec-*` Skills for every requested host

### Trellis

Use official project initialization through an exact resolved npm version. Verify the Trellis core and every requested host integration. Existing valid Trellis repositories may add another host without rewriting developer identity. Surface the AGPL-3.0 notice without making a legal conclusion.

### Superpowers

Prefer the verifiable official Codex plugin when Codex is the only host. Otherwise default to project-local Skills unless the user explicitly requests official integration.

Project-Skills mode is a compatibility fallback. It does not provide the full host plugin's SessionStart hook or plugin update lifecycle. Never describe it as equivalent to the official plugin.

## Minimal policy

`--minimal` writes an independent, managed rule to the appropriate project instruction file:

```text
Make the smallest correct change.
Do not add unnecessary abstractions, dependencies, or unrelated cleanup.
Never simplify away required correctness, lifecycle, synchronization,
resource ownership, diagnostics, security, compatibility, or tests.
```

It may coexist with any retained framework. Re-running it must be idempotent. It must never silently remove an existing minimal policy.

## Removed frameworks

Spec Kit, BMAD, Compound Engineering, and Ponytail are no longer installable by Agent Compass. Their installation and verification code has been removed.

Still detect their legacy markers, lock-file sources, managed blocks, and OpenCode plugin references. If found, stop rather than installing another framework on top. Never uninstall or migrate them automatically.

## Non-negotiable safety rules

1. Select exactly one primary framework.
2. Never combine official and project-Skills variants of the same framework.
3. Never delete, disable, migrate, commit, or push without an explicit request.
4. Stop on corrupt state, incomplete initialization, symbolic-link writes, path escape, or framework conflicts.
5. An installer exit code is not proof of readiness; verify postconditions.
6. Write `ready` only after verification. Otherwise write `pending` with exact next actions.
7. Pin executable package versions or record exact resolved revisions and checksums.

## Run the bootstrapper

Resolve the repository root and this Skill's script path, then run:

```bash
python3 scripts/compass_bootstrap.py <framework> \
  --project-root <repo-root> \
  --harness <host>
```

Forward supplied options:

```text
--harness <host>           repeatable or comma-separated
--integration <mode>       auto, official, or project-skills
--minimal                  add the minimal-change policy
--user <name>              Trellis developer name
--trellis-version <ver>    exact npm version
--openspec-version <ver>   exact npm version
--repair                   allow Trellis repair
--finalize                 verify pending initialization
--yes
--dry-run
--timeout <seconds>
```

Use `python` only when `python3` is unavailable.

## State

The bootstrapper writes `.agent-compass.json` schema 5 containing:

- `ready` or `pending`
- framework, integration, and harnesses
- independent `minimal` state
- resolved versions and source revisions
- copied Skill checksums
- verification results
- pending actions, activation notes, and limitations

`none` only skips the operation. It does not uninstall anything and cannot be combined with `--minimal`.

## Completion response

Report only:

- selected framework and why it matched
- host and integration
- whether minimal mode is enabled
- `ready` or `pending`
- any exact activation or finalization step
- capability limits or failure reason
