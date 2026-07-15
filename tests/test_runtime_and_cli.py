from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import op13
from lib.config import Dependency, discover_configs
from lib.errors import BuildToolError, SourceChanged
from lib.runtime import (
    REPO_INIT_STORAGE_FLAGS,
    REPO_SYNC_STORAGE_FLAGS,
    CommandRunner,
    _verify_git_checkout,
    check_manifest_update,
    validate_resolved_manifest,
)
from tests.support import PROJECT_COMMIT, make_repository


class SequenceRunner:
    def __init__(self, commits: list[str]) -> None:
        self.commits = iter(commits)
        self.commands: list[list[str]] = []

    def run(self, argv, *, capture=False, **kwargs):
        self.commands.append(list(argv))
        commit = next(self.commits)
        return subprocess.CompletedProcess(argv, 0, f"{commit}\t{argv[-1]}\n", "")


class RuntimeAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))
        _, _, self.profiles, _ = discover_configs(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_source_monitor_compares_manifest_and_oneplus_projects(self) -> None:
        profile = self.profiles["oos16"]
        runner = SequenceRunner([profile.manifest_revision, PROJECT_COMMIT])
        output = self.root / "monitor"
        self.assertFalse(check_manifest_update(profile, output, runner=runner))
        self.assertEqual(len(runner.commands), 2)
        self.assertIn("unchanged", (output / "source-changes.md").read_text(encoding="utf-8"))

    def test_source_monitor_reports_project_drift(self) -> None:
        profile = self.profiles["oos16"]
        runner = SequenceRunner([profile.manifest_revision, "9" * 40])
        output = self.root / "monitor"
        self.assertTrue(check_manifest_update(profile, output, runner=runner))
        report = (output / "source-changes.md").read_text(encoding="utf-8")
        self.assertIn("Changed locks", report)
        self.assertIn("9" * 40, report)

    def test_resolved_manifest_rejects_moving_revision(self) -> None:
        path = self.root / "moving.xml"
        path.write_text(
            '<manifest><project name="x" path="x" revision="refs/heads/main" /></manifest>',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BuildToolError, "not pinned"):
            validate_resolved_manifest(path)

    def test_repo_sync_is_shallow_and_storage_optimized(self) -> None:
        self.assertIn("--depth=1", REPO_INIT_STORAGE_FLAGS)
        self.assertIn("--no-tags", REPO_INIT_STORAGE_FLAGS)
        self.assertIn("--optimized-fetch", REPO_SYNC_STORAGE_FLAGS)
        self.assertIn("--detach", REPO_SYNC_STORAGE_FLAGS)

    def test_cached_git_dependency_must_be_byte_clean(self) -> None:
        checkout = self.root / "dependency-checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=checkout, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.invalid"],
            cwd=checkout,
            check=True,
        )
        (checkout / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
        tracked = checkout / "module.c"
        tracked.write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=checkout, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=checkout, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        url = "https://example.invalid/dependency.git"
        subprocess.run(["git", "remote", "add", "origin", url], cwd=checkout, check=True)
        dependency = Dependency(
            id="fixture",
            kind="git",
            url=url,
            commit=commit,
            ref=commit,
            sha256=None,
            required_for=("test",),
            raw={},
        )
        runner = CommandRunner(verbose=False)
        _verify_git_checkout(checkout, dependency, runner)

        tracked.write_text("modified\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "modified, untracked, or ignored"):
            _verify_git_checkout(checkout, dependency, runner)
        tracked.write_text("clean\n", encoding="utf-8")

        untracked = checkout / "untracked.c"
        untracked.write_text("untracked\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "modified, untracked, or ignored"):
            _verify_git_checkout(checkout, dependency, runner)
        untracked.unlink()

        ignored = checkout / "generated.ignored"
        ignored.write_text("ignored\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "modified, untracked, or ignored"):
            _verify_git_checkout(checkout, dependency, runner)

    def test_pipeline_scan_parses_python_instead_of_matching_diagnostic_text(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "diagnostic.py").write_text(
            'MESSAGE = "shell=True is forbidden"\n',
            encoding="utf-8",
        )
        op13._scan_pipeline_sources(self.root)
        (scripts / "unsafe.py").write_text(
            "import subprocess\nsubprocess.run(['true'], shell=True)\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BuildToolError, "shell execution"):
            op13._scan_pipeline_sources(self.root)

    def test_workflow_argument_vectors_parse(self) -> None:
        parser = op13.build_parser()
        vectors = [
            ["sync-sources", "--base", "oos16", "--output", "out/source"],
            [
                "apply-series",
                "--base",
                "oos16",
                "--profile",
                "full",
                "--root",
                "kernelsu-next",
                "--source-dir",
                "out/source",
                "--dry-run",
                "--log",
                "out/debug",
            ],
            [
                "configure",
                "--base",
                "oos16",
                "--profile",
                "full",
                "--root",
                "none",
                "--optimization",
                "O3",
                "--lto",
                "full",
                "--build-target",
                "mixed",
                "--source-dir",
                "out/source",
                "--output",
                "out/build",
            ],
            ["build-kernel", "--source-dir", "out/source", "--output", "out/build", "--clean", "--debug"],
            [
                "build-modules",
                "--source-dir",
                "out/source",
                "--kernel-output",
                "out/build",
                "--output",
                "out/build/modules",
            ],
            [
                "verify",
                "--base",
                "oos16",
                "--profile",
                "full",
                "--root",
                "kernelsu",
                "--build-target",
                "kernel",
                "--output",
                "out/build",
            ],
            [
                "package",
                "--base",
                "oos16",
                "--profile",
                "full",
                "--root",
                "kernelsu-next",
                "--build-target",
                "mixed",
                "--input",
                "out/build",
                "--output",
                "out/dist",
                "--pre-release",
            ],
        ]
        for vector in vectors:
            with self.subTest(command=vector[0]):
                parsed = parser.parse_args(vector)
                self.assertTrue(callable(parsed.handler))

    def test_source_changed_maps_to_exit_two(self) -> None:
        with mock.patch.object(op13, "monitor_or_raise", side_effect=SourceChanged("moved")):
            code = op13.main(
                [
                    "sync-sources",
                    "--repo-root",
                    str(self.root),
                    "--base",
                    "oos16",
                    "--check-only",
                    "--output",
                    str(self.root / "monitor"),
                ]
            )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
