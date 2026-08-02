import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "agent-compass"
    / "scripts"
    / "compass_bootstrap.py"
)
spec = importlib.util.spec_from_file_location("compass_bootstrap", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class TempRepo:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        return self.root

    def __exit__(self, *args):
        self.tmp.cleanup()


def create_skill(root: Path, base: str, name: str, body: str = "# Skill\n") -> Path:
    path = root / base / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def create_valid_trellis(root: Path) -> None:
    (root / ".trellis").mkdir(exist_ok=True)
    for relative in (".version", "workflow.md", "config.yaml"):
        (root / ".trellis" / relative).write_text("ok\n", encoding="utf-8")


def create_trellis_platform(root: Path, harness: str) -> None:
    for relative in module.TRELLIS_PLATFORM_PATHS[harness]:
        path = root / relative
        if relative.suffix == "":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ok\n", encoding="utf-8")


class SelectionTests(unittest.TestCase):
    def test_short_low_risk_project_skips_framework(self):
        with mock.patch.object(
            module, "prompt_choice", return_value=1
        ) as choice_prompt, mock.patch.object(
            module, "prompt_yes_no"
        ) as minimal_prompt:
            self.assertEqual(
                module.choose_framework_interactively(), ("none", False)
            )
            choice_prompt.assert_called_once()
            minimal_prompt.assert_not_called()

    def test_maintained_project_maps_need_to_framework(self):
        expected = {
            1: "matt",
            2: "openspec",
            3: "trellis",
            4: "superpowers",
            5: "none",
        }
        for answer, framework in expected.items():
            with self.subTest(answer=answer), mock.patch.object(
                module, "prompt_choice", side_effect=[2, answer]
            ), mock.patch.object(
                module, "prompt_yes_no", return_value=True
            ) as minimal_prompt:
                selected, minimal = module.choose_framework_interactively()
                self.assertEqual(selected, framework)
                self.assertEqual(minimal, framework != "none")
                if framework == "none":
                    minimal_prompt.assert_not_called()
                else:
                    minimal_prompt.assert_called_once()

    def test_only_retained_frameworks_are_parser_choices(self):
        parser = module.build_parser()
        for framework in ("matt", "openspec", "trellis", "superpowers", "none"):
            self.assertEqual(parser.parse_args([framework]).framework, framework)
        for removed in ("speckit", "bmad", "compound", "ponytail"):
            with self.subTest(removed=removed), contextlib.redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit):
                parser.parse_args([removed])
        self.assertTrue(parser.parse_args(["--doctor"]).doctor)

    def test_aliases_are_limited_to_retained_frameworks(self):
        self.assertEqual(module.canonical_framework("open-spec"), "openspec")
        self.assertEqual(module.canonical_framework("mattpocock"), "matt")
        self.assertEqual(module.canonical_framework("spec-kit"), "spec-kit")

    def test_find_project_root_uses_git_parent(self):
        with TempRepo() as root:
            child = root / "a" / "b"
            child.mkdir(parents=True)
            self.assertEqual(module.find_project_root(child), root.resolve())

    def test_normalize_multiple_harnesses(self):
        with TempRepo() as root:
            result = module.normalize_harnesses(
                ["codex,cursor", "claude-code"], root
            )
            self.assertEqual(result, ["codex", "cursor", "claude-code"])

    def test_resolve_integration_is_simple_and_predictable(self):
        self.assertEqual(
            module.resolve_integration("matt", "auto", ["codex"]),
            "project-skills",
        )
        self.assertEqual(
            module.resolve_integration("openspec", "project-skills", ["codex"]),
            "official",
        )
        self.assertEqual(
            module.resolve_integration("superpowers", "auto", ["codex"]),
            "official",
        )
        self.assertEqual(
            module.resolve_integration("superpowers", "auto", ["cursor"]),
            "project-skills",
        )


class SafeFileTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "symlink"), "symlink not supported")
    def test_framework_instruction_rejects_symlink(self):
        with TempRepo() as root:
            outside = root.parent / (root.name + "-outside.txt")
            outside.write_text("safe", encoding="utf-8")
            try:
                (root / "AGENTS.md").symlink_to(outside)
                with module.ManagedFileTransaction(root) as tx:
                    with self.assertRaises(module.BootstrapError):
                        module.write_managed_instruction(
                            root,
                            ["codex"],
                            "superpowers",
                            "project-skills",
                            dry_run=False,
                            transaction=tx,
                        )
                self.assertEqual(outside.read_text(encoding="utf-8"), "safe")
            finally:
                outside.unlink(missing_ok=True)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink not supported")
    def test_minimal_instruction_rejects_symlink(self):
        with TempRepo() as root:
            outside = root.parent / (root.name + "-outside-minimal.txt")
            outside.write_text("safe", encoding="utf-8")
            try:
                (root / "AGENTS.md").symlink_to(outside)
                with module.ManagedFileTransaction(root) as tx:
                    with self.assertRaises(module.BootstrapError):
                        module.write_minimal_instruction(
                            root,
                            ["codex"],
                            dry_run=False,
                            transaction=tx,
                        )
                self.assertEqual(outside.read_text(encoding="utf-8"), "safe")
            finally:
                outside.unlink(missing_ok=True)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink not supported")
    def test_state_symlink_is_rejected(self):
        with TempRepo() as root:
            outside = root.parent / (root.name + "-state.json")
            outside.write_text("{}", encoding="utf-8")
            try:
                (root / module.STATE_FILE).symlink_to(outside)
                with self.assertRaises(module.BootstrapError):
                    module.load_state(root)
            finally:
                outside.unlink(missing_ok=True)

    def test_transaction_rolls_back_managed_files(self):
        with TempRepo() as root:
            original = root / "AGENTS.md"
            original.write_text("before\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                with module.ManagedFileTransaction(root) as tx:
                    tx.snapshot(original)
                    module.atomic_write_text(root, original, "after\n")
                    raise RuntimeError("boom")
            self.assertEqual(original.read_text(encoding="utf-8"), "before\n")

    def test_minimal_instruction_is_idempotent(self):
        with TempRepo() as root:
            for _ in range(2):
                with module.ManagedFileTransaction(root) as tx:
                    module.write_minimal_instruction(
                        root,
                        ["codex"],
                        dry_run=False,
                        transaction=tx,
                    )
                    tx.commit()
            text = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(text.count(module.MINIMAL_START), 1)
            self.assertEqual(text.count(module.MINIMAL_END), 1)
            self.assertIn("smallest correct change", text)

    def test_superpowers_and_minimal_blocks_can_coexist(self):
        with TempRepo() as root:
            with module.ManagedFileTransaction(root) as tx:
                module.write_managed_instruction(
                    root,
                    ["codex"],
                    "superpowers",
                    "project-skills",
                    dry_run=False,
                    transaction=tx,
                )
                module.write_minimal_instruction(
                    root,
                    ["codex"],
                    dry_run=False,
                    transaction=tx,
                )
                tx.commit()
            text = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(text.count(module.MANAGED_START_PREFIX), 1)
            self.assertEqual(text.count(module.MINIMAL_START), 1)


class DetectionTests(unittest.TestCase):
    def test_valid_trellis_detected(self):
        with TempRepo() as root:
            create_valid_trellis(root)
            self.assertEqual(module.detect_existing_frameworks(root), {"trellis"})

    def test_empty_trellis_is_not_success(self):
        with TempRepo() as root:
            (root / ".trellis").mkdir()
            with self.assertRaises(module.BootstrapError):
                module.detect_existing_frameworks(root)

    def test_openspec_detected(self):
        with TempRepo() as root:
            (root / "openspec/specs").mkdir(parents=True)
            (root / "openspec/changes").mkdir(parents=True)
            self.assertEqual(module.detect_existing_frameworks(root), {"openspec"})

    def test_removed_framework_markers_are_legacy_conflicts(self):
        markers = {
            "speckit": ".specify",
            "bmad": "_bmad",
            "compound": ".compound-engineering",
        }
        for framework, marker in markers.items():
            with self.subTest(framework=framework), TempRepo() as root:
                (root / marker).mkdir()
                self.assertIn(
                    framework, module.detect_existing_frameworks(root)
                )

    def test_removed_framework_lock_sources_are_legacy_conflicts(self):
        for framework, source in module.LEGACY_SOURCE_HINTS.items():
            with self.subTest(framework=framework), TempRepo() as root:
                (root / "skills-lock.json").write_text(
                    json.dumps({"source": source}), encoding="utf-8"
                )
                self.assertIn(
                    framework, module.detect_existing_frameworks(root)
                )

    def test_generic_tdd_folder_does_not_imply_matt(self):
        with TempRepo() as root:
            create_skill(root, ".agents/skills", "tdd")
            self.assertEqual(module.detect_existing_frameworks(root), set())

    def test_corrupt_state_aborts(self):
        with TempRepo() as root:
            (root / module.STATE_FILE).write_text("{broken", encoding="utf-8")
            with self.assertRaises(module.BootstrapError):
                module.load_state(root)


class VerificationAndCommandTests(unittest.TestCase):
    def test_multi_root_skill_verification_requires_every_root(self):
        with TempRepo() as root:
            for skill in module.MATT_SKILLS:
                create_skill(root, ".agents/skills", skill)
            with self.assertRaises(module.BootstrapError):
                module.verify_project_skills(
                    root, "matt", ["codex", "claude-code"], dry_run=False
                )
            for skill in module.MATT_SKILLS:
                create_skill(root, ".claude/skills", skill)
            verification, checksums, _ = module.verify_project_skills(
                root, "matt", ["codex", "claude-code"], dry_run=False
            )
            self.assertTrue(all(verification.values()))
            self.assertEqual(len(checksums), len(module.MATT_SKILLS) * 2)

    def test_matt_finalize_requires_setup_outputs(self):
        with TempRepo() as root:
            for skill in module.MATT_SKILLS:
                create_skill(root, ".agents/skills", skill)
            with self.assertRaises(module.BootstrapError):
                module.finalize_framework(
                    root,
                    "matt",
                    ["codex"],
                    "project-skills",
                    dry_run=False,
                    timeout=10,
                )
            (root / "docs/agents").mkdir(parents=True)
            (root / "docs/agents/issue-tracker.md").write_text("ok\n")
            (root / "docs/agents/domain.md").write_text("ok\n")
            (root / "AGENTS.md").write_text("## Agent skills\n")
            outcome = module.finalize_framework(
                root,
                "matt",
                ["codex"],
                "project-skills",
                dry_run=False,
                timeout=10,
            )
            self.assertEqual(outcome.status, "ready")

    def test_skills_command_is_pinned_and_project_local(self):
        command = module.skills_command(
            "mattpocock/skills", ["tdd"], ["codex", "cursor"]
        )
        self.assertIn(f"skills@{module.SKILLS_CLI_VERSION}", command)
        self.assertIn("--copy", command)
        self.assertIn("--agent", command)
        self.assertNotIn("--global", command)

    def test_trellis_dry_run_uses_all_platform_flags(self):
        with TempRepo() as root, mock.patch.object(
            module, "ensure_node_18"
        ), mock.patch.object(
            module, "require_executable", return_value="git"
        ), mock.patch.object(
            module, "run"
        ) as run_mock:
            outcome = module.install_trellis(
                root,
                ["codex", "cursor"],
                "tester",
                "1.2.3",
                repair=False,
                dry_run=True,
                timeout=10,
                mutation_tracker=module.MutationTracker(),
            )
            command = run_mock.call_args.args[0]
            self.assertIn("@mindfoldhq/trellis@1.2.3", command)
            self.assertIn("--codex", command)
            self.assertIn("--cursor", command)
            self.assertEqual(outcome.status, "activation_pending")
            self.assertEqual(outcome.readiness["installation"], "ready")
            self.assertEqual(outcome.readiness["activation"], "pending")
            self.assertEqual(outcome.readiness["bootstrap"], "pending")

    def test_trellis_finalize_advances_explicit_readiness_gates(self):
        with TempRepo() as root:
            create_valid_trellis(root)
            create_trellis_platform(root, "codex")

            activation_pending = module.finalize_framework(
                root,
                "trellis",
                ["codex"],
                "official",
                dry_run=False,
                timeout=10,
            )
            self.assertEqual(
                activation_pending.status, "activation_pending"
            )

            bootstrap_pending = module.finalize_framework(
                root,
                "trellis",
                ["codex"],
                "official",
                dry_run=False,
                timeout=10,
                confirm_trellis_activation=True,
            )
            self.assertEqual(
                bootstrap_pending.status, "bootstrap_pending"
            )

            ready = module.finalize_framework(
                root,
                "trellis",
                ["codex"],
                "official",
                dry_run=False,
                timeout=10,
                confirm_trellis_activation=True,
                confirm_trellis_bootstrap=True,
            )
            self.assertEqual(ready.status, "ready")
            self.assertTrue(all(ready.verification.values()))

    def test_openspec_dry_run_uses_tools_and_exact_version(self):
        with TempRepo() as root, mock.patch.object(
            module, "ensure_node_20"
        ), mock.patch.object(module, "run") as run_mock:
            outcome = module.install_openspec(
                root,
                ["codex", "cursor"],
                "2.0.0",
                dry_run=True,
                timeout=10,
                mutation_tracker=module.MutationTracker(),
            )
            command = run_mock.call_args.args[0]
            self.assertIn("@fission-ai/openspec@2.0.0", command)
            self.assertIn("codex,cursor", command)
            self.assertEqual(outcome.status, "ready")

    def test_available_codex_plugin_is_not_treated_as_installed(self):
        data = {"available": [{"name": "superpowers", "installed": False}]}
        self.assertIsNotNone(module.find_codex_plugin(data, "superpowers"))
        self.assertIsNone(
            module.find_codex_plugin(data, "superpowers", installed_only=True)
        )

    def test_current_codex_plugin_list_text_is_parsed(self):
        data = module.parse_codex_plugin_list(
            "\n".join(
                (
                    "Marketplace `openai-curated`",
                    "Path: /tmp/marketplace.json",
                    "  github@openai-curated (installed, enabled)",
                    "  superpowers@openai-curated (installed, disabled)",
                    "  linear@openai-curated (not installed, disabled)",
                )
            )
        )
        superpowers = module.find_codex_plugin(
            data, "superpowers", installed_only=True
        )
        self.assertIsNotNone(superpowers)
        self.assertFalse(superpowers["enabled"])
        self.assertEqual(
            module.plugin_marketplace(superpowers), "openai-curated"
        )
        with mock.patch.object(
            module, "codex_plugin_inventory", return_value=data
        ), mock.patch.object(module.shutil, "which", return_value="codex"):
            self.assertEqual(
                module.detect_codex_plugin_frameworks(
                    Path.cwd(), dry_run=False, timeout=10
                ),
                set(),
            )
        data["plugins"][1]["enabled"] = True
        with mock.patch.object(
            module, "codex_plugin_inventory", return_value=data
        ), mock.patch.object(module.shutil, "which", return_value="codex"):
            self.assertEqual(
                module.detect_codex_plugin_frameworks(
                    Path.cwd(), dry_run=False, timeout=10
                ),
                {"superpowers"},
            )


class StateAndMainTests(unittest.TestCase):
    def test_state_records_minimal_and_preserves_it(self):
        first = module.state_payload(
            module.InstallOutcome("matt", "project-skills"),
            ["codex"],
            None,
            minimal=True,
        )
        self.assertTrue(first["minimal"])
        second = module.state_payload(
            module.InstallOutcome("matt", "project-skills"),
            ["cursor"],
            first,
            minimal=False,
        )
        self.assertTrue(second["minimal"])
        self.assertEqual(second["harnesses"], ["codex", "cursor"])
        self.assertEqual(second["schema"], module.STATE_SCHEMA)

    def test_trellis_state_records_phase_readiness(self):
        outcome = module.InstallOutcome("trellis", "official")
        module.set_trellis_readiness(
            outcome, activation="pending", bootstrap="pending"
        )
        state = module.state_payload(
            outcome, ["codex"], None, minimal=False
        )
        self.assertEqual(state["status"], "activation_pending")
        self.assertEqual(
            state["readiness"],
            {
                "installation": "ready",
                "activation": "pending",
                "bootstrap": "pending",
            },
        )

    def test_doctor_is_read_only_and_reports_pending_trellis(self):
        with TempRepo() as root:
            create_valid_trellis(root)
            create_trellis_platform(root, "codex")
            outcome = module.InstallOutcome("trellis", "official")
            module.set_trellis_readiness(
                outcome, activation="pending", bootstrap="pending"
            )
            state_path = root / module.STATE_FILE
            state_path.write_text(
                json.dumps(
                    module.state_payload(
                        outcome, ["codex"], None, minimal=False
                    )
                ),
                encoding="utf-8",
            )
            before = state_path.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = module.doctor_project(root, timeout=10)
            self.assertEqual(rc, 1)
            self.assertEqual(state_path.read_bytes(), before)

    def test_doctor_reports_ready_trellis(self):
        with TempRepo() as root:
            create_valid_trellis(root)
            create_trellis_platform(root, "codex")
            outcome = module.InstallOutcome("trellis", "official")
            module.set_trellis_readiness(
                outcome, activation="ready", bootstrap="ready"
            )
            (root / module.STATE_FILE).write_text(
                json.dumps(
                    module.state_payload(
                        outcome, ["codex"], None, minimal=False
                    )
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc = module.doctor_project(root, timeout=10)
            self.assertEqual(rc, 0)

    def test_doctor_requires_schema_6_trellis_readiness(self):
        with TempRepo() as root:
            create_valid_trellis(root)
            create_trellis_platform(root, "codex")
            (root / module.STATE_FILE).write_text(
                json.dumps(
                    {
                        "schema": 5,
                        "framework": "trellis",
                        "integration": "official",
                        "harnesses": ["codex"],
                        "status": "ready",
                    }
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc = module.doctor_project(root, timeout=10)
            self.assertEqual(rc, 1)

    def test_doctor_rejects_ignored_install_options(self):
        with TempRepo() as root:
            with self.assertRaises(module.BootstrapError):
                module.main(
                    [
                        "--doctor",
                        "--harness",
                        "codex",
                        "--project-root",
                        str(root),
                    ]
                )

    def test_trellis_confirmation_requires_finalize(self):
        with TempRepo() as root:
            with self.assertRaises(module.BootstrapError):
                module.main(
                    [
                        "trellis",
                        "--confirm-trellis-bootstrap",
                        "--project-root",
                        str(root),
                        "--harness",
                        "codex",
                    ]
                )

    def test_trellis_finalize_preserves_prior_confirmation(self):
        with TempRepo() as root:
            create_valid_trellis(root)
            create_trellis_platform(root, "codex")
            outcome = module.InstallOutcome("trellis", "official")
            module.set_trellis_readiness(
                outcome, activation="ready", bootstrap="pending"
            )
            (root / module.STATE_FILE).write_text(
                json.dumps(
                    module.state_payload(
                        outcome, ["codex"], None, minimal=False
                    )
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                module, "detect_codex_plugin_frameworks", return_value=set()
            ), contextlib.redirect_stdout(io.StringIO()):
                rc = module.main(
                    [
                        "--finalize",
                        "--confirm-trellis-bootstrap",
                        "--project-root",
                        str(root),
                        "--yes",
                    ]
                )
            self.assertEqual(rc, 0)
            state = json.loads(
                (root / module.STATE_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "ready")
            self.assertEqual(state["readiness"]["activation"], "ready")
            self.assertEqual(state["readiness"]["bootstrap"], "ready")

    def test_main_adds_minimal_policy_after_successful_install(self):
        with TempRepo() as root, mock.patch.object(
            module,
            "install_framework",
            return_value=module.InstallOutcome(
                framework="openspec", integration="official"
            ),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = module.main(
                    [
                        "openspec",
                        "--harness",
                        "codex",
                        "--project-root",
                        str(root),
                        "--minimal",
                        "--yes",
                    ]
                )
            self.assertEqual(rc, 0)
            text = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(module.MINIMAL_START, text)
            state = json.loads(
                (root / module.STATE_FILE).read_text(encoding="utf-8")
            )
            self.assertTrue(state["minimal"])
            self.assertEqual(state["framework"], "openspec")

    def test_none_preserves_existing_state(self):
        with TempRepo() as root:
            state_path = root / module.STATE_FILE
            state_path.write_text(
                json.dumps(
                    {
                        "framework": "matt",
                        "integration": "project-skills",
                        "status": "ready",
                    }
                ),
                encoding="utf-8",
            )
            before = state_path.read_text(encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    module.main(
                        ["none", "--project-root", str(root), "--yes"]
                    ),
                    0,
                )
            self.assertEqual(state_path.read_text(encoding="utf-8"), before)

    def test_none_cannot_be_combined_with_minimal(self):
        with TempRepo() as root:
            with self.assertRaises(module.BootstrapError):
                module.main(
                    [
                        "none",
                        "--minimal",
                        "--project-root",
                        str(root),
                        "--yes",
                    ]
                )

    def test_legacy_framework_blocks_new_install(self):
        with TempRepo() as root:
            (root / ".specify").mkdir()
            with self.assertRaises(module.BootstrapError) as ctx:
                module.main(
                    [
                        "openspec",
                        "--harness",
                        "codex",
                        "--project-root",
                        str(root),
                        "--yes",
                        "--dry-run",
                    ]
                )
            self.assertIn("遗留框架", str(ctx.exception))

    def test_matt_official_is_rejected(self):
        with TempRepo() as root:
            with self.assertRaises(module.BootstrapError):
                module.main(
                    [
                        "matt",
                        "--harness",
                        "codex",
                        "--integration",
                        "official",
                        "--project-root",
                        str(root),
                        "--yes",
                        "--dry-run",
                    ]
                )


if __name__ == "__main__":
    unittest.main()

class NamingTests(unittest.TestCase):
    def test_skill_uses_only_agent_compass_name(self):
        skill = Path(__file__).parents[1] / "skills" / "agent-compass" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        self.assertIn("name: agent-compass", text)
        self.assertIn("/agent-compass", text)
        self.assertNotIn("name: agent-framework", text)
        self.assertNotIn("/agent-framework", text)

    def test_retired_state_file_is_rejected(self):
        with TempRepo() as root:
            (root / module.RETIRED_STATE_FILE).write_text("{}", encoding="utf-8")
            with self.assertRaises(module.BootstrapError):
                module.reject_retired_name_traces(root)

    def test_retired_managed_block_is_rejected(self):
        with TempRepo() as root:
            (root / "AGENTS.md").write_text(
                "<!-- agent-framework-selector:start framework=matt integration=project-skills -->\n",
                encoding="utf-8",
            )
            with self.assertRaises(module.BootstrapError):
                module.reject_retired_name_traces(root)

    def test_retired_skill_directory_is_rejected(self):
        with TempRepo() as root:
            old = root / ".agents/skills/agent-framework"
            old.mkdir(parents=True)
            with self.assertRaises(module.BootstrapError):
                module.reject_retired_name_traces(root)
