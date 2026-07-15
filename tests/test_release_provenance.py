from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.release_provenance import ReleaseProvenanceError, generate_release_provenance


REPOSITORY = "Hipuu/OnePlus13-KernelBuilder"
REVISION = "1" * 40
MANIFEST_REVISION = "2" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ReleaseProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.assets = Path(self.temporary.name) / "release-assets"
        self.assets.mkdir()
        self.repository_root = Path(self.temporary.name) / "repository"
        self.resolved_manifest = self.assets / "OnePlus13-oos16-manifest.xml"
        self.resolved_manifest.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<manifest>\n"
            "  <remote name=\"origin\" fetch=\"https://github.com/OnePlusOSS\"/>\n"
            "  <default remote=\"origin\"/>\n"
            f"  <project name=\"android_kernel_common_oneplus_sm8750\" path=\"kernel_platform/common\" revision=\"{'3' * 40}\"/>\n"
            f"  <project name=\"android_kernel_modules_and_devicetree_oneplus_sm8750\" path=\"./\" revision=\"{'4' * 40}\"/>\n"
            "</manifest>\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.assets / "kernel-Image").write_bytes(b"kernel\n")
        (self.assets / "SHA256SUMS").write_text("fixture\n", encoding="utf-8")
        locked_manifest = self.repository_root / "manifests" / "lockfiles" / "oos16.xml"
        locked_manifest.parent.mkdir(parents=True)
        locked_manifest.write_bytes(self.resolved_manifest.read_bytes())
        self.locked_manifest_sha256 = _sha256(locked_manifest)
        lock_document = {
            "schema_version": 1,
            "dependencies": {
                "anykernel3": {
                    "kind": "git",
                    "url": "https://github.com/osm0sis/AnyKernel3.git",
                    "commit": "5" * 40,
                    "required_for": ["package-anykernel3"],
                },
                "nethunter_wireless_firmware": {
                    "kind": "release_asset",
                    "url": "https://github.com/example/firmware/releases/download/v1/fw.zip",
                    "repository": "https://github.com/example/firmware.git",
                    "commit": "6" * 40,
                    "sha256": "c" * 64,
                    "required_for": ["nethunter", "release"],
                },
                "oneplus_manifest": {
                    "kind": "git",
                    "url": "https://github.com/OnePlusOSS/kernel_manifest.git",
                    "commit": MANIFEST_REVISION,
                    "required_for": ["source-sync"],
                },
                "repo_launcher": {
                    "kind": "file",
                    "url": "https://storage.googleapis.com/git-repo-downloads/repo-2.54",
                    "sha256": "d" * 64,
                    "repo_url": "https://gerrit.googlesource.com/git-repo",
                    "repo_commit": "7" * 40,
                    "required_for": ["source-sync"],
                },
            },
        }
        lock_path = self.repository_root / "dependencies" / "lock.yml"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(
            json.dumps(lock_document, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.lock_sha256 = _sha256(lock_path)
        self.lock_canonical_sha256 = _canonical_sha256(lock_document)
        self.build_manifest = {
            "schema_version": 2,
            "builder": {
                "repository": REPOSITORY,
                "revision": REVISION,
                "workflow_run_id": "1234",
            },
            "base": "oos16",
            "root_variant": "kernelsu-next",
            "feature_profile": "full",
            "build_target": "mixed",
            "debug": True,
            "pre_release": True,
            "smoke": False,
            "source": {
                "url": "https://github.com/OnePlusOSS/kernel_manifest.git",
                "branch": "oneplus/sm8750",
                "file": "oneplus_13_b.xml",
                "revision": MANIFEST_REVISION,
                "locked_path": "manifests/lockfiles/oos16.xml",
                "locked_sha256": self.locked_manifest_sha256,
                "resolved_path": self.resolved_manifest.name,
                "sha256": _sha256(self.resolved_manifest),
            },
            "dependency_lock": {
                "path": "dependencies/lock.yml",
                "sha256": self.lock_sha256,
                "canonical_sha256": self.lock_canonical_sha256,
            },
            "dependencies": [
                {
                    "id": "anykernel3",
                    "kind": "git",
                    "required_for": ["package-anykernel3"],
                    "source": {
                        "uri": "https://github.com/osm0sis/AnyKernel3.git",
                        "commit": "5" * 40,
                    },
                },
                {
                    "id": "nethunter_wireless_firmware",
                    "kind": "release_asset",
                    "required_for": ["nethunter", "release"],
                    "resource": {
                        "uri": "https://github.com/example/firmware/releases/download/v1/fw.zip",
                        "sha256": "c" * 64,
                    },
                    "source": {
                        "uri": "https://github.com/example/firmware.git",
                        "commit": "6" * 40,
                    },
                },
                {
                    "id": "oneplus_manifest",
                    "kind": "git",
                    "required_for": ["source-sync"],
                    "source": {
                        "uri": "https://github.com/OnePlusOSS/kernel_manifest.git",
                        "commit": MANIFEST_REVISION,
                    },
                },
                {
                    "id": "repo_launcher",
                    "kind": "file",
                    "required_for": ["source-sync"],
                    "resource": {
                        "uri": "https://storage.googleapis.com/git-repo-downloads/repo-2.54",
                        "sha256": "d" * 64,
                    },
                    "source": {
                        "uri": "https://gerrit.googlesource.com/git-repo",
                        "commit": "7" * 40,
                    },
                },
            ],
            "configuration": {"optimization": "O2", "lto": "thin"},
            "kernel": {
                # A modules-only release can package debug evidence while its
                # reused prerequisite kernel was built without debug mode.
                "debug": False,
                "branding": "OnePlus13-KernelBuilder",
                "source_date_epoch": 1783987200,
            },
        }
        self._write_build_manifest()
        self.parameters = {
            "tag": "v1.0.0",
            "base": "oos16",
            "root": "kernelsu-next",
            "profile": "full",
            "target": "mixed",
            "optimization": "O2",
            "lto": "thin",
            "clean": True,
            "debug": True,
            "preRelease": True,
            "branding": "OnePlus13-KernelBuilder",
            "buildTimestamp": "",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_build_manifest(self) -> None:
        (self.assets / "BUILD-MANIFEST.json").write_text(
            json.dumps(self.build_manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _generate(self) -> tuple[Path, Path]:
        return generate_release_provenance(
            assets_dir=self.assets,
            repository_root=self.repository_root,
            repository=REPOSITORY,
            revision=REVISION,
            run_id="1234",
            run_attempt="2",
            external_parameters=self.parameters,
        )

    def test_records_locked_manifests_projects_and_dependency_digests(self) -> None:
        provenance_path, checksum_path = self._generate()
        first_provenance = provenance_path.read_bytes()
        first_checksums = checksum_path.read_bytes()
        self._generate()
        self.assertEqual(provenance_path.read_bytes(), first_provenance)
        self.assertEqual(checksum_path.read_bytes(), first_checksums)

        statement = json.loads(first_provenance)
        definition = statement["predicate"]["buildDefinition"]
        dependencies = {record["name"]: record for record in definition["resolvedDependencies"]}
        self.assertEqual(dependencies["orchestrator"]["digest"], {"gitCommit": REVISION})
        self.assertEqual(
            dependencies["dependency-lock"]["digest"],
            {"sha256": self.lock_sha256},
        )
        self.assertEqual(
            dependencies["oneplus-manifest-lock:oos16"]["digest"],
            {"sha256": self.locked_manifest_sha256},
        )
        self.assertEqual(
            dependencies["oneplus-manifest-resolved:oos16"]["digest"],
            {"sha256": _sha256(self.resolved_manifest)},
        )
        self.assertEqual(
            dependencies["locked-dependency:anykernel3"]["digest"],
            {"gitCommit": "5" * 40},
        )
        self.assertEqual(
            dependencies["locked-dependency:repo_launcher"]["digest"],
            {"sha256": "d" * 64},
        )
        self.assertEqual(
            dependencies["locked-dependency-source:repo_launcher"]["digest"],
            {"gitCommit": "7" * 40},
        )
        self.assertEqual(
            dependencies["oneplus-project:kernel_platform/common"]["digest"],
            {"gitCommit": "3" * 40},
        )
        self.assertEqual(
            dependencies["oneplus-project:."]["digest"],
            {"gitCommit": "4" * 40},
        )
        self.assertEqual(definition["internalParameters"]["lockedDependencyCount"], 4)
        self.assertEqual(definition["internalParameters"]["onePlusProjectCount"], 2)
        self.assertEqual(
            [record["name"] for record in definition["resolvedDependencies"]],
            sorted(record["name"] for record in definition["resolvedDependencies"]),
        )
        checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(checksum_lines, sorted(checksum_lines, key=lambda line: line.split("  ", 1)[1]))
        self.assertTrue(any(line.endswith("  provenance.intoto.jsonl") for line in checksum_lines))

    def test_rejects_tampered_resolved_manifest(self) -> None:
        self.resolved_manifest.write_text("<manifest/>\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseProvenanceError, "digest differs"):
            self._generate()

    def test_rejects_release_parameter_mismatch(self) -> None:
        self.parameters = {**self.parameters, "root": "kernelsu"}
        with self.assertRaisesRegex(ReleaseProvenanceError, "parameter root differs"):
            self._generate()

    def test_rejects_dependency_without_immutable_commit(self) -> None:
        mutated = deepcopy(self.build_manifest)
        del mutated["dependencies"][0]["source"]["commit"]
        self.build_manifest = mutated
        self._write_build_manifest()
        with self.assertRaisesRegex(ReleaseProvenanceError, "inventory differs"):
            self._generate()

    def test_release_workflow_uses_pinned_tooling_without_checkout_credentials(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 scripts/generate-release-provenance.py", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("python3 - <<'PY'", workflow)


if __name__ == "__main__":
    unittest.main()
