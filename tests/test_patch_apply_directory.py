from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import discover_configs
from lib.context import new_context, write_context
from lib.errors import BuildToolError
from lib.patches import _execute_operation, _load_series, apply_patch_series
from tests.support import make_repository, write_json


class PatchApplyDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))
        _, self.lock, self.profiles, self.features = discover_configs(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _patch(self) -> Path:
        patch = self.root / "patches" / "common" / "directory.patch"
        patch.write_text(
            "diff --git a/fixture.txt b/fixture.txt\n"
            "--- a/fixture.txt\n"
            "+++ b/fixture.txt\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n",
            encoding="utf-8",
            newline="\n",
        )
        return patch

    def _source_context(self) -> tuple[Path, Path]:
        source = self.root / "out" / "source"
        target = source / "nested" / "fixture.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("before\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=source,
            check=True,
        )
        resolved = source / ".op13" / "resolved.xml"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(self.profiles["oos16"].locked_manifest.read_bytes())
        context_path = source / ".op13" / "build-context.json"
        write_context(
            context_path,
            new_context(self.profiles["oos16"], self.lock, resolved, smoke=False),
        )
        return source, context_path

    def _write_operation(self, operation: dict[str, object]) -> Path:
        path = self.root / "patches" / "series" / "test.yml"
        write_json(
            path,
            {
                "schema_version": 1,
                "id": "test",
                "operations": [operation],
            },
        )
        return path

    def test_directory_applies_from_git_top_and_records_changed_target(self) -> None:
        patch = self._patch()
        source, context_path = self._source_context()
        self._write_operation(
            {
                "id": "nested",
                "type": "apply",
                "path": patch.relative_to(self.root).as_posix(),
                "cwd": ".",
                "directory": "nested",
                "strip": 1,
            }
        )

        records = apply_patch_series(
            root=self.root,
            source_dir=source,
            cache_root=self.root / ".cache" / "op13",
            context_path=context_path,
            profile=self.profiles["oos16"],
            feature=self.features["test"],
            lock=self.lock,
            root_variant="none",
            check_only=False,
            smoke=False,
            log_dir=self.root / "out" / "debug",
        )

        self.assertEqual(
            (source / "nested" / "fixture.txt").read_text(encoding="utf-8"),
            "after\n",
        )
        self.assertEqual(records[0]["directory"], "nested")
        self.assertEqual(records[0]["declared_targets"], ["nested/fixture.txt"])
        self.assertEqual(
            records[0]["pre_sha256"]["nested/fixture.txt"],
            hashlib.sha256(b"before\n").hexdigest(),
        )
        self.assertEqual(
            records[0]["post_sha256"]["nested/fixture.txt"],
            hashlib.sha256(b"after\n").hexdigest(),
        )

    def test_successful_git_skip_is_rejected(self) -> None:
        patch = self._patch()
        source, context_path = self._source_context()
        self._write_operation(
            {
                "id": "nested-without-directory",
                "type": "apply",
                "path": patch.relative_to(self.root).as_posix(),
                "cwd": "nested",
                "strip": 1,
            }
        )

        with self.assertRaisesRegex(BuildToolError, "skipped a declared patch target"):
            apply_patch_series(
                root=self.root,
                source_dir=source,
                cache_root=self.root / ".cache" / "op13",
                context_path=context_path,
                profile=self.profiles["oos16"],
                feature=self.features["test"],
                lock=self.lock,
                root_variant="none",
                check_only=False,
                smoke=False,
                log_dir=self.root / "out" / "debug",
            )
        self.assertEqual(
            (source / "nested" / "fixture.txt").read_text(encoding="utf-8"),
            "before\n",
        )

    def test_directory_requires_cwd_to_be_git_top(self) -> None:
        patch = self._patch()
        source, context_path = self._source_context()
        self._write_operation(
            {
                "id": "nested-directory-from-subdirectory",
                "type": "apply",
                "path": patch.relative_to(self.root).as_posix(),
                "cwd": "nested",
                "directory": ".",
                "strip": 1,
            }
        )

        with self.assertRaisesRegex(BuildToolError, "must run from the Git top"):
            apply_patch_series(
                root=self.root,
                source_dir=source,
                cache_root=self.root / ".cache" / "op13",
                context_path=context_path,
                profile=self.profiles["oos16"],
                feature=self.features["test"],
                lock=self.lock,
                root_variant="none",
                check_only=False,
                smoke=False,
                log_dir=self.root / "out" / "debug",
            )

    def test_directory_rejects_unsafe_and_fuzzy_declarations(self) -> None:
        patch = self._patch()
        cases = (
            (
                {
                    "id": "escape",
                    "type": "apply",
                    "path": patch.relative_to(self.root).as_posix(),
                    "directory": "../escape",
                },
                "path must remain relative",
            ),
            (
                {
                    "id": "fuzzy",
                    "type": "apply",
                    "path": patch.relative_to(self.root).as_posix(),
                    "directory": "nested",
                    "fuzz": 1,
                },
                "incompatible with fuzzy",
            ),
            (
                {
                    "id": "wrong-type",
                    "type": "copy",
                    "path": patch.relative_to(self.root).as_posix(),
                    "destination": "fixture.patch",
                    "directory": "nested",
                },
                "supported only for patch operations",
            ),
        )
        for operation, message in cases:
            with self.subTest(operation=operation["id"]):
                series = self._write_operation(operation)
                with self.assertRaisesRegex(BuildToolError, message):
                    _load_series(series)

    def test_unchanged_declared_target_is_rejected_after_apply(self) -> None:
        patch = self._patch()
        source, _ = self._source_context()
        runner = Mock()

        def fake_run(
            argv: object,
            *,
            cwd: Path,
            capture: bool = False,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            command = [str(item) for item in argv]
            if command[-2:] == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=str(cwd.resolve()) + "\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="Checking patch nested/fixture.txt...\n",
                stderr="",
            )

        runner.run.side_effect = fake_run
        operation = {
            "id": "test:unchanged",
            "type": "apply",
            "path": patch.relative_to(self.root).as_posix(),
            "cwd": ".",
            "directory": "nested",
            "strip": 1,
        }

        with self.assertRaisesRegex(
            BuildToolError,
            "left declared patch targets unchanged",
        ):
            _execute_operation(
                operation,
                root=self.root,
                source_dir=source,
                cache_root=self.root / ".cache" / "op13",
                lock=self.lock,
                base="oos16",
                root_variant="none",
                runner=runner,
                check_only=False,
                smoke=False,
            )

    def test_common_manifest_uses_directory_for_baseband_patch(self) -> None:
        document = json.loads(
            (ROOT / "patches" / "series" / "common.yml").read_text(encoding="utf-8")
        )
        operation = next(
            item
            for item in document["operations"]
            if item["id"] == "pin-baseband-guard-build-version"
        )
        self.assertEqual(operation["cwd"], "kernel_platform/{kernel_tree}")
        self.assertEqual(operation["directory"], "security/baseband-guard")


if __name__ == "__main__":
    unittest.main()
