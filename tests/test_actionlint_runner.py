from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_pinned_actionlint",
    ROOT / "scripts" / "run-pinned-actionlint.py",
)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


def _fixture_archive(binary: bytes, *, mode: int = 0o755) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        member = tarfile.TarInfo("actionlint")
        member.mode = mode
        member.size = len(binary)
        member.mtime = 0
        archive.addfile(member, io.BytesIO(binary))
    return output.getvalue()


class PinnedActionlintRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_verified_binary_checks_both_archive_and_inner_binary(self) -> None:
        binary = b"synthetic-actionlint"
        archive = _fixture_archive(binary)
        path = self.root / "actionlint.tar.gz"
        path.write_bytes(archive)
        with (
            patch.object(RUNNER, "ARCHIVE_MEMBERS", ("actionlint",)),
            patch.object(RUNNER, "ARCHIVE_SIZE", len(archive)),
            patch.object(RUNNER, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest()),
            patch.object(RUNNER, "BINARY_SIZE", len(binary)),
            patch.object(RUNNER, "BINARY_SHA256", hashlib.sha256(binary).hexdigest()),
        ):
            self.assertEqual(RUNNER.verified_binary(path), binary)

    def test_inner_binary_mode_is_enforced(self) -> None:
        binary = b"synthetic-actionlint"
        archive = _fixture_archive(binary, mode=0o644)
        path = self.root / "actionlint.tar.gz"
        path.write_bytes(archive)
        with (
            patch.object(RUNNER, "ARCHIVE_MEMBERS", ("actionlint",)),
            patch.object(RUNNER, "ARCHIVE_SIZE", len(archive)),
            patch.object(RUNNER, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest()),
            patch.object(RUNNER, "BINARY_SIZE", len(binary)),
            patch.object(RUNNER, "BINARY_SHA256", hashlib.sha256(binary).hexdigest()),
        ):
            with self.assertRaisesRegex(RUNNER.ActionlintError, "metadata differs"):
                RUNNER.verified_binary(path)

    def test_runner_outer_identity_matches_dependency_lock(self) -> None:
        lock = json.loads(
            (ROOT / "dependencies" / "lock.yml").read_text(encoding="utf-8")
        )
        dependency = lock["dependencies"]["actionlint_linux_amd64"]
        self.assertEqual(dependency["size"], RUNNER.ARCHIVE_SIZE)
        self.assertEqual(dependency["sha256"], RUNNER.ARCHIVE_SHA256)
        self.assertEqual(dependency["version"], "v1.7.11")
        self.assertEqual(
            dependency["commit"],
            "393031adb9afb225ee52ae2ccd7a5af5525e03e8",
        )

    def test_real_cached_release_asset_matches_inner_contract(self) -> None:
        candidates = sorted(
            (ROOT / ".cache" / "op13" / "files").glob(
                "actionlint_linux_amd64-900919a84f22*.tar.gz"
            )
        )
        if not candidates:
            self.skipTest("pinned Actionlint release asset is not cached")
        binary = RUNNER.verified_binary(candidates[0])
        self.assertEqual(len(binary), RUNNER.BINARY_SIZE)

    def test_runner_passes_the_verified_shellcheck_path_and_disables_pyflakes(self) -> None:
        completed = Mock(returncode=0)
        with (
            patch.object(RUNNER, "verified_binary", return_value=b"actionlint"),
            patch.object(
                RUNNER,
                "verified_shellcheck_binary",
                return_value=b"shellcheck",
            ),
            patch.object(RUNNER.subprocess, "run", return_value=completed) as run,
        ):
            status = RUNNER.run_actionlint(
                self.root / "actionlint.tar.gz",
                self.root / "shellcheck.tar.xz",
                ["workflow.yml", "workflow.yaml"],
            )
        self.assertEqual(status, 0)
        command = run.call_args.args[0]
        self.assertIn("-pyflakes=", command)
        shellcheck_arguments = [
            value for value in command if value.startswith("-shellcheck=")
        ]
        self.assertEqual(len(shellcheck_arguments), 1)
        self.assertTrue(Path(shellcheck_arguments[0].split("=", 1)[1]).is_absolute())
        self.assertEqual(command[-2:], ["workflow.yml", "workflow.yaml"])


if __name__ == "__main__":
    unittest.main()
