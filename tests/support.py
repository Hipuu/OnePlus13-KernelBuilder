from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lib.config import (
    ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS,
    ANYKERNEL_SOURCE_DEPENDENCY_IDS,
)


COMMIT = "1" * 40
PROJECT_COMMIT = "2" * 40
SHA256 = "a" * 64


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def make_repository(root: Path, *, external_modules: list[str] | None = None) -> Path:
    external_modules = ["rtw88"] if external_modules is None else external_modules
    source_dependencies: dict[str, dict[str, object]] = {}
    for dependency_id in ANYKERNEL_SOURCE_DEPENDENCY_IDS:
        is_registry_crate = dependency_id in ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS
        record: dict[str, object] = {
            "kind": "file",
            "url": f"https://example.com/{dependency_id}.tar.gz",
            "size": 1,
            "sha256": hashlib.sha256(dependency_id.encode("utf-8")).hexdigest(),
            "license": "SEE-UPSTREAM",
            "required_for": ["package-anykernel3-source"],
        }
        if not is_registry_crate:
            record["repository"] = f"https://example.com/{dependency_id}.git"
            record["commit"] = hashlib.sha1(
                dependency_id.encode("utf-8")
            ).hexdigest()
        source_dependencies[dependency_id] = record
    write_json(
        root / "dependencies" / "lock.yml",
        {
            "schema_version": 1,
            "generated_at": "2026-07-14T00:00:00Z",
            "platform": {
                "device": "OnePlus 13",
                "codename": "dodge",
                "soc": "SM8750",
                "target": "sun",
                "architecture": "arm64",
                "kmi": "android15-6.6",
            },
            "policy": {
                "allow_mutable_checkout": False,
                "require_full_git_commit": True,
                "require_archive_sha256": True,
                "allow_pipe_to_shell": False,
            },
            "dependencies": {
                "repo_launcher": {
                    "kind": "file",
                    "url": "https://example.com/repo-2.54",
                    "size": 1,
                    "sha256": SHA256,
                    "repo_url": "https://example.com/git-repo.git",
                    "repo_commit": "3" * 40,
                    "required_for": ["source-sync"],
                },
                "oneplus_manifest": {
                    "kind": "git",
                    "url": "https://github.com/OnePlusOSS/kernel_manifest.git",
                    "ref": COMMIT,
                    "commit": COMMIT,
                    "required_for": ["source-sync"],
                },
                "kernelsu": {
                    "kind": "git",
                    "url": "https://example.com/kernelsu.git",
                    "commit": "4" * 40,
                    "required_for": ["root-ksu"],
                },
                "kernelsu_next": {
                    "kind": "git",
                    "url": "https://example.com/kernelsu-next.git",
                    "commit": "5" * 40,
                    "required_for": ["root-ksun"],
                },
                "anykernel3": {
                    "kind": "git",
                    "url": "https://example.com/anykernel3.git",
                    "commit": "6" * 40,
                    "license": "SEE-UPSTREAM-MULTIPLE",
                    "required_for": ["package-anykernel3"],
                },
                "magisk_release_apk": {
                    "kind": "release_asset",
                    "url": "https://example.com/releases/download/v1/Magisk-v1.apk",
                    "repository": "https://example.com/Magisk.git",
                    "ref": "refs/tags/v1",
                    "commit": "9" * 40,
                    "version": "v1",
                    "size": 1,
                    "sha256": SHA256,
                    "license": "SEE-UPSTREAM-MULTIPLE",
                    "required_for": ["package-anykernel3"],
                },
                **source_dependencies,
                "rtw88": {
                    "kind": "git",
                    "url": "https://example.com/rtw88.git",
                    "commit": "7" * 40,
                    "required_for": ["modules"],
                },
                "nethunter_wireless_firmware": {
                    "kind": "release_asset",
                    "url": "https://example.com/nethunter-wireless-firmware.zip",
                    "ref": "refs/tags/v1.0.0",
                    "commit": "8" * 40,
                    "size": 1,
                    "sha256": SHA256,
                    "repository": "https://example.com/nethunter-wireless-firmware.git",
                    "version": "v1.0.0",
                    "license": "SEE-CURATION-MANIFEST",
                    "required_for": ["nethunter", "release"],
                },
            },
        },
    )
    write_json(
        root / "configs" / "devices" / "oneplus13.yml",
        {
            "schema_version": 1,
            "device": "oneplus13",
            "name": "OnePlus 13",
            "vendor": "OnePlus",
            "codename": "dodge",
            "soc": "SM8750",
            "soc_name": "Snapdragon 8 Elite",
            "target": "sun",
            "arch": "arm64",
            "kmi": "android15-6.6",
            "official_build": {
                "script": "kernel_platform/oplus/build/oplus_build_kernel.sh",
                "args": ["sun", "perf"],
                "variant": "perf",
                "cache_dir": "bazel-cache",
            },
            "source_layout": {
                "common_kernel": "kernel_platform/common",
                "vendor_kernel": "kernel_platform/msm-kernel",
                "modules_and_devicetree": ".",
                "defconfig": "kernel_platform/msm-kernel/arch/arm64/configs/vendor/sun_perf.config",
            },
        },
    )
    profile_files = {
        "oos15-cn": "oneplus_13.xml",
        "oos15-global": "oneplus_13_global.xml",
        "oos16": "oneplus_13_b.xml",
    }
    for profile_id, manifest_file in profile_files.items():
        lock_path = root / "manifests" / "lockfiles" / f"{profile_id}.xml"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!-- upstream branch oneplus/test. -->\n"
            "<manifest>\n"
            "  <remote name=\"origin\" fetch=\"https://github.com/OnePlusOSS\" />\n"
            f"  <project remote=\"origin\" name=\"android_kernel_{profile_id}\" path=\"kernel_platform/common\" revision=\"{PROJECT_COMMIT}\" />\n"
            "</manifest>\n",
            encoding="utf-8",
            newline="\n",
        )
        write_json(
            root / "configs" / "profiles" / f"{profile_id}.yml",
            {
                "schema_version": 1,
                "id": profile_id,
                "label": profile_id,
                "device": "oneplus13",
                "target": "sun",
                "arch": "arm64",
                "kmi": "android15-6.6",
                "os": {"name": "OxygenOS", "major": 16, "region": "test"},
                "manifest": {
                    "url": "https://github.com/OnePlusOSS/kernel_manifest.git",
                    "branch": "oneplus/sm8750",
                    "file": manifest_file,
                    "revision": COMMIT,
                },
                "locked_manifest": f"manifests/lockfiles/{profile_id}.xml",
                "build": {"variant": "perf"},
                "compatibility": {"susfs": "supported", "raw_partition_images": False},
            },
        )
    fragment = root / "patches" / "common" / "test.config"
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text(
        "CONFIG_KSU=y\nCONFIG_MT76x0U=m\nCONFIG_TEST_FEATURE=y\n",
        encoding="utf-8",
        newline="\n",
    )
    write_json(
        root / "configs" / "features" / "test.yml",
        {
            "schema_version": 1,
            "id": "test",
            "label": "test",
            "description": "test",
            "root": {
                "supported_variants": ["kernelsu", "kernelsu-next"],
                "default_variant": "kernelsu-next",
            },
            "feature_flags": {
                "root.kernelsu": True,
                "test.feature": True,
                "artifact.wireless_firmware": True,
            },
            "patch_series": ["patches/series/test.yml"],
            "kconfig_fragments": [{"path": "patches/common/test.config", "scope": "common", "required": True}],
            "required_symbols": {"CONFIG_KSU": "y", "CONFIG_MT76x0U": "m", "CONFIG_TEST_FEATURE": "y"},
            "external_modules": external_modules,
            "defaults": {"optimization": "O2", "lto": "thin"},
        },
    )
    write_json(root / "patches" / "series" / "root.yml", {"schema_version": 1, "id": "root", "operations": []})
    write_json(
        root / "patches" / "series" / "test.yml",
        {
            "schema_version": 1,
            "id": "test",
            "operations": [
                {
                    "id": "replace-token",
                    "type": "replace",
                    "bases": ["oos16"],
                    "cwd": ".",
                    "target": "fixture.txt",
                    "find": "before",
                    "replace": "after",
                    "count": 1,
                },
                {
                    "id": "append-token",
                    "type": "append",
                    "bases": ["oos16"],
                    "cwd": ".",
                    "target": "fixture.txt",
                    "lines": ["tail"],
                },
            ],
        },
    )
    (root / "packaging" / "anykernel3").mkdir(parents=True, exist_ok=True)
    return root
