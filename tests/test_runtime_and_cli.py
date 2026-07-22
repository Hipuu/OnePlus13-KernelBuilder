from __future__ import annotations

import hashlib
from email.message import Message
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import op13
from lib.config import Dependency, discover_configs, load_dependency_lock
from lib.errors import BuildToolError, SourceChanged
from lib.runtime import (
    REPO_INIT_STORAGE_FLAGS,
    REPO_SYNC_NETWORK_JOBS,
    REPO_SYNC_STORAGE_FLAGS,
    CommandRunner,
    _download_verified,
    _fetch_git,
    _repo_sync_job_flags,
    _verify_git_checkout,
    assert_manifest_matches_lock,
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


class DownloadResponse:
    def __init__(self, payload: bytes, *, content_length: str | None) -> None:
        self.payload = payload
        self.position = 0
        self.read_sizes: list[int] = []
        self.bytes_returned = 0
        self.headers = Message()
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            size = len(self.payload) - self.position
        end = min(len(self.payload), self.position + size)
        block = self.payload[self.position : end]
        self.position = end
        self.bytes_returned += len(block)
        return block


class RuntimeAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))
        _, _, self.profiles, _ = discover_configs(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _download_dependency(payload: bytes, *, size: int | None) -> Dependency:
        raw = {} if size is None else {"size": size}
        return Dependency(
            id="fixture-download",
            kind="file",
            url="https://example.invalid/fixture.bin",
            commit=None,
            ref=None,
            sha256=hashlib.sha256(payload).hexdigest(),
            required_for=("test",),
            raw=raw,
        )

    def _assert_no_download_cache_files(self, destination: Path) -> None:
        self.assertFalse(destination.exists())
        if destination.parent.exists():
            self.assertEqual(list(destination.parent.iterdir()), [])

    def test_locked_download_validates_content_length_and_caches_atomically(self) -> None:
        payload = b"locked download payload\n"
        dependency = self._download_dependency(payload, size=len(payload))
        destination = self.root / "download-cache" / "fixture.bin"
        response = DownloadResponse(payload, content_length=str(len(payload)))

        with mock.patch(
            "lib.runtime.urllib.request.urlopen",
            return_value=response,
        ) as urlopen:
            result = _download_verified(dependency, destination, offline=False)

        self.assertEqual(result, destination)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(
            list(destination.parent.glob(f".{dependency.id}.*")),
            [],
        )
        urlopen.assert_called_once()

    def test_locked_download_rejects_mismatched_content_length_before_reading(self) -> None:
        payload = b"locked payload"
        dependency = self._download_dependency(payload, size=len(payload))
        destination = self.root / "download-cache" / "fixture.bin"
        response = DownloadResponse(payload, content_length=str(len(payload) + 1))

        with mock.patch(
            "lib.runtime.urllib.request.urlopen",
            return_value=response,
        ), self.assertRaisesRegex(BuildToolError, "Content-Length mismatch"):
            _download_verified(dependency, destination, offline=False)

        self.assertEqual(response.read_sizes, [])
        self._assert_no_download_cache_files(destination)

    def test_locked_download_stops_after_first_oversized_byte_without_header(self) -> None:
        expected_payload = b"four"
        dependency = self._download_dependency(
            expected_payload,
            size=len(expected_payload),
        )
        destination = self.root / "download-cache" / "fixture.bin"
        response = DownloadResponse(
            expected_payload + b"excess bytes",
            content_length=None,
        )

        with mock.patch(
            "lib.runtime.urllib.request.urlopen",
            return_value=response,
        ), self.assertRaisesRegex(BuildToolError, "exceeds locked size"):
            _download_verified(dependency, destination, offline=False)

        self.assertEqual(response.bytes_returned, len(expected_payload) + 1)
        self.assertEqual(response.read_sizes, [len(expected_payload) + 1])
        self._assert_no_download_cache_files(destination)

    def test_locked_download_rejects_short_body_and_removes_temporary_file(self) -> None:
        expected_payload = b"complete locked payload"
        dependency = self._download_dependency(
            expected_payload,
            size=len(expected_payload),
        )
        destination = self.root / "download-cache" / "fixture.bin"
        response = DownloadResponse(
            expected_payload[:-1],
            content_length=str(len(expected_payload)),
        )

        with mock.patch(
            "lib.runtime.urllib.request.urlopen",
            return_value=response,
        ), self.assertRaisesRegex(BuildToolError, "differs from Content-Length"):
            _download_verified(dependency, destination, offline=False)

        self._assert_no_download_cache_files(destination)

    def test_download_without_locked_size_still_validates_content_length(self) -> None:
        payload = b"legacy download payload"
        dependency = self._download_dependency(payload, size=None)
        destination = self.root / "download-cache" / "fixture.bin"
        response = DownloadResponse(payload[:-1], content_length=str(len(payload)))

        with mock.patch(
            "lib.runtime.urllib.request.urlopen",
            return_value=response,
        ), self.assertRaisesRegex(BuildToolError, "differs from Content-Length"):
            _download_verified(dependency, destination, offline=False)

        self._assert_no_download_cache_files(destination)

    def test_download_rejects_malformed_content_length_before_reading(self) -> None:
        payload = b"locked payload"
        dependency = self._download_dependency(payload, size=len(payload))
        destination = self.root / "download-cache" / "fixture.bin"
        response = DownloadResponse(payload, content_length="12, 12")

        with mock.patch(
            "lib.runtime.urllib.request.urlopen",
            return_value=response,
        ), self.assertRaisesRegex(BuildToolError, "invalid Content-Length"):
            _download_verified(dependency, destination, offline=False)

        self.assertEqual(response.read_sizes, [])
        self._assert_no_download_cache_files(destination)

    def test_repository_lock_bounds_every_download(self) -> None:
        lock = load_dependency_lock(ROOT / "dependencies" / "lock.yml")
        downloads = {
            dependency_id: dependency.raw.get("size")
            for dependency_id, dependency in lock.dependencies.items()
            if dependency.kind != "git"
        }
        self.assertTrue(downloads)
        self.assertTrue(
            all(
                isinstance(size, int) and not isinstance(size, bool) and size > 0
                for size in downloads.values()
            )
        )
        self.assertEqual(
            {
                dependency_id: downloads[dependency_id]
                for dependency_id in (
                    "repo_launcher",
                    "magisk_release_apk",
                    "nethunter_wireless_firmware",
                )
            },
            {
                "repo_launcher": 44_952,
                "magisk_release_apk": 11_613_864,
                "nethunter_wireless_firmware": 28_235_671,
            },
        )

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

    def test_manifest_lock_accepts_repo_root_path_spelling_alias(self) -> None:
        locked = self.root / "locked-root.xml"
        resolved = self.root / "resolved-root.xml"
        locked.write_text(
            f'<manifest><project name="root" path="./" revision="{PROJECT_COMMIT}" /></manifest>',
            encoding="utf-8",
        )
        resolved.write_text(
            f'<manifest><project name="root" path="." revision="{PROJECT_COMMIT}" /></manifest>',
            encoding="utf-8",
        )

        assert_manifest_matches_lock(resolved, locked)

    def test_manifest_path_canonicalization_is_narrow_and_duplicate_safe(self) -> None:
        locked = self.root / "locked-subdirectory.xml"
        resolved = self.root / "resolved-subdirectory.xml"
        locked.write_text(
            f'<manifest><project name="root" path="./kernel" revision="{PROJECT_COMMIT}" /></manifest>',
            encoding="utf-8",
        )
        resolved.write_text(
            f'<manifest><project name="root" path="kernel" revision="{PROJECT_COMMIT}" /></manifest>',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BuildToolError, "differs from its profile lock"):
            assert_manifest_matches_lock(resolved, locked)

        duplicate = self.root / "duplicate-root.xml"
        duplicate.write_text(
            "<manifest>"
            f'<project name="a" path="." revision="{PROJECT_COMMIT}" />'
            f'<project name="b" path="./" revision="{PROJECT_COMMIT}" />'
            "</manifest>",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BuildToolError, "repeats checkout path"):
            validate_resolved_manifest(duplicate)

    def test_repo_sync_is_shallow_and_storage_optimized(self) -> None:
        self.assertIn("--depth=1", REPO_INIT_STORAGE_FLAGS)
        self.assertIn("--no-tags", REPO_INIT_STORAGE_FLAGS)
        self.assertIn("--optimized-fetch", REPO_SYNC_STORAGE_FLAGS)
        self.assertIn("--detach", REPO_SYNC_STORAGE_FLAGS)
        self.assertEqual(REPO_SYNC_NETWORK_JOBS, 1)
        self.assertEqual(
            _repo_sync_job_flags(4),
            ("--jobs-network", "1", "--jobs-checkout", "4", "-j", "4"),
        )
        with self.assertRaisesRegex(BuildToolError, "jobs must be positive"):
            _repo_sync_job_flags(0)

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

    def test_new_git_dependency_is_materialized_with_lf_bytes(self) -> None:
        origin = self.root / "line-ending-origin"
        origin.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=origin, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=origin, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=origin, check=True)
        tracked = origin / "series.patch"
        tracked.write_bytes(b"first\nsecond\n")
        subprocess.run(["git", "add", "series.patch"], cwd=origin, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=origin, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=origin, check=True, capture_output=True, text=True
        ).stdout.strip()
        url = str(origin.resolve())
        dependency = Dependency(
            id="line-endings",
            kind="git",
            url=url,
            commit=commit,
            ref=commit,
            sha256=None,
            required_for=("test",),
            raw={},
        )
        checkout = self.root / "dependency-cache" / "line-endings"

        _fetch_git(dependency, checkout, CommandRunner(verbose=False), offline=False)

        self.assertEqual((checkout / "series.patch").read_bytes(), b"first\nsecond\n")
        self.assertEqual(
            subprocess.run(
                ["git", "config", "--local", "core.autocrlf"],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "false",
        )

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
