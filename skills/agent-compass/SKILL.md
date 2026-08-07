---
name: agent-compass
description: Choose, install, finalize, diagnose, or repair one repository-level AI coding workflow among Matt Pocock Skills, OpenSpec, Trellis, and Superpowers. Use when a user invokes Agent Compass, asks which coding-agent framework fits a project, wants specification/TDD/project-memory workflow support, needs to avoid mixing agent frameworks, or wants an existing Agent Compass installation verified—even if they do not name Agent Compass. Also use for Chinese requests such as 选择编码 Agent 工作流、安装规格或 TDD 流程、检查框架冲突。
---

# Agent Compass

Install exactly one primary framework. Keep framework jargon out of automatic selection, explain maintenance cost, and never report `ready` before all verifiable and user-confirmed gates pass.

## Invocation

```text
/agent-compass
/agent-compass matt
/agent-compass openspec --minimal
/agent-compass trellis --harness codex,cursor
/agent-compass superpowers --integration project-skills
/agent-compass none
/agent-compass --doctor
```

Aliases:

```text
open-spec  → openspec
mattpocock → matt
```

On hosts that invoke Skills with `$`, use `$agent-compass` with the same arguments.

## Automatic selection

When no framework is supplied, use the requested language from `--language`, or the locale when it is `auto`. First ask whether all three skip conditions apply. In Chinese:

```text
这个任务是否同时满足：一次性、低风险、无需严格工程流程？

1. 是，三项都满足
2. 否，任一项不满足，或者我不确定
```

In English:

```text
Does this task meet all three conditions: one-off, low-risk, and no need for a strict engineering workflow?

1. Yes, all three conditions apply
2. No, at least one does not apply, or I am unsure
```

Map answer 1 to `none` and stop. For answer 2, ask for the primary need in the same language:

```text
你最想解决哪类问题？

1. 由我主导，按需调用调试、评审和 TDD Skills
2. 每次变更先形成可评审的规格，再开始实现
3. 跨会话接续项目规范、任务进度和设计决策
4. 让单次复杂任务遵循严格的规划、实现和验证流程
5. 仍然不安装任何框架
```

Internal mapping:

| Need | Framework |
|---|---|
| On-demand engineering Skills | Matt Pocock Skills |
| Reviewable specs before implementation | OpenSpec |
| Long-lived project context and task continuity | Trellis |
| Strict single-task engineering workflow | Superpowers |
| No framework | None |

After selecting a framework, ask whether AI should default to the smallest necessary change. This maps to `--minimal`; do not ask after `none`.

When the user supplies a framework name directly, skip all selection questions. Enable minimal behavior only with `--minimal`.

## Supported hosts

```text
codex
claude-code
cursor
opencode
```

Allow repeated or comma-separated `--harness`. If absent, prefer the current host or inspect project markers. Ask one host question only when detection remains ambiguous.

## Framework boundaries

### Matt Pocock Skills

Install these project-local Skills only:

```text
setup-matt-pocock-skills
diagnosing-bugs
code-review
codebase-design
tdd
handoff
```

Record `pending`, invoke `setup-matt-pocock-skills`, complete its repository questions, then run `matt --finalize --yes`. Report success only after `ready`.

Resolve the upstream `HEAD`, fetch and detach-checkout that exact commit in a temporary directory, install from the pinned checkout, and rewrite `skills-lock.json` entries to the remote source plus that commit. Finalize and doctor must reject missing, movable, or inconsistent lock revisions.

### OpenSpec

Use an exact resolved official npm version. Verify `openspec/`, `openspec/specs/`, `openspec/changes/`, and generated `openspec-*` Skills for every requested host.

### Trellis

Use an exact resolved official npm version. Verify core and requested host files, but treat file installation separately from activation and project bootstrap.

Use these readiness phases:

| Status | Meaning |
|---|---|
| `installed` | Files exist, but a readiness gate is unknown |
| `activation_pending` | Host activation or Hook approval remains |
| `bootstrap_pending` | Initial project-specific spec bootstrap remains |
| `ready` | Installation, activation, and bootstrap are complete |

For Codex, require the user to enable hooks and approve the Trellis Hook through `/hooks`. Never infer that approval from file presence. Require explicit `--confirm-trellis-activation` with `--finalize`.

Require the initial `00-bootstrap-guidelines` workflow to be completed from the real codebase. Require explicit `--confirm-trellis-bootstrap` with `--finalize`. Preserve prior confirmed gates on later finalize runs.

Existing valid Trellis repositories may add another host without rewriting developer identity. If bootstrap state is not recorded, use `installed`, not `ready`. Surface the AGPL-3.0 notice without making a legal conclusion.

### Superpowers

Prefer the verifiable `superpowers@openai-curated` Codex plugin when Codex is the only host. Reject a same-named plugin from a custom Marketplace as the official integration. Otherwise default to project-local Skills unless the user requests official integration.

Project-Skills mode is a compatibility fallback without the full host plugin's SessionStart Hook or update lifecycle. Never call it equivalent to the official plugin.

For non-Codex official installations, keep each host `pending` until the user has completed the host's official installation instructions and explicitly runs `superpowers --integration official --finalize --confirm-superpowers-installation`. Record that this confirmation is user-attested rather than machine-verified.

## Health check

Run a read-only diagnosis with:

```bash
python3 scripts/compass_bootstrap.py --doctor --project-root <repo-root>
```

Do not combine `--doctor` with install, repair, minimal, finalize, confirmation, harness, integration, yes, or dry-run options. Allow `--language auto|zh|en` because it changes output only. Do not write state or repair files. Return 0 only when state is structurally valid, exactly one framework is present, managed artifacts and checksums match, host identity checks pass, and the recorded status is `ready`. Return 1 for missing state, a repository lock, pending gates, legacy readiness, conflicts, tampering, or failed checks.

Report failures in the requested language, including the embedded cause, and keep the failing command's own output tail in the message. When the Codex plugin inventory cannot be read, report that a plugin-installed framework cannot be ruled out and give the reason; never downgrade that to a clean result.

## Minimal policy

`--minimal` writes an independent managed rule to the appropriate instruction file:

```text
Make the smallest correct change.
Do not add unnecessary abstractions, dependencies, or unrelated cleanup.
Never simplify away required correctness, lifecycle, synchronization,
resource ownership, diagnostics, security, compatibility, or tests.
```

Allow it alongside any retained framework. Keep it idempotent and never silently remove it.

## Removed frameworks

Do not install Spec Kit, BMAD, Compound Engineering, or Ponytail. Detect their legacy markers, lock sources, managed blocks, and OpenCode references. Stop on detection; never uninstall or migrate automatically.

## Safety rules

1. Select exactly one primary framework.
2. Never combine official and project-Skills variants of one framework.
3. Never delete, disable, migrate, commit, or push without an explicit request.
4. Stop on corrupt state, incomplete initialization, symbolic-link writes, path escape, or framework conflicts.
5. Treat installer success as insufficient; verify postconditions.
6. Write `ready` only after every required gate passes; otherwise record the exact phase and next actions.
7. Pin executable package versions or record exact revisions and checksums.
8. Do not reimplement an upstream framework's task lifecycle inside Agent Compass.
9. Serialize real mutations with `.agent-compass.lock` and repeat conflict detection after acquiring it.
10. Treat Codex plugin inventory failures as blocking; never assume absence when detection fails.
11. A dry run may describe intended commands, but must report `not_installed`, must not write state, and must not claim installation/readiness.

## Run the bootstrapper

Resolve the repository root and this Skill's script path, then run:

```bash
python3 scripts/compass_bootstrap.py <framework> \
  --project-root <repo-root> \
  --harness <host>
```

Forward supplied options:

```text
--doctor                              read-only health check
--harness <host>                      repeatable or comma-separated
--integration <mode>                  auto, official, project-skills
--language <auto|zh|en>               questionnaire, summary, and doctor language
--minimal                             add the minimal-change policy
--user <name>                         Trellis developer name
--trellis-version <ver>               exact npm version
--openspec-version <ver>              exact npm version
--repair                              allow Trellis repair; Trellis install only
--finalize                            verify pending initialization
--confirm-trellis-activation          confirm host activation with finalize
--confirm-trellis-bootstrap           confirm initial spec bootstrap with finalize
--confirm-superpowers-installation    attest non-Codex official install with finalize
--yes
--dry-run
--timeout <seconds>
```

Use `python` only when `python3` is unavailable.

## State

Write `.agent-compass.json` schema 7 with:

- framework, integration, hosts, scope, and independent minimal state
- overall status, per-host readiness, and Trellis bootstrap readiness
- exact versions, plugin identities, source revisions, and artifact checksums
- verification results, pending actions, activation notes, and limitations

Read schemas 5 and 6 only for migration. Require finalize into schema 7 before `--doctor` can report ready; never trust legacy aggregate readiness as per-host confirmation.

`none` skips the operation; it never uninstalls anything and cannot combine with `--minimal`.

## Completion response

Report only the selected framework and reason, host and integration, minimal mode, exact readiness phase, next activation/finalization action, and capability limits or failure reason.
