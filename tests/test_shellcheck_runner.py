from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import lzma
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_pinned_shellcheck",
    ROOT / "scripts" / "run-pinned-shellcheck.py",
)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


def _fixture_archive(binary: bytes, *, mode: int = 0o755) -> bytes:
    uncompressed = io.BytesIO()
    with tarfile.open(
        fileobj=uncompressed,
        mode="w",
        format=tarfile.USTAR_FORMAT,
    ) as archive:
        member = tarfile.TarInfo("shellcheck")
        member.mode = mode
        member.size = len(binary)
        member.mtime = 0
        archive.addfile(member, io.BytesIO(binary))
    return lzma.compress(uncompressed.getvalue(), format=lzma.FORMAT_XZ)


class PinnedShellCheckRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_verified_binary_checks_outer_and_inner_identities(self) -> None:
        binary = b"synthetic-shellcheck"
        archive = _fixture_archive(binary)
        path = self.root / "shellcheck.tar.xz"
        path.write_bytes(archive)
        contract = {"shellcheck": (0o755, len(binary), hashlib.sha256(binary).hexdigest())}
        with (
            patch.object(RUNNER, "ARCHIVE_MEMBERS", ("shellcheck",)),
            patch.object(RUNNER, "MEMBER_CONTRACTS", contract),
            patch.object(RUNNER, "BINARY_MEMBER", "shellcheck"),
            patch.object(RUNNER, "ARCHIVE_SIZE", len(archive)),
            patch.object(RUNNER, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest()),
        ):
            self.assertEqual(RUNNER.verified_binary(path), binary)

    def test_inner_binary_mode_is_enforced(self) -> None:
        binary = b"synthetic-shellcheck"
        archive = _fixture_archive(binary, mode=0o644)
        path = self.root / "shellcheck.tar.xz"
        path.write_bytes(archive)
        contract = {"shellcheck": (0o755, len(binary), hashlib.sha256(binary).hexdigest())}
        with (
            patch.object(RUNNER, "ARCHIVE_MEMBERS", ("shellcheck",)),
            patch.object(RUNNER, "MEMBER_CONTRACTS", contract),
            patch.object(RUNNER, "BINARY_MEMBER", "shellcheck"),
            patch.object(RUNNER, "ARCHIVE_SIZE", len(archive)),
            patch.object(RUNNER, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest()),
        ):
            with self.assertRaisesRegex(RUNNER.ShellCheckError, "metadata differs"):
                RUNNER.verified_binary(path)

    def test_runner_outer_identity_matches_dependency_lock(self) -> None:
        lock = json.loads(
            (ROOT / "dependencies" / "lock.yml").read_text(encoding="utf-8")
        )
        dependency = lock["dependencies"]["shellcheck_linux_x86_64"]
        self.assertEqual(dependency["size"], RUNNER.ARCHIVE_SIZE)
        self.assertEqual(dependency["sha256"], RUNNER.ARCHIVE_SHA256)
        self.assertEqual(dependency["version"], "v0.11.0")
        self.assertEqual(
            dependency["commit"],
            "aac0823e6b58f8a499e856e93738082691cbf212",
        )

    def test_real_cached_release_asset_matches_inner_contract(self) -> None:
        cache = ROOT / ".cache" / "op13" / "files"
        candidates = sorted(
            {
                *cache.glob("shellcheck_linux_x86_64-8c3be12b05d5*.tar.xz"),
                *cache.glob("shellcheck-v0.11.0.linux.x86_64.tar.xz"),
            }
        )
        if not candidates:
            self.skipTest("pinned ShellCheck release asset is not cached")
        binary = RUNNER.verified_binary(candidates[0])
        self.assertEqual(len(binary), RUNNER.MEMBER_CONTRACTS[RUNNER.BINARY_MEMBER][1])


if __name__ == "__main__":
    unittest.main()
