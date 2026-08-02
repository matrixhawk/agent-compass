# Agent Compass

[English](README.md) | [简体中文](README.zh-CN.md)

选择你的编码 Agent 应该如何工作。Agent Compass 只用一个自然语言问题了解你的偏好，随后安装四种受支持框架中的一种、验证安装结果，并记录项目状态。

## v0.5.0 保留的框架

| 你的需求 | 框架 |
|---|---|
| 由你主导，AI 按需协助 | Matt Pocock Skills |
| AI 先提出方案，确认后再编码 | OpenSpec |
| AI 长期记住项目规则 | Trellis |
| AI 自主规划并完成整个任务 | Superpowers |

Spec Kit、BMAD、Compound Engineering 和 Ponytail 已被移除，因为它们提供的用户价值与上述框架重叠较多。Agent Compass 仍会检测这些框架遗留的文件，并拒绝在存在冲突时继续安装。

## 安装 Agent Compass

旧的 Skill 名称和斜杠命令均已停用，且不提供兼容别名。请仅使用 `agent-compass` 和 `/agent-compass`。安装前必须明确处理检测到的旧版文件。

在解压后的目录中运行：

```bash
npx skills@1.5.9 add /path/to/agent-compass \
  --skill agent-compass \
  --agent codex \
  --copy \
  --yes
```

根据所用工具，将 `codex` 替换为 `claude-code`、`cursor` 或 `opencode`。

## 使用

```text
/agent-compass
```

Agent Compass 会询问：

```text
你希望 AI 主要怎么工作？

1. 我来主导，AI 按需帮我
2. AI 先给方案，我确认后再做
3. AI 长期记住这个项目的规则
4. AI 自己规划并完成整个任务
5. 不安装任何框架
```

选择 1–4 后，它还会询问 AI 是否应默认采用最小必要改动。

也可以直接指定框架：

```text
/agent-compass matt
/agent-compass openspec --minimal
/agent-compass trellis --harness codex,cursor
/agent-compass superpowers --integration project-skills
/agent-compass none
```

## 最小改动模式

`--minimal` 不是第五种框架。它会向 `AGENTS.md` 或 `CLAUDE.md` 写入一小段托管规则，要求 AI 在保证正确性、生命周期管理、同步、资源所有权、诊断、安全、兼容性和测试的前提下，只做最小必要改动。

该模式可以与任一保留框架组合使用，并且支持幂等执行。

## 各框架的安装行为

### Matt Pocock Skills

安装六个精选的项目级 Skills，并将状态记录为 `pending`。运行 `setup-matt-pocock-skills` 后，再执行：

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py matt \
  --project-root . \
  --harness codex \
  --finalize \
  --yes
```

### OpenSpec

解析并固定 `@fission-ai/openspec` 的准确版本，为选定宿主完成初始化，然后验证 OpenSpec 目录及生成的 Skills。

### Trellis

解析并固定 `@mindfoldhq/trellis` 的准确版本，为所有指定宿主完成初始化，并验证核心文件与平台文件。它不会全局安装 Trellis。

### Superpowers

仅选择 Codex 时使用官方 Codex 插件。其他宿主默认使用项目级 Skills；项目级模式会明确标记为兼容性回退，不会声称支持官方插件钩子。

## 安全特性

- 每个仓库只允许一个主要框架
- 不自动卸载或迁移已有框架
- 检测旧框架冲突
- 拒绝符号链接和路径逃逸
- 对 Agent Compass 管理的文件执行原子写入
- 失败时回滚 Agent Compass 管理的文件
- 记录准确版本或修订号
- 安装后验证通过才标记为 `ready`
- 支持多宿主验证
- 区分 `ready` 和 `pending` 状态

状态保存在 schema 版本为 5 的 `.agent-compass.json` 中。

## 开发

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

执行模拟安装：

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py openspec \
  --project-root . \
  --harness codex \
  --minimal \
  --yes \
  --dry-run
```
