from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.build_evidence import (
    EXPECTED_BAZEL_ROLES,
    EXPECTED_COMPILER_TOOLS,
    KMI_NAME,
    TOOLCHAIN_KIND,
    TOOLCHAIN_NAME,
    WIRELESS_LED_KMI_NAME,
    capture_source_kmi_evidence,
    copy_preserved_build_evidence,
    preserve_source_build_evidence,
    validate_packaged_build_evidence,
    validate_preserved_build_evidence,
)
from lib.errors import BuildToolError


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _binding(project_path: str, commit: str, path: str, *, mode: str = "100755") -> dict[str, object]:
    object_id = hashlib.sha1(f"{project_path}:{path}".encode("utf-8")).hexdigest()
    return {
        "project_path": project_path,
        "manifest_commit": commit,
        "checkout_head": commit,
        "path": path,
        "tree_mode": mode,
        "tree_type": "blob",
        "tree_object": object_id,
        "worktree_object": object_id,
        "status": "exact-manifest-tree",
    }


def _component(
    *,
    name_or_role: tuple[str, str],
    project_path: str,
    commit: str,
    path: str,
) -> dict[str, object]:
    key, value = name_or_role
    digest = _sha256(path.encode("utf-8"))
    return {
        key: value,
        "selected_path": (
            path if project_path == "." else f"{project_path}/{path}"
        ),
        "canonical_path": (
            path if project_path == "." else f"{project_path}/{path}"
        ),
        "selected_path_is_symlink": False,
        "manifest_project": {
            "name": f"fixture/{project_path}",
            "path": project_path,
            "commit": commit,
        },
        "selected_git_tree": _binding(project_path, commit, path),
        "canonical_git_tree": _binding(project_path, commit, path),
        "size": len(path) + 1,
        "sha256": digest,
        "kind": "elf",
    }


class BuildEvidenceFixture:
    def __init__(self, root: Path) -> None:
        self.source = root / "source"
        self.output = root / "output"
        self.source.mkdir()
        self.output.mkdir()
        self.manifest = self.source / ".op13" / "oos16-manifest-resolved.xml"
        self.manifest.parent.mkdir()
        self.manifest.write_text(
            f'<manifest><project name="fixture" path="./" revision="{"a" * 40}"/></manifest>\n',
            encoding="utf-8",
            newline="\n",
        )
        self.manifest_sha256 = _sha256(self.manifest.read_bytes())
        common_commit = "b" * 40
        clang_commit = "c" * 40
        root_commit = "d" * 40
        build_tools_commit = "e" * 40
        kernel_build_tools_commit = "f" * 40
        declaration_path = "build.config.constants"
        declaration = _component(
            name_or_role=("role", "clang-version-declaration"),
            project_path="kernel_platform/common",
            commit=common_commit,
            path=declaration_path,
        )
        declaration["path"] = declaration.pop("selected_path")
        toolchain = {
            "kind": TOOLCHAIN_KIND,
            "schema_version": 2,
            "path_scope": "synced-source-relative",
            "resolved_manifest": {
                "path": ".op13/oos16-manifest-resolved.xml",
                "size": self.manifest.stat().st_size,
                "sha256": self.manifest_sha256,
            },
            "selection": {
                "environment": {"LLVM": "1", "LLVM_IAS": "1"},
                "declaration": declaration,
                "clang_version": "rTEST",
                "toolchain_bin": "kernel_platform/prebuilts/clang/host/linux-x86/clang-rTEST/bin",
                "manifest_project": {
                    "name": "fixture/clang",
                    "path": "kernel_platform/prebuilts/clang/host/linux-x86",
                    "commit": clang_commit,
                },
            },
            "compiler_tools": [
                _component(
                    name_or_role=("name", name),
                    project_path="kernel_platform/prebuilts/clang/host/linux-x86",
                    commit=clang_commit,
                    path=f"clang-rTEST/bin/{name}",
                )
                for name in sorted(EXPECTED_COMPILER_TOOLS)
            ],
            "bazel_launcher": {
                "entrypoint": "kernel_platform/tools/bazel",
                "components": [
                    _component(
                        name_or_role=("role", role),
                        project_path=(
                            "kernel_platform/prebuilts/build-tools"
                            if role == "launcher-python-interpreter"
                            else (
                                "kernel_platform/prebuilts/kernel-build-tools"
                                if role == "bazel-binary"
                                else "."
                            )
                        ),
                        commit=(
                            build_tools_commit
                            if role == "launcher-python-interpreter"
                            else (
                                kernel_build_tools_commit
                                if role == "bazel-binary"
                                else root_commit
                            )
                        ),
                        path=f"fixture/{role}",
                    )
                    for role in EXPECTED_BAZEL_ROLES
                ],
            },
            "host_environment_tools": [
                {
                    "name": name,
                    "provenance": "environment-provided",
                    "immutable": False,
                    "status": "available",
                    "version_probe": ["--version"],
                    "version_output": f"{name} fixture",
                }
                for name in ("bash", "git", "make", "python3")
            ],
            "github_runner_image": {
                "provenance": "github-actions-environment",
                "immutable": False,
                "status": "recorded",
                "image_os": "ubuntu24",
                "image_version": "fixture",
            },
        }
        (self.source / ".op13" / TOOLCHAIN_NAME).write_text(
            json.dumps(toolchain, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        common = self.source / "kernel_platform" / "common"
        records = []
        for path, symbol, consumer in (
            (
                "android/abi_gki_aarch64_oplus",
                "from_kuid",
                "oplus_bsp_mm_osvelte.ko",
            ),
            (
                "android/abi_gki_aarch64_qcom",
                "from_kuid_munged",
                "msm_sysstats.ko",
            ),
        ):
            payload = f"header\n  {symbol}\n".encode("ascii")
            target = common.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            records.append(
                {
                    "path": path,
                    "symbol": symbol,
                    "consumer": consumer,
                    "status": "integrated",
                    "pre_size": len(payload) - 1,
                    "pre_sha256": "1" * 64,
                    "post_size": len(payload),
                    "post_sha256": _sha256(payload),
                }
            )
        (common / ".op13-kmi-symbol-exports.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "integration": "minimal-vendor-module-kmi-symbol-closure",
                    "base": "oos16",
                    "strict_mode": True,
                    "symbols": records,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def install_wireless_led_stamp(self) -> None:
        target = (
            self.source
            / "kernel_platform"
            / "common"
            / "android"
            / "abi_gki_aarch64_qcom"
        )
        preimage = target.read_bytes()
        additions = (
            b"  __ieee80211_get_radio_led_name\n"
            b"  __ieee80211_create_tpt_led_trigger\n"
        )
        postimage = preimage + additions
        target.write_bytes(postimage)
        stamp = {
            "schema_version": 1,
            "integration": "nethunter-mac80211-led-kmi-symbol-closure",
            "feature": "nethunter.wifi_ath",
            "base": "oos16",
            "strict_mode": True,
            "pre_size": len(preimage),
            "pre_sha256": _sha256(preimage),
            "post_size": len(postimage),
            "post_sha256": _sha256(postimage),
            "symbols": [
                {
                    "path": "android/abi_gki_aarch64_qcom",
                    "symbol": "__ieee80211_get_radio_led_name",
                    "consumers": ["ath9k.ko", "ath9k_htc.ko"],
                    "status": "integrated",
                },
                {
                    "path": "android/abi_gki_aarch64_qcom",
                    "symbol": "__ieee80211_create_tpt_led_trigger",
                    "consumers": ["ath9k.ko", "ath9k_htc.ko", "mt76.ko"],
                    "status": "integrated",
                },
            ],
        }
        (target.parents[1] / ".op13-kmi-wireless-led-exports.json").write_text(
            json.dumps(stamp, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def preserve(
        self,
        *,
        wireless_led_exports_required: bool = False,
    ) -> dict[str, object]:
        return preserve_source_build_evidence(
            source_dir=self.source,
            output_dir=self.output,
            base="oos16",
            resolved_manifest=self.manifest,
            kleaf_repo_manifest={
                "schema_version": 1,
                "environment_variable": "KLEAF_REPO_MANIFEST",
                "status": "applied",
                "path_scope": "synced-source-relative",
                "base": "oos16",
                "repository_root": ".",
                "resolved_manifest": ".op13/oos16-manifest-resolved.xml",
                "resolved_manifest_sha256": self.manifest_sha256,
                "manifest_url": "https://github.com/OnePlusOSS/kernel_manifest.git",
                "manifest_file": "oneplus_13_b.xml",
                "manifest_revision": "a" * 40,
            },
            wireless_led_exports_required=wireless_led_exports_required,
        )


class BuildEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = BuildEvidenceFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_evidence_survives_output_release_and_debug_copies(self) -> None:
        evidence = self.fixture.preserve()
        validate_preserved_build_evidence(
            output_dir=self.fixture.output,
            evidence_value=evidence,
            base="oos16",
            resolved_manifest_sha256=self.fixture.manifest_sha256,
            wireless_led_exports_required=False,
        )
        for destination_name in ("release", "debug"):
            destination = Path(self.temporary.name) / destination_name
            destination.mkdir()
            packaged = copy_preserved_build_evidence(
                input_dir=self.fixture.output,
                destination=destination,
                evidence_value=evidence,
                base="oos16",
                resolved_manifest_sha256=self.fixture.manifest_sha256,
                wireless_led_exports_required=False,
            )
            self.assertTrue((destination / TOOLCHAIN_NAME).is_file())
            self.assertTrue((destination / KMI_NAME).is_file())
            validate_packaged_build_evidence(
                assets_dir=destination,
                evidence_value=packaged,
                base="oos16",
                resolved_manifest_sha256=self.fixture.manifest_sha256,
                wireless_led_exports_required=False,
            )

    def test_changed_preserved_toolchain_file_is_rejected(self) -> None:
        evidence = self.fixture.preserve()
        (self.fixture.output / ".op13" / TOOLCHAIN_NAME).write_text(
            "{}\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(BuildToolError, "sealed record"):
            validate_preserved_build_evidence(
                output_dir=self.fixture.output,
                evidence_value=evidence,
                base="oos16",
                resolved_manifest_sha256=self.fixture.manifest_sha256,
                wireless_led_exports_required=False,
            )

    def test_kmi_stamp_must_match_postimage_bytes(self) -> None:
        target = (
            self.fixture.source
            / "kernel_platform"
            / "common"
            / "android"
            / "abi_gki_aarch64_oplus"
        )
        target.write_bytes(target.read_bytes() + b"changed\n")
        with self.assertRaisesRegex(BuildToolError, "differs from"):
            self.fixture.preserve()

    def test_feature_required_wireless_led_evidence_survives_all_copies(self) -> None:
        self.fixture.install_wireless_led_stamp()
        early_source = Path(self.temporary.name) / "early-source-metadata"
        early_debug = Path(self.temporary.name) / "early-debug"
        capture_source_kmi_evidence(
            source_dir=self.fixture.source,
            base="oos16",
            wireless_led_exports_required=True,
            destinations=(early_source, early_debug),
        )
        for destination in (early_source, early_debug):
            self.assertTrue((destination / KMI_NAME).is_file())
            self.assertTrue((destination / WIRELESS_LED_KMI_NAME).is_file())
        evidence = self.fixture.preserve(wireless_led_exports_required=True)
        self.assertTrue(
            (self.fixture.output / ".op13" / WIRELESS_LED_KMI_NAME).is_file()
        )
        validate_preserved_build_evidence(
            output_dir=self.fixture.output,
            evidence_value=evidence,
            base="oos16",
            resolved_manifest_sha256=self.fixture.manifest_sha256,
            wireless_led_exports_required=True,
        )
        destination = Path(self.temporary.name) / "wireless-release"
        destination.mkdir()
        packaged = copy_preserved_build_evidence(
            input_dir=self.fixture.output,
            destination=destination,
            evidence_value=evidence,
            base="oos16",
            resolved_manifest_sha256=self.fixture.manifest_sha256,
            wireless_led_exports_required=True,
        )
        self.assertTrue((destination / WIRELESS_LED_KMI_NAME).is_file())
        validate_packaged_build_evidence(
            assets_dir=destination,
            evidence_value=packaged,
            base="oos16",
            resolved_manifest_sha256=self.fixture.manifest_sha256,
            wireless_led_exports_required=True,
        )

    def test_feature_required_wireless_led_stamp_must_exist(self) -> None:
        with self.assertRaisesRegex(BuildToolError, "wireless LED KMI.*missing"):
            self.fixture.preserve(wireless_led_exports_required=True)

    def test_disabled_wireless_led_feature_rejects_a_stale_stamp(self) -> None:
        self.fixture.install_wireless_led_stamp()
        with self.assertRaisesRegex(BuildToolError, "disabled.*stamp is present"):
            self.fixture.preserve(wireless_led_exports_required=False)

    def test_wireless_led_stamp_must_match_final_qcom_postimage(self) -> None:
        self.fixture.install_wireless_led_stamp()
        target = (
            self.fixture.source
            / "kernel_platform"
            / "common"
            / "android"
            / "abi_gki_aarch64_qcom"
        )
        target.write_bytes(target.read_bytes() + b"changed\n")
        with self.assertRaisesRegex(BuildToolError, "wireless LED KMI evidence differs"):
            self.fixture.preserve(wireless_led_exports_required=True)

    def test_wireless_led_consumer_sets_are_exact(self) -> None:
        self.fixture.install_wireless_led_stamp()
        stamp = (
            self.fixture.source
            / "kernel_platform"
            / "common"
            / ".op13-kmi-wireless-led-exports.json"
        )
        document = json.loads(stamp.read_text(encoding="utf-8"))
        document["symbols"][0]["consumers"].append("unexpected.ko")
        stamp.write_text(json.dumps(document) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "exact consumer sets"):
            self.fixture.preserve(wireless_led_exports_required=True)

    def test_manifest_lineage_mismatch_is_rejected(self) -> None:
        different_manifest = Path(self.temporary.name) / "different-manifest.xml"
        different_manifest.write_text("<manifest/>\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "resolved manifest lineage"):
            preserve_source_build_evidence(
                source_dir=self.fixture.source,
                output_dir=self.fixture.output,
                base="oos16",
                resolved_manifest=different_manifest,
                kleaf_repo_manifest={
                    "schema_version": 1,
                    "environment_variable": "KLEAF_REPO_MANIFEST",
                    "status": "applied",
                    "path_scope": "synced-source-relative",
                    "base": "oos16",
                    "repository_root": ".",
                    "resolved_manifest": ".op13/oos16-manifest-resolved.xml",
                    "resolved_manifest_sha256": self.fixture.manifest_sha256,
                    "manifest_url": "https://github.com/OnePlusOSS/kernel_manifest.git",
                    "manifest_file": "oneplus_13_b.xml",
                    "manifest_revision": "a" * 40,
                },
                wireless_led_exports_required=False,
            )


if __name__ == "__main__":
    unittest.main()
