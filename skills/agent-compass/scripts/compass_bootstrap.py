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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

VERSION = "0.6.1"
STATE_SCHEMA = 7
READABLE_STATE_SCHEMAS = {5, 6, STATE_SCHEMA}
SKILLS_CLI_VERSION = "1.5.9"
TRELLIS_PACKAGE = "@mindfoldhq/trellis"
OPENSPEC_PACKAGE = "@fission-ai/openspec"
CODEX_OFFICIAL_MARKETPLACE = "openai-curated"

FRAMEWORKS = ("auto", "trellis", "openspec", "superpowers", "matt", "none")
FRAMEWORK_ALIASES = {
    "open-spec": "openspec",
    "mattpocock": "matt",
}
FRAMEWORK_CHOICES = (*FRAMEWORKS, *FRAMEWORK_ALIASES)
LEGACY_FRAMEWORKS = {"speckit", "bmad", "compound", "ponytail"}
HARNESSES = ("auto", "codex", "claude-code", "cursor", "opencode")
ACTIVE_HARNESSES = tuple(item for item in HARNESSES if item != "auto")
INTEGRATIONS = ("auto", "official", "project-skills")
STATE_FILE = ".agent-compass.json"
LOCK_FILE = ".agent-compass.lock"
RETIRED_STATE_FILE = ".agent-framework.json"
RETIRED_MANAGED_PREFIX = "<!-- agent-framework-selector:"
RETIRED_SKILL_NAME = "agent-framework"
MANAGED_START_PREFIX = "<!-- agent-compass:start"
MANAGED_END = "<!-- agent-compass:end -->"
MINIMAL_START = "<!-- agent-compass:minimal:start -->"
MINIMAL_END = "<!-- agent-compass:minimal:end -->"
DEFAULT_TIMEOUT_SECONDS = 600
READINESS_VALUES = {"unknown", "pending", "ready"}
STATE_STATUSES = {
    "pending",
    "installed",
    "activation_pending",
    "bootstrap_pending",
    "ready",
}
EXACT_SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)

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
    """Expected user-actionable bootstrap failure.

    Messages are authored in Chinese. Pass ``en`` to supply an English
    variant for interpolated messages; static messages are translated at the
    display boundary through ``ERROR_MESSAGE_ENGLISH``. ``str()`` keeps
    returning the Chinese text so existing callers and logs are unchanged.
    """

    def __init__(self, zh: str, en: str | None = None) -> None:
        super().__init__(zh)
        self.zh = zh
        self.en = en


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
    status: str = "ready"
    readiness: dict[str, str] = field(default_factory=dict)
    host_readiness: dict[str, dict[str, str]] = field(default_factory=dict)
    created_or_managed: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    identities: dict[str, str] = field(default_factory=dict)
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


class RepositoryLock:
    """Serialize Agent Compass mutations without making doctor write state."""

    def __init__(self, root: Path, *, enabled: bool) -> None:
        self.root = root
        self.path = root / LOCK_FILE
        self.enabled = enabled
        self._acquired = False
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> "RepositoryLock":
        if not self.enabled:
            return self
        ensure_safe_path(self.root, self.path, for_write=True)
        payload = (
            f"pid={os.getpid()}\n"
            f"started_at={datetime.now(timezone.utc).isoformat()}\n"
        ).encode("utf-8")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise BootstrapError(
                f"检测到 {LOCK_FILE}；另一个 Agent Compass 流程可能正在运行。"
                "确认没有活动进程后再人工移走陈旧锁文件。"
            ) from exc
        except OSError as exc:
            raise BootstrapError(f"无法创建仓库锁 {LOCK_FILE}：{exc}") from exc
        lock_stat = os.fstat(fd)
        self._identity = (lock_stat.st_dev, lock_stat.st_ino)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        self._acquired = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._acquired:
            try:
                ensure_safe_path(self.root, self.path, for_write=True)
                current = self.path.lstat()
                current_identity = (current.st_dev, current.st_ino)
                if current_identity != self._identity:
                    print(
                        f"警告：仓库锁 {LOCK_FILE} 已被替换，拒绝删除。",
                        file=sys.stderr,
                    )
                    return False
                self.path.unlink()
            except FileNotFoundError:
                pass
            except (BootstrapError, OSError) as lock_error:
                print(f"警告：无法移除仓库锁：{lock_error}", file=sys.stderr)
        return False


def lstat_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def ensure_safe_path(root: Path, path: Path, *, for_write: bool) -> None:
    """Reject path escapes and symlink components without following target paths."""
    # Keep the lexical paths for the initial containment check and symlink walk.
    # On macOS, resolving a path below /var produces /private/var; resolving only
    # the root would therefore make a legitimate child appear to escape it.
    root = root.absolute()
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise BootstrapError(
            f"目标不在项目目录内：{path}",
            f"Target is outside the project directory: {path}",
        ) from exc

    if absolute == root:
        if for_write and (not root.exists() or not root.is_dir()):
            raise BootstrapError(
                f"项目根目录不可写入：{root}",
                f"Project root is not writable: {root}",
            )
        return

    current = root
    for part in relative.parts:
        current = current / part
        if not lstat_exists(current):
            continue
        st = current.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise BootstrapError(
                f"拒绝访问符号链接路径：{current}",
                f"Refusing to access a symbolic-link path: {current}",
            )

    parent = absolute.parent
    # resolve(strict=False) follows existing parents; all existing components were
    # checked above, so this is now a containment sanity check rather than trust.
    resolved_root = root.resolve(strict=False)
    resolved_parent = parent.resolve(strict=False)
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise BootstrapError(
            f"目标父目录逃逸项目范围：{path}",
            f"Target parent directory escapes the project scope: {path}",
        )

    if for_write and lstat_exists(absolute):
        st = absolute.lstat()
        if not stat.S_ISREG(st.st_mode) and not stat.S_ISDIR(st.st_mode):
            raise BootstrapError(
                f"拒绝覆盖特殊文件：{path}",
                f"Refusing to overwrite a special file: {path}",
            )


def safe_read_text(root: Path, path: Path) -> str:
    ensure_safe_path(root, path, for_write=False)
    if not lstat_exists(path):
        raise BootstrapError(
            f"文件不存在：{path}",
            f"File does not exist: {path}",
        )
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise BootstrapError(
            f"拒绝读取非普通文件：{path}",
            f"Refusing to read a non-regular file: {path}",
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapError(
            f"文件不是有效 UTF-8：{path}",
            f"File is not valid UTF-8: {path}",
        ) from exc


def reject_symlinks_in_tree(root: Path, base: Path) -> None:
    """Reject existing symlinks before an upstream installer can see a target."""
    if not lstat_exists(base):
        return
    ensure_safe_path(root, base, for_write=False)
    if base.is_file():
        return
    if not base.is_dir():
        raise BootstrapError(
            f"安装目标不是普通文件或目录：{base}",
            f"Install target is not a regular file or directory: {base}",
        )
    for path in base.rglob("*"):
        ensure_safe_path(root, path, for_write=False)
        if path.is_symlink():
            raise BootstrapError(
                f"安装目标包含符号链接：{path}",
                f"Install target contains a symbolic link: {path}",
            )


def hash_file(root: Path, path: Path) -> str:
    ensure_safe_path(root, path, for_write=False)
    if not path.is_file() or path.is_symlink():
        raise BootstrapError(
            f"无法校验非普通文件：{path}",
            f"Cannot checksum a non-regular file: {path}",
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_directory(root: Path, directory: Path) -> str:
    ensure_safe_path(root, directory, for_write=False)
    if not directory.is_dir() or directory.is_symlink():
        raise BootstrapError(
            f"无法校验非普通目录：{directory}",
            f"Cannot checksum a non-regular directory: {directory}",
        )
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        ensure_safe_path(root, path, for_write=False)
        if path.is_symlink():
            raise BootstrapError(
                f"校验目录包含符号链接：{path}",
                f"Checksum directory contains a symbolic link: {path}",
            )
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        else:
            raise BootstrapError(
                f"校验目录包含特殊文件：{path}",
                f"Checksum directory contains a special file: {path}",
            )
    return digest.hexdigest()


def hash_verified_path(
    root: Path, path: Path, *, expect_directory: bool
) -> str:
    return (
        hash_directory(root, path)
        if expect_directory
        else hash_file(root, path)
    )


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


def detect_prompt_language(requested: str = "auto") -> str:
    if requested in {"zh", "en"}:
        return requested
    locale = " ".join(
        os.environ.get(name, "")
        for name in ("LC_ALL", "LC_MESSAGES", "LANG")
    ).lower()
    return "zh" if "zh" in locale else "en"


def localized_text(language: str, zh: str, en: str) -> str:
    return zh if detect_prompt_language(language) == "zh" else en


OUTCOME_MESSAGE_ENGLISH = {
    "项目 Skills 模式不包含宿主插件的 SessionStart Hook 或插件自动更新。": (
        "Project-Skills mode does not include the host plugin's SessionStart "
        "hook or automatic plugin updates."
    ),
    "如当前 Agent 尚未发现新 Skill，请开始一个新会话。": (
        "Start a new Agent session if the current session has not discovered "
        "the new Skills."
    ),
    (
        "在当前 Agent 会话调用 `setup-matt-pocock-skills`，完成 issue tracker 和 domain docs 配置；"
        "随后运行本脚本 `matt --finalize`。"
    ): (
        "Invoke `setup-matt-pocock-skills` in the current Agent session, "
        "complete the issue-tracker and domain-docs setup, then run this "
        "script with `matt --finalize`."
    ),
    "Trellis 以 AGPL-3.0 发布；企业或客户仓库使用前应确认内部合规要求。": (
        "Trellis is distributed under AGPL-3.0; confirm internal compliance "
        "requirements before using it in enterprise or client repositories."
    ),
    (
        "在 Codex 中启用 hooks，并通过 `/hooks` 审核 Trellis Hook；"
        "完成后使用 --finalize --confirm-trellis-activation。"
    ): (
        "Enable hooks in Codex and approve the Trellis hook through `/hooks`; "
        "then use --finalize --confirm-trellis-activation."
    ),
    (
        "完成 Trellis 的 00-bootstrap-guidelines，从真实代码生成首版 spec；"
        "确认后使用 --finalize --confirm-trellis-bootstrap。"
    ): (
        "Complete Trellis 00-bootstrap-guidelines against the real codebase "
        "to generate the initial spec; then use "
        "--finalize --confirm-trellis-bootstrap."
    ),
    "开始新 Agent 会话后使用 OpenSpec 的 propose/apply/archive Skills。": (
        "Start a new Agent session before using OpenSpec's "
        "propose/apply/archive Skills."
    ),
    "开始一个新 Codex 会话以加载插件。": (
        "Start a new Codex session to load the plugin."
    ),
    (
        "在 Claude Code 执行 `/plugin install superpowers@claude-plugins-official`，"
        "然后 `/reload-plugins`。"
    ): (
        "In Claude Code, run `/plugin install "
        "superpowers@claude-plugins-official`, then `/reload-plugins`."
    ),
    "在 Cursor Agent 执行 `/add-plugin superpowers`。": (
        "Run `/add-plugin superpowers` in Cursor Agent."
    ),
    "让 OpenCode 获取并严格执行 Superpowers 官方 `.opencode/INSTALL.md`。": (
        "Have OpenCode retrieve and follow the official Superpowers "
        "`.opencode/INSTALL.md` exactly."
    ),
    "非 Codex 官方插件由用户在宿主中安装；Agent Compass 只能记录显式人工确认。": (
        "Official plugins for non-Codex hosts are installed by the user; "
        "Agent Compass can only record explicit user confirmation."
    ),
    (
        "启用 Codex hooks 并通过 `/hooks` 审批后，"
        "使用 --finalize --confirm-trellis-activation。"
    ): (
        "After enabling Codex hooks and approving them through `/hooks`, "
        "use --finalize --confirm-trellis-activation."
    ),
    (
        "完成 00-bootstrap-guidelines 后，"
        "使用 --finalize --confirm-trellis-bootstrap。"
    ): (
        "After completing 00-bootstrap-guidelines, use "
        "--finalize --confirm-trellis-bootstrap."
    ),
    "非 Codex 官方插件状态来自用户显式确认，无法由 Agent Compass 机器验证。": (
        "Official plugin state for non-Codex hosts is user-attested and "
        "cannot be machine-verified by Agent Compass."
    ),
}


def localized_outcome_message(message: str, language: str) -> str:
    if detect_prompt_language(language) == "zh":
        return message
    translated = OUTCOME_MESSAGE_ENGLISH.get(message)
    if translated:
        return translated
    match = re.fullmatch(
        r"当前选择器无法无交互完成 Superpowers 在 (.+) 的官方安装。",
        message,
    )
    if match:
        return (
            "This selector cannot complete the official Superpowers "
            f"installation for {match.group(1)} non-interactively."
        )
    return message


ERROR_MESSAGE_ENGLISH = {
    "无法自动识别 Agent，请传入 --harness codex|claude-code|cursor|opencode。": (
        "Could not detect the Agent automatically; pass "
        "--harness codex|claude-code|cursor|opencode."
    ),
    "--harness auto 不能和其他 Agent 同时使用。": (
        "--harness auto cannot be combined with other Agents."
    ),
    "状态文件中的 framework 无效。": "Invalid framework in the state file.",
    "状态文件没有有效的 harnesses。": (
        "The state file has no valid harnesses."
    ),
    "状态文件 harnesses 必须全部是字符串。": (
        "State file harnesses must all be strings."
    ),
    "状态文件 readiness 必须是对象。": (
        "State file readiness must be an object."
    ),
    "状态文件 host_readiness 必须是对象。": (
        "State file host_readiness must be an object."
    ),
    "Trellis host_readiness 与 harnesses 不一致。": (
        "Trellis host_readiness is inconsistent with harnesses."
    ),
    "Superpowers host_readiness 与 harnesses 不一致。": (
        "Superpowers host_readiness is inconsistent with harnesses."
    ),
    "Matt status 无效。": "Invalid Matt status.",
    "状态文件 minimal 必须是布尔值。": (
        "State file minimal must be a boolean."
    ),
    "状态文件 verification 必须是布尔映射。": (
        "State file verification must be a mapping of booleans."
    ),
    ".trellis 存在但不是目录。": ".trellis exists but is not a directory.",
    (
        "检测到不完整的 .trellis 目录。为避免把半成品误判为成功，请先备份并清理，"
        "或确认后使用 --repair 让官方 CLI 尝试修复。"
    ): (
        "Detected an incomplete .trellis directory. To avoid treating a "
        "partial install as success, back it up and clean it first, or "
        "confirm and use --repair to let the official CLI attempt a repair."
    ),
    "openspec 存在但不是安全目录。": (
        "openspec exists but is not a safe directory."
    ),
    "opencode.json 不是有效 JSON，无法完成遗留框架检测。": (
        "opencode.json is not valid JSON, so legacy framework detection "
        "cannot complete."
    ),
    "skills 安装后未生成 skills-lock.json。": (
        "skills-lock.json was not generated after the skills install."
    ),
    "skills 安装后生成了无效锁文件。": (
        "The skills install produced an invalid lock file."
    ),
    "skills-lock.json 格式与 skills@1.5.9 不兼容。": (
        f"skills-lock.json format is incompatible with "
        f"skills@{SKILLS_CLI_VERSION}."
    ),
    "skills-lock.json 精确来源写入后验证失败。": (
        "Verification failed after writing the exact source into "
        "skills-lock.json."
    ),
    "skills-lock.json 中同一框架的提交不一致。": (
        "skills-lock.json records inconsistent commits for the same "
        "framework."
    ),
    "skills-lock.json 不是普通文件。": (
        "skills-lock.json is not a regular file."
    ),
    ".trellis 不完整；请备份后使用 --repair，或清理后重新初始化。": (
        ".trellis is incomplete; back it up and use --repair, or clean it "
        "and initialize again."
    ),
    "无法确定 Trellis 开发者名称，请传入 --user。": (
        "Could not determine the Trellis developer name; pass --user."
    ),
    "Trellis 开发者名称不能为空。": (
        "The Trellis developer name cannot be empty."
    ),
    "Codex 插件清单格式无法识别。": (
        "Unrecognized Codex plugin inventory format."
    ),
    "无法读取 Codex 插件冲突：缺少 codex 命令。": (
        "Cannot read Codex plugin conflicts: the codex command is missing."
    ),
    "Codex 官方 Marketplace 中未找到 Superpowers 插件。": (
        "The Superpowers plugin was not found in the official Codex "
        "Marketplace."
    ),
    "无法确定 Superpowers 插件所属 Marketplace。": (
        "Could not determine which Marketplace provides the Superpowers "
        "plugin."
    ),
    "Codex Superpowers 插件安装后未处于已安装/启用状态。": (
        "The Codex Superpowers plugin is not installed/enabled after "
        "installation."
    ),
    "无法记录 Codex Superpowers 插件身份。": (
        "Could not record the Codex Superpowers plugin identity."
    ),
    "Trellis 核心文件不完整。": "Trellis core files are incomplete.",
    "Superpowers project-skills 托管指令缺失或已变化。": (
        "The Superpowers project-skills managed instructions are missing or "
        "changed."
    ),
    "尚未检测到已安装并启用的 Codex Superpowers 插件。": (
        "No installed and enabled Codex Superpowers plugin was detected."
    ),
    "状态文件写入后验证失败。": (
        "Verification failed after writing the state file."
    ),
    "`none` 不能与安装、finalize 或配置参数组合。": (
        "`none` cannot be combined with install, finalize, or configuration "
        "options."
    ),
    "Trellis 确认参数只能用于 trellis --finalize。": (
        "Trellis confirmation options are only valid with "
        "trellis --finalize."
    ),
    "Superpowers 人工确认只适用于 official superpowers --finalize。": (
        "Superpowers manual confirmation only applies to official "
        "superpowers --finalize."
    ),
    "--repair 只适用于 Trellis 初始安装/修复。": (
        "--repair only applies to the initial Trellis install/repair."
    ),
    "--user/--trellis-version 只适用于 Trellis 安装。": (
        "--user/--trellis-version only apply to a Trellis install."
    ),
    "--openspec-version 只适用于 OpenSpec 安装。": (
        "--openspec-version only applies to an OpenSpec install."
    ),
    "--confirm-trellis-activation 只在已记录 Codex 宿主时有效。": (
        "--confirm-trellis-activation is only valid when a Codex host is "
        "recorded."
    ),
    "--confirm-superpowers-installation 只用于非 Codex 官方宿主。": (
        "--confirm-superpowers-installation only applies to non-Codex "
        "official hosts."
    ),
    "--timeout 必须大于 0。": "--timeout must be greater than 0.",
    "--doctor 不接受框架参数；它只读取已记录状态。": (
        "--doctor does not accept a framework argument; it only reads "
        "recorded state."
    ),
    "--doctor 只接受 --project-root、--timeout 和 --language。": (
        "--doctor only accepts --project-root, --timeout, and --language."
    ),
    "--finalize 需要显式框架，或一个有效的 Agent Compass 状态文件。": (
        "--finalize requires an explicit framework or a valid Agent Compass "
        "state file."
    ),
    "Matt 在本选择器中使用可编辑、可验证的 project-skills 模式。": (
        "Matt uses the editable, verifiable project-skills mode in this "
        "selector."
    ),
    (
        "Agent Compass 状态在确认期间已变化；"
        "为避免基于过期状态覆盖，请重新运行。"
    ): (
        "Agent Compass state changed during confirmation; re-run to avoid "
        "overwriting based on stale state."
    ),
}


def bootstrap_error_text(exc: BootstrapError, language: str) -> str:
    """Render a BootstrapError in the requested language.

    Prefers an explicit English variant, falls back to the static
    translation table, and finally to the original Chinese text.
    """
    message = str(exc)
    if detect_prompt_language(language) == "zh":
        return message
    explicit = getattr(exc, "en", None)
    if explicit:
        return explicit
    return ERROR_MESSAGE_ENGLISH.get(message, message)


def doctor_failure(exc: BootstrapError, language: str) -> str:
    return localized_text(
        language,
        f"诊断失败：{bootstrap_error_text(exc, 'zh')}",
        f"Doctor failed: {bootstrap_error_text(exc, 'en')}",
    )


def prompt_yes_no(
    question: str, *, default: bool = False, language: str = "zh"
) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "是", "好", "继续"}:
            return True
        if answer in {"n", "no", "否", "不", "取消"}:
            return False
        print("请输入 y 或 n。" if language == "zh" else "Enter y or n.")


def prompt_choice(
    question: str, options: Sequence[str], *, language: str = "zh"
) -> int:
    print(question)
    for index, option in enumerate(options, start=1):
        print(f"  {index}. {option}")
    while True:
        answer = input("请选择编号：" if language == "zh" else "Choose a number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        if language == "zh":
            print(f"请输入 1-{len(options)}。")
        else:
            print(f"Enter a number from 1 to {len(options)}.")


def choose_framework_interactively(language: str = "auto") -> tuple[str, bool]:
    """Choose one primary workflow after checking whether one is warranted."""
    language = detect_prompt_language(language)
    if language == "zh":
        fit_question = "这个任务是否同时满足：一次性、低风险、无需严格工程流程？"
        fit_options = (
            "是，三项都满足",
            "否，任一项不满足，或者我不确定",
        )
        need_question = "你最想解决哪类问题？"
        need_options = (
            "由我主导，按需调用调试、评审和 TDD Skills",
            "每次变更先形成可评审的规格，再开始实现",
            "跨会话接续项目规范、任务进度和设计决策",
            "让单次复杂任务遵循严格的规划、实现和验证流程",
            "仍然不安装任何框架",
        )
        minimal_question = "是否默认要求 AI 只做最小必要修改？"
    else:
        fit_question = (
            "Does this task meet all three conditions: one-off, low-risk, "
            "and no need for a strict engineering workflow?"
        )
        fit_options = (
            "Yes, all three conditions apply",
            "No, at least one does not apply, or I am unsure",
        )
        need_question = "What is the primary need?"
        need_options = (
            "Agent-led, on-demand debugging, review, and TDD skills",
            "Reviewable specifications before each implementation",
            "Project rules, task progress, and decisions across sessions",
            "A strict plan, implementation, and verification flow for one complex task",
            "Do not install a framework",
        )
        minimal_question = "Should the AI default to the smallest necessary change?"
    project_fit = prompt_choice(
        fit_question,
        fit_options,
        language=language,
    )
    if project_fit == 1:
        return "none", False

    choice = prompt_choice(
        need_question,
        need_options,
        language=language,
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
            minimal_question,
            default=False,
            language=language,
        )
    return framework, minimal


def trellis_status_from_readiness(readiness: Mapping[str, str]) -> str:
    normalized = {
        key: str(readiness.get(key, "unknown"))
        for key in ("installation", "activation", "bootstrap")
    }
    invalid = {
        f"{key}={value}"
        for key, value in normalized.items()
        if value not in READINESS_VALUES
    }
    if invalid:
        raise BootstrapError(
            "非法 Trellis readiness：" + ", ".join(sorted(invalid)),
            "Invalid Trellis readiness: " + ", ".join(sorted(invalid)),
        )
    installation = normalized["installation"]
    activation = normalized["activation"]
    bootstrap = normalized["bootstrap"]
    if installation != "ready":
        return "installed"
    if activation == "pending":
        return "activation_pending"
    if bootstrap == "pending":
        return "bootstrap_pending"
    if activation == "unknown" or bootstrap == "unknown":
        return "installed"
    return "ready"


def set_trellis_readiness(
    outcome: InstallOutcome,
    *,
    host_activation: Mapping[str, str],
    bootstrap: str,
) -> None:
    """Set Trellis readiness without collapsing unverifiable user actions."""
    if bootstrap not in READINESS_VALUES:
        raise BootstrapError(
            f"非法 Trellis bootstrap readiness：{bootstrap}",
            f"Invalid Trellis bootstrap readiness: {bootstrap}",
        )
    normalized_hosts: dict[str, dict[str, str]] = {}
    for harness, activation in host_activation.items():
        if harness not in ACTIVE_HARNESSES:
            raise BootstrapError(
                f"非法 Trellis host readiness：{harness}",
                f"Invalid Trellis host readiness: {harness}",
            )
        if activation not in READINESS_VALUES:
            raise BootstrapError(
                f"非法 Trellis activation readiness：{harness}={activation}",
                f"Invalid Trellis activation readiness: "
                f"{harness}={activation}",
            )
        normalized_hosts[harness] = {
            "installation": "ready",
            "activation": activation,
        }
    activation_values = {
        value["activation"] for value in normalized_hosts.values()
    }
    if "pending" in activation_values:
        activation = "pending"
    elif "unknown" in activation_values or not activation_values:
        activation = "unknown"
    else:
        activation = "ready"
    outcome.host_readiness = normalized_hosts
    outcome.readiness = {
        "installation": "ready",
        "activation": activation,
        "bootstrap": bootstrap,
    }
    outcome.status = trellis_status_from_readiness(outcome.readiness)


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


def normalize_harnesses(
    raw_values: Sequence[str] | None,
    root: Path,
    *,
    language: str = "auto",
) -> list[str]:
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
        language = detect_prompt_language(language)
        choice = prompt_choice(
            "选择 Agent：" if language == "zh" else "Choose an Agent:",
            ACTIVE_HARNESSES,
            language=language,
        )
        return [ACTIVE_HARNESSES[choice - 1]]

    if "auto" in values:
        raise BootstrapError("--harness auto 不能和其他 Agent 同时使用。")
    invalid = sorted(set(values) - set(HARNESSES))
    if invalid:
        raise BootstrapError("不支持的 Agent：" + ", ".join(invalid))
    return list(dict.fromkeys(values))


def require_executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise BootstrapError(
            f"缺少必需命令：{name}",
            f"Missing required command: {name}",
        )
    return path




COMMAND_DETAIL_MAX_LINES = 5
COMMAND_DETAIL_MAX_CHARS = 800


def command_failure_detail(stdout: str, stderr: str) -> str:
    """Return a trimmed tail of a failed command's output.

    Errors usually land on stderr, but some CLIs report them on stdout, so
    fall back to stdout when stderr is empty. The result is capped so a
    verbose command cannot flood the error message.
    """
    source = stderr.strip() or stdout.strip()
    if not source:
        return ""
    lines = source.splitlines()
    tail = lines[-COMMAND_DETAIL_MAX_LINES:]
    detail = "\n".join(line.rstrip() for line in tail).strip()
    if len(detail) > COMMAND_DETAIL_MAX_CHARS:
        detail = detail[-COMMAND_DETAIL_MAX_CHARS:].lstrip()
    return detail


def run(
    command: Sequence[str],
    *,
    cwd: Path,
    dry_run: bool,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    echo_output: bool = True,
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
        raise BootstrapError(
            f"命令超时（{timeout}s）：{printable}",
            f"Command timed out after {timeout}s: {printable}",
        ) from exc
    except OSError as exc:
        raise BootstrapError(
            f"无法执行命令：{printable}：{exc}",
            f"Could not execute command: {printable}: {exc}",
        ) from exc

    if completed.stdout and echo_output:
        print(completed.stdout, end="")
    if completed.stderr and echo_output:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        # Surface the failing command's own output. Callers that suppress
        # echo (echo_output=False) would otherwise discard the only
        # actionable cause, leaving just an exit code.
        detail = command_failure_detail(completed.stdout, completed.stderr)
        raise BootstrapError(
            f"命令失败（退出码 {completed.returncode}）：{printable}"
            + (f"\n命令输出：\n{detail}" if detail else ""),
            f"Command failed (exit code {completed.returncode}): {printable}"
            + (f"\nCommand output:\n{detail}" if detail else ""),
        )
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
        raise BootstrapError(
            f"命令未返回有效 JSON：{shlex.join(command)}",
            f"Command did not return valid JSON: {shlex.join(command)}",
        ) from exc


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
                f"需要 Node.js >= {required}，当前为：{result.stdout.strip()}",
                f"Node.js >= {required} is required, but found: "
                f"{result.stdout.strip()}",
            )
    if sys.version_info < (3, 10):
        raise BootstrapError(
            f"需要 Python >= 3.10，当前为：{sys.version.split()[0]}",
            f"Python >= 3.10 is required, but found: "
            f"{sys.version.split()[0]}",
        )


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
        root / ".codex/skills" / RETIRED_SKILL_NAME,
        root / ".cursor/skills" / RETIRED_SKILL_NAME,
        root / ".opencode/skills" / RETIRED_SKILL_NAME,
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
            f"{STATE_FILE} 已损坏，拒绝继续覆盖；请修复或移走该文件后重试。",
            f"{STATE_FILE} is corrupt; refusing to overwrite it. Repair or "
            "move the file, then retry.",
        ) from exc
    if not isinstance(state, dict):
        raise BootstrapError(
            f"{STATE_FILE} 顶层必须是 JSON 对象。",
            f"The top level of {STATE_FILE} must be a JSON object.",
        )
    return state


def validate_recorded_state(
    state: Mapping[str, Any], *, require_current_schema: bool
) -> tuple[str, str, list[str]]:
    schema = state.get("schema")
    if not isinstance(schema, int) or schema not in READABLE_STATE_SCHEMAS:
        raise BootstrapError(
            f"状态文件 schema 不受支持：{schema!r}",
            f"Unsupported state file schema: {schema!r}",
        )
    if require_current_schema and schema != STATE_SCHEMA:
        raise BootstrapError(
            f"状态文件 schema {schema} 需要重新 finalize 为 schema {STATE_SCHEMA}。",
            f"State file schema {schema} must be finalized again into "
            f"schema {STATE_SCHEMA}.",
        )

    framework = canonical_framework(str(state.get("framework") or ""))
    integration = str(state.get("integration") or "")
    valid_integrations = {
        "trellis": {"official"},
        "openspec": {"official"},
        "matt": {"project-skills"},
        "superpowers": {"official", "project-skills"},
    }
    if framework not in valid_integrations:
        raise BootstrapError("状态文件中的 framework 无效。")
    if integration not in valid_integrations[framework]:
        raise BootstrapError(
            f"状态文件中的集成组合无效：{framework}/{integration}。",
            f"Invalid integration combination in the state file: "
            f"{framework}/{integration}.",
        )

    raw_harnesses = state.get("harnesses")
    if not isinstance(raw_harnesses, list) or not raw_harnesses:
        raise BootstrapError("状态文件没有有效的 harnesses。")
    if any(not isinstance(item, str) for item in raw_harnesses):
        raise BootstrapError("状态文件 harnesses 必须全部是字符串。")
    harnesses = list(dict.fromkeys(raw_harnesses))
    invalid = sorted(set(harnesses) - set(ACTIVE_HARNESSES))
    if invalid or len(harnesses) != len(raw_harnesses):
        raise BootstrapError(
            "状态文件包含无效 harnesses："
            + (", ".join(invalid) if invalid else "存在重复项"),
            "The state file contains invalid harnesses: "
            + (", ".join(invalid) if invalid else "duplicate entries"),
        )

    status = str(state.get("status") or "")
    if status not in STATE_STATUSES:
        raise BootstrapError(
            f"状态文件中的 status 无效：{status!r}",
            f"Invalid status in the state file: {status!r}",
        )

    if schema == STATE_SCHEMA:
        readiness = state.get("readiness", {})
        if not isinstance(readiness, Mapping):
            raise BootstrapError("状态文件 readiness 必须是对象。")
        for key, value in readiness.items():
            if not isinstance(key, str) or str(value) not in READINESS_VALUES:
                raise BootstrapError(
                    f"状态文件 readiness 无效：{key}={value}",
                    f"Invalid state file readiness: {key}={value}",
                )
        host_readiness = state.get("host_readiness", {})
        if not isinstance(host_readiness, Mapping):
            raise BootstrapError("状态文件 host_readiness 必须是对象。")
        for host, values in host_readiness.items():
            if host not in ACTIVE_HARNESSES or not isinstance(values, Mapping):
                raise BootstrapError(
                    f"状态文件 host_readiness 无效：{host}",
                    f"Invalid state file host_readiness: {host}",
                )
            for key, value in values.items():
                if not isinstance(key, str) or str(value) not in READINESS_VALUES:
                    raise BootstrapError(
                        f"状态文件 host_readiness 无效：{host}.{key}={value}",
                        f"Invalid state file host_readiness: "
                        f"{host}.{key}={value}",
                    )
        if framework == "trellis" and set(host_readiness) != set(harnesses):
            raise BootstrapError("Trellis host_readiness 与 harnesses 不一致。")
        if framework == "superpowers" and integration == "official" and set(
            host_readiness
        ) != set(harnesses):
            raise BootstrapError("Superpowers host_readiness 与 harnesses 不一致。")

        phased = framework == "trellis" or (
            framework == "superpowers" and integration == "official"
        )
        if phased:
            expected_readiness_keys = (
                {"installation", "activation", "bootstrap"}
                if framework == "trellis"
                else {"installation", "activation"}
            )
            if set(readiness) != expected_readiness_keys:
                raise BootstrapError(
                    f"{framework} readiness 字段不完整。",
                    f"{framework} readiness fields are incomplete.",
                )
            for host, values in host_readiness.items():
                if set(values) != {"installation", "activation"}:
                    raise BootstrapError(
                        f"{framework} host_readiness 字段不完整：{host}",
                        f"{framework} host_readiness fields are incomplete: "
                        f"{host}",
                    )
            expected_installation = aggregate_host_readiness(
                host_readiness, "installation"
            )
            expected_activation = aggregate_host_readiness(
                host_readiness, "activation"
            )
            if (
                readiness.get("installation") != expected_installation
                or readiness.get("activation") != expected_activation
            ):
                raise BootstrapError(
                    f"{framework} 总体 readiness 与逐宿主状态不一致。",
                    f"{framework} aggregate readiness is inconsistent with "
                    "the per-host state.",
                )
            expected_status = (
                trellis_status_from_readiness(readiness)
                if framework == "trellis"
                else (
                    "ready"
                    if expected_installation == "ready"
                    and expected_activation == "ready"
                    else "pending"
                )
            )
            if status != expected_status:
                raise BootstrapError(
                    f"{framework} status 与 readiness 不一致。",
                    f"{framework} status is inconsistent with readiness.",
                )
        elif readiness or host_readiness:
            raise BootstrapError(
                f"{framework}/{integration} 不应包含分阶段 readiness。",
                f"{framework}/{integration} must not contain phased "
                "readiness.",
            )

        if (
            framework == "openspec"
            or framework == "superpowers" and integration == "project-skills"
        ) and status != "ready":
            raise BootstrapError(
                f"{framework}/{integration} status 无效。",
                f"Invalid {framework}/{integration} status.",
            )
        if framework == "matt" and status not in {"pending", "ready"}:
            raise BootstrapError("Matt status 无效。")
        if not isinstance(state.get("minimal"), bool):
            raise BootstrapError("状态文件 minimal 必须是布尔值。")
        for key in ("versions", "identities", "source_revisions", "checksums"):
            value = state.get(key, {})
            if not isinstance(value, Mapping) or any(
                not isinstance(item_key, str) or not isinstance(item_value, str)
                for item_key, item_value in value.items()
            ):
                raise BootstrapError(
                    f"状态文件 {key} 必须是字符串映射。",
                    f"State file {key} must be a mapping of strings.",
                )
        verification = state.get("verification", {})
        if not isinstance(verification, Mapping) or any(
            not isinstance(item_key, str) or not isinstance(item_value, bool)
            for item_key, item_value in verification.items()
        ):
            raise BootstrapError("状态文件 verification 必须是布尔映射。")
        for key in (
            "created_or_managed",
            "pending_actions",
            "activation_notes",
            "limitations",
        ):
            value = state.get(key, [])
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise BootstrapError(
                    f"状态文件 {key} 必须是字符串列表。",
                    f"State file {key} must be a list of strings.",
                )

    return framework, integration, harnesses


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
            raise BootstrapError(
                f"技能锁文件损坏：{relative}",
                f"Corrupt skills lock file: {relative}",
            ) from exc
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


def detect_project_skill_frameworks(root: Path) -> set[str]:
    """Detect only complete, distinctive framework Skill sets."""
    found: set[str] = set()
    roots = {
        root / ".agents/skills",
        root / ".claude/skills",
        root / ".codex/skills",
        root / ".cursor/skills",
        root / ".opencode/skills",
    }
    for base in roots:
        if not lstat_exists(base):
            continue
        ensure_safe_path(root, base, for_write=False)
        if not base.is_dir() or base.is_symlink():
            raise BootstrapError(
                f"Skill 根路径不是安全目录：{base.relative_to(root)}",
                f"Skill root is not a safe directory: "
                f"{base.relative_to(root)}",
            )
        for framework, skills in EXPECTED_SKILLS.items():
            complete = True
            for skill in skills:
                path = base / skill / "SKILL.md"
                if not lstat_exists(path):
                    complete = False
                    break
                ensure_safe_path(root, path, for_write=False)
                if not path.is_file() or path.is_symlink():
                    complete = False
                    break
            if complete:
                found.add(framework)
    return found





def detect_existing_frameworks(root: Path, *, repair: bool = False) -> set[str]:
    found: set[str] = set()
    state = load_state(root)
    if state:
        framework, _, _ = validate_recorded_state(
            state, require_current_schema=False
        )
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
            raise BootstrapError(
                f"{path.relative_to(root)} 是符号链接，拒绝继续。",
                f"{path.relative_to(root)} is a symbolic link; refusing to "
                "continue.",
            )
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
    found.update(detect_project_skill_frameworks(root))
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
        raise BootstrapError(
            f"无需写入框架托管指令：{framework}",
            f"No managed framework instruction needs to be written: "
            f"{framework}",
        )
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
        action = "would write" if dry_run else "write"
        print(f"{action} {path.relative_to(root)}")
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
        action = "would write" if dry_run else "write"
        print(f"{action} {path.relative_to(root)} (minimal)")
        if not dry_run:
            transaction.snapshot(path)
            mode = stat.S_IMODE(path.lstat().st_mode) if lstat_exists(path) else 0o644
            atomic_write_text(root, path, updated, mode=mode)
        written.append(str(path.relative_to(root)))
    return written


def verify_minimal_policy(root: Path, harnesses: Sequence[str]) -> bool:
    expected = minimal_block()
    for path in instruction_files(root, harnesses):
        if not lstat_exists(path) or expected not in safe_read_text(root, path):
            return False
    return True


def verify_framework_instruction(
    root: Path,
    harnesses: Sequence[str],
    framework: str,
    integration: str,
) -> bool:
    expected = managed_block(framework, integration)
    for path in instruction_files(root, harnesses):
        if not lstat_exists(path) or expected not in safe_read_text(root, path):
            return False
    return True



def resolve_npm_version(
    root: Path,
    package: str,
    requested: str | None,
    *,
    dry_run: bool,
    timeout: int,
) -> str:
    if requested:
        if not EXACT_SEMVER.fullmatch(requested):
            raise BootstrapError(
                f"npm 版本必须是准确 semver，不能使用 dist-tag 或范围：{requested}"
            )
        return requested
    if dry_run:
        return "<resolved-version>"
    data = run_json(("npm", "view", package, "version", "--json"), cwd=root, dry_run=False, timeout=timeout)
    if not isinstance(data, str) or not EXACT_SEMVER.fullmatch(data.strip()):
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


@contextmanager
def pinned_repository_checkout(
    root: Path,
    source: str,
    revision: str,
    *,
    timeout: int,
) -> Iterator[Path]:
    """Yield a temporary checkout whose HEAD is the exact recorded revision."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise BootstrapError(f"无法检出非精确提交：{revision}")
    require_executable("git")
    url = f"https://github.com/{source}.git"
    with tempfile.TemporaryDirectory(
        prefix="agent-compass-source-"
    ) as temp_name:
        checkout = Path(temp_name) / "source"
        run(
            ("git", "init", "--quiet", str(checkout)),
            cwd=root,
            dry_run=False,
            timeout=timeout,
        )
        run(
            (
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--quiet",
                "--depth",
                "1",
                url,
                revision,
            ),
            cwd=root,
            dry_run=False,
            timeout=timeout,
        )
        run(
            (
                "git",
                "-C",
                str(checkout),
                "checkout",
                "--quiet",
                "--detach",
                "FETCH_HEAD",
            ),
            cwd=root,
            dry_run=False,
            timeout=timeout,
        )
        actual = run(
            ("git", "-C", str(checkout), "rev-parse", "HEAD"),
            cwd=root,
            dry_run=False,
            timeout=timeout,
            echo_output=False,
        ).stdout.strip().lower()
        if actual != revision:
            raise BootstrapError(
                f"上游检出提交不一致：期望 {revision}，实际 {actual}"
            )
        yield checkout


def read_project_skills_lock(root: Path) -> tuple[Path, dict[str, Any]]:
    path = root / "skills-lock.json"
    if not lstat_exists(path):
        raise BootstrapError("skills 安装后未生成 skills-lock.json。")
    try:
        data = json.loads(safe_read_text(root, path))
    except json.JSONDecodeError as exc:
        raise BootstrapError("skills 安装后生成了无效锁文件。") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("skills"), dict)
    ):
        raise BootstrapError("skills-lock.json 格式与 skills@1.5.9 不兼容。")
    return path, data


def rewrite_project_skills_lock(
    root: Path,
    framework: str,
    revision: str,
) -> None:
    """Replace temporary-local provenance with the exact remote revision."""
    path, data = read_project_skills_lock(root)
    source = SOURCE_REPOSITORIES[framework]
    for skill in EXPECTED_SKILLS[framework]:
        entry = data["skills"].get(skill)
        if not isinstance(entry, dict):
            raise BootstrapError(
                f"skills-lock.json 缺少条目：{skill}",
                f"skills-lock.json is missing an entry: {skill}",
            )
        entry["source"] = source
        entry["sourceType"] = "github"
        entry["ref"] = revision
    atomic_write_text(
        root,
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        mode=stat.S_IMODE(path.lstat().st_mode),
    )
    verified = json.loads(safe_read_text(root, path))
    if any(
        verified["skills"][skill].get("source") != source
        or verified["skills"][skill].get("ref") != revision
        for skill in EXPECTED_SKILLS[framework]
    ):
        raise BootstrapError("skills-lock.json 精确来源写入后验证失败。")


def verify_project_skills_lock_provenance(
    root: Path,
    framework: str,
) -> str:
    """Return the one exact revision shared by all selected Skill entries."""
    _, data = read_project_skills_lock(root)
    source = SOURCE_REPOSITORIES[framework]
    revisions: set[str] = set()
    for skill in EXPECTED_SKILLS[framework]:
        entry = data["skills"].get(skill)
        if not isinstance(entry, dict):
            raise BootstrapError(
                f"skills-lock.json 缺少条目：{skill}",
                f"skills-lock.json is missing an entry: {skill}",
            )
        revision = str(entry.get("ref") or "").lower()
        if (
            entry.get("source") != source
            or entry.get("sourceType") != "github"
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
        ):
            raise BootstrapError(
                f"skills-lock.json 来源未精确固定：{skill}",
                f"skills-lock.json source is not pinned exactly: {skill}",
            )
        revisions.add(revision)
    if len(revisions) != 1:
        raise BootstrapError("skills-lock.json 中同一框架的提交不一致。")
    return revisions.pop()


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
            reject_symlinks_in_tree(root, skill_root)
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
    return hash_directory(root, skill_file.parent)


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
                raise BootstrapError(
                    f"安装后未找到预期 Skill：{root_label}/{skill}",
                    f"Expected Skill not found after installation: "
                    f"{root_label}/{skill}",
                )
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
    skills = EXPECTED_SKILLS[framework]
    outcome = InstallOutcome(framework=framework, integration="project-skills")
    revision = resolve_repository_head(
        root, SOURCE_REPOSITORIES[framework], dry_run=dry_run, timeout=timeout
    )
    outcome.source_revisions[SOURCE_REPOSITORIES[framework]] = revision
    lock_path = root / "skills-lock.json"
    if not dry_run:
        transaction.snapshot(lock_path)
    mutation_tracker.mark(dry_run=dry_run)
    if dry_run:
        run(
            skills_command(
                f"{SOURCE_REPOSITORIES[framework]}#{revision}",
                skills,
                harnesses,
            ),
            cwd=root,
            dry_run=True,
            timeout=timeout,
            env={"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
        )
    else:
        with pinned_repository_checkout(
            root,
            SOURCE_REPOSITORIES[framework],
            revision,
            timeout=timeout,
        ) as checkout:
            run(
                skills_command(checkout, skills, harnesses),
                cwd=root,
                dry_run=False,
                timeout=timeout,
                env={"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
            )
        rewrite_project_skills_lock(root, framework, revision)
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
        outcome.verification["framework-instruction"] = True
        outcome.limitations.append(
            "项目 Skills 模式不包含宿主插件的 SessionStart Hook 或插件自动更新。"
        )
        if not dry_run:
            outcome.activation_notes.append(
                "如当前 Agent 尚未发现新 Skill，请开始一个新会话。"
            )
    elif framework == "matt":
        outcome.status = "pending"
        outcome.pending_actions.append(
            "在当前 Agent 会话调用 `setup-matt-pocock-skills`，完成 issue tracker 和 domain docs 配置；"
            "随后运行本脚本 `matt --finalize`。"
        )
    else:
        raise BootstrapError(f"不支持的项目 Skills 框架：{framework}")
    return outcome


def meaningful_markdown(root: Path, path: Path) -> bool:
    if not lstat_exists(path):
        return False
    text = safe_read_text(root, path).strip()
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 40 or not re.search(r"(?m)^#{1,6}\s+\S", text):
        return False
    return text.lower() not in {"ok", "todo", "tbd", "placeholder"}


def agent_skills_section(root: Path, path: Path) -> str | None:
    if not lstat_exists(path):
        return None
    text = safe_read_text(root, path)
    match = re.search(
        r"(?ms)^## Agent skills\s*\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
    )
    if not match:
        return None
    body = match.group("body").strip()
    return body if len(re.sub(r"\s+", "", body)) >= 40 else None


def verify_matt_setup(
    root: Path, harnesses: Sequence[str]
) -> dict[str, bool]:
    required = {
        "docs/agents/issue-tracker.md": root / "docs/agents/issue-tracker.md",
        "docs/agents/domain.md": root / "docs/agents/domain.md",
    }
    result: dict[str, bool] = {}
    for name, path in required.items():
        result[name] = meaningful_markdown(root, path)
    for path in instruction_files(root, harnesses):
        key = f"instruction:{path.relative_to(root)}:Agent skills"
        result[key] = agent_skills_section(root, path) is not None
    return result


def matt_setup_checksums(
    root: Path, harnesses: Sequence[str]
) -> dict[str, str]:
    checksums = {
        "setup:docs/agents/issue-tracker.md": hash_file(
            root, root / "docs/agents/issue-tracker.md"
        ),
        "setup:docs/agents/domain.md": hash_file(
            root, root / "docs/agents/domain.md"
        ),
    }
    for path in instruction_files(root, harnesses):
        section = agent_skills_section(root, path)
        if section is None:
            raise BootstrapError(
                f"{path.relative_to(root)} 缺少实质 Agent skills 指令。",
                f"{path.relative_to(root)} lacks substantive Agent skills "
                "instructions.",
            )
        key = f"setup:instruction:{path.relative_to(root)}:Agent skills"
        checksums[key] = hashlib.sha256(
            section.encode("utf-8")
        ).hexdigest()
    return checksums


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
        reject_symlinks_in_tree(root, target)


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
    language: str = "auto",
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
            developer = input(
                localized_text(
                    language,
                    "Trellis 开发者名称：",
                    "Trellis developer name: ",
                )
            ).strip()
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
        outcome.checksums[name] = hash_verified_path(
            root, path, expect_directory=expect_directory
        )
    outcome.created_or_managed.extend(sorted(required))
    outcome.limitations.append("Trellis 以 AGPL-3.0 发布；企业或客户仓库使用前应确认内部合规要求。")
    host_activation = {
        harness: "pending" if harness == "codex" else "ready"
        for harness in harnesses
    }
    bootstrap = "pending" if not existing_valid else "unknown"
    set_trellis_readiness(
        outcome,
        host_activation=host_activation,
        bootstrap=bootstrap,
    )
    if host_activation.get("codex") == "pending":
        outcome.pending_actions.append(
            "在 Codex 中启用 hooks，并通过 `/hooks` 审核 Trellis Hook；"
            "完成后使用 --finalize --confirm-trellis-activation。"
        )
    if bootstrap != "ready":
        outcome.pending_actions.append(
            "完成 Trellis 的 00-bootstrap-guidelines，从真实代码生成首版 spec；"
            "确认后使用 --finalize --confirm-trellis-bootstrap。"
        )
    return outcome



def verify_generated_skill_document(
    root: Path, skill_file: Path, expected_name: str
) -> bool:
    text = safe_read_text(root, skill_file)
    match = re.match(
        r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)\Z",
        text,
        re.DOTALL,
    )
    if not match:
        return False
    fields: dict[str, str] = {}
    for line in match.group("frontmatter").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip("\"'")
    description = fields.get("description", "")
    body = re.sub(r"\s+", "", match.group("body"))
    return (
        fields.get("name") == expected_name
        and len(description) >= 20
        and len(body) >= 40
    )


def verify_openspec_skills(
    root: Path, harnesses: Sequence[str], *, dry_run: bool
) -> tuple[dict[str, bool], dict[str, str], list[str]]:
    verification: dict[str, bool] = {}
    checksums: dict[str, str] = {}
    created: list[str] = []
    for harness in harnesses:
        base = root / OPENSPEC_SKILL_ROOT[harness]
        key = f"{OPENSPEC_SKILL_ROOT[harness].as_posix()}:openspec-*/SKILL.md"
        if dry_run:
            verification[key] = True
            continue
        if not lstat_exists(base):
            verification[key] = False
            raise BootstrapError(
                f"OpenSpec {harness} Skill 根目录不存在。",
                f"The OpenSpec {harness} Skill root directory does not exist.",
            )
        ensure_safe_path(root, base, for_write=False)
        if not base.is_dir() or base.is_symlink():
            raise BootstrapError(
                f"OpenSpec {harness} Skill 根目录不安全。",
                f"The OpenSpec {harness} Skill root directory is not safe.",
            )
        reject_symlinks_in_tree(root, base)
        skill_files: list[Path] = []
        for child in sorted(base.iterdir()):
            if not child.name.startswith("openspec-") or not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            if not lstat_exists(skill_file):
                continue
            ensure_safe_path(root, skill_file, for_write=False)
            if skill_file.is_file() and not skill_file.is_symlink():
                if not verify_generated_skill_document(
                    root, skill_file, child.name
                ):
                    raise BootstrapError(
                        f"OpenSpec {harness} Skill 文档无效或为占位内容："
                        f"{skill_file.relative_to(root)}",
                        f"The OpenSpec {harness} Skill document is invalid or "
                        f"placeholder content: {skill_file.relative_to(root)}",
                    )
                skill_files.append(skill_file)
        verification[key] = bool(skill_files)
        if not skill_files:
            raise BootstrapError(
                f"OpenSpec {harness} 未生成有效的 openspec-*/SKILL.md。",
                f"OpenSpec {harness} did not generate a valid "
                "openspec-*/SKILL.md.",
            )
        for skill_file in skill_files:
            relative = skill_file.parent.relative_to(root).as_posix()
            checksums[relative] = hash_skill_directory(root, skill_file)
            created.append(relative + "/")
    return verification, checksums, created


def protect_paths(root: Path, paths: Iterable[Path]) -> None:
    for path in paths:
        ensure_safe_path(root, path, for_write=True)
        reject_symlinks_in_tree(root, path)


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
    verification, checksums, created = verify_openspec_skills(
        root, harnesses, dry_run=dry_run
    )
    outcome.verification.update(verification)
    outcome.checksums.update(checksums)
    outcome.created_or_managed.extend(created)
    if not dry_run:
        outcome.activation_notes.append(
            "开始新 Agent 会话后使用 OpenSpec 的 propose/apply/archive Skills。"
        )
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


def parse_codex_plugin_list(raw: str) -> dict[str, list[dict[str, Any]]]:
    """Parse the human-readable inventory emitted by current Codex CLIs."""
    entries: list[dict[str, Any]] = []
    marketplace = ""
    for line in raw.splitlines():
        marketplace_match = re.fullmatch(r"Marketplace `([^`]+)`", line.strip())
        if marketplace_match:
            marketplace = marketplace_match.group(1)
            continue
        plugin_match = re.fullmatch(
            r"\s{2}(.+?)@([^\s]+) \((installed|not installed)"
            r"(?:, (enabled|disabled))?\)",
            line,
        )
        if not plugin_match:
            continue
        name, listed_marketplace, installed, enabled = plugin_match.groups()
        effective_marketplace = marketplace or listed_marketplace
        entries.append(
            {
                "name": name,
                "pluginId": f"{name}@{effective_marketplace}",
                "marketplaceName": effective_marketplace,
                "installed": installed == "installed",
                "enabled": enabled == "enabled",
            }
        )
    return {"plugins": entries}


def plugin_name(entry: Mapping[str, Any]) -> str:
    return str(entry.get("name") or entry.get("pluginId") or entry.get("id") or "")


def plugin_marketplace(entry: Mapping[str, Any]) -> str:
    return str(entry.get("marketplaceName") or entry.get("marketplace") or "")


def plugin_is_installed(entry: Mapping[str, Any]) -> bool:
    section = str(entry.get("_selector_section") or "")
    installed = entry.get("installed")
    if installed is False or section == "available" and installed is not True:
        return False
    return installed is True or section == "installed"


def plugin_spec(entry: Mapping[str, Any]) -> str:
    name = plugin_name(entry).split("@", 1)[0]
    marketplace = plugin_marketplace(entry)
    if not marketplace:
        plugin_id = str(entry.get("pluginId") or "")
        if "@" in plugin_id:
            marketplace = plugin_id.split("@", 1)[1]
    safe_part = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
    if not safe_part.fullmatch(name) or not safe_part.fullmatch(marketplace):
        return ""
    return f"{name}@{marketplace}"


def find_codex_plugin(
    data: Any,
    framework: str,
    *,
    installed_only: bool = False,
    marketplace: str | None = None,
) -> dict[str, Any] | None:
    names = PLUGIN_FRAMEWORK_NAMES[framework]
    matches: list[dict[str, Any]] = []
    for entry in codex_plugin_entries(data):
        name = plugin_name(entry).lower().split("@")[0]
        if name not in names:
            continue
        if marketplace and plugin_marketplace(entry) != marketplace:
            continue
        if installed_only and not plugin_is_installed(entry):
            continue
        matches.append(entry)
    if not matches:
        return None

    def priority(entry: Mapping[str, Any]) -> tuple[bool, bool, bool]:
        installed = plugin_is_installed(entry)
        enabled = bool(entry.get("enabled", True))
        return (
            not (installed and enabled),
            plugin_marketplace(entry) != "openai-curated",
            not installed,
        )

    return min(matches, key=priority)


def codex_plugin_inventory(root: Path, *, dry_run: bool, timeout: int, include_available: bool) -> Any:
    require_executable("codex")
    result = run(
        ("codex", "plugin", "list"),
        cwd=root,
        dry_run=dry_run,
        timeout=timeout,
        echo_output=False,
    )
    if dry_run:
        return {}
    raw = result.stdout.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = parse_codex_plugin_list(raw)
    if not codex_plugin_entries(data):
        raise BootstrapError("Codex 插件清单格式无法识别。")
    if include_available:
        return data
    installed = [
        entry
        for entry in codex_plugin_entries(data)
        if plugin_is_installed(entry)
    ]
    return {"plugins": installed}



def detect_codex_plugin_frameworks(
    root: Path,
    *,
    dry_run: bool,
    timeout: int,
    inventory_out: dict[str, Any] | None = None,
) -> set[str]:
    if not shutil.which("codex"):
        raise BootstrapError("无法读取 Codex 插件冲突：缺少 codex 命令。")
    data = codex_plugin_inventory(
        root, dry_run=False, timeout=timeout, include_available=True
    )
    if inventory_out is not None:
        inventory_out["codex"] = data
    entry = find_codex_plugin(data, "superpowers", installed_only=True)
    if entry and bool(entry.get("enabled", True)):
        return {"superpowers"}
    return set()



def install_codex_plugin(
    root: Path,
    framework: str,
    *,
    dry_run: bool,
    timeout: int,
    mutation_tracker: MutationTracker,
    plugin_inventory: Any | None = None,
) -> InstallOutcome:
    if framework != "superpowers":
        raise BootstrapError(f"不支持的 Codex 官方插件：{framework}")
    require_executable("codex")
    outcome = InstallOutcome(framework=framework, integration="official")

    inventory = (
        plugin_inventory
        if plugin_inventory is not None
        else codex_plugin_inventory(
            root,
            dry_run=False,
            timeout=timeout,
            include_available=True,
        )
    )
    entry = find_codex_plugin(
        inventory,
        framework,
        marketplace=CODEX_OFFICIAL_MARKETPLACE,
    )
    if not entry:
        raise BootstrapError(
            "Codex 官方 Marketplace 中未找到 Superpowers 插件。"
        )
    resolved_spec = plugin_spec(entry)
    if not resolved_spec:
        raise BootstrapError("无法确定 Superpowers 插件所属 Marketplace。")
    plugin_spec_value = resolved_spec

    installed_entry = find_codex_plugin(
        inventory,
        framework,
        installed_only=True,
        marketplace=CODEX_OFFICIAL_MARKETPLACE,
    )
    if installed_entry and not bool(installed_entry.get("enabled", True)):
        raise BootstrapError(
            f"Codex Superpowers 插件 {plugin_spec_value} 已安装但未启用；"
            "请先在 Codex 中重新启用，或显式移除后再重试。"
        )
    if not installed_entry:
        mutation_tracker.mark(dry_run=dry_run)
        run(
            ("codex", "plugin", "add", plugin_spec_value),
            cwd=root,
            dry_run=dry_run,
            timeout=timeout,
        )
        outcome.external_commands_ran = not dry_run

    if dry_run:
        outcome.verification["codex-plugin:superpowers"] = True
    else:
        final_inventory = codex_plugin_inventory(
            root, dry_run=False, timeout=timeout, include_available=False
        )
        final_entry = find_codex_plugin(
            final_inventory,
            framework,
            installed_only=True,
            marketplace=CODEX_OFFICIAL_MARKETPLACE,
        )
        ok = final_entry is not None and bool(final_entry.get("enabled", True))
        outcome.verification["codex-plugin:superpowers"] = ok
        if not ok:
            raise BootstrapError(
                "Codex Superpowers 插件安装后未处于已安装/启用状态。"
            )
        if final_entry and final_entry.get("version"):
            outcome.versions[framework] = str(final_entry["version"])
        if final_entry:
            final_spec = plugin_spec(final_entry)
            if not final_spec:
                raise BootstrapError("无法记录 Codex Superpowers 插件身份。")
            outcome.identities["codex-plugin:superpowers"] = final_spec

    if dry_run:
        outcome.identities["codex-plugin:superpowers"] = plugin_spec_value
    outcome.host_readiness["codex"] = {
        "installation": "ready",
        "activation": "ready",
    }

    outcome.created_or_managed.append("Codex plugin: superpowers")
    if not dry_run:
        outcome.activation_notes.append("开始一个新 Codex 会话以加载插件。")
    return outcome












def pending_official_install(
    framework: str, harnesses: Sequence[str]
) -> InstallOutcome:
    if framework != "superpowers":
        raise BootstrapError(
            f"不支持的待人工官方安装：{framework}",
            f"Unsupported pending manual official installation: {framework}",
        )
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
        outcome.host_readiness[harness] = {
            "installation": "pending",
            "activation": "unknown",
        }
        outcome.pending_actions.append(
            actions.get(
                harness,
                f"当前选择器无法无交互完成 Superpowers 在 {harness} 的官方安装。",
            )
        )
    outcome.limitations.append(
        "非 Codex 官方插件由用户在宿主中安装；Agent Compass 只能记录显式人工确认。"
    )
    return outcome


def merge_outcome(target: InstallOutcome, source: InstallOutcome) -> None:
    target.created_or_managed.extend(source.created_or_managed)
    target.versions.update(source.versions)
    target.identities.update(source.identities)
    target.source_revisions.update(source.source_revisions)
    target.checksums.update(source.checksums)
    target.verification.update(source.verification)
    target.host_readiness.update(source.host_readiness)
    target.pending_actions.extend(source.pending_actions)
    target.activation_notes.extend(source.activation_notes)
    target.limitations.extend(source.limitations)
    target.external_commands_ran = (
        target.external_commands_ran or source.external_commands_ran
    )


def install_superpowers_official(
    root: Path,
    harnesses: Sequence[str],
    *,
    dry_run: bool,
    timeout: int,
    mutation_tracker: MutationTracker,
    plugin_inventory: Any | None = None,
) -> InstallOutcome:
    outcome = InstallOutcome("superpowers", "official")
    if "codex" in harnesses:
        merge_outcome(
            outcome,
            install_codex_plugin(
                root,
                "superpowers",
                dry_run=dry_run,
                timeout=timeout,
                mutation_tracker=mutation_tracker,
                plugin_inventory=plugin_inventory,
            ),
        )
    manual_hosts = [harness for harness in harnesses if harness != "codex"]
    if manual_hosts:
        merge_outcome(
            outcome,
            pending_official_install("superpowers", manual_hosts),
        )
        outcome.status = "pending"
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
    plugin_inventory: Any | None = None,
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
            language=args.language,
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
    return install_superpowers_official(
        root,
        harnesses,
        dry_run=args.dry_run,
        timeout=args.timeout,
        mutation_tracker=mutation_tracker,
        plugin_inventory=plugin_inventory,
    )



def finalize_framework(
    root: Path,
    framework: str,
    harnesses: Sequence[str],
    integration: str,
    *,
    dry_run: bool,
    timeout: int,
    confirm_trellis_activation: bool = False,
    confirm_trellis_bootstrap: bool = False,
    confirm_superpowers_installation: bool = False,
    plugin_inventory: Any | None = None,
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
            verify_matt_setup(root, harnesses)
            if not dry_run
            else dict.fromkeys(
                [
                    "docs/agents/issue-tracker.md",
                    "docs/agents/domain.md",
                    *(
                        f"instruction:{path.relative_to(root)}:Agent skills"
                        for path in instruction_files(root, harnesses)
                    ),
                ],
                True,
            )
        )
        outcome.verification.update(setup)
        missing = [name for name, ok in setup.items() if not ok]
        if missing:
            raise BootstrapError(
                "Matt 初始化尚未完成，缺少：" + ", ".join(missing),
                "Matt initialization is not complete; missing: "
                + ", ".join(missing),
            )
        if not dry_run:
            outcome.checksums.update(
                matt_setup_checksums(root, harnesses)
            )
            outcome.source_revisions[
                SOURCE_REPOSITORIES["matt"]
            ] = verify_project_skills_lock_provenance(root, "matt")
        else:
            outcome.source_revisions[
                SOURCE_REPOSITORIES["matt"]
            ] = "<verified-lock-revision>"
        outcome.versions["skills-cli"] = SKILLS_CLI_VERSION
        return outcome

    if framework == "trellis":
        if not trellis_core_valid(root):
            raise BootstrapError("Trellis 核心文件不完整。")
        outcome.verification["trellis-core"] = True
        for relative in (
            Path(".trellis/.version"),
            Path(".trellis/workflow.md"),
            Path(".trellis/config.yaml"),
        ):
            outcome.checksums[str(relative)] = hash_file(
                root, root / relative
            )
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
                        f"Trellis 平台配置缺失或类型错误：{relative}",
                        f"Trellis platform configuration is missing or has "
                        f"the wrong type: {relative}",
                    )
                outcome.checksums[str(relative)] = hash_verified_path(
                    root, path, expect_directory=expect_directory
                )
        host_activation = {
            harness: (
                "ready"
                if harness != "codex" or confirm_trellis_activation
                else "pending"
            )
            for harness in harnesses
        }
        bootstrap = "ready" if confirm_trellis_bootstrap else "pending"
        set_trellis_readiness(
            outcome,
            host_activation=host_activation,
            bootstrap=bootstrap,
        )
        if host_activation.get("codex") == "pending":
            outcome.pending_actions.append(
                "启用 Codex hooks 并通过 `/hooks` 审批后，"
                "使用 --finalize --confirm-trellis-activation。"
            )
        if bootstrap == "pending":
            outcome.pending_actions.append(
                "完成 00-bootstrap-guidelines 后，"
                "使用 --finalize --confirm-trellis-bootstrap。"
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
                raise BootstrapError(
                    f"OpenSpec 核心目录缺失：{relative}",
                    f"Missing OpenSpec core directory: {relative}",
                )
        verification, checksums, created = verify_openspec_skills(
            root, harnesses, dry_run=dry_run
        )
        outcome.verification.update(verification)
        outcome.checksums.update(checksums)
        outcome.created_or_managed.extend(created)
        return outcome

    if framework != "superpowers":
        raise BootstrapError(
            f"不支持 finalize：{framework}",
            f"Finalize is not supported for: {framework}",
        )

    if integration == "project-skills":
        verification, checksums, created = verify_project_skills(
            root, "superpowers", harnesses, dry_run=dry_run
        )
        outcome.verification.update(verification)
        outcome.checksums.update(checksums)
        outcome.created_or_managed.extend(created)
        instruction_ok = dry_run or verify_framework_instruction(
            root, harnesses, "superpowers", "project-skills"
        )
        outcome.verification["framework-instruction"] = instruction_ok
        if not instruction_ok:
            raise BootstrapError(
                "Superpowers project-skills 托管指令缺失或已变化。"
            )
        if not dry_run:
            outcome.source_revisions[
                SOURCE_REPOSITORIES["superpowers"]
            ] = verify_project_skills_lock_provenance(
                root, "superpowers"
            )
        else:
            outcome.source_revisions[
                SOURCE_REPOSITORIES["superpowers"]
            ] = "<verified-lock-revision>"
        outcome.versions["skills-cli"] = SKILLS_CLI_VERSION
        return outcome

    if "codex" in harnesses:
        data = (
            plugin_inventory
            if plugin_inventory is not None
            else codex_plugin_inventory(
                root,
                dry_run=False,
                timeout=timeout,
                include_available=False,
            )
        )
        entry = find_codex_plugin(
            data,
            "superpowers",
            installed_only=True,
            marketplace=CODEX_OFFICIAL_MARKETPLACE,
        )
        ok = entry is not None and bool(entry.get("enabled", True))
        outcome.verification["codex-plugin:superpowers"] = ok
        if not ok:
            raise BootstrapError(
                "尚未检测到已安装并启用的 Codex Superpowers 插件。"
            )
        if entry and entry.get("version"):
            outcome.versions["superpowers"] = str(entry["version"])
        if entry:
            resolved_spec = plugin_spec(entry)
            if not resolved_spec:
                raise BootstrapError("无法记录 Codex Superpowers 插件身份。")
            outcome.identities["codex-plugin:superpowers"] = resolved_spec
        outcome.host_readiness["codex"] = {
            "installation": "ready",
            "activation": "ready",
        }

    manual_hosts = [harness for harness in harnesses if harness != "codex"]
    if manual_hosts and not confirm_superpowers_installation:
        pending = pending_official_install("superpowers", manual_hosts)
        merge_outcome(outcome, pending)
        outcome.status = "pending"
        return outcome
    for harness in manual_hosts:
        outcome.verification[f"host-confirmed:{harness}:superpowers"] = True
        outcome.host_readiness[harness] = {
            "installation": "ready",
            "activation": "ready",
        }
    if manual_hosts:
        outcome.limitations.append(
            "非 Codex 官方插件状态来自用户显式确认，无法由 Agent Compass 机器验证。"
        )
    return outcome


def merge_readiness_value(previous: str, current: str) -> str:
    for value in (previous, current):
        if value not in READINESS_VALUES:
            raise BootstrapError(
                f"非法 readiness 值：{value}",
                f"Invalid readiness value: {value}",
            )
    order = {"unknown": 0, "pending": 1, "ready": 2}
    return previous if order[previous] >= order[current] else current


def aggregate_host_readiness(
    host_readiness: Mapping[str, Mapping[str, str]], field_name: str
) -> str:
    values = [str(values.get(field_name, "unknown")) for values in host_readiness.values()]
    if any(value not in READINESS_VALUES for value in values):
        raise BootstrapError(
            f"非法 host readiness 字段：{field_name}",
            f"Invalid host readiness field: {field_name}",
        )
    if not values or "unknown" in values:
        return "unknown"
    if "pending" in values:
        return "pending"
    return "ready"


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

    def merged_host_readiness() -> dict[str, dict[str, str]]:
        base_value = (
            previous.get("host_readiness", {})
            if same_selection
            and previous
            and previous.get("schema") == STATE_SCHEMA
            else {}
        )
        merged: dict[str, dict[str, str]] = {}
        if isinstance(base_value, Mapping):
            for host, values in base_value.items():
                if host in ACTIVE_HARNESSES and isinstance(values, Mapping):
                    merged[str(host)] = {
                        str(key): str(value) for key, value in values.items()
                    }
        for host, values in outcome.host_readiness.items():
            if host not in ACTIVE_HARNESSES:
                raise BootstrapError(
                    f"非法 outcome host readiness：{host}",
                    f"Invalid outcome host readiness: {host}",
                )
            target = merged.setdefault(host, {})
            for key, value in values.items():
                current = str(value)
                previous_value = str(target.get(key, "unknown"))
                target[str(key)] = merge_readiness_value(
                    previous_value, current
                )
        return merged

    def merged_readiness(
        host_values: Mapping[str, Mapping[str, str]]
    ) -> dict[str, str]:
        preserve_base = bool(
            same_selection
            and previous
            and (
                outcome.framework not in {"trellis", "superpowers"}
                or previous.get("schema") == STATE_SCHEMA
            )
        )
        base = previous.get("readiness", {}) if preserve_base and previous else {}
        merged = {
            str(key): str(value)
            for key, value in base.items()
        } if isinstance(base, Mapping) else {}
        for key, value in outcome.readiness.items():
            current = str(value)
            merged[str(key)] = merge_readiness_value(
                str(merged.get(key, "unknown")), current
            )
        if outcome.framework == "trellis":
            merged["installation"] = aggregate_host_readiness(
                host_values, "installation"
            )
            merged["activation"] = aggregate_host_readiness(
                host_values, "activation"
            )
            merged.setdefault("bootstrap", "unknown")
        elif outcome.framework == "superpowers" and outcome.integration == "official":
            merged["installation"] = aggregate_host_readiness(
                host_values, "installation"
            )
            merged["activation"] = aggregate_host_readiness(
                host_values, "activation"
            )
        for key, value in merged.items():
            if value not in READINESS_VALUES:
                raise BootstrapError(
                    f"非法 state readiness：{key}={value}",
                    f"Invalid state readiness: {key}={value}",
                )
        return merged

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
    prior_minimal = (
        bool(previous.get("minimal", False))
        if same_selection and previous
        else False
    )
    host_readiness = merged_host_readiness()
    readiness = merged_readiness(host_readiness)
    effective_status = outcome.status
    if outcome.framework == "trellis":
        effective_status = trellis_status_from_readiness(readiness)
    elif outcome.framework == "superpowers" and outcome.integration == "official":
        effective_status = (
            "ready"
            if readiness.get("installation") == "ready"
            and readiness.get("activation") == "ready"
            else "pending"
        )
    return {
        "schema": STATE_SCHEMA,
        "installer": f"agent-compass/{VERSION}",
        "status": effective_status,
        "readiness": readiness,
        "host_readiness": host_readiness,
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
        "identities": dict(outcome.identities),
        "source_revisions": dict(outcome.source_revisions),
        "checksums": dict(outcome.checksums),
        "created_or_managed": sorted(
            set(
                merged_list(
                    "created_or_managed", outcome.created_or_managed
                )
            )
        ),
        "verification": dict(outcome.verification),
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
) -> dict[str, Any]:
    path = root / STATE_FILE
    previous = load_state(root)
    state = state_payload(
        outcome, harnesses, previous, minimal=minimal
    )
    state_status = str(state["status"])
    validate_recorded_state(state, require_current_schema=True)
    if dry_run:
        print(
            f"would write {STATE_FILE} "
            "(simulation only; readiness was not verified)"
        )
    else:
        print(f"write {STATE_FILE} ({state_status})")
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
        if reread != state:
            raise BootstrapError("状态文件写入后验证失败。")
    return state


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
                "请先人工卸载旧集成并移走状态文件。",
                f"The project already records {framework}/{recorded}; "
                f"refusing to stack {integration} on top. Manually uninstall "
                "the previous integration and move the state file first.",
            )
    managed = detect_managed_instruction_frameworks(root) | detect_frameworks_from_lock(root)
    if integration == "official" and framework in managed:
        raise BootstrapError(
            f"检测到 {framework} 的项目 Skills 集成，拒绝再安装官方插件版。",
            f"Detected the project-Skills integration for {framework}; "
            "refusing to also install the official plugin variant.",
        )
    if integration == "project-skills" and framework in detected_codex_plugins:
        raise BootstrapError(
            f"检测到已安装的 Codex {framework} 官方插件，拒绝再安装项目 Skills 版。",
            f"Detected an installed official Codex {framework} plugin; "
            "refusing to also install the project-Skills variant.",
        )


def validate_option_scope(
    args: argparse.Namespace, framework: str, integration: str | None
) -> None:
    if framework == "none":
        if any(
            (
                args.harness,
                args.integration != "auto",
                args.user,
                args.trellis_version,
                args.openspec_version,
                args.repair,
                args.finalize,
                args.minimal,
                args.confirm_trellis_activation,
                args.confirm_trellis_bootstrap,
                args.confirm_superpowers_installation,
            )
        ):
            raise BootstrapError("`none` 不能与安装、finalize 或配置参数组合。")
        return
    trellis_confirmed = (
        args.confirm_trellis_activation or args.confirm_trellis_bootstrap
    )
    if trellis_confirmed and (not args.finalize or framework != "trellis"):
        raise BootstrapError("Trellis 确认参数只能用于 trellis --finalize。")
    if args.confirm_superpowers_installation and (
        not args.finalize
        or framework != "superpowers"
        or integration != "official"
    ):
        raise BootstrapError(
            "Superpowers 人工确认只适用于 official superpowers --finalize。"
        )
    if args.repair and (framework != "trellis" or args.finalize):
        raise BootstrapError("--repair 只适用于 Trellis 初始安装/修复。")
    if (args.user or args.trellis_version) and (
        framework != "trellis" or args.finalize
    ):
        raise BootstrapError("--user/--trellis-version 只适用于 Trellis 安装。")
    if args.openspec_version and (framework != "openspec" or args.finalize):
        raise BootstrapError("--openspec-version 只适用于 OpenSpec 安装。")


def merge_recorded_harnesses(
    previous: Mapping[str, Any] | None,
    framework: str,
    integration: str,
    harnesses: Sequence[str],
) -> list[str]:
    if not previous:
        return list(harnesses)
    previous_framework = canonical_framework(
        str(previous.get("framework") or "")
    )
    if (
        previous_framework != framework
        or str(previous.get("integration") or "") != integration
    ):
        return list(harnesses)
    _, _, recorded = validate_recorded_state(
        previous, require_current_schema=False
    )
    return list(dict.fromkeys([*recorded, *harnesses]))


def validate_confirmation_hosts(
    args: argparse.Namespace, harnesses: Sequence[str]
) -> None:
    if args.confirm_trellis_activation and "codex" not in harnesses:
        raise BootstrapError(
            "--confirm-trellis-activation 只在已记录 Codex 宿主时有效。"
        )
    if args.confirm_superpowers_installation and all(
        harness == "codex" for harness in harnesses
    ):
        raise BootstrapError(
            "--confirm-superpowers-installation 只用于非 Codex 官方宿主。"
        )


def ensure_no_framework_conflicts(
    root: Path,
    framework: str,
    integration: str,
    harnesses: Sequence[str],
    *,
    repair: bool,
    dry_run: bool,
    timeout: int,
    finalize: bool,
) -> Any | None:
    def reject_conflicts(existing_frameworks: set[str]) -> None:
        conflicts = existing_frameworks - {framework}
        if not conflicts:
            return
        legacy = conflicts & LEGACY_FRAMEWORKS
        suffix = (
            " 其中包含已移除的遗留框架，请先按官方方式卸载或迁移。"
            if legacy
            else ""
        )
        suffix_en = (
            " That set includes a removed legacy framework; uninstall or "
            "migrate it the official way first."
            if legacy
            else ""
        )
        raise BootstrapError(
            "检测到其他框架："
            + ", ".join(sorted(conflicts))
            + "。本工具不会自动删除或混用；请先人工处理后重试。"
            + suffix,
            "Detected other frameworks: "
            + ", ".join(sorted(conflicts))
            + ". This tool never removes or mixes them automatically; "
            "resolve them manually and retry."
            + suffix_en,
        )

    existing = detect_existing_frameworks(root, repair=repair)
    reject_conflicts(existing)
    codex_plugins: set[str] = set()
    inventory_holder: dict[str, Any] = {}
    if "codex" in harnesses:
        codex_plugins = detect_codex_plugin_frameworks(
            root,
            dry_run=dry_run,
            timeout=timeout,
            inventory_out=inventory_holder,
        )
        existing.update(codex_plugins)
    reject_conflicts(existing)
    validate_integration_consistency(
        root,
        framework,
        integration,
        detected_codex_plugins=codex_plugins,
        finalize=finalize,
    )
    return inventory_holder.get("codex")



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
        "--doctor",
        action="store_true",
        help="Read-only health check for the recorded Agent Compass setup",
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
        "--language",
        default="auto",
        choices=("auto", "zh", "en"),
        help="Questionnaire, summary, and doctor language; default follows the locale",
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
        "--confirm-trellis-activation",
        action="store_true",
        help="With --finalize, confirm Trellis host activation is complete",
    )
    parser.add_argument(
        "--confirm-trellis-bootstrap",
        action="store_true",
        help="With --finalize, confirm the initial Trellis spec bootstrap is complete",
    )
    parser.add_argument(
        "--confirm-superpowers-installation",
        action="store_true",
        help="With --finalize, confirm non-Codex official Superpowers installation",
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
    language: str = "auto",
) -> str:
    language = detect_prompt_language(language)
    if language == "zh":
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

    scope = {
        ("trellis", "official"): (
            "initialize long-lived project specifications, tasks, and memory"
        ),
        ("openspec", "official"): (
            "install the lightweight proposal/apply/archive specification flow"
        ),
        ("matt", "project-skills"): (
            "install on-demand debugging, review, design, and TDD Skills"
        ),
        ("superpowers", "official"): (
            "install and verify the host's official Superpowers plugin"
        ),
        ("superpowers", "project-skills"): (
            "install the editable project-Skills compatibility mode without "
            "host hooks"
        ),
    }.get((framework, integration), "run the selected integration")
    suffix = "; enable the smallest-correct-change rule" if minimal else ""
    return (
        f"Selection: {framework} / {integration} / "
        f"{', '.join(harnesses)}; {scope}{suffix}."
    )


def doctor_project(
    root: Path, *, timeout: int, language: str = "auto"
) -> int:
    """Verify recorded setup without modifying project or host state."""
    language = detect_prompt_language(language)
    print(localized_text(language, f"项目：{root}", f"Project: {root}"))
    lock_path = root / LOCK_FILE
    if lstat_exists(lock_path):
        try:
            ensure_safe_path(root, lock_path, for_write=False)
        except BootstrapError as exc:
            print(
                doctor_failure(exc, language)
            )
            return 1
        print(
            localized_text(
                language,
                f"诊断待处理：检测到 {LOCK_FILE}，变更流程可能正在运行。",
                f"Doctor needs attention: {LOCK_FILE} exists; a mutation "
                "may be in progress.",
            )
        )
        return 1

    try:
        state = load_state(root)
    except BootstrapError as exc:
        print(
            doctor_failure(exc, language)
        )
        return 1
    if not state:
        try:
            detected_set = detect_existing_frameworks(root)
        except BootstrapError as exc:
            print(
                doctor_failure(exc, language)
            )
            return 1
        # Keep the plugin inventory fail-closed: a failed query never proves
        # absence. Record the cause instead of discarding it, so the report
        # can say why the answer is incomplete.
        plugin_error: BootstrapError | None = None
        if shutil.which("codex"):
            try:
                detected_set.update(
                    detect_codex_plugin_frameworks(
                        root, dry_run=False, timeout=timeout
                    )
                )
            except BootstrapError as exc:
                plugin_error = exc
        detected = sorted(detected_set)
        if detected:
            print(
                localized_text(
                    language,
                    "诊断：检测到未由 Agent Compass 记录的框架："
                    + ", ".join(detected),
                    "Doctor: detected frameworks not recorded by Agent "
                    "Compass: "
                    + ", ".join(detected),
                )
            )
        elif plugin_error is None:
            print(
                localized_text(
                    language,
                    "诊断：未找到 Agent Compass 状态或已知框架。",
                    "Doctor: no Agent Compass state or known framework was found.",
                )
            )
        else:
            print(
                localized_text(
                    language,
                    "诊断：未找到 Agent Compass 状态；文件系统中也没有已知框架。",
                    "Doctor: no Agent Compass state was found, and no known "
                    "framework is present on the filesystem.",
                )
            )
        if plugin_error is not None:
            print(
                localized_text(
                    language,
                    "诊断未完成：无法读取 Codex 插件清单，因此无法排除以插件形式"
                    "安装的框架。原因：\n"
                    + bootstrap_error_text(plugin_error, "zh"),
                    "Doctor incomplete: could not read the Codex plugin "
                    "inventory, so a framework installed as a plugin cannot "
                    "be ruled out. Cause:\n"
                    + bootstrap_error_text(plugin_error, "en"),
                )
            )
        return 1

    try:
        framework, integration, harnesses = validate_recorded_state(
            state, require_current_schema=False
        )
    except BootstrapError as exc:
        print(
            doctor_failure(exc, language)
        )
        return 1

    schema = state.get("schema", "legacy")
    print(
        localized_text(
            language,
            f"记录：schema {schema}；{framework} / {integration} / "
            f"{', '.join(harnesses)}；状态 {state.get('status', 'unknown')}。",
            f"Record: schema {schema}; {framework} / {integration} / "
            f"{', '.join(harnesses)}; status "
            f"{state.get('status', 'unknown')}.",
        )
    )
    readiness = state.get("readiness", {})
    host_readiness = state.get("host_readiness", {})
    if isinstance(readiness, Mapping) and readiness:
        for key in ("installation", "activation", "bootstrap"):
            if key in readiness:
                print(f"readiness.{key}: {readiness[key]}")

    if state.get("schema") != STATE_SCHEMA:
        print(
            localized_text(
                language,
                f"诊断待处理：schema {state.get('schema')} 需要重新 finalize 为 "
                f"schema {STATE_SCHEMA}。",
                f"Doctor needs attention: schema {state.get('schema')} must "
                f"be finalized again as schema {STATE_SCHEMA}.",
            )
        )
        return 1

    try:
        plugin_inventory = ensure_no_framework_conflicts(
            root,
            framework,
            integration,
            harnesses,
            repair=False,
            dry_run=False,
            timeout=timeout,
            finalize=True,
        )
    except BootstrapError as exc:
        print(
            doctor_failure(exc, language)
        )
        return 1

    codex_activation_ready = bool(
        isinstance(host_readiness, Mapping)
        and isinstance(host_readiness.get("codex"), Mapping)
        and host_readiness["codex"].get("activation") == "ready"
    )
    manual_superpowers_ready = all(
        isinstance(host_readiness, Mapping)
        and isinstance(host_readiness.get(harness), Mapping)
        and host_readiness[harness].get("installation") == "ready"
        for harness in harnesses
        if harness != "codex"
    )

    try:
        outcome = finalize_framework(
            root,
            framework,
            harnesses,
            integration,
            dry_run=False,
            timeout=timeout,
            confirm_trellis_activation=codex_activation_ready,
            confirm_trellis_bootstrap=(
                isinstance(readiness, Mapping)
                and readiness.get("bootstrap") == "ready"
            ),
            confirm_superpowers_installation=manual_superpowers_ready,
            plugin_inventory=plugin_inventory,
        )
    except BootstrapError as exc:
        print(
            doctor_failure(exc, language)
        )
        return 1

    failed = [name for name, ok in outcome.verification.items() if not ok]
    if state.get("minimal"):
        outcome.verification["minimal-policy"] = verify_minimal_policy(
            root, harnesses
        )
        failed = [name for name, ok in outcome.verification.items() if not ok]
    if failed:
        print(
            localized_text(
                language,
                "诊断失败：验证未通过：" + ", ".join(failed),
                "Doctor failed: verification did not pass: "
                + ", ".join(failed),
            )
        )
        return 1
    recorded_checksums = state.get("checksums", {})
    if (
        not isinstance(recorded_checksums, Mapping)
        or dict(recorded_checksums) != outcome.checksums
    ):
        print(
            localized_text(
                language,
                "诊断失败：记录的 checksums 与当前安装不一致。",
                "Doctor failed: recorded checksums do not match the current installation.",
            )
        )
        return 1
    recorded_identities = state.get("identities", {})
    if (
        not isinstance(recorded_identities, Mapping)
        or dict(recorded_identities) != outcome.identities
    ):
        print(
            localized_text(
                language,
                "诊断失败：记录的宿主插件身份与当前安装不一致。",
                "Doctor failed: recorded host-plugin identities do not match "
                "the current installation.",
            )
        )
        return 1
    recorded_revisions = state.get("source_revisions", {})
    if (
        not isinstance(recorded_revisions, Mapping)
        or dict(recorded_revisions) != outcome.source_revisions
    ):
        print(
            localized_text(
                language,
                "诊断失败：记录的源码修订与精确锁定条目不一致。",
                "Doctor failed: recorded source revisions do not match the "
                "exact lock entries.",
            )
        )
        return 1
    recorded_verification = state.get("verification", {})
    if (
        not isinstance(recorded_verification, Mapping)
        or dict(recorded_verification) != outcome.verification
    ):
        print(
            localized_text(
                language,
                "诊断失败：记录的 verification 与当前验证结果不一致。",
                "Doctor failed: recorded verification does not match the "
                "current verification result.",
            )
        )
        return 1
    recorded_versions = state.get("versions", {})
    if not isinstance(recorded_versions, Mapping) or any(
        recorded_versions.get(key) != value
        for key, value in outcome.versions.items()
    ):
        print(
            localized_text(
                language,
                "诊断失败：记录的版本与当前可验证版本不一致。",
                "Doctor failed: recorded versions do not match the currently "
                "verifiable versions.",
            )
        )
        return 1

    print(
        localized_text(
            language,
            "文件、状态与集成检查：通过。",
            "Files, state, and integration checks: passed.",
        )
    )
    if outcome.status != "ready" or state.get("status") != "ready":
        print(
            localized_text(
                language,
                f"诊断待处理：当前阶段为 {outcome.status}。",
                f"Doctor needs attention: current phase is {outcome.status}.",
            )
        )
        for action in outcome.pending_actions:
            print(
                localized_text(language, "待完成：", "Pending: ")
                + localized_outcome_message(action, language)
            )
        return 1

    print(localized_text(language, "诊断结果：ready。", "Doctor result: ready."))
    return 0



def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        raise BootstrapError("--timeout 必须大于 0。")
    language = detect_prompt_language(args.language)

    root = find_project_root(args.project_root)
    reject_retired_name_traces(root)
    requested_framework = canonical_framework(args.framework)

    if args.doctor:
        if requested_framework != "auto":
            raise BootstrapError("--doctor 不接受框架参数；它只读取已记录状态。")
        if any(
            (
                args.harness,
                args.integration != "auto",
                args.user,
                args.trellis_version,
                args.openspec_version,
                args.finalize,
                args.repair,
                args.minimal,
                args.confirm_trellis_activation,
                args.confirm_trellis_bootstrap,
                args.confirm_superpowers_installation,
                args.yes,
                args.dry_run,
            )
        ):
            raise BootstrapError(
                "--doctor 只接受 --project-root、--timeout 和 --language。"
            )
        return doctor_project(root, timeout=args.timeout, language=language)

    previous_state = load_state(root)
    interactive_minimal = False

    if args.finalize and requested_framework == "auto":
        if not previous_state:
            raise BootstrapError(
                "--finalize 需要显式框架，或一个有效的 Agent Compass 状态文件。"
            )
        framework, _, _ = validate_recorded_state(
            previous_state, require_current_schema=False
        )
    elif requested_framework == "auto":
        framework, interactive_minimal = choose_framework_interactively(
            args.language
        )
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
        validate_option_scope(args, framework, None)
        if previous_state:
            print(
                localized_text(
                    language,
                    "未执行安装；`none` 仅表示跳过本次操作，不会禁用或删除现有框架。"
                    f" 当前记录：{previous_state.get('framework')} / "
                    f"{previous_state.get('status', 'unknown')}。",
                    "No installation ran; `none` skips this operation and does "
                    "not disable or remove the existing framework. Current "
                    f"record: {previous_state.get('framework')} / "
                    f"{previous_state.get('status', 'unknown')}.",
                )
            )
        else:
            print(
                localized_text(
                    language,
                    "未执行安装，项目保持不变。",
                    "No installation ran; the project is unchanged.",
                )
            )
        return 0

    if not args.harness and previous_state and canonical_framework(
        str(previous_state.get("framework") or "")
    ) == framework:
        _, _, recorded_harnesses = validate_recorded_state(
            previous_state, require_current_schema=False
        )
        harnesses = list(recorded_harnesses)
    else:
        harnesses = normalize_harnesses(
            args.harness, root, language=language
        )

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

    validate_option_scope(args, framework, integration)
    harnesses = merge_recorded_harnesses(
        previous_state,
        framework,
        integration,
        harnesses,
    )
    validate_confirmation_hosts(args, harnesses)

    if (
        framework in {"trellis", "openspec"}
        and args.integration == "project-skills"
    ):
        raise BootstrapError(f"{framework} 只支持 official 集成。")
    if framework == "matt" and args.integration == "official":
        raise BootstrapError(
            "Matt 在本选择器中使用可编辑、可验证的 project-skills 模式。"
        )

    plugin_inventory = ensure_no_framework_conflicts(
        root,
        framework,
        integration,
        harnesses,
        repair=args.repair,
        dry_run=args.dry_run,
        timeout=args.timeout,
        finalize=args.finalize,
    )

    print(localized_text(language, f"项目：{root}", f"Project: {root}"))
    print(
        describe_plan(
            framework,
            integration,
            harnesses,
            minimal=minimal_enabled,
            language=language,
        )
    )
    if framework == "trellis":
        print(
            localized_text(
                language,
                "许可证提示：Trellis 为 AGPL-3.0；"
                "请自行确认企业或客户项目的合规要求。",
                "License notice: Trellis is AGPL-3.0; confirm compliance "
                "requirements for enterprise or client projects.",
            )
        )
    if integration == "official" and framework == "superpowers":
        print(
            localized_text(
                language,
                "范围提示：官方宿主插件通常安装到用户/宿主范围，"
                "而不是只写入当前仓库。",
                "Scope notice: official host plugins are usually installed at "
                "user/host scope, not only inside this repository.",
            )
        )
    if not args.yes and not args.dry_run:
        if not prompt_yes_no(
            "继续吗？" if language == "zh" else "Continue?",
            language=language,
        ):
            print(localized_text(language, "已取消。", "Cancelled."))
            return 0

    mutation_tracker = MutationTracker()
    written_state: dict[str, Any]
    try:
        with RepositoryLock(root, enabled=not args.dry_run):
            if not args.dry_run:
                locked_state = load_state(root)
                if locked_state != previous_state:
                    raise BootstrapError(
                        "Agent Compass 状态在确认期间已变化；"
                        "为避免基于过期状态覆盖，请重新运行。"
                    )
                plugin_inventory = ensure_no_framework_conflicts(
                    root,
                    framework,
                    integration,
                    harnesses,
                    repair=args.repair,
                    dry_run=False,
                    timeout=args.timeout,
                    finalize=args.finalize,
                )
            with ManagedFileTransaction(root) as transaction:
                previous_readiness = (
                    previous_state.get("readiness", {})
                    if previous_state
                    and previous_state.get("schema") == STATE_SCHEMA
                    else {}
                )
                previous_host_readiness = (
                    previous_state.get("host_readiness", {})
                    if previous_state
                    and previous_state.get("schema") == STATE_SCHEMA
                    else {}
                )
                previous_codex_ready = bool(
                    isinstance(previous_host_readiness, Mapping)
                    and isinstance(previous_host_readiness.get("codex"), Mapping)
                    and previous_host_readiness["codex"].get("activation") == "ready"
                )
                previous_manual_superpowers_ready = all(
                    isinstance(previous_host_readiness, Mapping)
                    and isinstance(previous_host_readiness.get(harness), Mapping)
                    and previous_host_readiness[harness].get("installation") == "ready"
                    for harness in harnesses
                    if harness != "codex"
                )
                if args.finalize:
                    outcome = finalize_framework(
                        root,
                        framework,
                        harnesses,
                        integration,
                        dry_run=args.dry_run,
                        timeout=args.timeout,
                        confirm_trellis_activation=(
                            args.confirm_trellis_activation
                            or previous_codex_ready
                        ),
                        confirm_trellis_bootstrap=(
                            args.confirm_trellis_bootstrap
                            or (
                                isinstance(previous_readiness, Mapping)
                                and previous_readiness.get("bootstrap") == "ready"
                            )
                        ),
                        confirm_superpowers_installation=(
                            args.confirm_superpowers_installation
                            or previous_manual_superpowers_ready
                        ),
                        plugin_inventory=plugin_inventory,
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
                        plugin_inventory=plugin_inventory,
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

                written_state = write_state(
                    root,
                    outcome,
                    harnesses,
                    minimal=minimal_enabled,
                    dry_run=args.dry_run,
                    transaction=transaction,
                )
                transaction.commit()
    except BaseException:
        if mutation_tracker.external_command_started:
            print(
                localized_text(
                    language,
                    "警告：上游安装命令已运行，可能留下部分更改；"
                    "Agent Compass 自己管理的文件已尝试回滚，且不会写入 ready 状态。"
                    "请检查 git diff 和宿主插件列表。",
                    "Warning: an upstream installer ran and may have left "
                    "partial changes. Agent-Compass-managed files were rolled "
                    "back where possible, and no ready state was written. "
                    "Inspect git diff and the host plugin inventory.",
                ),
                file=sys.stderr,
            )
        raise

    if args.dry_run:
        print(
            localized_text(
                language,
                "阶段：not_installed（dry-run）；未执行安装、未写入状态，"
                "也未验证或宣称 ready。",
                "Phase: not_installed (dry-run); no installation ran, no state "
                "was written, and readiness remains unverified.",
            )
        )
    elif written_state["status"] == "ready":
        print(
            localized_text(
                language,
                "安装与验证完成；阶段：ready。",
                "Installation and verification completed; phase: ready.",
            )
        )
    else:
        print(
            localized_text(
                language,
                f"安装阶段完成，当前状态为 {written_state['status']}；未宣称 ready。",
                f"Installation phase completed; current phase: "
                f"{written_state['status']}. Readiness was not claimed.",
            )
        )
    for note in outcome.pending_actions:
        print(
            localized_text(language, "待完成：", "Pending: ")
            + localized_outcome_message(note, language)
        )
    for note in outcome.activation_notes:
        print(
            localized_text(language, "激活提示：", "Activation: ")
            + localized_outcome_message(note, language)
        )
    for limitation in outcome.limitations:
        print(
            localized_text(language, "能力边界：", "Limitation: ")
            + localized_outcome_message(limitation, language)
        )
    return 0


def requested_language_from_argv(argv: Sequence[str]) -> str:
    """Recover --language before argparse runs.

    The top-level handler reports failures raised before or outside main's
    parsed arguments, so the language has to come straight from argv.
    """
    for index, item in enumerate(argv):
        if item.startswith("--language="):
            return detect_prompt_language(item.split("=", 1)[1])
        if item == "--language" and index + 1 < len(argv):
            return detect_prompt_language(argv[index + 1])
    return detect_prompt_language("auto")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        error_language = requested_language_from_argv(sys.argv[1:])
        print(
            localized_text(error_language, "错误：", "Error: ")
            + bootstrap_error_text(exc, error_language),
            file=sys.stderr,
        )
        raise SystemExit(2)
