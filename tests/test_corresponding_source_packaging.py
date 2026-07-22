from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import artifacts as artifact_lib
from lib.artifacts import (
    ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS,
    ANYKERNEL_CARGO_GIT_ARCHIVE,
    ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES,
    ANYKERNEL_CORRESPONDING_SOURCE_FORMAT,
    ANYKERNEL_CORRESPONDING_SOURCE_MANIFEST,
    ANYKERNEL_CORRESPONDING_SOURCE_POLICY_MEMBER,
    ANYKERNEL_MAGISK_CARGO_LOCK_IDENTITY,
    _corresponding_source_cache_path,
    _expected_corresponding_source_root,
    _fetch_anykernel_corresponding_source_dependencies,
    _load_anykernel_corresponding_source_policy,
    _prepare_anykernel_corresponding_source_tree,
    _verify_corresponding_source_tarball,
    _verify_zip_file_manifest,
    deterministic_zip,
)
from lib.config import load_dependency_lock, sha256_file
from lib.errors import BuildToolError


MAGISK_GITMODULES = b"""[submodule "selinux"]
	path = native/src/external/selinux
	url = https://github.com/topjohnwu/selinux.git
[submodule "lz4"]
	path = native/src/external/lz4
	url = https://github.com/lz4/lz4.git
[submodule "libcxx"]
	path = native/src/external/libcxx
	url = https://github.com/topjohnwu/libcxx.git
[submodule "cxx-rs"]
	path = native/src/external/cxx-rs
	url = https://github.com/topjohnwu/cxx.git
[submodule "lsplt"]
	path = native/src/external/lsplt
	url = https://github.com/LSPosed/LSPlt.git
[submodule "system_properties"]
	path = native/src/external/system_properties
	url = https://github.com/topjohnwu/system_properties.git
[submodule "crt0"]
	path = native/src/external/crt0
	url = https://github.com/topjohnwu/crt0.git
"""


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _tar_gz_bytes(
    source_root: str,
    *,
    files: dict[str, bytes] | None = None,
    symlink_target: str | None = None,
) -> bytes:
    payloads = {"README.md": b"fixture source\n", **(files or {})}
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.USTAR_FORMAT,
        ) as archive:
            directory = tarfile.TarInfo(source_root)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            directory.mtime = 0
            archive.addfile(directory)
            for relative, content in sorted(payloads.items()):
                member = tarfile.TarInfo(f"{source_root}/{relative}")
                member.size = len(content)
                member.mode = 0o644
                member.mtime = 0
                archive.addfile(member, io.BytesIO(content))
            if symlink_target is not None:
                link = tarfile.TarInfo(f"{source_root}/source-link")
                link.type = tarfile.SYMTYPE
                link.linkname = symlink_target
                link.mode = 0o777
                link.mtime = 0
                archive.addfile(link)
    return output.getvalue()


def _cargo_manifest(name: str, version: str, license_value: str) -> bytes:
    return (
        "[package]\n"
        f"name = {json.dumps(name)}\n"
        f"version = {json.dumps(version)}\n"
        f"license = {json.dumps(license_value)}\n"
    ).encode("utf-8")


def _synthetic_cargo_lock(policy: dict[str, object]) -> bytes:
    lines = ["version = 4", ""]
    for index in range(13):
        lines.extend(
            [
                "[[package]]",
                f'name = "local-fixture-{index}"',
                'version = "0.0.0"',
                "",
            ]
        )
    for record in policy["archives"]:
        for package in record["cargo_packages"]:
            lines.extend(
                [
                    "[[package]]",
                    f"name = {json.dumps(package['name'])}",
                    f"version = {json.dumps(package['version'])}",
                    f"source = {json.dumps(package['source'])}",
                ]
            )
            if package["checksum"] is not None:
                lines.append(f"checksum = {json.dumps(package['checksum'])}")
            lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


class CorrespondingSourcePackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy_path = (
            self.root / "packaging" / "anykernel3" / "CORRESPONDING-SOURCE.json"
        )
        self.lock_path = self.root / "dependencies" / "lock.yml"
        self.policy_data = json.loads(
            (ROOT / "packaging" / "anykernel3" / "CORRESPONDING-SOURCE.json").read_text(
                encoding="utf-8"
            )
        )
        self.lock_data = json.loads(
            (ROOT / "dependencies" / "lock.yml").read_text(encoding="utf-8")
        )
        self.state: dict[str, object] = {
            "schema_version": 1,
            "dependencies": {},
            "dry_run": False,
        }
        self.assets = self.root / "assets"
        self.assets.mkdir()

        for record in self.policy_data["archives"]:
            if record["dependency"] == "magisk_source":
                continue
            dependency_id = record["dependency"]
            source_root = _expected_corresponding_source_root(record)
            files: dict[str, bytes] = {}
            for package in record["cargo_packages"]:
                files[package["manifest_path"]] = _cargo_manifest(
                    package["name"],
                    package["version"],
                    record["license"],
                )
            archive_path = self.assets / f"{dependency_id}.tar.gz"
            archive_path.write_bytes(_tar_gz_bytes(source_root, files=files))
            self._seal_fixture_archive(record, archive_path)

        cargo_lock = _synthetic_cargo_lock(self.policy_data)
        self.policy_data["cargo_lock"] = {
            **self.policy_data["cargo_lock"],
            "size": len(cargo_lock),
            "sha256": hashlib.sha256(cargo_lock).hexdigest(),
        }
        magisk_record = self.policy_data["archives"][0]
        magisk_root = _expected_corresponding_source_root(magisk_record)
        magisk_archive = self.assets / "magisk_source.tar.gz"
        magisk_archive.write_bytes(
            _tar_gz_bytes(
                magisk_root,
                files={
                    ".gitmodules": MAGISK_GITMODULES,
                    "native/src/Cargo.lock": cargo_lock,
                },
            )
        )
        self._seal_fixture_archive(magisk_record, magisk_archive)

        _write_json(self.policy_path, self.policy_data)
        _write_json(self.lock_path, self.lock_data)
        self.lock = load_dependency_lock(self.lock_path)
        self.magisk_dependency = self.lock.dependencies["magisk_release_apk"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seal_fixture_archive(
        self,
        record: dict[str, object],
        archive_path: Path,
    ) -> None:
        dependency_id = str(record["dependency"])
        size = archive_path.stat().st_size
        digest = sha256_file(archive_path)
        record["size"] = size
        record["sha256"] = digest
        if record["relationship"] == "magisk-cargo-registry":
            record["cargo_packages"][0]["checksum"] = digest
        dependency = self.lock_data["dependencies"][dependency_id]
        dependency["size"] = size
        dependency["sha256"] = digest
        self.state["dependencies"][dependency_id] = {
            "kind": "file",
            "path": str(archive_path),
            "sha256": digest,
        }

    @contextmanager
    def _fixture_contract(self):
        quick_record = next(
            record
            for record in self.policy_data["archives"]
            if record["dependency"] == "magisk_cargo_git_quick_protobuf"
        )
        quick_identity = {
            **dict(ANYKERNEL_CARGO_GIT_ARCHIVE),
            "size": quick_record["size"],
            "sha256": quick_record["sha256"],
        }
        with mock.patch.object(
            artifact_lib,
            "ANYKERNEL_MAGISK_CARGO_LOCK_IDENTITY",
            dict(self.policy_data["cargo_lock"]),
        ), mock.patch.object(
            artifact_lib,
            "ANYKERNEL_CARGO_GIT_ARCHIVE",
            quick_identity,
        ), mock.patch.object(
            artifact_lib,
            "_verify_corresponding_source_git_tree_archive",
            return_value=None,
        ):
            yield

    def _prepare(self, name: str):
        with self._fixture_contract():
            return _prepare_anykernel_corresponding_source_tree(
                root=self.root,
                destination=self.root / name,
                state=self.state,
                lock=self.lock,
                magisk_dependency=self.magisk_dependency,
            )

    def test_checked_in_policy_matches_exact_dependency_and_cargo_locks(self) -> None:
        lock = load_dependency_lock(ROOT / "dependencies" / "lock.yml")
        policy = _load_anykernel_corresponding_source_policy(
            root=ROOT,
            lock=lock,
            magisk_dependency=lock.dependencies["magisk_release_apk"],
        )
        self.assertEqual(policy["format"], ANYKERNEL_CORRESPONDING_SOURCE_FORMAT)
        self.assertEqual(policy["cargo_lock"], dict(ANYKERNEL_MAGISK_CARGO_LOCK_IDENTITY))
        self.assertEqual(
            [record["dependency"] for record in policy["archives"]],
            list(ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES),
        )
        self.assertEqual(len(policy["archives"]), 150)
        self.assertEqual(
            sum(record["relationship"] == "magisk-cargo-registry" for record in policy["archives"]),
            140,
        )
        self.assertEqual(len(policy["magisk_gitlinks"]), 7)
        git_tree_records = {
            record["dependency"]: record.get("tree")
            for record in policy["archives"]
            if record["dependency"] in artifact_lib.ANYKERNEL_GIT_SOURCE_TREE_IDS
        }
        self.assertEqual(
            git_tree_records,
            dict(artifact_lib.ANYKERNEL_GIT_SOURCE_TREE_IDS),
        )
        self.assertTrue(
            all(
                "tree" not in record
                for record in policy["archives"]
                if record["relationship"] == "magisk-cargo-registry"
            )
        )

    def test_companion_tree_and_stored_zip_are_exact_and_deterministic(self) -> None:
        first = self._prepare("stage-first")
        second = self._prepare("stage-second")
        first_zip = self.root / "first.zip"
        second_zip = self.root / "second.zip"
        epoch = 1_704_067_200
        first_modes = {record["path"]: 0o644 for record in first["output_records"]}
        second_modes = {record["path"]: 0o644 for record in second["output_records"]}
        deterministic_zip(
            self.root / "stage-first",
            first_zip,
            epoch=epoch,
            member_modes=first_modes,
            compression=zipfile.ZIP_STORED,
        )
        deterministic_zip(
            self.root / "stage-second",
            second_zip,
            epoch=epoch,
            member_modes=second_modes,
            compression=zipfile.ZIP_STORED,
        )
        self.assertEqual(first_zip.read_bytes(), second_zip.read_bytes())
        _verify_zip_file_manifest(
            first_zip,
            first["output_records"],
            role="fixture corresponding source",
        )
        self.assertEqual(first["archive_count"], 150)
        self.assertEqual(len(first["output_records"]), 152)
        self.assertEqual(
            first["archive_dependencies"],
            list(ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES),
        )
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])

        with zipfile.ZipFile(first_zip, "r") as archive:
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
            self.assertTrue(
                all(
                    info.create_system == 3
                    and ((info.external_attr >> 16) & 0o777) == 0o644
                    and info.compress_type == zipfile.ZIP_STORED
                    for info in archive.infolist()
                )
            )
            manifest = json.loads(archive.read(ANYKERNEL_CORRESPONDING_SOURCE_MANIFEST))
            self.assertEqual(manifest["dependency_lock_sha256"], self.lock.digest)
            self.assertEqual(len(manifest["archives"]), 150)
            self.assertEqual(
                manifest["binary_relationships"][0]["source_dependencies"],
                ["magisk_busybox_source"],
            )
            self.assertEqual(
                set(manifest["binary_relationships"][1]["source_dependencies"]),
                {
                    "magisk_source",
                    *ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES[2:],
                },
            )
            for record in manifest["archives"]:
                data = archive.read(record["archive_path"])
                self.assertEqual(len(data), record["size"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"])

    def test_preseeded_closure_fetch_is_offline_and_exact(self) -> None:
        cache_root = self.root / "preseeded-cache"
        for dependency_id in ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES:
            dependency = self.lock.dependencies[dependency_id]
            destination = _corresponding_source_cache_path(cache_root, dependency)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = Path(self.state["dependencies"][dependency_id]["path"])
            shutil.copy2(source, destination)
        state = _fetch_anykernel_corresponding_source_dependencies(
            self.lock,
            cache_root,
            offline=True,
        )
        self.assertEqual(
            list(state["dependencies"]),
            list(ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES),
        )

    def test_changed_source_archive_is_rejected(self) -> None:
        dependency_id = ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS[0]
        source = Path(self.state["dependencies"][dependency_id]["path"])
        source.write_bytes(source.read_bytes() + b"tamper")
        with self.assertRaisesRegex(BuildToolError, "differs from its exact policy"):
            self._prepare("tampered-stage")

    def test_cargo_archive_cannot_diverge_from_embedded_cargo_lock(self) -> None:
        dependency_id = ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS[0]
        record = next(
            item
            for item in self.policy_data["archives"]
            if item["dependency"] == dependency_id
        )
        package = record["cargo_packages"][0]
        source = Path(self.state["dependencies"][dependency_id]["path"])
        source.write_bytes(
            _tar_gz_bytes(
                _expected_corresponding_source_root(record),
                files={
                    package["manifest_path"]: _cargo_manifest(
                        package["name"],
                        package["version"],
                        record["license"],
                    ),
                    "CHANGED.txt": b"resealed but absent from Cargo.lock\n",
                },
            )
        )
        self._seal_fixture_archive(record, source)
        _write_json(self.policy_path, self.policy_data)
        _write_json(self.lock_path, self.lock_data)
        self.lock = load_dependency_lock(self.lock_path)
        self.magisk_dependency = self.lock.dependencies["magisk_release_apk"]
        with self.assertRaisesRegex(BuildToolError, "Cargo.lock source closure differs"):
            self._prepare("cargo-lock-divergence")

    def test_staged_archive_is_reverified_after_copy(self) -> None:
        original_copy = artifact_lib.shutil.copy2
        corrupted = False

        def corrupt_first_archive(source: Path, destination: Path, *args, **kwargs):
            nonlocal corrupted
            result = original_copy(source, destination, *args, **kwargs)
            destination_path = Path(destination)
            if not corrupted and destination_path.suffix in {".gz", ".crate"}:
                destination_path.write_bytes(destination_path.read_bytes() + b"changed")
                corrupted = True
            return result

        with mock.patch.object(artifact_lib.shutil, "copy2", corrupt_first_archive):
            with self.assertRaisesRegex(BuildToolError, "staged corresponding-source"):
                self._prepare("copy-race")

    def test_policy_order_is_fail_closed(self) -> None:
        archives = self.policy_data["archives"]
        archives[0], archives[1] = archives[1], archives[0]
        _write_json(self.policy_path, self.policy_data)
        with self._fixture_contract(), self.assertRaisesRegex(
            BuildToolError,
            "canonical order",
        ):
            _load_anykernel_corresponding_source_policy(
                root=self.root,
                lock=self.lock,
                magisk_dependency=self.magisk_dependency,
            )

    def test_source_policy_rejects_duplicate_object_keys(self) -> None:
        text = self.policy_path.read_text(encoding="utf-8")
        self.policy_path.write_text(
            text.replace(
                '"schema_version": 1,',
                '"schema_version": 1,\n  "schema_version": 1,',
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(BuildToolError, "invalid JSON"):
            _load_anykernel_corresponding_source_policy(
                root=self.root,
                lock=self.lock,
                magisk_dependency=self.magisk_dependency,
            )

    def test_gitlink_identity_cannot_be_resealed_by_policy_and_lock(self) -> None:
        record = next(
            item
            for item in self.policy_data["archives"]
            if item["dependency"] == "magisk_submodule_crt0"
        )
        record["repository"] = "https://github.com/example/unrelated.git"
        record["url"] = (
            "https://github.com/example/unrelated/archive/"
            f"{record['commit']}.tar.gz"
        )
        dependency = self.lock_data["dependencies"][record["dependency"]]
        dependency["repository"] = record["repository"]
        dependency["url"] = record["url"]
        _write_json(self.policy_path, self.policy_data)
        _write_json(self.lock_path, self.lock_data)
        changed_lock = load_dependency_lock(self.lock_path)
        with self._fixture_contract(), self.assertRaisesRegex(
            BuildToolError,
            "Gitlink identity",
        ):
            _load_anykernel_corresponding_source_policy(
                root=self.root,
                lock=changed_lock,
                magisk_dependency=changed_lock.dependencies["magisk_release_apk"],
            )

    def _standalone_record(self, archive: Path) -> dict[str, object]:
        return {
            "dependency": "fixture",
            "repository": "https://github.com/example/source.git",
            "commit": "f" * 40,
            "relationship": "magisk-root",
            "cargo_packages": [],
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
        }

    def test_source_tarball_rejects_escaping_symlink(self) -> None:
        archive = self.root / "unsafe.tar.gz"
        source_root = "source-" + "f" * 40
        archive.write_bytes(_tar_gz_bytes(source_root, symlink_target="../../outside"))
        with self.assertRaisesRegex(BuildToolError, "link escapes its root"):
            _verify_corresponding_source_tarball(
                archive,
                self._standalone_record(archive),
            )

    def test_source_tarball_rejects_windows_drive_symlink(self) -> None:
        archive = self.root / "windows-drive.tar.gz"
        source_root = "source-" + "f" * 40
        archive.write_bytes(_tar_gz_bytes(source_root, symlink_target="C:/outside"))
        with self.assertRaisesRegex(BuildToolError, "unsafe link"):
            _verify_corresponding_source_tarball(
                archive,
                self._standalone_record(archive),
            )

    def test_source_tarball_allows_internal_symlink(self) -> None:
        archive = self.root / "safe.tar.gz"
        source_root = "source-" + "f" * 40
        archive.write_bytes(_tar_gz_bytes(source_root, symlink_target="README.md"))
        self.assertEqual(
            _verify_corresponding_source_tarball(
                archive,
                self._standalone_record(archive),
            ),
            source_root,
        )

    def test_git_archive_content_must_derive_the_pinned_tree(self) -> None:
        record = deepcopy(
            next(
                item
                for item in self.policy_data["archives"]
                if item["dependency"] == "magisk_submodule_crt0"
            )
        )
        source_root = _expected_corresponding_source_root(record)
        archive = self.root / "resealed-arbitrary-git-source.tar.gz"
        archive.write_bytes(
            _tar_gz_bytes(
                source_root,
                files={"arbitrary.c": b"not the pinned Git tree\n"},
            )
        )
        record["size"] = archive.stat().st_size
        record["sha256"] = sha256_file(archive)
        with self.assertRaisesRegex(BuildToolError, "archive Git tree differs"):
            _verify_corresponding_source_tarball(archive, record)

    def test_real_cached_pinned_source_archives_form_complete_companion(self) -> None:
        lock = load_dependency_lock(ROOT / "dependencies" / "lock.yml")
        cache = ROOT / ".cache" / "op13"
        try:
            state = _fetch_anykernel_corresponding_source_dependencies(
                lock,
                cache,
                offline=True,
            )
        except BuildToolError as exc:
            self.skipTest(f"real pinned corresponding sources are not cached: {exc}")
        result = _prepare_anykernel_corresponding_source_tree(
            root=ROOT,
            destination=self.root / "real-companion",
            state=state,
            lock=lock,
            magisk_dependency=lock.dependencies["magisk_release_apk"],
        )
        self.assertEqual(result["archive_count"], 150)
        self.assertEqual(len(result["output_records"]), 152)


if __name__ == "__main__":
    unittest.main()
