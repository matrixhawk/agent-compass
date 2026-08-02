#!/usr/bin/env python3
"""Safely select, install, verify, and record one AI coding framework.

Design goals:
- one primary framework per repository;
- official integrations when they can be automated and verified;
- explicit, accurately-labelled project-skills fallback;
- no false success: READY is written only after post-install verification;
- reject symlink/path-escape writes;
- pin executable package versions and record source revisions/checksums;
- never delete an existing framework automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

VERSION = "0.5.0"
SKILLS_CLI_VERSION = "1.5.9"
TRELLIS_PACKAGE = "@mindfoldhq/trellis"
OPENSPEC_PACKAGE = "@fission-ai/openspec"

FRAMEWORKS = ("auto", "trellis", "openspec", "superpowers", "matt", "none")
FRAMEWORK_ALIASES = {
    "open-spec": "openspec",
    "mattpocock": "matt",
}
FRAMEWORK_CHOICES = (*FRAMEWORKS, *FRAMEWORK_ALIASES)
LEGACY_FRAMEWORKS = {"speckit", "bmad", "compound", "ponytail"}
HARNESSES = ("auto", "codex", "claude-code", "cursor", "opencode")
INTEGRATIONS = ("auto", "official", "project-skills")
STATE_FILE = ".agent-compass.json"
RETIRED_STATE_FILE = ".agent-framework.json"
RETIRED_MANAGED_PREFIX = "<!-- agent-framework-selector:"
RETIRED_SKILL_NAME = "agent-framework"
MANAGED_START_PREFIX = "<!-- agent-compass:start"
MANAGED_END = "<!-- agent-compass:end -->"
MINIMAL_START = "<!-- agent-compass:minimal:start -->"
MINIMAL_END = "<!-- agent-compass:minimal:end -->"
DEFAULT_TIMEOUT_SECONDS = 600

SOURCE_REPOSITORIES = {
    "superpowers": "obra/superpowers",
    "matt": "mattpocock/skills",
}

LEGACY_SOURCE_HINTS = {
    "speckit": "github/spec-kit",
    "bmad": "bmad-method",
    "compound": "everyinc/compound-engineering-plugin",
    "ponytail": "dietrichgebert/ponytail",
}

SUPERPOWERS_SKILLS = (
    "brainstorming",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "receiving-code-review",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
)

MATT_SKILLS = (
    "setup-matt-pocock-skills",
    "diagnosing-bugs",
    "code-review",
    "codebase-design",
    "tdd",
    "handoff",
)

EXPECTED_SKILLS = {
    "superpowers": SUPERPOWERS_SKILLS,
    "matt": MATT_SKILLS,
}

SKILL_ROOT_BY_HARNESS = {
    "codex": Path(".agents/skills"),
    "claude-code": Path(".claude/skills"),
    "cursor": Path(".agents/skills"),
    "opencode": Path(".agents/skills"),
}

TRELLIS_PLATFORM_FLAG = {
    "codex": "--codex",
    "claude-code": "--claude",
    "cursor": "--cursor",
    "opencode": "--opencode",
}

TRELLIS_PLATFORM_PATHS = {
    "codex": (
        Path(".codex/agents"),
        Path(".codex/skills"),
        Path(".codex/hooks"),
        Path("AGENTS.md"),
        Path(".agents/skills"),
    ),
    "claude-code": (
        Path(".claude/commands/trellis"),
        Path(".claude/agents"),
        Path(".claude/skills"),
        Path(".claude/hooks"),
        Path(".agents/skills"),
    ),
    "cursor": (
        Path(".cursor/commands"),
        Path(".cursor/agents"),
        Path(".cursor/skills"),
        Path(".cursor/hooks"),
        Path(".agents/skills"),
    ),
    "opencode": (
        Path(".opencode/commands/trellis"),
        Path(".opencode/agents"),
        Path(".opencode/skills"),
        Path(".opencode/plugins"),
        Path(".agents/skills"),
    ),
}

PLUGIN_FRAMEWORK_NAMES = {
    "superpowers": {"superpowers"},
}

OPENSPEC_TOOL_ID = {
    "codex": "codex",
    "claude-code": "claude",
    "cursor": "cursor",
    "opencode": "opencode",
}

OPENSPEC_SKILL_ROOT = {
    "codex": Path(".codex/skills"),
    "claude-code": Path(".claude/skills"),
    "cursor": Path(".cursor/skills"),
    "opencode": Path(".opencode/skills"),
}

class BootstrapError(RuntimeError):
    """Expected user-actionable bootstrap failure."""


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class InstallOutcome:
    framework: str
    integration: str
    status: str = "ready"  # ready | pending
    created_or_managed: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    source_revisions: dict[str, str] = field(default_factory=dict)
    checksums: dict[str, str] = field(default_factory=dict)
    verification: dict[str, bool] = field(default_factory=dict)
    pending_actions: list[str] = field(default_factory=list)
    activation_notes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    external_commands_ran: bool = False


@dataclass
class Snapshot:
    path: Path
    existed: bool
    data: bytes | None
    mode: int | None


@dataclass
class MutationTracker:
    """Tracks whether an upstream mutating command has started."""

    external_command_started: bool = False

    def mark(self, *, dry_run: bool) -> None:
        if not dry_run:
            self.external_command_started = True


class ManagedFileTransaction:
    """Rollback only files Agent Compass writes itself.

    Upstream installers may write other files. Those changes cannot be rolled back
    safely without knowing the upstream tool's semantics, so failures report that
    partial upstream changes may remain.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._snapshots: dict[Path, Snapshot] = {}
        self._committed = False

    def snapshot(self, path: Path) -> None:
        path = path.absolute()
        if path in self._snapshots:
            return
        ensure_safe_path(self.root, path, for_write=True)
        if lstat_exists(path):
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise BootstrapError(f"拒绝修改非普通文件：{path}")
            self._snapshots[path] = Snapshot(path, True, path.read_bytes(), stat.S_IMODE(st.st_mode))
        else:
            self._snapshots[path] = Snapshot(path, False, None, None)

    def commit(self) -> None:
        self._committed = True

    def rollback(self) -> None:
        for snapshot in reversed(list(self._snapshots.values())):
            try:
                if snapshot.existed:
                    assert snapshot.data is not None
                    atomic_write_bytes(self.root, snapshot.path, snapshot.data, mode=snapshot.mode)
                elif lstat_exists(snapshot.path):
                    ensure_safe_path(self.root, snapshot.path, for_write=True)
                    if snapshot.path.is_file():
                        snapshot.path.unlink()
            except Exception as exc:  # best-effort rollback, preserve original error
                print(f"警告：回滚 {snapshot.path} 失败：{exc}", file=sys.stderr)

    def __enter__(self) -> "ManagedFileTransaction":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None and not self._committed:
            self.rollback()
        return False


def lstat_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def ensure_safe_path(root: Path, path: Path, *, for_write: bool) -> None:
    """Reject path escapes and symlink components without following target paths."""
    root = root.resolve()
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise BootstrapError(f"目标不在项目目录内：{path}") from exc

    if absolute == root:
        if for_write and (not root.exists() or not root.is_dir()):
            raise BootstrapError(f"项目根目录不可写入：{root}")
        return

    current = root
    for part in relative.parts:
        current = current / part
        if not lstat_exists(current):
            continue
        st = current.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise BootstrapError(f"拒绝访问符号链接路径：{current}")

    parent = absolute.parent
    # resolve(strict=False) follows existing parents; all existing components were
    # checked above, so this is now a containment sanity check rather than trust.
    resolved_parent = parent.resolve(strict=False)
    if resolved_parent != root and root not in resolved_parent.parents:
        raise BootstrapError(f"目标父目录逃逸项目范围：{path}")

    if for_write and lstat_exists(absolute):
        st = absolute.lstat()
        if not stat.S_ISREG(st.st_mode) and not stat.S_ISDIR(st.st_mode):
            raise BootstrapError(f"拒绝覆盖特殊文件：{path}")


def safe_read_text(root: Path, path: Path) -> str:
    ensure_safe_path(root, path, for_write=False)
    if not lstat_exists(path):
        raise BootstrapError(f"文件不存在：{path}")
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise BootstrapError(f"拒绝读取非普通文件：{path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapError(f"文件不是有效 UTF-8：{path}") from exc


def atomic_write_bytes(root: Path, path: Path, data: bytes, *, mode: int | None = None) -> None:
    ensure_safe_path(root, path, for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_safe_path(root, path.parent, for_write=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        ensure_safe_path(root, path, for_write=True)
        os.replace(temp_path, path)
    finally:
        if lstat_exists(temp_path):
            temp_path.unlink()


def atomic_write_text(root: Path, path: Path, text: str, *, mode: int | None = None) -> None:
    atomic_write_bytes(root, path, text.encode("utf-8"), mode=mode)


def prompt_yes_no(question: str, *, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "是", "好", "继续"}:
            return True
        if answer in {"n", "no", "否", "不", "取消"}:
            return False
        print("请输入 y 或 n。")


def prompt_choice(question: str, options: Sequence[str]) -> int:
    print(question)
    for index, option in enumerate(options, start=1):
        print(f"  {index}. {option}")
    while True:
        answer = input("请选择编号：").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        print(f"请输入 1-{len(options)}。")


def choose_framework_interactively() -> tuple[str, bool]:
    """Choose one primary workflow using plain collaboration language."""
    choice = prompt_choice(
        "你希望 AI 主要怎么工作？",
        (
            "我来主导，AI 按需帮我",
            "AI 先给方案，我确认后再做",
            "AI 长期记住这个项目的规则",
            "AI 自己规划并完成整个任务",
            "不安装任何框架",
        ),
    )
    framework = {
        1: "matt",
        2: "openspec",
        3: "trellis",
        4: "superpowers",
        5: "none",
    }[choice]
    minimal = False
    if framework != "none":
        minimal = prompt_yes_no(
            "是否默认要求 AI 只做最小必要修改？",
            default=False,
        )
    return framework, minimal


def canonical_framework(value: str) -> str:
    return FRAMEWORK_ALIASES.get(value, value)


def find_project_root(start: Path) -> Path:
    start = start.expanduser().resolve()
    probe = start
    while True:
        if (probe / ".git").exists():
            return probe
        if probe.parent == probe:
            return start
        probe = probe.parent


def detect_harness(root: Path) -> str | None:
    env_hints = {
        "CODEX_HOME": "codex",
        "CLAUDECODE": "claude-code",
        "CLAUDE_CODE": "claude-code",
        "OPENCODE": "opencode",
    }
    for env_name, harness in env_hints.items():
        if os.environ.get(env_name):
            return harness

    markers = {
        "codex": root / ".codex",
        "claude-code": root / ".claude",
        "cursor": root / ".cursor",
        "opencode": root / ".opencode",
    }
    found = [name for name, path in markers.items() if lstat_exists(path)]
    return found[0] if len(found) == 1 else None


def normalize_harnesses(raw_values: Sequence[str] | None, root: Path) -> list[str]:
    values: list[str] = []
    for raw in raw_values or ["auto"]:
        values.extend(part.strip() for part in raw.split(",") if part.strip())
    if not values or values == ["auto"]:
        detected = detect_harness(root)
        if detected:
            return [detected]
        if not sys.stdin.isatty():
            raise BootstrapError(
                "无法自动识别 Agent，请传入 --harness codex|claude-code|cursor|opencode。"
            )
        print("选择 Agent：1) codex  2) claude-code  3) cursor  4) opencode")
        mapping = {"1": "codex", "2": "claude-code", "3": "cursor", "4": "opencode"}
        selected = mapping.get(input("编号：").strip())
        if not selected:
            raise BootstrapError("未选择有效 Agent。")
        return [selected]

    if "auto" in values:
        raise BootstrapError("--harness auto 不能和其他 Agent 同时使用。")
    invalid = sorted(set(values) - set(HARNESSES))
    if invalid:
        raise BootstrapError("不支持的 Agent：" + ", ".join(invalid))
    return list(dict.fromkeys(values))


def require_executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise BootstrapError(f"缺少必需命令：{name}")
    return path




def run(
    command: Sequence[str],
    *,
    cwd: Path,
    dry_run: bool,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    printable = shlex.join(str(item) for item in command)
    print(f"$ {printable}")
    if dry_run:
        return CommandResult(tuple(str(item) for item in command), 0, "", "")

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            [str(item) for item in command],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env=merged_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"命令超时（{timeout}s）：{printable}") from exc
    except OSError as exc:
        raise BootstrapError(f"无法执行命令：{printable}：{exc}") from exc

    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        raise BootstrapError(f"命令失败（退出码 {completed.returncode}）：{printable}")
    return CommandResult(
        tuple(str(item) for item in command),
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def run_json(
    command: Sequence[str],
    *,
    cwd: Path,
    dry_run: bool,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    result = run(command, cwd=cwd, dry_run=dry_run, timeout=timeout)
    if dry_run:
        return {}
    raw = result.stdout.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"命令未返回有效 JSON：{shlex.join(command)}") from exc


def parse_node_version(raw: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", raw)
    return (int(match.group(1)), int(match.group(2))) if match else None




def ensure_node_version(
    root: Path,
    minimum: tuple[int, int],
    *,
    dry_run: bool,
    timeout: int,
) -> None:
    require_executable("node")
    require_executable("npm")
    require_executable("npx")
    result = run(("node", "--version"), cwd=root, dry_run=dry_run, timeout=timeout)
    if not dry_run:
        version = parse_node_version(result.stdout)
        if version is None or version < minimum:
            required = f"{minimum[0]}.{minimum[1]}"
            raise BootstrapError(
                f"需要 Node.js >= {required}，当前为：{result.stdout.strip()}"
            )
    if sys.version_info < (3, 9):
        raise BootstrapError(f"需要 Python >= 3.9，当前为：{sys.version.split()[0]}")


def ensure_node_18(root: Path, *, dry_run: bool, timeout: int) -> None:
    ensure_node_version(root, (18, 0), dry_run=dry_run, timeout=timeout)


def ensure_node_20(root: Path, *, dry_run: bool, timeout: int) -> None:
    ensure_node_version(root, (20, 19), dry_run=dry_run, timeout=timeout)


def git_user(root: Path, *, dry_run: bool, timeout: int) -> str | None:
    if not shutil.which("git") or dry_run:
        return None
    try:
        completed = subprocess.run(
            ["git", "config", "user.name"],
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value or None


def reject_retired_name_traces(root: Path) -> None:
    """Reject artifacts from the retired pre-Agent-Compass name.

    The old slash command and Skill name are intentionally unsupported.
    We stop instead of guessing whether an old installation is safe to reuse.
    """
    retired_state = root / RETIRED_STATE_FILE
    if lstat_exists(retired_state):
        ensure_safe_path(root, retired_state, for_write=False)
        raise BootstrapError(
            f"检测到已废弃的 {RETIRED_STATE_FILE}。旧版名称不再受支持；"
            "请先人工确认旧安装状态并移走该文件，再使用 /agent-compass。"
        )

    for relative in (Path("AGENTS.md"), Path("CLAUDE.md")):
        path = root / relative
        if not lstat_exists(path):
            continue
        if RETIRED_MANAGED_PREFIX in safe_read_text(root, path):
            raise BootstrapError(
                f"{relative} 中存在旧版托管区块。旧版名称已废弃；"
                "请先人工清理或迁移，再使用 /agent-compass。"
            )

    retired_skill_paths = {
        root / "skills" / RETIRED_SKILL_NAME,
        root / ".agents/skills" / RETIRED_SKILL_NAME,
        root / ".claude/skills" / RETIRED_SKILL_NAME,
    }
    for path in retired_skill_paths:
        if not lstat_exists(path):
            continue
        ensure_safe_path(root, path, for_write=False)
        raise BootstrapError(
            f"检测到旧版 Skill 目录：{path.relative_to(root)}。"
            "旧版名称已废弃，请先移除旧 Skill，再安装 agent-compass。"
        )


def load_state(root: Path) -> dict[str, Any] | None:
    path = root / STATE_FILE
    if not lstat_exists(path):
        return None
    text = safe_read_text(root, path)
    try:
        state = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            f"{STATE_FILE} 已损坏，拒绝继续覆盖；请修复或移走该文件后重试。"
        ) from exc
    if not isinstance(state, dict):
        raise BootstrapError(f"{STATE_FILE} 顶层必须是 JSON 对象。")
    return state


def trellis_core_valid(root: Path) -> bool:
    required = (
        root / ".trellis/.version",
        root / ".trellis/workflow.md",
        root / ".trellis/config.yaml",
    )
    for path in required:
        if not lstat_exists(path):
            return False
        ensure_safe_path(root, path, for_write=False)
        if not path.is_file() or path.is_symlink():
            return False
    return True


def flatten_json_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from flatten_json_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from flatten_json_strings(item)


def detect_frameworks_from_lock(root: Path) -> set[str]:
    found: set[str] = set()
    for relative in (Path("skills-lock.json"), Path(".agents/skills-lock.json")):
        path = root / relative
        if not lstat_exists(path):
            continue
        try:
            data = json.loads(safe_read_text(root, path))
        except json.JSONDecodeError as exc:
            raise BootstrapError(f"技能锁文件损坏：{relative}") from exc
        joined = "\n".join(flatten_json_strings(data)).lower()
        for framework, source in SOURCE_REPOSITORIES.items():
            if source.lower() in joined:
                found.add(framework)
        for framework, source in LEGACY_SOURCE_HINTS.items():
            if source in joined:
                found.add(framework)
    return found


def detect_managed_instruction_frameworks(root: Path) -> set[str]:
    found: set[str] = set()
    pattern = re.compile(
        r"<!-- agent-compass:start\s+framework=([a-z-]+)(?:\s+integration=[a-z-]+)?\s*-->"
    )
    for relative in (Path("AGENTS.md"), Path("CLAUDE.md")):
        path = root / relative
        if not lstat_exists(path):
            continue
        text = safe_read_text(root, path)
        found.update(match.group(1) for match in pattern.finditer(text))
    return {
        item for item in found
        if item in FRAMEWORKS or item in LEGACY_FRAMEWORKS
    }





def detect_existing_frameworks(root: Path, *, repair: bool = False) -> set[str]:
    found: set[str] = set()
    state = load_state(root)
    if state:
        framework = canonical_framework(str(state.get("framework") or ""))
        if framework in FRAMEWORKS and framework not in {"auto", "none"}:
            found.add(framework)
        elif framework in LEGACY_FRAMEWORKS:
            found.add(framework)

    trellis_path = root / ".trellis"
    if lstat_exists(trellis_path):
        ensure_safe_path(root, trellis_path, for_write=False)
        if not trellis_path.is_dir():
            raise BootstrapError(".trellis 存在但不是目录。")
        if trellis_core_valid(root):
            found.add("trellis")
        elif not repair:
            raise BootstrapError(
                "检测到不完整的 .trellis 目录。为避免把半成品误判为成功，请先备份并清理，"
                "或确认后使用 --repair 让官方 CLI 尝试修复。"
            )

    openspec_path = root / "openspec"
    if lstat_exists(openspec_path):
        ensure_safe_path(root, openspec_path, for_write=False)
        if not openspec_path.is_dir() or openspec_path.is_symlink():
            raise BootstrapError("openspec 存在但不是安全目录。")
        markers = (openspec_path / "specs", openspec_path / "changes", openspec_path / "config.yaml")
        if all(lstat_exists(path) for path in markers[:2]) or lstat_exists(markers[2]):
            found.add("openspec")

    legacy_markers = {
        "speckit": root / ".specify",
        "bmad": root / "_bmad",
        "compound": root / ".compound-engineering",
    }
    for framework, path in legacy_markers.items():
        if not lstat_exists(path):
            continue
        ensure_safe_path(root, path, for_write=False)
        if path.is_symlink():
            raise BootstrapError(f"{path.relative_to(root)} 是符号链接，拒绝继续。")
        found.add(framework)

    opencode = root / "opencode.json"
    if lstat_exists(opencode):
        try:
            data = json.loads(safe_read_text(root, opencode))
        except json.JSONDecodeError as exc:
            raise BootstrapError("opencode.json 不是有效 JSON，无法完成遗留框架检测。") from exc
        joined = "\n".join(flatten_json_strings(data)).lower()
        if "ponytail" in joined:
            found.add("ponytail")
        if "compound-engineering" in joined:
            found.add("compound")

    found.update(detect_frameworks_from_lock(root))
    found.update(detect_managed_instruction_frameworks(root))
    return found


def instruction_files(root: Path, harnesses: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    if "claude-code" in harnesses:
        files.append(root / "CLAUDE.md")
    if any(harness != "claude-code" for harness in harnesses):
        files.append(root / "AGENTS.md")
    return list(dict.fromkeys(files))


def managed_block(framework: str, integration: str) -> str:
    if framework != "superpowers":
        raise BootstrapError(f"无需写入框架托管指令：{framework}")
    body = (
        "## Superpowers project-skills integration\n\n"
        "This repository uses the project-local Superpowers skills integration, not the full "
        "host plugin. At the start of every turn, before responding or asking clarifying "
        "questions, check whether an installed Superpowers skill applies. If one applies, "
        "invoke it and follow it. Do not combine it with another primary workflow framework."
    )
    return (
        f"{MANAGED_START_PREFIX} framework={framework} integration={integration} -->\n"
        f"{body}\n{MANAGED_END}"
    )


def write_managed_instruction(
    root: Path,
    harnesses: Sequence[str],
    framework: str,
    integration: str,
    *,
    dry_run: bool,
    transaction: ManagedFileTransaction,
) -> list[str]:
    block = managed_block(framework, integration)
    written: list[str] = []
    pattern = re.compile(
        re.escape(MANAGED_START_PREFIX) + r".*?" + re.escape(MANAGED_END),
        re.DOTALL,
    )
    for path in instruction_files(root, harnesses):
        ensure_safe_path(root, path, for_write=True)
        current = safe_read_text(root, path) if lstat_exists(path) else ""
        if MANAGED_START_PREFIX in current and MANAGED_END not in current:
            raise BootstrapError(f"{path.name} 中存在残缺的 Agent Compass 托管区块，拒绝自动覆盖。")
        if MANAGED_START_PREFIX in current:
            updated = pattern.sub(block, current, count=1)
        else:
            separator = "\n\n" if current.strip() else ""
            updated = current.rstrip() + separator + block + "\n"
        print(f"write {path.relative_to(root)}")
        if not dry_run:
            transaction.snapshot(path)
            mode = stat.S_IMODE(path.lstat().st_mode) if lstat_exists(path) else 0o644
            atomic_write_text(root, path, updated, mode=mode)
        written.append(str(path.relative_to(root)))
    return written

def minimal_block() -> str:
    body = (
        "## Minimal change policy\n\n"
        "- Make the smallest correct change that satisfies the request.\n"
        "- Do not add unnecessary abstractions, dependencies, or unrelated cleanup.\n"
        "- Never simplify away validation, lifecycle cleanup, synchronization, resource "
        "ownership, error handling, diagnostics, security, accessibility, compatibility, or tests."
    )
    return f"{MINIMAL_START}\n{body}\n{MINIMAL_END}"


def write_minimal_instruction(
    root: Path,
    harnesses: Sequence[str],
    *,
    dry_run: bool,
    transaction: ManagedFileTransaction,
) -> list[str]:
    block = minimal_block()
    written: list[str] = []
    pattern = re.compile(
        re.escape(MINIMAL_START) + r".*?" + re.escape(MINIMAL_END),
        re.DOTALL,
    )
    for path in instruction_files(root, harnesses):
        ensure_safe_path(root, path, for_write=True)
        current = safe_read_text(root, path) if lstat_exists(path) else ""
        if MINIMAL_START in current and MINIMAL_END not in current:
            raise BootstrapError(f"{path.name} 中存在残缺的 minimal 托管区块，拒绝自动覆盖。")
        if MINIMAL_START in current:
            updated = pattern.sub(block, current, count=1)
        else:
            separator = "\n\n" if current.strip() else ""
            updated = current.rstrip() + separator + block + "\n"
        print(f"write {path.relative_to(root)} (minimal)")
        if not dry_run:
            transaction.snapshot(path)
            mode = stat.S_IMODE(path.lstat().st_mode) if lstat_exists(path) else 0o644
            atomic_write_text(root, path, updated, mode=mode)
        written.append(str(path.relative_to(root)))
    return written



def resolve_npm_version(
    root: Path,
    package: str,
    requested: str | None,
    *,
    dry_run: bool,
    timeout: int,
) -> str:
    if requested:
        if not re.fullmatch(r"[0-9A-Za-z.+_-]+", requested):
            raise BootstrapError(f"非法 npm 版本：{requested}")
        return requested
    if dry_run:
        return "<resolved-version>"
    data = run_json(("npm", "view", package, "version", "--json"), cwd=root, dry_run=False, timeout=timeout)
    if not isinstance(data, str) or not data.strip():
        raise BootstrapError(f"无法解析 {package} 的版本。")
    return data.strip()


def resolve_repository_head(
    root: Path,
    source: str,
    *,
    dry_run: bool,
    timeout: int,
) -> str:
    """Resolve and record the exact upstream HEAD without changing install source semantics."""
    require_executable("git")
    url = f"https://github.com/{source}.git"
    result = run(("git", "ls-remote", url, "HEAD"), cwd=root, dry_run=dry_run, timeout=timeout)
    if dry_run:
        return "<resolved-commit>"
    first = result.stdout.strip().split()
    if not first or not re.fullmatch(r"[0-9a-fA-F]{40}", first[0]):
        raise BootstrapError(f"无法解析 {source} 的提交哈希。")
    return first[0].lower()


def skills_command(source: str | Path, skills: Iterable[str], harnesses: Sequence[str]) -> list[str]:
    command = [
        "npx",
        "--yes",
        f"skills@{SKILLS_CLI_VERSION}",
        "add",
        str(source),
        "--copy",
        "--yes",
    ]
    for harness in harnesses:
        command.extend(("--agent", harness))
    for skill in skills:
        command.extend(("--skill", skill))
    return command


def expected_skill_roots(root: Path, harnesses: Sequence[str]) -> list[Path]:
    return list(dict.fromkeys(root / SKILL_ROOT_BY_HARNESS[harness] for harness in harnesses))


def protect_installer_targets(
    root: Path, harnesses: Sequence[str], framework: str
) -> None:
    for skill_root in expected_skill_roots(root, harnesses):
        ensure_safe_path(root, skill_root, for_write=True)
        if lstat_exists(skill_root):
            if not skill_root.is_dir():
                raise BootstrapError(f"Skill 根路径不是目录：{skill_root}")
            for child in skill_root.iterdir():
                if child.is_symlink():
                    raise BootstrapError(f"Skill 根目录含符号链接，拒绝交给上游安装器：{child}")
        for skill in EXPECTED_SKILLS[framework]:
            ensure_safe_path(root, skill_root / skill, for_write=True)
    ensure_safe_path(root, root / "skills-lock.json", for_write=True)


def skill_file_at(root: Path, base: Path, skill: str) -> Path | None:
    candidate = base / skill / "SKILL.md"
    if not lstat_exists(candidate):
        return None
    ensure_safe_path(root, candidate, for_write=False)
    st = candidate.lstat()
    return candidate if stat.S_ISREG(st.st_mode) else None


def hash_skill_directory(root: Path, skill_file: Path) -> str:
    directory = skill_file.parent
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        ensure_safe_path(root, path, for_write=False)
        if path.is_symlink():
            raise BootstrapError(f"安装结果包含符号链接，拒绝信任：{path}")
        if path.is_file():
            relative = path.relative_to(directory).as_posix().encode("utf-8")
            digest.update(relative + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def verify_project_skills(
    root: Path,
    framework: str,
    harnesses: Sequence[str],
    *,
    dry_run: bool,
) -> tuple[dict[str, bool], dict[str, str], list[str]]:
    verification: dict[str, bool] = {}
    checksums: dict[str, str] = {}
    created: list[str] = []
    for base in expected_skill_roots(root, harnesses):
        root_label = base.relative_to(root).as_posix()
        for skill in EXPECTED_SKILLS[framework]:
            key = f"skill:{root_label}/{skill}"
            if dry_run:
                verification[key] = True
                continue
            path = skill_file_at(root, base, skill)
            ok = path is not None
            verification[key] = ok
            if not ok:
                raise BootstrapError(f"安装后未找到预期 Skill：{root_label}/{skill}")
            assert path is not None
            created.append(str(path.parent.relative_to(root)) + "/")
            checksums[f"{root_label}/{skill}"] = hash_skill_directory(root, path)
    lock = root / "skills-lock.json"
    if lstat_exists(lock):
        ensure_safe_path(root, lock, for_write=False)
        if not lock.is_file():
            raise BootstrapError("skills-lock.json 不是普通文件。")
        created.append(str(lock.relative_to(root)))
    return verification, checksums, sorted(set(created))



def install_project_skills(
    root: Path,
    framework: str,
    harnesses: Sequence[str],
    *,
    dry_run: bool,
    timeout: int,
    transaction: ManagedFileTransaction,
    mutation_tracker: MutationTracker,
) -> InstallOutcome:
    ensure_node_18(root, dry_run=dry_run, timeout=timeout)
    protect_installer_targets(root, harnesses, framework)
    skills = ("*",) if framework == "superpowers" else EXPECTED_SKILLS[framework]
    outcome = InstallOutcome(framework=framework, integration="project-skills")
    revision = resolve_repository_head(
        root, SOURCE_REPOSITORIES[framework], dry_run=dry_run, timeout=timeout
    )
    outcome.source_revisions[SOURCE_REPOSITORIES[framework]] = revision
    mutation_tracker.mark(dry_run=dry_run)
    run(
        skills_command(SOURCE_REPOSITORIES[framework], skills, harnesses),
        cwd=root,
        dry_run=dry_run,
        timeout=timeout,
        env={"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    )
    outcome.external_commands_ran = not dry_run
    verification, checksums, created = verify_project_skills(
        root, framework, harnesses, dry_run=dry_run
    )
    outcome.verification.update(verification)
    outcome.checksums.update(checksums)
    outcome.created_or_managed.extend(created)
    outcome.versions["skills-cli"] = SKILLS_CLI_VERSION

    if framework == "superpowers":
        managed = write_managed_instruction(
            root,
            harnesses,
            framework,
            "project-skills",
            dry_run=dry_run,
            transaction=transaction,
        )
        outcome.created_or_managed.extend(managed)
        outcome.limitations.append(
            "项目 Skills 模式不包含宿主插件的 SessionStart Hook 或插件自动更新。"
        )
        outcome.activation_notes.append("如当前 Agent 尚未发现新 Skill，请开始一个新会话。")
    elif framework == "matt":
        outcome.status = "pending"
        outcome.pending_actions.append(
            "在当前 Agent 会话调用 `setup-matt-pocock-skills`，完成 issue tracker 和 domain docs 配置；"
            "随后运行本脚本 `matt --finalize`。"
        )
    else:
        raise BootstrapError(f"不支持的项目 Skills 框架：{framework}")
    return outcome


def verify_matt_setup(root: Path) -> dict[str, bool]:
    required = {
        "docs/agents/issue-tracker.md": root / "docs/agents/issue-tracker.md",
        "docs/agents/domain.md": root / "docs/agents/domain.md",
    }
    result: dict[str, bool] = {}
    for name, path in required.items():
        if lstat_exists(path):
            ensure_safe_path(root, path, for_write=False)
        result[name] = lstat_exists(path) and path.is_file() and not path.is_symlink()
    instruction_candidates = [root / "CLAUDE.md", root / "AGENTS.md"]
    result["instruction:Agent skills"] = False
    for path in instruction_candidates:
        if lstat_exists(path) and "## Agent skills" in safe_read_text(root, path):
            result["instruction:Agent skills"] = True
            break
    return result


def verify_path_kind(root: Path, path: Path, *, expect_directory: bool) -> bool:
    if not lstat_exists(path):
        return False
    ensure_safe_path(root, path, for_write=False)
    if path.is_symlink():
        return False
    return path.is_dir() if expect_directory else path.is_file()


def protect_trellis_targets(root: Path, harnesses: Sequence[str]) -> None:
    targets = [
        root / ".trellis",
        root / ".trellis/.version",
        root / ".trellis/workflow.md",
        root / ".trellis/config.yaml",
    ]
    for harness in harnesses:
        targets.extend(root / relative for relative in TRELLIS_PLATFORM_PATHS[harness])
    for target in targets:
        ensure_safe_path(root, target, for_write=True)


def install_trellis(
    root: Path,
    harnesses: Sequence[str],
    user: str | None,
    trellis_version: str | None,
    *,
    repair: bool,
    dry_run: bool,
    timeout: int,
    mutation_tracker: MutationTracker,
) -> InstallOutcome:
    ensure_node_18(root, dry_run=dry_run, timeout=timeout)
    require_executable("git")
    protect_trellis_targets(root, harnesses)
    trellis_path = root / ".trellis"
    existing_valid = False
    if lstat_exists(trellis_path):
        ensure_safe_path(root, trellis_path, for_write=True)
        if not trellis_path.is_dir():
            raise BootstrapError(".trellis 存在但不是目录。")
        existing_valid = trellis_core_valid(root)
        if not existing_valid and not repair:
            raise BootstrapError(".trellis 不完整；请备份后使用 --repair，或清理后重新初始化。")

    developer: str | None = None
    if not existing_valid:
        developer = user or git_user(root, dry_run=dry_run, timeout=timeout)
        if not developer:
            if not sys.stdin.isatty():
                raise BootstrapError("无法确定 Trellis 开发者名称，请传入 --user。")
            developer = input("Trellis 开发者名称：").strip()
        if not developer:
            raise BootstrapError("Trellis 开发者名称不能为空。")

    resolved_version = resolve_npm_version(
        root,
        TRELLIS_PACKAGE,
        trellis_version,
        dry_run=dry_run,
        timeout=timeout,
    )
    command = [
        "npx",
        "--yes",
        "--package",
        f"{TRELLIS_PACKAGE}@{resolved_version}",
        "trellis",
        "init",
        "--yes",
    ]
    for harness in harnesses:
        command.append(TRELLIS_PLATFORM_FLAG[harness])
    if developer is not None:
        command.extend(("-u", developer))
    mutation_tracker.mark(dry_run=dry_run)
    run(
        command,
        cwd=root,
        dry_run=dry_run,
        timeout=timeout,
        env={"OPENSPEC_TELEMETRY": "0", "DO_NOT_TRACK": "1"},
    )

    outcome = InstallOutcome(framework="trellis", integration="official")
    outcome.external_commands_ran = not dry_run
    outcome.versions["trellis"] = resolved_version
    required: dict[str, tuple[Path, bool]] = {
        ".trellis/.version": (root / ".trellis/.version", False),
        ".trellis/workflow.md": (root / ".trellis/workflow.md", False),
        ".trellis/config.yaml": (root / ".trellis/config.yaml", False),
    }
    for harness in harnesses:
        for relative in TRELLIS_PLATFORM_PATHS[harness]:
            required[str(relative)] = (root / relative, relative.suffix == "")
    for name, (path, expect_directory) in required.items():
        if dry_run:
            outcome.verification[name] = True
            continue
        ok = verify_path_kind(root, path, expect_directory=expect_directory)
        outcome.verification[name] = ok
        if not ok:
            kind = "目录" if expect_directory else "文件"
            raise BootstrapError(f"Trellis 初始化后缺少预期{kind}：{name}")
    outcome.created_or_managed.extend(sorted(required))
    outcome.limitations.append("Trellis 以 AGPL-3.0 发布；企业或客户仓库使用前应确认内部合规要求。")
    if "codex" in harnesses:
        outcome.activation_notes.append("在 Codex 中启用 hooks，并在 `/hooks` 审核 Trellis Hook。")
    return outcome



def safe_recursive_contains(root: Path, base: Path, token: str) -> bool:
    if not lstat_exists(base):
        return False
    ensure_safe_path(root, base, for_write=False)
    if not base.is_dir() or base.is_symlink():
        return False
    token = token.lower()
    for path in base.rglob("*"):
        ensure_safe_path(root, path, for_write=False)
        if path.is_symlink():
            raise BootstrapError(f"安装结果包含符号链接，拒绝信任：{path}")
        if token in path.name.lower() and (path.is_file() or path.is_dir()):
            return True
    return False


def protect_paths(root: Path, paths: Iterable[Path]) -> None:
    for path in paths:
        ensure_safe_path(root, path, for_write=True)


def install_openspec(
    root: Path,
    harnesses: Sequence[str],
    version: str | None,
    *,
    dry_run: bool,
    timeout: int,
    mutation_tracker: MutationTracker,
) -> InstallOutcome:
    ensure_node_20(root, dry_run=dry_run, timeout=timeout)
    targets = [root / "openspec"] + [root / OPENSPEC_SKILL_ROOT[h] for h in harnesses]
    protect_paths(root, targets)
    resolved_version = resolve_npm_version(
        root, OPENSPEC_PACKAGE, version, dry_run=dry_run, timeout=timeout
    )
    tools = ",".join(OPENSPEC_TOOL_ID[h] for h in harnesses)
    command = (
        "npx", "--yes", "--package", f"{OPENSPEC_PACKAGE}@{resolved_version}",
        "openspec", "init", "--tools", tools,
    )
    mutation_tracker.mark(dry_run=dry_run)
    run(command, cwd=root, dry_run=dry_run, timeout=timeout)

    outcome = InstallOutcome(framework="openspec", integration="official")
    outcome.external_commands_ran = not dry_run
    outcome.versions["openspec"] = resolved_version
    for relative in (Path("openspec"), Path("openspec/specs"), Path("openspec/changes")):
        ok = dry_run or verify_path_kind(root, root / relative, expect_directory=True)
        outcome.verification[str(relative) + "/"] = ok
        if not ok:
            raise BootstrapError(f"OpenSpec 初始化后缺少目录：{relative}")
        outcome.created_or_managed.append(str(relative) + "/")
    for harness in harnesses:
        base = root / OPENSPEC_SKILL_ROOT[harness]
        ok = dry_run or safe_recursive_contains(root, base, "openspec-")
        key = f"{OPENSPEC_SKILL_ROOT[harness].as_posix()}:openspec-*"
        outcome.verification[key] = ok
        if not ok:
            raise BootstrapError(f"OpenSpec 未为 {harness} 生成预期 Skills：{base.relative_to(root)}")
        outcome.created_or_managed.append(str(base.relative_to(root)) + "/")
    outcome.activation_notes.append("开始新 Agent 会话后使用 OpenSpec 的 propose/apply/archive Skills。")
    return outcome








def codex_plugin_entries(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return []
    entries: list[dict[str, Any]] = []
    for key in ("installed", "available", "plugins"):
        value = data.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    annotated = dict(item)
                    annotated.setdefault("_selector_section", key)
                    entries.append(annotated)
    return entries


def plugin_name(entry: Mapping[str, Any]) -> str:
    return str(entry.get("name") or entry.get("pluginId") or entry.get("id") or "")


def plugin_marketplace(entry: Mapping[str, Any]) -> str:
    return str(entry.get("marketplaceName") or entry.get("marketplace") or "")


def find_codex_plugin(data: Any, framework: str, *, installed_only: bool = False) -> dict[str, Any] | None:
    names = PLUGIN_FRAMEWORK_NAMES[framework]
    for entry in codex_plugin_entries(data):
        name = plugin_name(entry).lower().split("@")[0]
        if name not in names:
            continue
        if installed_only:
            section = str(entry.get("_selector_section") or "")
            installed = entry.get("installed")
            if section == "available" and installed is not True:
                continue
            if section == "plugins" and installed is not True:
                continue
            if installed is False:
                continue
        return entry
    return None


def codex_plugin_inventory(root: Path, *, dry_run: bool, timeout: int, include_available: bool) -> Any:
    require_executable("codex")
    command = ["codex", "plugin", "list"]
    if include_available:
        command.append("--available")
    command.append("--json")
    return run_json(command, cwd=root, dry_run=dry_run, timeout=timeout)



def detect_codex_plugin_frameworks(
    root: Path, *, dry_run: bool, timeout: int
) -> set[str]:
    if dry_run or not shutil.which("codex"):
        return set()
    try:
        data = codex_plugin_inventory(
            root, dry_run=False, timeout=timeout, include_available=False
        )
    except BootstrapError as exc:
        print(
            f"警告：无法读取 Codex 插件清单，宿主级冲突检测不完整：{exc}",
            file=sys.stderr,
        )
        return set()
    return {
        "superpowers"
        for _ in [0]
        if find_codex_plugin(data, "superpowers", installed_only=True)
    }



def install_codex_plugin(
    root: Path,
    framework: str,
    *,
    dry_run: bool,
    timeout: int,
    mutation_tracker: MutationTracker,
) -> InstallOutcome:
    if framework != "superpowers":
        raise BootstrapError(f"不支持的 Codex 官方插件：{framework}")
    require_executable("codex")
    outcome = InstallOutcome(framework=framework, integration="official")

    inventory = codex_plugin_inventory(
        root, dry_run=dry_run, timeout=timeout, include_available=True
    )
    entry = find_codex_plugin(inventory, framework)
    if dry_run:
        plugin_spec = "superpowers@<official-marketplace>"
    else:
        if not entry:
            raise BootstrapError("Codex Marketplace 中未找到 Superpowers 插件。")
        name = plugin_name(entry).split("@")[0]
        marketplace = plugin_marketplace(entry)
        if not marketplace:
            plugin_id = str(entry.get("pluginId") or "")
            if "@" in plugin_id:
                marketplace = plugin_id.split("@", 1)[1]
        if not marketplace:
            raise BootstrapError("无法确定 Superpowers 插件所属 Marketplace。")
        plugin_spec = f"{name}@{marketplace}"

    installed_entry = find_codex_plugin(
        inventory, framework, installed_only=True
    )
    if not installed_entry:
        mutation_tracker.mark(dry_run=dry_run)
        result = run_json(
            ("codex", "plugin", "add", plugin_spec, "--json"),
            cwd=root,
            dry_run=dry_run,
            timeout=timeout,
        )
        outcome.external_commands_ran = not dry_run
        if isinstance(result, Mapping) and result.get("version"):
            outcome.versions[framework] = str(result["version"])

    if dry_run:
        outcome.verification["codex-plugin:superpowers"] = True
    else:
        final_inventory = codex_plugin_inventory(
            root, dry_run=False, timeout=timeout, include_available=False
        )
        final_entry = find_codex_plugin(
            final_inventory, framework, installed_only=True
        )
        ok = final_entry is not None and bool(final_entry.get("enabled", True))
        outcome.verification["codex-plugin:superpowers"] = ok
        if not ok:
            raise BootstrapError(
                "Codex Superpowers 插件安装后未处于已安装/启用状态。"
            )
        if final_entry and final_entry.get("version"):
            outcome.versions[framework] = str(final_entry["version"])

    outcome.created_or_managed.append("Codex plugin: superpowers")
    outcome.activation_notes.append("开始一个新 Codex 会话以加载插件。")
    return outcome












def pending_official_install(
    framework: str, harnesses: Sequence[str]
) -> InstallOutcome:
    if framework != "superpowers":
        raise BootstrapError(f"不支持的待人工官方安装：{framework}")
    outcome = InstallOutcome(
        framework=framework, integration="official", status="pending"
    )
    actions = {
        "claude-code": (
            "在 Claude Code 执行 `/plugin install "
            "superpowers@claude-plugins-official`，然后 `/reload-plugins`。"
        ),
        "cursor": "在 Cursor Agent 执行 `/add-plugin superpowers`。",
        "opencode": (
            "让 OpenCode 获取并严格执行 Superpowers 官方 `.opencode/INSTALL.md`。"
        ),
    }
    for harness in harnesses:
        outcome.pending_actions.append(
            actions.get(
                harness,
                f"当前选择器无法无交互完成 Superpowers 在 {harness} 的官方安装。",
            )
        )
    return outcome



def resolve_integration(
    framework: str, requested: str, harnesses: Sequence[str]
) -> str:
    if framework in {"trellis", "openspec"}:
        return "official"
    if framework == "matt":
        return "project-skills"
    if requested != "auto":
        return requested
    if framework == "superpowers" and harnesses == ["codex"]:
        return "official"
    return "project-skills"



def install_framework(
    root: Path,
    framework: str,
    harnesses: Sequence[str],
    integration: str,
    args: argparse.Namespace,
    transaction: ManagedFileTransaction,
    mutation_tracker: MutationTracker,
) -> InstallOutcome:
    if framework == "trellis":
        return install_trellis(
            root,
            harnesses,
            args.user,
            args.trellis_version,
            repair=args.repair,
            dry_run=args.dry_run,
            timeout=args.timeout,
            mutation_tracker=mutation_tracker,
        )
    if framework == "openspec":
        return install_openspec(
            root,
            harnesses,
            args.openspec_version,
            dry_run=args.dry_run,
            timeout=args.timeout,
            mutation_tracker=mutation_tracker,
        )
    if framework == "matt":
        return install_project_skills(
            root,
            "matt",
            harnesses,
            dry_run=args.dry_run,
            timeout=args.timeout,
            transaction=transaction,
            mutation_tracker=mutation_tracker,
        )
    if framework != "superpowers":
        raise BootstrapError(f"不支持的框架：{framework}")

    if integration == "project-skills":
        return install_project_skills(
            root,
            "superpowers",
            harnesses,
            dry_run=args.dry_run,
            timeout=args.timeout,
            transaction=transaction,
            mutation_tracker=mutation_tracker,
        )
    if integration != "official":
        raise BootstrapError(f"不支持的集成方式：{integration}")
    if harnesses == ["codex"]:
        return install_codex_plugin(
            root,
            "superpowers",
            dry_run=args.dry_run,
            timeout=args.timeout,
            mutation_tracker=mutation_tracker,
        )
    return pending_official_install("superpowers", harnesses)



def finalize_framework(
    root: Path,
    framework: str,
    harnesses: Sequence[str],
    integration: str,
    *,
    dry_run: bool,
    timeout: int,
) -> InstallOutcome:
    outcome = InstallOutcome(framework=framework, integration=integration)
    if framework == "matt":
        verification, checksums, created = verify_project_skills(
            root, "matt", harnesses, dry_run=dry_run
        )
        outcome.verification.update(verification)
        outcome.checksums.update(checksums)
        outcome.created_or_managed.extend(created)
        setup = (
            verify_matt_setup(root)
            if not dry_run
            else {
                "docs/agents/issue-tracker.md": True,
                "docs/agents/domain.md": True,
                "instruction:Agent skills": True,
            }
        )
        outcome.verification.update(setup)
        missing = [name for name, ok in setup.items() if not ok]
        if missing:
            raise BootstrapError(
                "Matt 初始化尚未完成，缺少：" + ", ".join(missing)
            )
        outcome.versions["skills-cli"] = SKILLS_CLI_VERSION
        return outcome

    if framework == "trellis":
        if not trellis_core_valid(root):
            raise BootstrapError("Trellis 核心文件不完整。")
        outcome.verification["trellis-core"] = True
        for harness in harnesses:
            for relative in TRELLIS_PLATFORM_PATHS[harness]:
                path = root / relative
                expect_directory = relative.suffix == ""
                ok = verify_path_kind(
                    root, path, expect_directory=expect_directory
                )
                outcome.verification[str(relative)] = ok
                if not ok:
                    raise BootstrapError(
                        f"Trellis 平台配置缺失或类型错误：{relative}"
                    )
        return outcome

    if framework == "openspec":
        for relative in (
            Path("openspec"),
            Path("openspec/specs"),
            Path("openspec/changes"),
        ):
            ok = dry_run or verify_path_kind(
                root, root / relative, expect_directory=True
            )
            outcome.verification[str(relative) + "/"] = ok
            if not ok:
                raise BootstrapError(f"OpenSpec 核心目录缺失：{relative}")
        for harness in harnesses:
            ok = dry_run or safe_recursive_contains(
                root, root / OPENSPEC_SKILL_ROOT[harness], "openspec-"
            )
            outcome.verification[
                f"{OPENSPEC_SKILL_ROOT[harness]}:openspec-*"
            ] = ok
            if not ok:
                raise BootstrapError(f"OpenSpec {harness} 集成缺失。")
        return outcome

    if framework != "superpowers":
        raise BootstrapError(f"不支持 finalize：{framework}")

    if integration == "project-skills":
        verification, checksums, created = verify_project_skills(
            root, "superpowers", harnesses, dry_run=dry_run
        )
        outcome.verification.update(verification)
        outcome.checksums.update(checksums)
        outcome.created_or_managed.extend(created)
        return outcome

    if harnesses == ["codex"]:
        data = codex_plugin_inventory(
            root,
            dry_run=dry_run,
            timeout=timeout,
            include_available=False,
        )
        entry = find_codex_plugin(
            data, "superpowers", installed_only=True
        )
        ok = dry_run or (
            entry is not None and bool(entry.get("enabled", True))
        )
        outcome.verification["codex-plugin:superpowers"] = ok
        if not ok:
            raise BootstrapError(
                "尚未检测到已安装并启用的 Codex Superpowers 插件。"
            )
        if entry and entry.get("version"):
            outcome.versions["superpowers"] = str(entry["version"])
        return outcome

    raise BootstrapError(
        "该官方集成无法由脚本自动验证；请完成宿主安装后手工确认，"
        "或改用 project-skills 模式。"
    )


def state_payload(
    outcome: InstallOutcome,
    harnesses: Sequence[str],
    previous: dict[str, Any] | None,
    *,
    minimal: bool,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    same_selection = bool(
        previous
        and previous.get("framework") == outcome.framework
        and previous.get("integration") == outcome.integration
    )

    def merged_mapping(
        key: str, current: Mapping[str, Any]
    ) -> dict[str, Any]:
        base = previous.get(key, {}) if same_selection and previous else {}
        merged = dict(base) if isinstance(base, Mapping) else {}
        merged.update(current)
        return merged

    def merged_list(key: str, current: Sequence[str]) -> list[str]:
        base = previous.get(key, []) if same_selection and previous else []
        prior = list(base) if isinstance(base, list) else []
        return list(
            dict.fromkeys([*(str(item) for item in prior), *current])
        )

    prior_harnesses = (
        previous.get("harnesses", []) if same_selection and previous else []
    )
    effective_harnesses = list(
        dict.fromkeys(
            [
                *(
                    str(item)
                    for item in prior_harnesses
                    if isinstance(item, str)
                ),
                *harnesses,
            ]
        )
    )
    first_installed = (
        previous.get("installed_at") if same_selection and previous else None
    )
    prior_minimal = bool(previous.get("minimal", False)) if same_selection and previous else False
    return {
        "schema": 5,
        "installer": f"agent-compass/{VERSION}",
        "status": outcome.status,
        "framework": outcome.framework,
        "integration": outcome.integration,
        "harnesses": effective_harnesses,
        "minimal": prior_minimal or minimal,
        "scope": (
            "project"
            if outcome.integration != "official"
            or outcome.framework in {"trellis", "openspec"}
            else "host-or-project"
        ),
        "installed_at": first_installed or now,
        "updated_at": now,
        "versions": merged_mapping("versions", outcome.versions),
        "source_revisions": merged_mapping(
            "source_revisions", outcome.source_revisions
        ),
        "checksums": merged_mapping("checksums", outcome.checksums),
        "created_or_managed": sorted(
            set(
                merged_list(
                    "created_or_managed", outcome.created_or_managed
                )
            )
        ),
        "verification": merged_mapping(
            "verification", outcome.verification
        ),
        "pending_actions": outcome.pending_actions,
        "activation_notes": merged_list(
            "activation_notes", outcome.activation_notes
        ),
        "limitations": merged_list(
            "limitations", outcome.limitations
        ),
    }


def write_state(
    root: Path,
    outcome: InstallOutcome,
    harnesses: Sequence[str],
    *,
    minimal: bool,
    dry_run: bool,
    transaction: ManagedFileTransaction,
) -> None:
    path = root / STATE_FILE
    previous = load_state(root)
    state = state_payload(
        outcome, harnesses, previous, minimal=minimal
    )
    print(f"write {STATE_FILE} ({outcome.status})")
    if not dry_run:
        transaction.snapshot(path)
        mode = (
            stat.S_IMODE(path.lstat().st_mode)
            if lstat_exists(path)
            else 0o644
        )
        atomic_write_text(
            root,
            path,
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            mode=mode,
        )
        reread = load_state(root)
        if not reread or reread.get("status") != outcome.status:
            raise BootstrapError("状态文件写入后验证失败。")


def validate_integration_consistency(
    root: Path,
    framework: str,
    integration: str,
    *,
    detected_codex_plugins: set[str],
    finalize: bool,
) -> None:
    state = load_state(root)
    if state and state.get("framework") == framework:
        recorded = state.get("integration")
        if recorded and recorded != integration:
            raise BootstrapError(
                f"项目已记录 {framework}/{recorded}，拒绝叠加 {integration}。"
                "请先人工卸载旧集成并移走状态文件。"
            )
    if finalize:
        return
    managed = detect_managed_instruction_frameworks(root) | detect_frameworks_from_lock(root)
    if integration == "official" and framework in managed:
        raise BootstrapError(
            f"检测到 {framework} 的项目 Skills 集成，拒绝再安装官方插件版。"
        )
    if integration == "project-skills" and framework in detected_codex_plugins:
        raise BootstrapError(
            f"检测到已安装的 Codex {framework} 官方插件，拒绝再安装项目 Skills 版。"
        )



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agent Compass: select, install, and verify one AI coding framework."
    )
    parser.add_argument(
        "framework",
        nargs="?",
        default="auto",
        choices=FRAMEWORK_CHOICES,
    )
    parser.add_argument(
        "--harness",
        action="append",
        help="Repeat or comma-separate: codex, claude-code, cursor, opencode",
    )
    parser.add_argument(
        "--integration",
        default="auto",
        choices=INTEGRATIONS,
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
    )
    parser.add_argument(
        "--user",
        help="Developer name used by Trellis",
    )
    parser.add_argument(
        "--trellis-version",
        help="Exact Trellis npm version; default resolves current stable",
    )
    parser.add_argument(
        "--openspec-version",
        help="Exact OpenSpec npm version; default resolves current stable",
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Add a project rule requiring the smallest correct change",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Allow official Trellis CLI to repair an incomplete .trellis",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Verify a pending/manual initialization and mark it ready",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip final confirmation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions only",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    return parser



def describe_plan(
    framework: str,
    integration: str,
    harnesses: Sequence[str],
    *,
    minimal: bool,
) -> str:
    scope = {
        ("trellis", "official"): "初始化长期项目规范、任务和工作记忆",
        ("openspec", "official"): "安装轻量 proposal/apply/archive 规格流程",
        ("matt", "project-skills"): "安装按需调用的调试、评审、设计和 TDD Skills",
        ("superpowers", "official"): "安装并验证宿主官方 Superpowers 插件",
        ("superpowers", "project-skills"): (
            "安装可编辑的项目 Skills 兼容模式；不包含宿主 Hook"
        ),
    }.get((framework, integration), "执行所选集成")
    suffix = "；启用最小正确修改规则" if minimal else ""
    return (
        f"选择：{framework} / {integration} / "
        f"{', '.join(harnesses)}；{scope}{suffix}。"
    )



def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        raise BootstrapError("--timeout 必须大于 0。")

    root = find_project_root(args.project_root)
    reject_retired_name_traces(root)
    previous_state = load_state(root)
    requested_framework = canonical_framework(args.framework)
    interactive_minimal = False

    if args.finalize and requested_framework == "auto":
        recorded = (
            canonical_framework(
                str(previous_state.get("framework") or "")
            )
            if previous_state
            else ""
        )
        if recorded not in FRAMEWORKS or recorded in {"auto", "none"}:
            raise BootstrapError(
                "--finalize 需要显式框架，或一个有效的 Agent Compass 状态文件。"
            )
        framework = recorded
    elif requested_framework == "auto":
        framework, interactive_minimal = choose_framework_interactively()
    else:
        framework = requested_framework

    minimal_enabled = bool(
        args.minimal
        or interactive_minimal
        or (
            previous_state
            and previous_state.get("framework") == framework
            and previous_state.get("minimal", False)
        )
    )

    if framework == "none":
        if args.minimal or interactive_minimal:
            raise BootstrapError(
                "`none` 表示完全跳过；要启用最小修改规则，请同时选择一个框架。"
            )
        if previous_state:
            print(
                "未执行安装；`none` 仅表示跳过本次操作，不会禁用或删除现有框架。"
                f" 当前记录：{previous_state.get('framework')} / "
                f"{previous_state.get('status', 'unknown')}。"
            )
        else:
            print("未执行安装，项目保持不变。")
        return 0

    if args.finalize and not args.harness and previous_state:
        recorded_harnesses = previous_state.get("harnesses")
        if isinstance(recorded_harnesses, list) and recorded_harnesses:
            harnesses = normalize_harnesses(
                [str(item) for item in recorded_harnesses], root
            )
        else:
            harnesses = normalize_harnesses(args.harness, root)
    else:
        harnesses = normalize_harnesses(args.harness, root)

    if (
        args.finalize
        and args.integration == "auto"
        and previous_state
    ):
        integration = str(
            previous_state.get("integration")
            or resolve_integration(framework, "auto", harnesses)
        )
    else:
        integration = resolve_integration(
            framework, args.integration, harnesses
        )

    if (
        framework in {"trellis", "openspec"}
        and args.integration == "project-skills"
    ):
        raise BootstrapError(f"{framework} 只支持 official 集成。")
    if framework == "matt" and args.integration == "official":
        raise BootstrapError(
            "Matt 在本选择器中使用可编辑、可验证的 project-skills 模式。"
        )

    existing = detect_existing_frameworks(root, repair=args.repair)
    codex_plugins: set[str] = set()
    if "codex" in harnesses:
        codex_plugins = detect_codex_plugin_frameworks(
            root,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
        existing.update(codex_plugins)
    conflicts = existing - {framework}
    if conflicts:
        legacy = conflicts & LEGACY_FRAMEWORKS
        suffix = (
            " 其中包含已从 v0.5 移除的遗留框架，请先按其官方方式卸载或迁移。"
            if legacy
            else ""
        )
        raise BootstrapError(
            "检测到其他框架："
            + ", ".join(sorted(conflicts))
            + "。本工具不会自动删除或混用；请先人工处理后重试。"
            + suffix
        )

    validate_integration_consistency(
        root,
        framework,
        integration,
        detected_codex_plugins=codex_plugins,
        finalize=args.finalize,
    )

    print(f"项目：{root}")
    print(
        describe_plan(
            framework,
            integration,
            harnesses,
            minimal=minimal_enabled,
        )
    )
    if framework == "trellis":
        print(
            "许可证提示：Trellis 为 AGPL-3.0；"
            "请自行确认企业或客户项目的合规要求。"
        )
    if integration == "official" and framework == "superpowers":
        print(
            "范围提示：官方宿主插件通常安装到用户/宿主范围，"
            "而不是只写入当前仓库。"
        )
    if not args.yes and not args.dry_run:
        if not prompt_yes_no("继续吗？"):
            print("已取消。")
            return 0

    mutation_tracker = MutationTracker()
    try:
        with ManagedFileTransaction(root) as transaction:
            if args.finalize:
                outcome = finalize_framework(
                    root,
                    framework,
                    harnesses,
                    integration,
                    dry_run=args.dry_run,
                    timeout=args.timeout,
                )
            else:
                outcome = install_framework(
                    root,
                    framework,
                    harnesses,
                    integration,
                    args,
                    transaction,
                    mutation_tracker,
                )

            if minimal_enabled:
                managed = write_minimal_instruction(
                    root,
                    harnesses,
                    dry_run=args.dry_run,
                    transaction=transaction,
                )
                outcome.created_or_managed.extend(managed)
                outcome.verification["minimal-policy"] = True

            write_state(
                root,
                outcome,
                harnesses,
                minimal=minimal_enabled,
                dry_run=args.dry_run,
                transaction=transaction,
            )
            transaction.commit()
    except Exception:
        if mutation_tracker.external_command_started:
            print(
                "警告：上游安装命令已运行，可能留下部分更改；"
                "Agent Compass 自己管理的文件已尝试回滚，且不会写入 ready 状态。"
                "请检查 git diff 和宿主插件列表。",
                file=sys.stderr,
            )
        raise

    if outcome.status == "ready":
        print("安装与验证完成。")
    else:
        print("安装阶段完成，但初始化仍为 pending；未宣称 ready。")
    for note in outcome.pending_actions:
        print(f"待完成：{note}")
    for note in outcome.activation_notes:
        print(f"激活提示：{note}")
    for limitation in outcome.limitations:
        print(f"能力边界：{limitation}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
