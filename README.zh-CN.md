# Agent Compass

[English](README.md) | [简体中文](README.zh-CN.md)

选择你的编码 Agent 应该如何工作。Agent Compass 会先判断项目是否真的需要框架，再按实际需求选择四种受支持方案中的一种、验证安装结果，并诚实记录就绪状态。

## v0.6.0 支持的框架

| 你的需求 | 框架 |
|---|---|
| 按需使用调试、评审、设计和 TDD 能力 | Matt Pocock Skills |
| 实现前先形成可评审的规格 | OpenSpec |
| 跨会话保存项目上下文并接续任务 | Trellis |
| 让复杂任务遵循严格的规划、实现和验证流程 | Superpowers |

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
这个项目是否需要长期维护或严格工程流程？

1. 否，只是短期、低风险或一次性任务
2. 是，或者我还不确定
```

短期低风险任务默认不安装框架。其他情况继续询问：

```text
你最想解决哪类问题？

1. 由我主导，按需调用调试、评审和 TDD Skills
2. 每次变更先形成可评审的规格，再开始实现
3. 跨会话接续项目规范、任务进度和设计决策
4. 让单次复杂任务遵循严格的规划、实现和验证流程
5. 仍然不安装任何框架
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

仅完成文件安装不会再被视为完全就绪。Agent Compass 会记录以下阶段之一：

- `installed`：文件存在，但有就绪门槛无法确定
- `activation_pending`：仍需完成宿主激活或 Hook 审批
- `bootstrap_pending`：仍需通过 `00-bootstrap-guidelines` 生成项目专属规范
- `ready`：安装、激活和 bootstrap 均已完成

在 Codex 中需要启用 hooks，并通过 `/hooks` 审批 Trellis Hook。完成激活和 bootstrap 后显式 finalize：

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

仅选择 Codex 时使用官方 Codex 插件。其他宿主默认使用项目级 Skills；项目级模式会明确标记为兼容性回退，不会声称支持官方插件钩子。

## 只读健康检查

在不修改项目或宿主状态的情况下检查当前安装：

```bash
python3 skills/agent-compass/scripts/compass_bootstrap.py \
  --project-root . \
  --doctor
```

只有已记录且验证为 `ready` 的安装返回 0。状态缺失、旧版 Trellis 状态、未完成门槛或验证失败均返回 1。

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
- Trellis 分阶段就绪状态，不误报成功
- 只读健康诊断

状态保存在 schema 版本为 6 的 `.agent-compass.json` 中，同时兼容读取 schema 5。

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
