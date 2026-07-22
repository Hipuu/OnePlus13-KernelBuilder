"""Safe artifact verification, deterministic packaging, and provenance."""

from __future__ import annotations

import base64
import binascii
from collections import Counter
import concurrent.futures
import configparser
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from .build import (
    KERNEL_RESOURCE_POLICY_SCHEMA_VERSION,
    OOS16_HOSTED_BAZEL_JOBS,
    OOS16_HOSTED_EXTRA_KBUILD_ARGS,
    OOS16_HOSTED_LOCAL_CPU_RESOURCES,
    OOS16_HOSTED_LOCAL_RAM_RESOURCES_MIB,
    OOS16_HOSTED_SWAP_MIB,
    ROOT_VARIANTS,
    _validate_official_module_payload_records,
    assert_build_target_contract,
    assert_symbols,
    expected_symbols_for_target,
    parse_dotconfig,
)
from .build_evidence import (
    KMI_NAME,
    TOOLCHAIN_NAME,
    WIRELESS_LED_KMI_NAME,
    copy_preserved_build_evidence,
    validate_preserved_build_evidence,
    wireless_led_exports_required,
)
from .config import (
    ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS,
    ANYKERNEL_CARGO_CRATE_IDENTITIES,
    ANYKERNEL_CARGO_GIT_DEPENDENCY_ID,
    ANYKERNEL_PROGRAM_SOURCE_DEPENDENCY_IDS,
    ANYKERNEL_SOURCE_DEPENDENCY_IDS,
    DependencyLock,
    FeatureProfile,
    Profile,
    sha256_bytes,
    sha256_file,
    strict_json_loads,
)
from .context import (
    advance_context,
    atomic_write_json,
    load_context,
    record_for_file,
    validate_lineage,
    write_context,
)
from .errors import BuildToolError
from .module_outputs import mapped_module_output_paths, verify_produced_module_outputs
from .runtime import fetch_dependencies


FORBIDDEN_PARTITION_NAMES = {
    "boot.img",
    "init_boot.img",
    "vendor_boot.img",
    "vendor_kernel_boot.img",
    "dtbo.img",
    "system_dlkm.img",
    "vendor_dlkm.img",
    "vbmeta.img",
}
MAX_ZIP_EPOCH = int(datetime(2107, 12, 31, 23, 59, 58, tzinfo=timezone.utc).timestamp())
ANYKERNEL_UPSTREAM_MEMBERS = (
    "LICENSE",
    "META-INF/com/google/android/update-binary",
    "META-INF/com/google/android/updater-script",
    "tools/ak3-core.sh",
)
ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS: tuple[Mapping[str, Any], ...] = (
    {
        "path": "LICENSE",
        "git_mode": "100644",
        "git_blob": "20a447ebe28baf309eeed88eac8cd86a4c3eeeec",
        "size": 11630,
        "sha256": "df6f65da47d459fdf2a2ef0eadb34f37d838befc91352b64a84f35507bea95cd",
    },
    {
        "path": "META-INF/com/google/android/update-binary",
        "git_mode": "100755",
        "git_blob": "8c7006e7e3f6ef10f8f4117b291d6df204ef285e",
        "size": 19724,
        "sha256": "cf3ebe7183d5e3f25e9215cde7ef9dd956e836232a70f2a671891ab4889c7e7d",
    },
    {
        "path": "META-INF/com/google/android/updater-script",
        "git_mode": "100644",
        "git_blob": "8f5b52376c03dfa0b3f61446a830ecca9e8a03cc",
        "size": 115,
        "sha256": "ec154e2509d82dd1e409355faa600ce3f146b372047f449322a7b40d108f5deb",
    },
    {
        "path": "tools/ak3-core.sh",
        "git_mode": "100755",
        "git_blob": "43baccb2b6b1febf4815bc9f74f81da7d72db61d",
        "size": 34976,
        "sha256": "5c3f2ec85f18311552da79dfc2631509c59c763b099682c30ea538f2ee7a9aab",
    },
)
ANYKERNEL_EXECUTABLE_PROVENANCE = "EXECUTABLE-PROVENANCE.json"
ANYKERNEL_SOURCE_CONVEYANCE = "SOURCE-CONVEYANCE.md"
ANYKERNEL_CORRESPONDING_SOURCE_POLICY = (
    "packaging/anykernel3/CORRESPONDING-SOURCE.json"
)
ANYKERNEL_CORRESPONDING_SOURCE_MANIFEST = "SOURCE-MANIFEST.json"
ANYKERNEL_CORRESPONDING_SOURCE_POLICY_MEMBER = "SOURCE-POLICY.json"
ANYKERNEL_CORRESPONDING_SOURCE_FORMAT = (
    "oneplus13-anykernel-corresponding-source"
)
ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES = ANYKERNEL_SOURCE_DEPENDENCY_IDS
ANYKERNEL_CARGO_REGISTRY_SOURCE = (
    "registry+https://github.com/rust-lang/crates.io-index"
)
ANYKERNEL_CARGO_GIT_SOURCE = (
    "git+https://github.com/topjohnwu/quick-protobuf.git"
    "#980b0fb0ff81f59c0faa6e6db490fb8ecf59c633"
)
ANYKERNEL_CARGO_GIT_ARCHIVE: Mapping[str, Any] = {
    "dependency": ANYKERNEL_CARGO_GIT_DEPENDENCY_ID,
    "repository": "https://github.com/topjohnwu/quick-protobuf.git",
    "commit": "980b0fb0ff81f59c0faa6e6db490fb8ecf59c633",
    "size": 139451,
    "sha256": "fac4bd9a16005996d3d1d142da96cee0ad40ffa5e9d4b0f075d3f3974f6c673e",
}
# GitHub's archive bytes are a cache-level pin; these Git tree IDs are the
# durable content identities that remain meaningful if GitHub regenerates a
# tarball for the same commit.
ANYKERNEL_GIT_SOURCE_TREE_IDS: Mapping[str, str] = {
    "magisk_source": "c16108b21893da4e2b74dcc061f4733c5796fec8",
    "magisk_busybox_source": "ec9080534981f111e499014726020804019e3257",
    "magisk_submodule_crt0": "d08c655b733e3dd347f20bd962073281f5479d56",
    "magisk_submodule_cxx": "7761fae6787803ead0b2ee390e2b42bc702e4d76",
    "magisk_submodule_libcxx": "02652cca9404b3cff2a4258a35c0b7c2d60f308b",
    "magisk_submodule_lsplt": "b676e2e07404f5cd7aad5d3264de100fb855003f",
    "magisk_submodule_lz4": "a8c019ae0cd4aa3195e28dd63e7859a6e977a968",
    "magisk_submodule_selinux": "7a980986097c38089c2b19a622e764e62c6c75e7",
    "magisk_submodule_system_properties": "7cc3a64dcd8fb5ca08ed561fa43aeabb1b68b863",
    "magisk_cargo_git_quick_protobuf": "efeb42686ca16726392f4c41ed3790499b91585b",
}
ANYKERNEL_MAGISK_GITMODULES_IDENTITY: Mapping[str, Any] = {
    "dependency": "magisk_source",
    "archive_member": (
        "Magisk-e8a58776f1d7bdf852072ad0baa6eceb9a1e4aac/.gitmodules"
    ),
    "size": 728,
    "sha256": "ec1f2be281362e71d1c5169b8e1e488f59764d13cec2c477f34ad73a6e1bb0c4",
}
ANYKERNEL_MAGISK_CARGO_LOCK_IDENTITY: Mapping[str, Any] = {
    "dependency": "magisk_source",
    "archive_member": (
        "Magisk-e8a58776f1d7bdf852072ad0baa6eceb9a1e4aac/"
        "native/src/Cargo.lock"
    ),
    "format_version": 4,
    "size": 35178,
    "sha256": "a04ff0b1edfb97123446dc8c04e44603f772e89ece87967d2b8b291a1bb6d659",
    "package_count": 155,
    "local_package_count": 13,
    "registry_package_count": 140,
    "git_package_count": 2,
    "registry_archive_count": 140,
    "git_archive_count": 1,
    "registry_source": ANYKERNEL_CARGO_REGISTRY_SOURCE,
    "git_source": ANYKERNEL_CARGO_GIT_SOURCE,
}
ANYKERNEL_SOURCE_FETCH_WORKERS = 8
ANYKERNEL_SOURCE_ARCHIVE_MAX_BYTES = 512 * 1024 * 1024
ANYKERNEL_MAGISK_GITLINKS: Mapping[str, Mapping[str, str]] = {
    "magisk_submodule_crt0": {
        "path": "native/src/external/crt0",
        "repository": "https://github.com/topjohnwu/crt0.git",
        "commit": "9dfa67b4d543f1b6bf2e936f560fbe77ca2a226a",
    },
    "magisk_submodule_cxx": {
        "path": "native/src/external/cxx-rs",
        "repository": "https://github.com/topjohnwu/cxx.git",
        "commit": "b09b91554b392523f633b9e3cbe0b43273528c71",
    },
    "magisk_submodule_libcxx": {
        "path": "native/src/external/libcxx",
        "repository": "https://github.com/topjohnwu/libcxx.git",
        "commit": "d5117df3ba7704aab06c3a30b97c7529c931662b",
    },
    "magisk_submodule_lsplt": {
        "path": "native/src/external/lsplt",
        "repository": "https://github.com/LSPosed/LSPlt.git",
        "commit": "cef80a97a73184b4def9b3e1148884365fc173fd",
    },
    "magisk_submodule_lz4": {
        "path": "native/src/external/lz4",
        "repository": "https://github.com/lz4/lz4.git",
        "commit": "d44371841a2f1728a3f36839fd4b7e872d0927d3",
    },
    "magisk_submodule_selinux": {
        "path": "native/src/external/selinux",
        "repository": "https://github.com/topjohnwu/selinux.git",
        "commit": "be1b39a657fee7faacfae548b75cb53302043a01",
    },
    "magisk_submodule_system_properties": {
        "path": "native/src/external/system_properties",
        "repository": "https://github.com/topjohnwu/system_properties.git",
        "commit": "b7c2088565fbe13d22fe074960332e89615bb4aa",
    },
}
ANYKERNEL_MAGISK_SUBMODULE_PATHS = {
    dependency_id: record["path"]
    for dependency_id, record in ANYKERNEL_MAGISK_GITLINKS.items()
}
ANYKERNEL_LICENSE_MEMBERS = {
    "LICENSES/GPL-2.0-only": "licenses/GPL-2.0-only",
    "LICENSES/GPL-3.0-or-later": "licenses/GPL-3.0-or-later",
}
ANYKERNEL_TOOL_CONTRACTS: Mapping[str, Mapping[str, Any]] = {
    "tools/busybox": {
        "archive_member": "lib/arm64-v8a/libbusybox.so",
        "size": 1710600,
        "sha256": "4d60ab3f5a59ebb2ca863f2f514e6924401b581e9b64f602665c008177626651",
        "version": "1.36.1.1",
        "license": "GPL-2.0-only",
        "license_path": "LICENSES/GPL-2.0-only",
        "source": {
            "repository": "https://github.com/topjohnwu/ndk-busybox.git",
            "commit": "1c0ca97aafb9698ab7770ce1f67af1a84b469cdb",
            "relationship": "official-version-source-exact-byte-rebuild-not-verified",
        },
        "upstream_build_input": {
            "uri": "https://github.com/topjohnwu/magisk-files/releases/download/files/busybox-1.36.1.1.zip",
            "sha256": "b4d0551feabaf314e53c79316c980e8f66432e9fb91a69dbbf10a93564b40951",
            "archive_member": "arm64-v8a/libbusybox.so",
        },
    },
    "tools/magiskboot": {
        "archive_member": "lib/arm64-v8a/libmagiskboot.so",
        "size": 788840,
        "sha256": "d7440e2cd89899426e809554bf793baef9804ccbe5a52ce34a8b6242725d3c77",
        "version": "30.7",
        "license": "GPL-3.0-or-later",
        "license_path": "LICENSES/GPL-3.0-or-later",
        "source": {
            "repository": "https://github.com/topjohnwu/Magisk.git",
            "commit": "e8a58776f1d7bdf852072ad0baa6eceb9a1e4aac",
            "relationship": "official-release-source-exact-byte-rebuild-not-verified",
        },
    },
}
ANYKERNEL_ZIP_MODES: Mapping[str, int] = {
    "LICENSE": 0o644,
    "META-INF/com/google/android/update-binary": 0o755,
    "META-INF/com/google/android/updater-script": 0o644,
    "tools/ak3-core.sh": 0o755,
    "tools/busybox": 0o755,
    "tools/magiskboot": 0o755,
    "anykernel.sh": 0o755,
    ANYKERNEL_EXECUTABLE_PROVENANCE: 0o644,
    ANYKERNEL_SOURCE_CONVEYANCE: 0o644,
    "LICENSES/GPL-2.0-only": 0o644,
    "LICENSES/GPL-3.0-or-later": 0o644,
    "Image": 0o644,
}
ANYKERNEL_EXECUTABLE_MEMBERS = frozenset(
    member for member, mode in ANYKERNEL_ZIP_MODES.items() if mode == 0o755
)
WIRELESS_FIRMWARE_POLICY = "packaging/wireless-firmware/SOURCE-MEMBER-POLICY.json"
WIRELESS_FIRMWARE_CURATION_README = "packaging/wireless-firmware/README.md"
WIRELESS_FIRMWARE_WHENCE_SOURCE = "packaging/wireless-firmware/WHENCE"
WIRELESS_FIRMWARE_PROVENANCE = "WIRELESS-FIRMWARE-PROVENANCE.json"
WIRELESS_FIRMWARE_README = "CURATION-README.md"
WIRELESS_FIRMWARE_WHENCE = "WHENCE"
WIRELESS_FIRMWARE_LICENSE_DIRECTORY = "LICENSES"
WIRELESS_FIRMWARE_LICENSE = "PROPRIETARY-REDISTRIBUTABLE-FIRMWARE"
WIRELESS_FIRMWARE_LOCK_LICENSE = "SEE-CURATION-MANIFEST"
WIRELESS_FIRMWARE_PROVENANCE_STATUS = (
    "opaque-runtime-firmware-exact-upstream-release-bytes-no-reproducible-source-claimed"
)
WIRELESS_FIRMWARE_FAMILIES = frozenset(
    {
        "ath9k-htc",
        "ath10k",
        "mt76",
        "rtw88",
        "usb-bluetooth-mediatek",
        "usb-bluetooth-realtek",
    }
)
WIRELESS_FIRMWARE_UPSTREAM_ATTRIBUTION_OUTPUTS = {
    "LICENSE.md": "UPSTREAM-PACKAGE-LICENSE.md",
    "README.md": "UPSTREAM-README.md",
}
WIRELESS_FIRMWARE_REQUIRED_GAP_IDS = frozenset(
    {
        "ath11k",
        "mt7603-mt7628",
        "bcm203x-bfusb",
        "broadcom-hcd",
        "intel-bluetooth",
        "upstream-license-map",
        "firmware-reproducibility",
    }
)


def _record_matches(path: Path, record: Mapping[str, Any], role: str) -> None:
    if not path.is_file():
        raise BuildToolError(f"missing {role}: {path}")
    if path.stat().st_size != record.get("size"):
        raise BuildToolError(f"{role} size differs from build lineage")
    if sha256_file(path) != record.get("sha256"):
        raise BuildToolError(f"{role} digest differs from build lineage")


def _recorded_vermagic(value: object, *, where: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value or any(
        character in value for character in "\x00\r\n"
    ):
        raise BuildToolError(f"{where} full vermagic is invalid")
    release = value.split()[0]
    if not release or any(character.isspace() for character in release):
        raise BuildToolError(f"{where} kernel release is invalid")
    return value, release


def _verify_depmod_proof(
    proof: object,
    *,
    output_dir: Path,
    kernel_release: str,
    system_map_sha256: object,
    smoke: bool,
) -> None:
    if not isinstance(proof, dict):
        raise BuildToolError("depmod verification proof is absent")
    if proof.get("kernel_release") != kernel_release:
        raise BuildToolError("depmod verification kernel release differs from module lineage")
    if smoke:
        if proof.get("status") != "not-run-smoke":
            raise BuildToolError("smoke depmod verification status is invalid")
        return
    returncode = proof.get("returncode")
    if (
        proof.get("status") != "passed"
        or not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or returncode != 0
    ):
        raise BuildToolError("depmod verification did not record a successful result")
    if proof.get("system_map_sha256") != system_map_sha256:
        raise BuildToolError("depmod verification System.map differs from kernel lineage")
    argv = proof.get("argv")
    if (
        not isinstance(argv, list)
        or len(argv) != 7
        or not all(isinstance(argument, str) for argument in argv)
    ):
        raise BuildToolError("depmod verification command is invalid")
    if argv[:3] != ["depmod", "-e", "-F"] or argv[4] != "-b" or argv[6:] != [
        kernel_release
    ]:
        raise BuildToolError("depmod verification command contract differs")
    try:
        system_map_argument = Path(str(argv[3])).resolve()
        staging_argument = Path(str(argv[5])).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise BuildToolError("depmod verification paths are invalid") from exc
    if system_map_argument != (output_dir / "System.map").resolve():
        raise BuildToolError("depmod verification used a different System.map")
    if staging_argument != (output_dir / "modules" / "staging").resolve():
        raise BuildToolError("depmod verification used a different staging tree")
    output_sha256 = proof.get("output_sha256")
    output_size = proof.get("output_size")
    if (
        not isinstance(output_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", output_sha256) is None
        or not isinstance(output_size, int)
        or isinstance(output_size, bool)
        or output_size < 0
    ):
        raise BuildToolError("depmod verification output evidence is invalid")


def _resolve_staged_module(output_dir: Path, record: Mapping[str, Any], *, role: str) -> Path:
    staging = output_dir / "modules" / "staging"
    value = record.get("path")
    if not isinstance(value, str) or not value or "\\" in value:
        raise BuildToolError(f"recorded {role} has no portable staging-relative path")
    recorded_path = PurePosixPath(value)
    if recorded_path.is_absolute() or ".." in recorded_path.parts:
        raise BuildToolError(f"recorded {role} path escapes module staging")
    candidate = staging.joinpath(*recorded_path.parts)
    try:
        candidate.resolve().relative_to(staging.resolve())
    except ValueError as exc:
        raise BuildToolError(f"recorded {role} path escapes module staging") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise BuildToolError(f"recorded {role} is missing: {recorded_path.as_posix()}")
    return candidate


def _assert_no_partition_images(root: Path) -> None:
    offenders = sorted(
        str(path) for path in root.rglob("*") if path.is_file() and path.name.lower() in FORBIDDEN_PARTITION_NAMES
    )
    if offenders:
        raise BuildToolError(
            "raw partition images are outside the initial release scope: " + ", ".join(offenders)
        )


def verify_build_output(
    *,
    output_dir: Path,
    profile: Profile,
    feature: FeatureProfile,
    lock: DependencyLock,
    root_variant: str,
    build_target: str,
    smoke: bool,
) -> dict[str, Any]:
    if root_variant not in ROOT_VARIANTS:
        raise BuildToolError(f"unsupported root variant {root_variant!r}")
    assert_build_target_contract(build_target)
    context_path = output_dir / ".op13" / "build-context.json"
    context = load_context(context_path)
    required_stage = "modules-built" if build_target in {"modules", "mixed"} else "kernel-built"
    validate_lineage(context, profile, lock, minimum_stage=required_stage)
    if bool(context.get("smoke")) != bool(smoke):
        raise BuildToolError("smoke output must be verified explicitly and cannot be released as real")
    if smoke:
        if context.get("build_evidence") is not None:
            raise BuildToolError("smoke output must not claim real build evidence")
        for evidence_name in (TOOLCHAIN_NAME, KMI_NAME):
            if (output_dir / ".op13" / evidence_name).exists():
                raise BuildToolError("smoke output contains unexpected real build evidence")
    else:
        wireless_led_kmi_required = wireless_led_exports_required(
            context.get("features"),
            feature_profile=feature.id,
        )
        validate_preserved_build_evidence(
            output_dir=output_dir,
            evidence_value=context.get("build_evidence"),
            base=profile.id,
            resolved_manifest_sha256=str(context["manifest"]["sha256"]),
            wireless_led_exports_required=wireless_led_kmi_required,
        )
    configuration = context.get("configuration")
    if not isinstance(configuration, dict):
        raise BuildToolError("configuration record is absent")
    if (
        configuration.get("profile") != profile.id
        or configuration.get("feature_profile") != feature.id
        or configuration.get("root_variant") != root_variant
        or configuration.get("build_target") != build_target
    ):
        raise BuildToolError("requested verification tuple differs from the build context")
    config_path = output_dir / ".config"
    if not config_path.is_file() or sha256_file(config_path) != configuration.get("config_sha256"):
        raise BuildToolError("final .config digest differs from the configured build")
    required = expected_symbols_for_target(
        profile.source_path.parents[2],
        feature,
        root_variant=root_variant,
        optimization=str(configuration.get("optimization")),
        lto=str(configuration.get("lto")),
        build_target=build_target,
    )
    assert_symbols(config_path, required)
    sealed_required = configuration.get("required_symbols")
    if not isinstance(sealed_required, dict):
        raise BuildToolError("sealed common-tree Kconfig request is absent")
    assert_symbols(config_path, sealed_required)
    kernel = context.get("kernel")
    if not isinstance(kernel, dict):
        raise BuildToolError("kernel record is absent")
    resource_policy = kernel.get("resource_policy")
    if (
        not isinstance(resource_policy, dict)
        or resource_policy.get("schema_version")
        != KERNEL_RESOURCE_POLICY_SCHEMA_VERSION
    ):
        raise BuildToolError("kernel resource-policy evidence is absent")
    if (
        resource_policy.get("profile") != profile.id
        or resource_policy.get("workflow_input") is not False
    ):
        raise BuildToolError("kernel resource-policy lineage is invalid")
    if resource_policy.get("policy") == "oos16-hosted-bounded":
        if (
            profile.id != "oos16"
            or resource_policy.get("extra_kbuild_args")
            != OOS16_HOSTED_EXTRA_KBUILD_ARGS
            or resource_policy.get("bazel_jobs") != OOS16_HOSTED_BAZEL_JOBS
            or resource_policy.get("local_cpu_resources")
            != OOS16_HOSTED_LOCAL_CPU_RESOURCES
            or resource_policy.get("local_ram_resources_mib")
            != OOS16_HOSTED_LOCAL_RAM_RESOURCES_MIB
            or resource_policy.get("hosted_swap_mib") != OOS16_HOSTED_SWAP_MIB
        ):
            raise BuildToolError("bounded OOS16 resource-policy evidence is invalid")
    elif resource_policy.get("policy") == "tool-default":
        if any(
            resource_policy.get(field) is not None
            for field in (
                "extra_kbuild_args",
                "bazel_jobs",
                "local_cpu_resources",
                "local_ram_resources_mib",
                "hosted_swap_mib",
            )
        ):
            raise BuildToolError("default kernel resource-policy evidence is invalid")
    else:
        raise BuildToolError("kernel resource-policy evidence has an unknown policy")
    image_record = kernel.get("image")
    symvers_record = kernel.get("module_symvers")
    system_map_record = kernel.get("system_map")
    if not isinstance(image_record, dict) or not isinstance(symvers_record, dict) or not isinstance(system_map_record, dict):
        raise BuildToolError("kernel record lacks Image, Module.symvers, or System.map")
    _record_matches(output_dir / "Image", image_record, "Image")
    _record_matches(output_dir / "Module.symvers", symvers_record, "Module.symvers")
    _record_matches(output_dir / "System.map", system_map_record, "System.map")
    if not smoke and (output_dir / "Image").stat().st_size < 1024 * 1024:
        raise BuildToolError("Image is implausibly small")
    if not smoke:
        image_config_record = kernel.get("image_config")
        module_config_record = kernel.get("module_config")
        if not isinstance(image_config_record, dict) or not isinstance(module_config_record, dict):
            raise BuildToolError("kernel record lacks its built common/MSM configuration lineage")
        _record_matches(config_path, image_config_record, "Image .config")
        module_config_path = output_dir / "kernel-kit" / ".config"
        _record_matches(module_config_path, module_config_record, "kernel-kit .config")
        tree_configs = configuration.get("kernel_tree_configs")
        if not isinstance(tree_configs, dict):
            raise BuildToolError("sealed per-tree Kconfig requests are absent")
        common_tree = tree_configs.get("common")
        msm_tree = tree_configs.get("msm-kernel")
        if not isinstance(common_tree, dict) or not isinstance(msm_tree, dict):
            raise BuildToolError("sealed common/MSM Kconfig requests are absent")
        common_required = common_tree.get("required_symbols")
        msm_required = msm_tree.get("required_symbols")
        if not isinstance(common_required, dict) or not isinstance(msm_required, dict):
            raise BuildToolError("sealed common/MSM Kconfig symbol requests are absent")
        assert_symbols(config_path, common_required)
        assert_symbols(module_config_path, msm_required)
        order_record = kernel.get("official_modules_order")
        module_records = kernel.get("official_modules")
        if not isinstance(order_record, dict) or not isinstance(module_records, list):
            raise BuildToolError("kernel record lacks its official module payload lineage")
        _validate_official_module_payload_records(
            output_dir / "kernel-dist-modules",
            order_record,
            module_records,
        )
    module_count = 0
    in_tree_module_count = 0
    if required_stage == "modules-built":
        modules = context.get("modules")
        if not isinstance(modules, dict):
            raise BuildToolError("external modules record is absent")
        if modules.get("module_symvers_sha256") != symvers_record.get("sha256"):
            raise BuildToolError("modules were not built with this Module.symvers")
        kernel_vermagic, kernel_release = _recorded_vermagic(
            modules.get("kernel_vermagic"),
            where="module build",
        )
        if modules.get("kernel_release") != kernel_release:
            raise BuildToolError("module build kernel release differs from full vermagic")
        _verify_depmod_proof(
            modules.get("depmod_verification"),
            output_dir=output_dir,
            kernel_release=kernel_release,
            system_map_sha256=system_map_record.get("sha256"),
            smoke=smoke,
        )
        modules_log = output_dir / "modules" / ".op13" / "modules-build.log"
        modules_log_record = modules.get("build_log")
        if not isinstance(modules_log_record, dict):
            raise BuildToolError("module build log record is absent")
        _record_matches(modules_log, modules_log_record, "module build log")
        in_tree = modules.get("in_tree_modules")
        if not isinstance(in_tree, dict):
            raise BuildToolError("in-tree module record is absent")
        if in_tree.get("vermagic") != kernel_vermagic:
            raise BuildToolError("in-tree module full vermagic differs from module lineage")
        configured_modules = sorted(
            symbol for symbol, value in parse_dotconfig(config_path).items() if value == "m"
        )
        if in_tree.get("requested_symbols") != configured_modules:
            raise BuildToolError("in-tree module symbol request differs from the final .config")
        expected_memkernel_commit = (
            lock.dependencies["memkernel"].commit
            if feature.flags.get("nethunter.memkernel", False)
            else None
        )
        if in_tree.get("memkernel_commit") != expected_memkernel_commit:
            raise BuildToolError("MemKernel staging lineage differs from the dependency lock")
        recorded_module_signatures: list[tuple[str, int, str]] = []
        recorded_official_paths: list[str] = []
        for record in in_tree.get("modules", []):
            if not isinstance(record, dict):
                raise BuildToolError("invalid in-tree .ko record")
            recorded_path = _resolve_staged_module(output_dir, record, role="in-tree module")
            _record_matches(recorded_path, record, "in-tree module")
            recorded_module_signatures.append(
                (
                    recorded_path.relative_to(output_dir / "modules" / "staging").as_posix(),
                    int(record["size"]),
                    str(record["sha256"]),
                )
            )
            if not smoke:
                official_path = record.get("official_path")
                if not isinstance(official_path, str):
                    raise BuildToolError("real in-tree .ko record lacks its official path")
                recorded_official_paths.append(official_path)
            in_tree_module_count += 1
        if not smoke:
            if len(recorded_official_paths) != len(set(recorded_official_paths)):
                raise BuildToolError("in-tree module records repeat an official path")
            module_outputs = configuration.get("module_outputs")
            if not isinstance(module_outputs, dict):
                raise BuildToolError("Kleaf module-output configuration record is absent")
            declared_paths = module_outputs.get("requested_paths")
            if not isinstance(declared_paths, list):
                raise BuildToolError("Kleaf module-output declaration paths are absent")
            mapped_paths = set(mapped_module_output_paths())
            produced_paths = [
                path for path in recorded_official_paths if path in mapped_paths
            ]
            output_verification = verify_produced_module_outputs(
                declared_paths,
                produced_paths,
            )
            if in_tree.get("module_outputs") != output_verification:
                raise BuildToolError("staged module outputs differ from their build record")
            if module_outputs.get("produced") != output_verification:
                raise BuildToolError("staged module outputs differ from the kernel lineage")
        if configured_modules and in_tree_module_count == 0:
            raise BuildToolError("final .config requests modules but no in-tree modules were recorded")
        expected_dependency_ids = list(feature.external_modules)
        if modules.get("external_dependency_ids") != expected_dependency_ids:
            raise BuildToolError("external module dependency selection differs from the feature profile")
        external_dependencies = modules.get("external_modules")
        if not isinstance(external_dependencies, list):
            raise BuildToolError("external module dependency records are absent")
        observed_dependency_ids = [
            dependency.get("dependency") if isinstance(dependency, dict) else None
            for dependency in external_dependencies
        ]
        if observed_dependency_ids != expected_dependency_ids:
            raise BuildToolError("external module dependency records are missing, repeated, or reordered")
        for dependency in external_dependencies:
            if not isinstance(dependency, dict):
                raise BuildToolError("invalid external module record")
            dependency_id = str(dependency["dependency"])
            if dependency.get("vermagic") != kernel_vermagic:
                raise BuildToolError(
                    f"external module {dependency_id} full vermagic differs from module lineage"
                )
            locked_dependency = lock.dependencies.get(dependency_id)
            if (
                locked_dependency is None
                or locked_dependency.kind != "git"
                or dependency.get("locked_commit") != locked_dependency.commit
            ):
                raise BuildToolError(
                    f"external module {dependency_id} differs from its locked Git commit"
                )
            dependency_modules = dependency.get("modules")
            if not isinstance(dependency_modules, list) or not dependency_modules:
                raise BuildToolError(f"external module {dependency_id} recorded no .ko files")
            for record in dependency_modules:
                if not isinstance(record, dict):
                    raise BuildToolError("invalid .ko record")
                recorded_path = _resolve_staged_module(output_dir, record, role="external module")
                _record_matches(recorded_path, record, "external module")
                recorded_module_signatures.append(
                    (
                        recorded_path.relative_to(output_dir / "modules" / "staging").as_posix(),
                        int(record["size"]),
                        str(record["sha256"]),
                    )
                )
                module_count += 1
        staging = output_dir / "modules" / "staging"
        staged_paths = sorted(staging.rglob("*")) if staging.is_dir() else []
        if any(path.is_symlink() for path in staged_paths):
            raise BuildToolError("module staging contains a symbolic link")
        staged_signatures = [
            (path.relative_to(staging).as_posix(), path.stat().st_size, sha256_file(path))
            for path in staged_paths
            if path.is_file() and path.suffix == ".ko"
        ]
        if Counter(recorded_module_signatures) != Counter(staged_signatures):
            raise BuildToolError("module staging contains missing, changed, or unrecorded .ko files")
        staging_file_records = modules.get("staging_files")
        if not isinstance(staging_file_records, list):
            raise BuildToolError("module staging file manifest is absent")
        recorded_staging_signatures: list[tuple[str, int, str]] = []
        for record in staging_file_records:
            if not isinstance(record, dict):
                raise BuildToolError("invalid module staging file record")
            recorded_path = _resolve_staged_module(output_dir, record, role="staging file")
            _record_matches(recorded_path, record, "staging file")
            recorded_staging_signatures.append(
                (
                    recorded_path.relative_to(staging).as_posix(),
                    int(record["size"]),
                    str(record["sha256"]),
                )
            )
        actual_staging_signatures = [
            (path.relative_to(staging).as_posix(), path.stat().st_size, sha256_file(path))
            for path in staged_paths
            if path.is_file()
        ]
        if Counter(recorded_staging_signatures) != Counter(actual_staging_signatures):
            raise BuildToolError("module staging contains an unrecorded or changed packaged file")
    _assert_no_partition_images(output_dir)
    report = {
        "schema_version": 1,
        "profile": profile.id,
        "feature_profile": feature.id,
        "root_variant": root_variant,
        "build_target": build_target,
        "context_sha256": context["context_sha256"],
        "image_sha256": image_record["sha256"],
        "module_symvers_sha256": symvers_record["sha256"],
        "in_tree_module_count": in_tree_module_count,
        "external_module_count": module_count,
        "smoke": smoke,
    }
    atomic_write_json(output_dir / ".op13" / "verification.json", report)
    return report


def _zip_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    if epoch > MAX_ZIP_EPOCH:
        raise BuildToolError("ZIP timestamp exceeds 2107-12-31T23:59:58Z")
    minimum = int(datetime(1980, 1, 1, tzinfo=timezone.utc).timestamp())
    value = max(epoch, minimum)
    date = datetime.fromtimestamp(value, timezone.utc)
    # ZIP stores seconds with two-second precision.
    return date.year, date.month, date.day, date.hour, date.minute, date.second - date.second % 2


def deterministic_zip(
    source: Path,
    destination: Path,
    *,
    epoch: int,
    member_modes: Mapping[str, int] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> None:
    if not source.is_dir():
        raise BuildToolError(f"ZIP source directory is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix())
    files = [path for path in paths if path.is_file() and not path.is_symlink()]
    if member_modes is not None:
        actual_members = {path.relative_to(source).as_posix() for path in files}
        if set(member_modes) != actual_members:
            raise BuildToolError("explicit ZIP mode map differs from source members")
        if any(mode not in {0o644, 0o755} for mode in member_modes.values()):
            raise BuildToolError("explicit ZIP mode map contains an unsupported mode")
    if compression not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise BuildToolError("unsupported deterministic ZIP compression method")
    zip_options: dict[str, Any] = {"compression": compression}
    if compression == zipfile.ZIP_DEFLATED:
        zip_options["compresslevel"] = 9
    with zipfile.ZipFile(destination, "w", **zip_options) as archive:
        for path in paths:
            if path.is_symlink():
                raise BuildToolError(f"symlinks are forbidden in release ZIPs: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            if relative.startswith("../") or relative.startswith("/"):
                raise BuildToolError(f"unsafe ZIP member {relative}")
            info = zipfile.ZipInfo(relative, date_time=_zip_datetime(epoch))
            info.create_system = 3
            if member_modes is None:
                executable = bool(path.stat().st_mode & stat.S_IXUSR)
                mode = 0o755 if executable else 0o644
            else:
                mode = member_modes[relative]
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = compression
            write_options: dict[str, Any] = {"compress_type": compression}
            if compression == zipfile.ZIP_DEFLATED:
                write_options["compresslevel"] = 9
            archive.writestr(info, path.read_bytes(), **write_options)
    with zipfile.ZipFile(destination, "r") as archive:
        bad = archive.testzip()
        if bad is not None:
            raise BuildToolError(f"corrupt ZIP member after packaging: {bad}")


def _verify_zip_file_manifest(
    archive_path: Path,
    records: object,
    *,
    role: str,
) -> None:
    """Bind the completed ZIP bytes to a previously sealed file manifest."""

    if not isinstance(records, list):
        raise BuildToolError(f"{role} file manifest is absent")
    expected: dict[str, tuple[int, str]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise BuildToolError(f"{role} file manifest record {index} is invalid")
        value = record.get("path")
        if not isinstance(value, str) or not value or "\\" in value:
            raise BuildToolError(f"{role} file manifest has an unsafe member path")
        member = PurePosixPath(value)
        if member.is_absolute() or ".." in member.parts or member.as_posix() != value:
            raise BuildToolError(f"{role} file manifest has an unsafe member path")
        if value in expected:
            raise BuildToolError(f"{role} file manifest repeats {value}")
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(size, int) or size < 0 or not isinstance(digest, str):
            raise BuildToolError(f"{role} file manifest record {value} is incomplete")
        expected[value] = (size, digest)

    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise BuildToolError(f"{role} ZIP contains duplicate members")
        if set(names) != set(expected):
            missing = sorted(set(expected) - set(names))
            unexpected = sorted(set(names) - set(expected))
            details: list[str] = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unexpected:
                details.append("unexpected: " + ", ".join(unexpected))
            raise BuildToolError(f"{role} ZIP contents differ from its manifest ({'; '.join(details)})")
        for info in infos:
            data = archive.read(info)
            expected_size, expected_digest = expected[info.filename]
            if len(data) != expected_size or sha256_bytes(data) != expected_digest:
                raise BuildToolError(
                    f"{role} ZIP member {info.filename} differs from its manifest"
                )


def _safe_anykernel_member(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BuildToolError(f"{where} has an unsafe member path")
    member = PurePosixPath(value)
    if member.is_absolute() or ".." in member.parts or member.as_posix() != value:
        raise BuildToolError(f"{where} has an unsafe member path")
    return value


def _lf_text_bytes(data: bytes, *, where: str) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildToolError(f"{where} must be UTF-8 text") from exc
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise BuildToolError(f"{where} contains a non-LF line ending")
    return text.encode("utf-8")


def _copy_anykernel_member(
    source: Path,
    destination: Path,
    source_member: str,
    *,
    output_member: str | None = None,
    normalize_lf: bool = False,
) -> None:
    source_member = _safe_anykernel_member(source_member, where="AnyKernel3 source member")
    output_member = _safe_anykernel_member(
        source_member if output_member is None else output_member,
        where="AnyKernel3 output member",
    )
    source_path = source.joinpath(*PurePosixPath(source_member).parts)
    if source_path.is_symlink() or not source_path.is_file():
        raise BuildToolError(f"AnyKernel3 source tree lacks regular file {source_member}")
    destination_path = destination.joinpath(*PurePosixPath(output_member).parts)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if normalize_lf:
        destination_path.write_bytes(
            _lf_text_bytes(source_path.read_bytes(), where=f"AnyKernel3 source {source_member}")
        )
    else:
        shutil.copy2(source_path, destination_path)


def _validate_lf_file(path: Path, *, role: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise BuildToolError(f"{role} is missing")
    data = path.read_bytes()
    if b"\r" in data:
        raise BuildToolError(f"{role} must use LF line endings")
    _lf_text_bytes(data, where=role)


def _validate_anykernel_script(path: Path) -> None:
    _validate_lf_file(path, role="repository-owned OnePlus 13 anykernel.sh")
    text = path.read_text(encoding="utf-8")
    required_lines = {
        "device.name1=dodge",
        "do.devicecheck=1",
        "do.modules=0",
        "BLOCK=boot;",
        "IS_SLOT_DEVICE=1;",
        ". tools/ak3-core.sh;",
        "split_boot;",
        "flash_boot;",
    }
    for line in sorted(required_lines):
        if text.splitlines().count(line) != 1:
            raise BuildToolError(
                f"repository-owned anykernel.sh must contain exactly one {line!r}"
            )
    forbidden = (
        "omap_hsmmc",
        "maguro",
        "toro",
        "tuna",
        "dump_boot;",
        "write_boot;",
        "backup_file ",
        "replace_string ",
        "replace_section ",
        "replace_line ",
        "append_file ",
        "patch_fstab ",
    )
    for token in forbidden:
        if token in text:
            raise BuildToolError(
                f"repository-owned anykernel.sh contains forbidden template operation {token!r}"
            )


def _exact_object_keys(value: object, expected: set[str], *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise BuildToolError(f"{where} schema is incomplete or contains unknown fields")
    return value


def _anykernel_dependency_identity(
    dependency: Any,
    upstream_contracts: tuple[Mapping[str, Any], ...] = (
        ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS
    ),
) -> dict[str, Any]:
    license_value = dependency.raw.get("license")
    if (
        dependency.id != "anykernel3"
        or dependency.kind != "git"
        or not isinstance(dependency.url, str)
        or not dependency.url.startswith("https://")
        or not isinstance(dependency.commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", dependency.commit) is None
        or not isinstance(license_value, str)
        or not license_value
    ):
        raise BuildToolError("AnyKernel3 dependency lacks an immutable source identity")
    return {
        "dependency": dependency.id,
        "repository": dependency.url,
        "commit": dependency.commit,
        "license_classification": license_value,
        "license_member": "LICENSE",
        "template_members": [dict(record) for record in upstream_contracts],
    }


def _magisk_release_identity(dependency: Any) -> dict[str, str]:
    repository = dependency.raw.get("repository")
    version = dependency.raw.get("version")
    license_value = dependency.raw.get("license")
    if (
        dependency.id != "magisk_release_apk"
        or dependency.kind != "release_asset"
        or not isinstance(dependency.url, str)
        or not dependency.url.startswith("https://")
        or not isinstance(dependency.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", dependency.sha256) is None
        or not isinstance(repository, str)
        or not repository.startswith("https://")
        or not repository.endswith(".git")
        or not isinstance(dependency.ref, str)
        or not dependency.ref.startswith("refs/tags/")
        or not isinstance(dependency.commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", dependency.commit) is None
        or not isinstance(version, str)
        or not version
        or license_value != "SEE-UPSTREAM-MULTIPLE"
    ):
        raise BuildToolError("Magisk release dependency lacks an immutable asset/source identity")
    return {
        "dependency": dependency.id,
        "uri": dependency.url,
        "sha256": dependency.sha256,
        "repository": repository,
        "ref": dependency.ref,
        "source_commit": dependency.commit,
        "version": version,
        "license_classification": license_value,
        "archive_format": "apk-zip",
        "abi": "arm64-v8a",
    }


def _corresponding_source_cache_path(cache_root: Path, dependency: Any) -> Path:
    suffixes = Path(urlsplit(str(dependency.url)).path).suffixes
    suffix = (
        "".join(suffixes[-2:])
        if len(suffixes) >= 2 and suffixes[-2] == ".tar"
        else (suffixes[-1] if suffixes else "")
    )
    files_root = (cache_root.resolve() / "files")
    files_root.mkdir(parents=True, exist_ok=True)
    files_root = files_root.resolve()
    destination = (
        files_root / f"{dependency.id}-{str(dependency.sha256)[:12]}{suffix}"
    ).resolve()
    try:
        destination.relative_to(files_root)
    except ValueError as exc:
        raise BuildToolError("corresponding-source cache path escaped its root") from exc
    return destination


def _verify_corresponding_source_cache_file(path: Path, dependency: Any) -> None:
    expected_size = dependency.raw.get("size")
    if (
        path.is_symlink()
        or not path.is_file()
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 1
        or expected_size > ANYKERNEL_SOURCE_ARCHIVE_MAX_BYTES
        or path.stat().st_size != expected_size
        or sha256_file(path) != dependency.sha256
    ):
        raise BuildToolError(
            f"cached corresponding-source archive differs: {dependency.id}"
        )


def _download_corresponding_source_dependency(
    dependency: Any,
    destination: Path,
    *,
    offline: bool,
) -> Path:
    if destination.exists() or destination.is_symlink():
        _verify_corresponding_source_cache_file(destination, dependency)
        return destination
    if offline:
        raise BuildToolError(f"offline corresponding-source cache miss: {dependency.id}")
    expected_size = dependency.raw.get("size")
    if (
        dependency.kind != "file"
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 1
        or expected_size > ANYKERNEL_SOURCE_ARCHIVE_MAX_BYTES
        or not isinstance(dependency.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", dependency.sha256) is None
    ):
        raise BuildToolError(
            f"corresponding-source dependency lacks an exact file identity: {dependency.id}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{dependency.id}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        downloaded = 0
        request = urllib.request.Request(
            dependency.url,
            headers={"User-Agent": "OnePlus13-KernelBuilder-source-closure/1"},
        )
        try:
            with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(
                request,
                timeout=90,
            ) as response:
                if urlsplit(str(response.geturl())).scheme != "https":
                    raise BuildToolError(
                        f"corresponding-source redirect is not HTTPS: {dependency.id}"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) != expected_size:
                    raise BuildToolError(
                        f"corresponding-source Content-Length differs: {dependency.id}"
                    )
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    downloaded += len(block)
                    if downloaded > expected_size:
                        raise BuildToolError(
                            f"corresponding-source download exceeds its lock: {dependency.id}"
                        )
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            if isinstance(exc, BuildToolError):
                raise
            raise BuildToolError(
                f"corresponding-source download failed: {dependency.id}: {exc}"
            ) from exc
        if downloaded != expected_size or digest.hexdigest() != dependency.sha256:
            raise BuildToolError(
                f"corresponding-source download differs from its lock: {dependency.id}"
            )
        os.replace(temporary, destination)
        _verify_corresponding_source_cache_file(destination, dependency)
        return destination
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)


def _download_corresponding_source_dependency_with_retries(
    dependency: Any,
    destination: Path,
    *,
    offline: bool,
) -> Path:
    attempts = 1 if offline else 3
    error: BuildToolError | None = None
    for attempt in range(attempts):
        try:
            return _download_corresponding_source_dependency(
                dependency,
                destination,
                offline=offline,
            )
        except BuildToolError as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    if error is None:
        raise BuildToolError(
            f"corresponding-source acquisition failed: {dependency.id}"
        )
    raise error


def _fetch_anykernel_corresponding_source_dependencies(
    lock: DependencyLock,
    cache_root: Path,
    *,
    offline: bool,
) -> dict[str, Any]:
    dependencies = []
    for dependency_id in ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES:
        dependency = lock.dependencies.get(dependency_id)
        if dependency is None:
            raise BuildToolError(
                f"corresponding-source dependency is absent: {dependency_id}"
            )
        dependencies.append(dependency)
    results: dict[str, Path] = {}
    workers = min(ANYKERNEL_SOURCE_FETCH_WORKERS, len(dependencies))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _download_corresponding_source_dependency_with_retries,
                dependency,
                _corresponding_source_cache_path(cache_root, dependency),
                offline=offline,
            ): dependency.id
            for dependency in dependencies
        }
        try:
            for future in concurrent.futures.as_completed(futures):
                dependency_id = futures[future]
                results[dependency_id] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return {
        "schema_version": 1,
        "dependency_lock_sha256": lock.digest,
        "dependencies": {
            dependency_id: {
                "kind": "file",
                "path": str(results[dependency_id]),
                "sha256": lock.dependencies[dependency_id].sha256,
            }
            for dependency_id in ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES
        },
        "offline": offline,
    }


def _validated_cargo_packages(value: object, *, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise BuildToolError(f"{where} cargo package inventory is invalid")
    result: list[dict[str, Any]] = []
    for index, raw_package in enumerate(value):
        package = dict(
            _exact_object_keys(
                raw_package,
                {"name", "version", "source", "checksum", "manifest_path"},
                where=f"{where} cargo_packages[{index}]",
            )
        )
        if (
            not isinstance(package["name"], str)
            or not package["name"]
            or not isinstance(package["version"], str)
            or not package["version"]
            or not isinstance(package["source"], str)
            or not package["source"]
            or (
                package["checksum"] is not None
                and (
                    not isinstance(package["checksum"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", package["checksum"]) is None
                )
            )
            or _safe_anykernel_member(
                package["manifest_path"],
                where=f"{where} cargo manifest path",
            )
            != package["manifest_path"]
        ):
            raise BuildToolError(f"{where} cargo package identity is invalid")
        result.append(package)
    return result


def _load_anykernel_corresponding_source_policy(
    *,
    root: Path,
    lock: DependencyLock,
    magisk_dependency: Any,
) -> dict[str, Any]:
    policy_path = root / ANYKERNEL_CORRESPONDING_SOURCE_POLICY
    if policy_path.is_symlink() or not policy_path.is_file():
        raise BuildToolError("AnyKernel corresponding-source policy is missing")
    try:
        policy = strict_json_loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BuildToolError("AnyKernel corresponding-source policy is invalid JSON") from exc
    policy = dict(
        _exact_object_keys(
            policy,
            {
                "schema_version",
                "format",
                "scope",
                "magisk_gitmodules",
                "magisk_gitlinks",
                "cargo_lock",
                "archives",
            },
            where="AnyKernel corresponding-source policy",
        )
    )
    expected_gitlinks = [
        {"dependency": dependency_id, **dict(identity)}
        for dependency_id, identity in ANYKERNEL_MAGISK_GITLINKS.items()
    ]
    if (
        policy["schema_version"] != 1
        or policy["format"] != ANYKERNEL_CORRESPONDING_SOURCE_FORMAT
        or not isinstance(policy["scope"], str)
        or not policy["scope"].strip()
        or policy["magisk_gitmodules"]
        != dict(ANYKERNEL_MAGISK_GITMODULES_IDENTITY)
        or policy["magisk_gitlinks"] != expected_gitlinks
        or policy["cargo_lock"] != dict(ANYKERNEL_MAGISK_CARGO_LOCK_IDENTITY)
        or not isinstance(policy["archives"], list)
    ):
        raise BuildToolError("AnyKernel corresponding-source policy identity is invalid")

    expected_ids = list(ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES)
    if len(policy["archives"]) != len(expected_ids):
        raise BuildToolError("AnyKernel corresponding-source archive count differs")
    crate_identities = dict(
        zip(ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS, ANYKERNEL_CARGO_CRATE_IDENTITIES)
    )
    archives: list[dict[str, Any]] = []
    archive_paths: set[str] = set()
    seen_ids: list[str] = []
    for index, raw_record in enumerate(policy["archives"]):
        required_record_keys = {
            "dependency",
            "archive_path",
            "repository",
            "source_registry",
            "url",
            "commit",
            "size",
            "sha256",
            "license",
            "relationship",
            "submodule_path",
            "cargo_packages",
        }
        if not isinstance(raw_record, dict) or set(raw_record) not in {
            frozenset(required_record_keys),
            frozenset({*required_record_keys, "tree"}),
        }:
            raise BuildToolError(
                f"AnyKernel corresponding-source archive {index} schema is incomplete or contains unknown fields"
            )
        record = dict(raw_record)
        dependency_id = record["dependency"]
        archive_path = record["archive_path"]
        repository = record["repository"]
        source_registry = record["source_registry"]
        url = record["url"]
        commit = record["commit"]
        tree = record.get("tree")
        size = record["size"]
        digest = record["sha256"]
        license_value = record["license"]
        relationship = record["relationship"]
        submodule_path = record["submodule_path"]
        cargo_packages = _validated_cargo_packages(
            record["cargo_packages"],
            where=f"AnyKernel corresponding-source archive {index}",
        )
        record["cargo_packages"] = cargo_packages
        if (
            not isinstance(dependency_id, str)
            or dependency_id not in expected_ids
            or dependency_id in seen_ids
            or not isinstance(archive_path, str)
            or _safe_anykernel_member(
                archive_path,
                where=f"AnyKernel corresponding-source archive {index} path",
            )
            != archive_path
            or not archive_path.startswith("sources/")
            or archive_path in archive_paths
            or not isinstance(url, str)
            or not url.startswith("https://")
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or size > ANYKERNEL_SOURCE_ARCHIVE_MAX_BYTES
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(license_value, str)
            or not license_value
        ):
            raise BuildToolError(
                f"AnyKernel corresponding-source archive {index} is invalid"
            )

        expected_gitlink = ANYKERNEL_MAGISK_GITLINKS.get(dependency_id)
        expected_tree = ANYKERNEL_GIT_SOURCE_TREE_IDS.get(str(dependency_id))
        if expected_tree is None:
            if tree is not None:
                raise BuildToolError(
                    f"AnyKernel corresponding-source tree identity differs: {dependency_id}"
                )
        elif tree != expected_tree:
            raise BuildToolError(
                f"AnyKernel corresponding-source Git tree identity differs: {dependency_id}"
            )
        crate_identity = crate_identities.get(str(dependency_id))
        if dependency_id in ANYKERNEL_PROGRAM_SOURCE_DEPENDENCY_IDS:
            if (
                not archive_path.endswith(".tar.gz")
                or not isinstance(repository, str)
                or not repository.startswith("https://github.com/")
                or not repository.endswith(".git")
                or source_registry is not None
                or not isinstance(commit, str)
                or re.fullmatch(r"[0-9a-f]{40}", commit) is None
                or not isinstance(tree, str)
                or re.fullmatch(r"[0-9a-f]{40}", tree) is None
                or not url.startswith("https://github.com/")
                or not url.endswith(f"/{commit}.tar.gz")
                or cargo_packages
            ):
                raise BuildToolError(
                    f"AnyKernel program source identity differs: {dependency_id}"
                )
            if expected_gitlink is None:
                if submodule_path is not None or relationship not in {
                    "magisk-root",
                    "busybox-root",
                }:
                    raise BuildToolError(
                        f"AnyKernel corresponding-source relationship differs: {dependency_id}"
                    )
            elif (
                submodule_path != expected_gitlink["path"]
                or repository != expected_gitlink["repository"]
                or commit != expected_gitlink["commit"]
                or relationship != "magisk-git-submodule"
            ):
                raise BuildToolError(
                    f"AnyKernel Magisk Gitlink identity differs: {dependency_id}"
                )
        elif dependency_id == ANYKERNEL_CARGO_GIT_DEPENDENCY_ID:
            expected_packages = [
                {
                    "name": "pb-rs",
                    "version": "0.10.0",
                    "source": ANYKERNEL_CARGO_GIT_SOURCE,
                    "checksum": None,
                    "manifest_path": "pb-rs/Cargo.toml",
                },
                {
                    "name": "quick-protobuf",
                    "version": "0.8.1",
                    "source": ANYKERNEL_CARGO_GIT_SOURCE,
                    "checksum": None,
                    "manifest_path": "quick-protobuf/Cargo.toml",
                },
            ]
            if (
                archive_path
                != "sources/cargo/git/quick-protobuf-980b0fb0.tar.gz"
                or repository != ANYKERNEL_CARGO_GIT_ARCHIVE["repository"]
                or source_registry is not None
                or commit != ANYKERNEL_CARGO_GIT_ARCHIVE["commit"]
                or tree != ANYKERNEL_GIT_SOURCE_TREE_IDS[dependency_id]
                or size != ANYKERNEL_CARGO_GIT_ARCHIVE["size"]
                or digest != ANYKERNEL_CARGO_GIT_ARCHIVE["sha256"]
                or url
                != (
                    "https://github.com/topjohnwu/quick-protobuf/archive/"
                    f"{commit}.tar.gz"
                )
                or license_value != "MIT"
                or relationship != "magisk-cargo-git"
                or submodule_path is not None
                or cargo_packages != expected_packages
            ):
                raise BuildToolError("Magisk Cargo Git source identity differs")
        elif crate_identity is not None:
            crate_name, crate_version = crate_identity
            expected_package = {
                "name": crate_name,
                "version": crate_version,
                "source": ANYKERNEL_CARGO_REGISTRY_SOURCE,
                "checksum": digest,
                "manifest_path": "Cargo.toml",
            }
            if (
                archive_path
                != f"sources/cargo/registry/{crate_name}-{crate_version}.crate"
                or repository is not None
                or source_registry != f"https://crates.io/crates/{crate_name}"
                or url
                != (
                    f"https://static.crates.io/crates/{crate_name}/"
                    f"{crate_name}-{crate_version}.crate"
                )
                or commit is not None
                or tree is not None
                or relationship != "magisk-cargo-registry"
                or submodule_path is not None
                or cargo_packages != [expected_package]
            ):
                raise BuildToolError(
                    f"Magisk Cargo registry source identity differs: {dependency_id}"
                )
        else:
            raise BuildToolError(
                f"unclassified corresponding-source dependency: {dependency_id}"
            )

        dependency = lock.dependencies.get(str(dependency_id))
        if dependency is None or (
            dependency.kind != "file"
            or dependency.url != url
            or dependency.commit != commit
            or dependency.sha256 != digest
            or dependency.raw.get("repository") != repository
            or dependency.raw.get("size") != size
            or dependency.raw.get("license") != license_value
            or "package-anykernel3-source" not in dependency.required_for
        ):
            raise BuildToolError(
                f"AnyKernel corresponding-source policy differs from dependencies/lock.yml: {dependency_id}"
            )
        seen_ids.append(str(dependency_id))
        archive_paths.add(str(archive_path))
        archives.append(record)
    if seen_ids != expected_ids:
        raise BuildToolError(
            "AnyKernel corresponding-source dependencies are absent or out of canonical order"
        )

    magisk_identity = _magisk_release_identity(magisk_dependency)
    magisk_source = archives[0]
    if (
        magisk_source["dependency"] != "magisk_source"
        or magisk_source["relationship"] != "magisk-root"
        or magisk_source["repository"] != magisk_identity["repository"]
        or magisk_source["commit"] != magisk_identity["source_commit"]
    ):
        raise BuildToolError(
            "Magisk release and corresponding root source identities differ"
        )
    busybox_source = archives[1]
    busybox_contract = ANYKERNEL_TOOL_CONTRACTS["tools/busybox"]["source"]
    if (
        busybox_source["dependency"] != "magisk_busybox_source"
        or busybox_source["relationship"] != "busybox-root"
        or busybox_source["repository"] != busybox_contract["repository"]
        or busybox_source["commit"] != busybox_contract["commit"]
    ):
        raise BuildToolError(
            "BusyBox executable and corresponding source identities differ"
        )
    policy["archives"] = archives
    policy["path"] = ANYKERNEL_CORRESPONDING_SOURCE_POLICY
    policy["sha256"] = sha256_file(policy_path)
    return policy


def _expected_corresponding_source_root(record: Mapping[str, Any]) -> str:
    cargo_packages = record.get("cargo_packages")
    if record.get("relationship") == "magisk-cargo-registry":
        package = cargo_packages[0]
        return f"{package['name']}-{package['version']}"
    repository_name = PurePosixPath(urlsplit(str(record["repository"])).path).name
    if not repository_name.endswith(".git"):
        raise BuildToolError(
            f"corresponding source repository identity is invalid: {record['dependency']}"
        )
    return f"{repository_name[:-4]}-{record['commit']}"


def _verify_corresponding_source_git_tree_identity(
    record: Mapping[str, Any],
) -> str | None:
    """Return the independently pinned Git tree expected for this source record."""

    dependency_id = str(record.get("dependency"))
    expected_tree = ANYKERNEL_GIT_SOURCE_TREE_IDS.get(dependency_id)
    actual_tree = record.get("tree")
    if expected_tree is None:
        if actual_tree is not None:
            raise BuildToolError(
                f"non-Git corresponding-source archive has a tree identity: {dependency_id}"
            )
        return None
    if actual_tree != expected_tree:
        raise BuildToolError(
            f"corresponding-source Git tree identity differs: {dependency_id}"
        )
    return expected_tree


_UNRECOVERABLE_GIT_ARCHIVE_ATTRIBUTES = frozenset(
    {
        "crlf",
        "export-ignore",
        "export-subst",
        "filter",
        "ident",
        "working-tree-encoding",
    }
)
_MAX_GIT_ATTRIBUTES_BYTES = 1024 * 1024
_MAX_GIT_NORMALIZED_FILE_BYTES = 64 * 1024 * 1024


def _git_object_digest(kind: bytes, payload: bytes) -> bytes:
    header = kind + b" " + str(len(payload)).encode("ascii") + b"\0"
    return hashlib.sha1(header + payload, usedforsecurity=False).digest()


def _git_blob_digest(stream: Any, size: int, *, where: str) -> bytes:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(b"blob " + str(size).encode("ascii") + b"\0")
    remaining = size
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise BuildToolError(f"{where} ended before its declared size")
        digest.update(block)
        remaining -= len(block)
    if stream.read(1):
        raise BuildToolError(f"{where} exceeds its declared size")
    return digest.digest()


def _git_archive_crlf_rules(
    archive_path: Path,
    source_root: str,
    dependency_id: str,
) -> list[tuple[tuple[str, ...], str]]:
    rules: list[tuple[tuple[str, ...], str]] = []
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            attributes_members = [
                member
                for member in archive.getmembers()
                if member.isfile()
                and member.name.startswith(source_root + "/")
                and PurePosixPath(member.name).name == ".gitattributes"
            ]
            for member in attributes_members:
                if member.size < 0 or member.size > _MAX_GIT_ATTRIBUTES_BYTES:
                    raise BuildToolError(
                        f"Git attributes file is too large: {dependency_id}"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise BuildToolError(
                        f"Git attributes file cannot be read: {dependency_id}"
                    )
                payload = stream.read(_MAX_GIT_ATTRIBUTES_BYTES + 1)
                if len(payload) != member.size:
                    raise BuildToolError(
                        f"Git attributes file size differs: {dependency_id}"
                    )
                try:
                    text = payload.decode("utf-8", "strict")
                except UnicodeError as exc:
                    raise BuildToolError(
                        f"Git attributes file is not UTF-8: {dependency_id}"
                    ) from exc
                relative = PurePosixPath(member.name).relative_to(source_root)
                directory = relative.parent.parts
                for line_number, raw_line in enumerate(text.splitlines(), 1):
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    fields = line.split()
                    if len(fields) < 2 or fields[0].startswith("[attr]"):
                        raise BuildToolError(
                            f"unsupported Git attributes syntax in {dependency_id}:"
                            f"{relative}:{line_number}"
                        )
                    pattern, attributes = fields[0], fields[1:]
                    if pattern.startswith("!") or "\\" in pattern:
                        raise BuildToolError(
                            f"unsupported Git attributes pattern in {dependency_id}:"
                            f"{relative}:{line_number}"
                        )
                    for attribute in attributes:
                        name = attribute.lstrip("-!").split("=", 1)[0]
                        if name in _UNRECOVERABLE_GIT_ARCHIVE_ATTRIBUTES:
                            if name == "crlf":
                                raise BuildToolError(
                                    f"legacy crlf Git attribute is unsupported: {dependency_id}"
                                )
                            if name != "export-ignore" and name != "export-subst" and name not in {
                                "filter",
                                "ident",
                                "working-tree-encoding",
                            }:
                                continue
                            raise BuildToolError(
                                f"Git archive has an unrecoverable {name} attribute: {dependency_id}"
                            )
                    if "eol=crlf" in attributes:
                        if "/" in pattern or pattern in {"", ".", ".."}:
                            raise BuildToolError(
                                f"unsupported eol=crlf Git pattern in {dependency_id}:"
                                f"{relative}:{line_number}"
                            )
                        rules.append((directory, pattern))
    except (OSError, tarfile.TarError) as exc:
        raise BuildToolError(
            f"corresponding source Git archive cannot be inspected: {dependency_id}"
        ) from exc
    return rules


def _git_archive_path_uses_crlf(
    relative: tuple[str, ...],
    rules: list[tuple[tuple[str, ...], str]],
) -> bool:
    return any(
        len(relative) > len(directory)
        and relative[: len(directory)] == directory
        and fnmatchcase(relative[-1], pattern)
        for directory, pattern in rules
    )


def _git_tree_digest(entries: Mapping[tuple[str, ...], tuple[str, bytes]]) -> str:
    root: dict[bytes, Any] = {}
    for path, leaf in entries.items():
        if not path:
            raise BuildToolError("Git tree entry has an empty path")
        node = root
        for index, component in enumerate(path):
            try:
                name = component.encode("utf-8", "strict")
            except UnicodeError as exc:
                raise BuildToolError("Git tree entry path is not UTF-8") from exc
            if not name or b"\0" in name or b"/" in name:
                raise BuildToolError("Git tree entry path is invalid")
            final = index == len(path) - 1
            existing = node.get(name)
            if final:
                if existing is not None:
                    raise BuildToolError("Git tree entry path conflicts with another entry")
                node[name] = leaf
            else:
                if existing is None:
                    child: dict[bytes, Any] = {}
                    node[name] = child
                    node = child
                elif isinstance(existing, dict):
                    node = existing
                else:
                    raise BuildToolError("Git tree entry has a file as its parent")

    def digest_tree(node: Mapping[bytes, Any]) -> bytes:
        records: list[tuple[bytes, bytes]] = []
        for name, child in node.items():
            if isinstance(child, dict):
                mode = b"40000"
                object_id = digest_tree(child)
                sort_key = name + b"/"
            else:
                mode_text, object_id = child
                mode = mode_text.encode("ascii")
                sort_key = name
            records.append((sort_key, mode + b" " + name + b"\0" + object_id))
        payload = b"".join(record for _, record in sorted(records))
        return _git_object_digest(b"tree", payload)

    return digest_tree(root).hex()


def _derive_corresponding_source_git_tree(
    archive_path: Path,
    record: Mapping[str, Any],
    source_root: str,
) -> str:
    dependency_id = str(record["dependency"])
    crlf_rules = _git_archive_crlf_rules(
        archive_path,
        source_root,
        dependency_id,
    )
    entries: dict[tuple[str, ...], tuple[str, bytes]] = {}
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name.rstrip("/") == source_root:
                    continue
                if not member.name.startswith(source_root + "/"):
                    raise BuildToolError(
                        f"Git archive member is outside its source root: {dependency_id}"
                    )
                relative = PurePosixPath(member.name.rstrip("/")).relative_to(
                    source_root
                ).parts
                if member.isdir():
                    continue
                if member.islnk():
                    raise BuildToolError(
                        f"Git archive hard links are unsupported: {dependency_id}"
                    )
                if member.issym():
                    try:
                        payload = member.linkname.encode("utf-8", "strict")
                    except UnicodeError as exc:
                        raise BuildToolError(
                            f"Git archive symlink target is not UTF-8: {dependency_id}"
                        ) from exc
                    leaf = ("120000", _git_object_digest(b"blob", payload))
                elif member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise BuildToolError(
                            f"Git archive file cannot be read: {dependency_id}"
                        )
                    if _git_archive_path_uses_crlf(relative, crlf_rules):
                        if member.size > _MAX_GIT_NORMALIZED_FILE_BYTES:
                            raise BuildToolError(
                                f"Git CRLF-normalized file is too large: {dependency_id}"
                            )
                        payload = stream.read(_MAX_GIT_NORMALIZED_FILE_BYTES + 1)
                        if len(payload) != member.size:
                            raise BuildToolError(
                                f"Git archive file size differs: {dependency_id}"
                            )
                        payload = payload.replace(b"\r\n", b"\n")
                        object_id = _git_object_digest(b"blob", payload)
                    else:
                        object_id = _git_blob_digest(
                            stream,
                            member.size,
                            where=f"Git archive file in {dependency_id}",
                        )
                    leaf = (
                        "100755" if member.mode & 0o111 else "100644",
                        object_id,
                    )
                else:
                    raise BuildToolError(
                        f"Git archive member type is unsupported: {dependency_id}"
                    )
                if relative in entries:
                    raise BuildToolError(
                        f"Git archive contains duplicate paths: {dependency_id}"
                    )
                entries[relative] = leaf
    except (OSError, tarfile.TarError) as exc:
        raise BuildToolError(
            f"corresponding source Git archive cannot be hashed: {dependency_id}"
        ) from exc

    if dependency_id == "magisk_source":
        for gitlink in ANYKERNEL_MAGISK_GITLINKS.values():
            path = PurePosixPath(gitlink["path"]).parts
            if path in entries or any(
                candidate[: len(path)] == path for candidate in entries
            ):
                raise BuildToolError(
                    "Magisk source archive contains content beneath a pinned Gitlink"
                )
            entries[path] = ("160000", bytes.fromhex(gitlink["commit"]))
    return _git_tree_digest(entries)


def _verify_corresponding_source_git_tree_archive(
    archive_path: Path,
    record: Mapping[str, Any],
    source_root: str,
    expected_tree: str,
) -> None:
    actual_tree = _derive_corresponding_source_git_tree(
        archive_path,
        record,
        source_root,
    )
    if actual_tree != expected_tree:
        raise BuildToolError(
            f"corresponding-source archive Git tree differs: {record['dependency']}"
        )


def _verify_corresponding_source_tarball(
    path: Path,
    record: Mapping[str, Any],
) -> str:
    if path.is_symlink() or not path.is_file():
        raise BuildToolError(
            f"corresponding source dependency is not a plain file: {record['dependency']}"
        )
    if (
        path.stat().st_size != record["size"]
        or sha256_file(path) != record["sha256"]
    ):
        raise BuildToolError(
            f"corresponding source archive differs from its exact policy: {record['dependency']}"
        )
    expected_git_tree = _verify_corresponding_source_git_tree_identity(record)
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise BuildToolError(
            f"corresponding source archive is not a readable tar.gz: {record['dependency']}"
        ) from exc
    if not members or len(members) > 200_000:
        raise BuildToolError(
            f"corresponding source archive has an invalid member count: {record['dependency']}"
        )
    seen: set[str] = set()
    roots: set[str] = set()
    regular_count = 0
    declared_bytes = 0
    links: list[tuple[str, str, bool]] = []
    for member in members:
        name = member.name.rstrip("/")
        if (
            not name
            or "\\" in name
            or "\x00" in name
            or PurePosixPath(name).is_absolute()
            or PureWindowsPath(name).is_absolute()
            or bool(PureWindowsPath(name).drive)
            or ".." in PurePosixPath(name).parts
            or PurePosixPath(name).as_posix() != name
            or name in seen
            or not (member.isfile() or member.isdir() or member.issym() or member.islnk())
        ):
            raise BuildToolError(
                f"corresponding source archive has an unsafe member: {record['dependency']}"
            )
        seen.add(name)
        roots.add(name.split("/", 1)[0])
        if member.isfile():
            regular_count += 1
            declared_bytes += member.size
            if member.size < 0 or declared_bytes > 2 * 1024 * 1024 * 1024:
                raise BuildToolError(
                    f"corresponding source archive declares excessive content: {record['dependency']}"
                )
        elif member.issym() or member.islnk():
            linkname = member.linkname
            if (
                not linkname
                or "\\" in linkname
                or "\x00" in linkname
                or PurePosixPath(linkname).is_absolute()
                or PureWindowsPath(linkname).is_absolute()
                or bool(PureWindowsPath(linkname).drive)
            ):
                raise BuildToolError(
                    f"corresponding source archive has an unsafe link: {record['dependency']}"
                )
            links.append((name, linkname, member.issym()))
    if len(roots) != 1 or regular_count == 0:
        raise BuildToolError(
            f"corresponding source archive lacks one source-tree root: {record['dependency']}"
        )
    source_root = next(iter(roots))
    if source_root != _expected_corresponding_source_root(record):
        raise BuildToolError(
            f"corresponding source archive root differs: {record['dependency']}"
        )
    for name, linkname, is_symlink in links:
        base = PurePosixPath(name).parent.parts if is_symlink else ()
        resolved: list[str] = []
        for part in (*base, *PurePosixPath(linkname).parts):
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise BuildToolError(
                        f"corresponding source archive link escapes its root: {record['dependency']}"
                    )
                resolved.pop()
            else:
                resolved.append(part)
        if not resolved or resolved[0] != source_root:
            raise BuildToolError(
                f"corresponding source archive link escapes its root: {record['dependency']}"
            )
    if expected_git_tree is not None:
        _verify_corresponding_source_git_tree_archive(
            path,
            record,
            source_root,
            expected_git_tree,
        )
    return source_root


def _read_corresponding_source_member(
    archive_path: Path,
    member_name: str,
    *,
    maximum_size: int,
    where: str,
) -> bytes:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            member = archive.getmember(member_name)
            if not member.isfile() or member.size < 1 or member.size > maximum_size:
                raise BuildToolError(f"{where} member identity is invalid")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise BuildToolError(f"{where} member cannot be read")
            payload = extracted.read(maximum_size + 1)
    except (KeyError, OSError, tarfile.TarError) as exc:
        raise BuildToolError(f"{where} member is missing") from exc
    if len(payload) != member.size or len(payload) > maximum_size:
        raise BuildToolError(f"{where} member size differs")
    return payload


def _cargo_license_identity(package: Mapping[str, Any]) -> str | None:
    license_value = package.get("license")
    if isinstance(license_value, str) and license_value:
        return license_value
    license_file = package.get("license-file")
    if isinstance(license_file, str) and license_file:
        return f"LicenseRef-file:{license_file}"
    return None


def _verify_cargo_source_archive(
    archive_path: Path,
    source_root: str,
    record: Mapping[str, Any],
) -> None:
    if not record["cargo_packages"]:
        return
    for package_record in record["cargo_packages"]:
        manifest_member = (
            f"{source_root}/{package_record['manifest_path']}"
        )
        payload = _read_corresponding_source_member(
            archive_path,
            manifest_member,
            maximum_size=4 * 1024 * 1024,
            where=f"Cargo package {package_record['name']}",
        )
        try:
            manifest = tomllib.loads(payload.decode("utf-8", "strict"))
            package = manifest["package"]
        except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
            raise BuildToolError(
                f"Cargo package manifest is invalid: {package_record['name']}"
            ) from exc
        if (
            not isinstance(package, dict)
            or package.get("name") != package_record["name"]
            or package.get("version") != package_record["version"]
            or _cargo_license_identity(package) != record["license"]
        ):
            raise BuildToolError(
                f"Cargo package manifest identity differs: {package_record['name']}"
            )
        license_value = _cargo_license_identity(package)
        if license_value is not None and license_value.startswith("LicenseRef-file:"):
            license_member = license_value.split(":", 1)[1]
            _read_corresponding_source_member(
                archive_path,
                f"{source_root}/{license_member}",
                maximum_size=4 * 1024 * 1024,
                where=f"Cargo package {package_record['name']} license",
            )


def _verify_magisk_source_closure(
    magisk_archive: Path,
    policy: Mapping[str, Any],
) -> None:
    gitmodules_identity = policy["magisk_gitmodules"]
    gitmodules = _read_corresponding_source_member(
        magisk_archive,
        gitmodules_identity["archive_member"],
        maximum_size=1024 * 1024,
        where="Magisk .gitmodules",
    )
    if (
        len(gitmodules) != gitmodules_identity["size"]
        or sha256_bytes(gitmodules) != gitmodules_identity["sha256"]
    ):
        raise BuildToolError("Magisk .gitmodules identity differs")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(gitmodules.decode("utf-8", "strict"))
    except (UnicodeError, configparser.Error) as exc:
        raise BuildToolError("Magisk .gitmodules is invalid") from exc
    actual_gitmodules: set[tuple[str, str]] = set()
    for section in parser.sections():
        if re.fullmatch(r'submodule "[^"]+"', section) is None:
            raise BuildToolError("Magisk .gitmodules contains an unexpected section")
        values = parser[section]
        if set(values) != {"path", "url"}:
            raise BuildToolError("Magisk .gitmodules record keys differ")
        actual_gitmodules.add((values["path"], values["url"]))
    expected_gitmodules = {
        (record["path"], record["repository"])
        for record in policy["magisk_gitlinks"]
    }
    if actual_gitmodules != expected_gitmodules:
        raise BuildToolError("Magisk Gitlink repository/path inventory differs")

    cargo_identity = policy["cargo_lock"]
    cargo_lock = _read_corresponding_source_member(
        magisk_archive,
        cargo_identity["archive_member"],
        maximum_size=16 * 1024 * 1024,
        where="Magisk Cargo.lock",
    )
    if (
        len(cargo_lock) != cargo_identity["size"]
        or sha256_bytes(cargo_lock) != cargo_identity["sha256"]
    ):
        raise BuildToolError("Magisk Cargo.lock identity differs")
    try:
        lock_document = tomllib.loads(cargo_lock.decode("utf-8", "strict"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BuildToolError("Magisk Cargo.lock is invalid") from exc
    packages = lock_document.get("package")
    if (
        lock_document.get("version") != cargo_identity["format_version"]
        or not isinstance(packages, list)
        or len(packages) != cargo_identity["package_count"]
    ):
        raise BuildToolError("Magisk Cargo.lock package inventory differs")

    registry_packages: list[dict[str, Any]] = []
    git_packages: list[dict[str, Any]] = []
    local_count = 0
    for raw_package in packages:
        if not isinstance(raw_package, dict):
            raise BuildToolError("Magisk Cargo.lock contains an invalid package")
        name = raw_package.get("name")
        version = raw_package.get("version")
        source = raw_package.get("source")
        checksum = raw_package.get("checksum")
        if not isinstance(name, str) or not isinstance(version, str):
            raise BuildToolError("Magisk Cargo.lock package identity is invalid")
        package_identity = {
            "name": name,
            "version": version,
            "source": source,
            "checksum": checksum,
        }
        if source is None:
            local_count += 1
        elif source == ANYKERNEL_CARGO_REGISTRY_SOURCE:
            if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
                raise BuildToolError("Magisk Cargo.lock registry checksum is invalid")
            registry_packages.append(package_identity)
        elif source == ANYKERNEL_CARGO_GIT_SOURCE:
            if checksum is not None:
                raise BuildToolError("Magisk Cargo.lock Git checksum is unexpected")
            git_packages.append(package_identity)
        else:
            raise BuildToolError(f"Magisk Cargo.lock has an unsealed source: {source}")

    expected_registry: list[dict[str, Any]] = []
    expected_git: list[dict[str, Any]] = []
    for record in policy["archives"]:
        for package in record["cargo_packages"]:
            identity = {
                key: package[key]
                for key in ("name", "version", "source", "checksum")
            }
            if record["relationship"] == "magisk-cargo-registry":
                expected_registry.append(identity)
            elif record["relationship"] == "magisk-cargo-git":
                expected_git.append(identity)
    sort_key = lambda package: (package["name"], package["version"])
    if (
        local_count != cargo_identity["local_package_count"]
        or len(registry_packages) != cargo_identity["registry_package_count"]
        or len(git_packages) != cargo_identity["git_package_count"]
        or len(expected_registry) != cargo_identity["registry_archive_count"]
        or len({record["dependency"] for record in policy["archives"] if record["relationship"] == "magisk-cargo-git"})
        != cargo_identity["git_archive_count"]
        or sorted(registry_packages, key=sort_key)
        != sorted(expected_registry, key=sort_key)
        or sorted(git_packages, key=sort_key) != sorted(expected_git, key=sort_key)
    ):
        raise BuildToolError("Magisk Cargo.lock source closure differs")


def _prepare_anykernel_corresponding_source_tree(
    *,
    root: Path,
    destination: Path,
    state: Mapping[str, Any],
    lock: DependencyLock,
    magisk_dependency: Any,
) -> dict[str, Any]:
    policy = _load_anykernel_corresponding_source_policy(
        root=root,
        lock=lock,
        magisk_dependency=magisk_dependency,
    )
    if destination.exists():
        raise BuildToolError("AnyKernel corresponding-source staging path already exists")
    destination.mkdir(parents=True)
    state_dependencies = state.get("dependencies")
    if not isinstance(state_dependencies, dict):
        raise BuildToolError("corresponding-source dependency state is absent")
    archive_records: list[dict[str, Any]] = []
    magisk_source_path: Path | None = None
    for record in policy["archives"]:
        dependency_id = str(record["dependency"])
        state_record = state_dependencies.get(dependency_id)
        if not isinstance(state_record, dict) or (
            state_record.get("kind") != "file"
            or state_record.get("sha256") != record["sha256"]
            or not isinstance(state_record.get("path"), str)
        ):
            raise BuildToolError(
                f"corresponding-source dependency state differs: {dependency_id}"
            )
        source_path = Path(state_record["path"])
        source_root = _verify_corresponding_source_tarball(source_path, record)
        _verify_cargo_source_archive(source_path, source_root, record)
        if dependency_id == "magisk_source":
            magisk_source_path = source_path
        output_path = destination.joinpath(
            *PurePosixPath(str(record["archive_path"])).parts
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, output_path)
        output_path.chmod(0o644)
        if (
            output_path.stat().st_size != record["size"]
            or sha256_file(output_path) != record["sha256"]
        ):
            raise BuildToolError(
                f"staged corresponding-source archive differs: {dependency_id}"
            )
        archive_records.append(dict(record))

    if magisk_source_path is None:
        raise BuildToolError("Magisk root source is absent from the companion")
    _verify_magisk_source_closure(magisk_source_path, policy)

    policy_source = root / ANYKERNEL_CORRESPONDING_SOURCE_POLICY
    policy_destination = destination / ANYKERNEL_CORRESPONDING_SOURCE_POLICY_MEMBER
    shutil.copy2(policy_source, policy_destination)
    policy_destination.chmod(0o644)
    if sha256_file(policy_destination) != policy["sha256"]:
        raise BuildToolError("staged corresponding-source policy differs")
    release_identity = _magisk_release_identity(magisk_dependency)
    manifest = {
        "schema_version": 1,
        "format": ANYKERNEL_CORRESPONDING_SOURCE_FORMAT,
        "scope": policy["scope"],
        "dependency_lock_sha256": lock.digest,
        "source_policy": {
            "repository_path": policy["path"],
            "member": ANYKERNEL_CORRESPONDING_SOURCE_POLICY_MEMBER,
            "sha256": policy["sha256"],
        },
        "release_asset": {
            "dependency": release_identity["dependency"],
            "uri": release_identity["uri"],
            "sha256": release_identity["sha256"],
            "repository": release_identity["repository"],
            "commit": release_identity["source_commit"],
            "version": release_identity["version"],
        },
        "binary_relationships": [
            {
                "path": "tools/busybox",
                "sha256": ANYKERNEL_TOOL_CONTRACTS["tools/busybox"]["sha256"],
                "source_dependencies": ["magisk_busybox_source"],
            },
            {
                "path": "tools/magiskboot",
                "sha256": ANYKERNEL_TOOL_CONTRACTS["tools/magiskboot"]["sha256"],
                "source_dependencies": [
                    "magisk_source",
                    *sorted(ANYKERNEL_SOURCE_DEPENDENCY_IDS[2:]),
                ],
            },
        ],
        "archives": archive_records,
    }
    manifest_path = destination / ANYKERNEL_CORRESPONDING_SOURCE_MANIFEST
    atomic_write_json(manifest_path, manifest)
    manifest_path.chmod(0o644)
    output_records = [
        {
            "path": path.relative_to(destination).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(
            (candidate for candidate in destination.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(destination).as_posix().encode(
                "utf-8"
            ),
        )
    ]
    expected_paths = {
        ANYKERNEL_CORRESPONDING_SOURCE_MANIFEST,
        ANYKERNEL_CORRESPONDING_SOURCE_POLICY_MEMBER,
        *(str(record["archive_path"]) for record in policy["archives"]),
    }
    if {record["path"] for record in output_records} != expected_paths:
        raise BuildToolError("AnyKernel corresponding-source staging members differ")
    return {
        "output_records": output_records,
        "archive_dependencies": list(ANYKERNEL_CORRESPONDING_SOURCE_DEPENDENCIES),
        "archive_count": len(archive_records),
        "manifest_member": ANYKERNEL_CORRESPONDING_SOURCE_MANIFEST,
        "manifest_sha256": sha256_file(manifest_path),
        "policy_member": ANYKERNEL_CORRESPONDING_SOURCE_POLICY_MEMBER,
        "policy_sha256": policy["sha256"],
        "scope": policy["scope"],
    }


def _elf_descriptor(data: bytes, *, where: str) -> dict[str, str]:
    if len(data) < 20 or data[:4] != b"\x7fELF":
        raise BuildToolError(f"{where} is not an ELF executable")
    if data[4] != 2 or data[5] != 1 or data[6] != 1:
        raise BuildToolError(f"{where} must be ELFCLASS64 ELFDATA2LSB")
    elf_type = int.from_bytes(data[16:18], "little")
    machine = int.from_bytes(data[18:20], "little")
    if elf_type != 2 or machine != 183:
        raise BuildToolError(f"{where} must be ET_EXEC EM_AARCH64")
    return {
        "class": "ELFCLASS64",
        "data": "ELFDATA2LSB",
        "machine": "EM_AARCH64",
        "type": "ET_EXEC",
    }


def _extract_magisk_anykernel_tools(
    *,
    asset: Path,
    destination: Path,
    dependency: Any,
    tool_contracts: Mapping[str, Mapping[str, Any]],
) -> None:
    _validate_zip_asset(asset, role="pinned official Magisk APK")
    if sha256_file(asset) != dependency.sha256:
        raise BuildToolError("Magisk APK digest differs from dependencies/lock.yml")
    expected_members = {
        str(contract["archive_member"]): output_member
        for output_member, contract in tool_contracts.items()
    }
    if len(expected_members) != len(tool_contracts):
        raise BuildToolError("AnyKernel3 tool contracts repeat a Magisk archive member")
    with zipfile.ZipFile(asset, "r") as archive:
        infos: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            if info.filename in infos:
                raise BuildToolError(f"pinned official Magisk APK repeats {info.filename}")
            infos[info.filename] = info
        missing = sorted(set(expected_members) - set(infos))
        if missing:
            raise BuildToolError("pinned official Magisk APK lacks " + ", ".join(missing))
        for archive_member, output_member in sorted(expected_members.items()):
            info = infos[archive_member]
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if info.is_dir() or (info.create_system == 3 and stat.S_ISLNK(unix_mode)):
                raise BuildToolError(f"Magisk APK member {archive_member} is not a regular file")
            if info.flag_bits & 0x1:
                raise BuildToolError(f"Magisk APK member {archive_member} is encrypted")
            data = archive.read(info)
            contract = tool_contracts[output_member]
            if (
                len(data) != contract.get("size")
                or sha256_bytes(data) != contract.get("sha256")
            ):
                raise BuildToolError(
                    f"Magisk APK member {archive_member} differs from the exact arm64 contract"
                )
            _elf_descriptor(data, where=f"Magisk APK member {archive_member}")
            output_path = destination.joinpath(*PurePosixPath(output_member).parts)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(data)


def _validate_anykernel_executable_provenance(
    path: Path,
    *,
    package_tree: Path,
    anykernel_dependency: Any,
    magisk_dependency: Any,
    tool_contracts: Mapping[str, Mapping[str, Any]],
    upstream_contracts: tuple[Mapping[str, Any], ...] = (
        ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS
    ),
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise BuildToolError("AnyKernel3 executable provenance manifest is missing")
    try:
        data = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BuildToolError("AnyKernel3 executable provenance manifest is invalid") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise BuildToolError("AnyKernel3 executable provenance schema is unsupported")
    _exact_object_keys(
        data,
        {
            "schema_version",
            "policy",
            "anykernel3",
            "release_asset",
            "license_files",
            "source_conveyance",
            "executables",
        },
        where="AnyKernel3 executable provenance",
    )
    if not isinstance(data.get("policy"), str) or not data["policy"]:
        raise BuildToolError("AnyKernel3 executable provenance policy is absent")
    if data.get("anykernel3") != _anykernel_dependency_identity(
        anykernel_dependency,
        upstream_contracts,
    ):
        raise BuildToolError("AnyKernel3 provenance identity differs from dependencies/lock.yml")
    if len(upstream_contracts) != len(ANYKERNEL_UPSTREAM_MEMBERS):
        raise BuildToolError("AnyKernel3 upstream template contract set differs")
    for index, contract in enumerate(upstream_contracts):
        record = _exact_object_keys(
            contract,
            {"path", "git_mode", "git_blob", "size", "sha256"},
            where=f"AnyKernel3 upstream template member {index}",
        )
        member = _safe_anykernel_member(
            record.get("path"),
            where=f"AnyKernel3 upstream template member {index}",
        )
        expected_mode = "100755" if ANYKERNEL_ZIP_MODES.get(member) == 0o755 else "100644"
        if (
            member != ANYKERNEL_UPSTREAM_MEMBERS[index]
            or record.get("git_mode") != expected_mode
            or re.fullmatch(r"[0-9a-f]{40}", str(record.get("git_blob", ""))) is None
            or not isinstance(record.get("size"), int)
            or isinstance(record.get("size"), bool)
            or int(record["size"]) < 1
            or re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))) is None
        ):
            raise BuildToolError(
                f"AnyKernel3 upstream template contract is invalid: {member}"
            )
        candidate = package_tree.joinpath(*PurePosixPath(member).parts)
        _validate_lf_file(candidate, role=f"AnyKernel3 upstream template {member}")
        payload = candidate.read_bytes()
        if (
            len(payload) != record["size"]
            or sha256_bytes(payload) != record["sha256"]
            or _git_object_digest(b"blob", payload).hex() != record["git_blob"]
        ):
            raise BuildToolError(
                f"AnyKernel3 upstream template differs from its pinned Git blob: {member}"
            )
    release_identity = _magisk_release_identity(magisk_dependency)
    if data.get("release_asset") != release_identity:
        raise BuildToolError("Magisk provenance identity differs from dependencies/lock.yml")

    raw_license_files = data.get("license_files")
    if not isinstance(raw_license_files, list):
        raise BuildToolError("AnyKernel3 license file records are absent")
    license_files: dict[str, Mapping[str, Any]] = {}
    for index, raw_record in enumerate(raw_license_files):
        record = _exact_object_keys(
            raw_record,
            {"path", "size", "sha256", "spdx", "source"},
            where=f"AnyKernel3 license file record {index}",
        )
        member = _safe_anykernel_member(record.get("path"), where="AnyKernel3 license file")
        source = _exact_object_keys(
            record.get("source"),
            {"repository", "commit", "path"},
            where=f"AnyKernel3 license file {member} source",
        )
        if (
            member in license_files
            or member not in ANYKERNEL_LICENSE_MEMBERS
            or not isinstance(record.get("size"), int)
            or isinstance(record.get("size"), bool)
            or int(record["size"]) < 1
            or re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))) is None
            or not isinstance(record.get("spdx"), str)
            or not record["spdx"]
            or not isinstance(source.get("repository"), str)
            or not str(source["repository"]).startswith("https://")
            or not str(source["repository"]).endswith(".git")
            or re.fullmatch(r"[0-9a-f]{40}", str(source.get("commit", ""))) is None
            or not isinstance(source.get("path"), str)
            or not source["path"]
        ):
            raise BuildToolError(f"AnyKernel3 license file record {member} is invalid")
        actual = package_tree.joinpath(*PurePosixPath(member).parts)
        _validate_lf_file(actual, role=f"AnyKernel3 license file {member}")
        if actual.stat().st_size != record["size"] or sha256_file(actual) != record["sha256"]:
            raise BuildToolError(f"AnyKernel3 license file {member} differs from provenance")
        license_files[member] = record
    if set(license_files) != set(ANYKERNEL_LICENSE_MEMBERS):
        raise BuildToolError("AnyKernel3 license file set differs from packaging policy")

    conveyance = _exact_object_keys(
        data.get("source_conveyance"),
        {"path", "size", "sha256"},
        where="AnyKernel3 source conveyance",
    )
    if conveyance.get("path") != ANYKERNEL_SOURCE_CONVEYANCE:
        raise BuildToolError("AnyKernel3 source conveyance member differs")
    conveyance_path = package_tree / ANYKERNEL_SOURCE_CONVEYANCE
    _validate_lf_file(conveyance_path, role="AnyKernel3 source conveyance")
    if (
        conveyance_path.stat().st_size != conveyance.get("size")
        or sha256_file(conveyance_path) != conveyance.get("sha256")
    ):
        raise BuildToolError("AnyKernel3 source conveyance differs from provenance")

    records = data.get("executables")
    if not isinstance(records, list):
        raise BuildToolError("AnyKernel3 executable provenance records are absent")
    expected: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        record = _exact_object_keys(
            record,
            {
                "path",
                "size",
                "sha256",
                "version",
                "license",
                "license_path",
                "elf",
                "source",
                "binary_origin",
                "reproducible_build",
            },
            where=f"AnyKernel3 executable provenance record {index}",
        )
        member = _safe_anykernel_member(record.get("path"), where="AnyKernel3 executable")
        if member in expected or member not in tool_contracts:
            raise BuildToolError("AnyKernel3 executable provenance contains an unknown path")
        contract = tool_contracts[member]
        for key in ("size", "sha256", "version", "license", "license_path"):
            if record.get(key) != contract.get(key):
                raise BuildToolError(f"AnyKernel3 executable {member} differs from its contract")
        license_record = license_files.get(str(record["license_path"]))
        if license_record is None or license_record.get("spdx") != record.get("license"):
            raise BuildToolError(f"AnyKernel3 executable {member} lacks its exact license text")
        if record.get("source") != contract.get("source"):
            raise BuildToolError(f"AnyKernel3 executable {member} source origin differs")
        source = _exact_object_keys(
            record.get("source"),
            {"repository", "commit", "relationship"},
            where=f"AnyKernel3 executable {member} source",
        )
        if (
            not str(source.get("repository", "")).startswith("https://")
            or not str(source.get("repository", "")).endswith(".git")
            or re.fullmatch(r"[0-9a-f]{40}", str(source.get("commit", ""))) is None
            or not isinstance(source.get("relationship"), str)
            or not source["relationship"]
        ):
            raise BuildToolError(f"AnyKernel3 executable {member} source origin is invalid")
        binary_origin = record.get("binary_origin")
        origin_keys = {"dependency", "archive_member", "asset_sha256"}
        if contract.get("upstream_build_input") is not None:
            origin_keys.add("upstream_build_input")
        origin = _exact_object_keys(
            binary_origin,
            origin_keys,
            where=f"AnyKernel3 executable {member} binary origin",
        )
        if (
            origin.get("dependency") != magisk_dependency.id
            or origin.get("archive_member") != contract.get("archive_member")
            or origin.get("asset_sha256") != release_identity["sha256"]
            or origin.get("upstream_build_input") != contract.get("upstream_build_input")
        ):
            raise BuildToolError(f"AnyKernel3 executable {member} binary origin differs")
        reproduction = _exact_object_keys(
            record.get("reproducible_build"),
            {"status", "note"},
            where=f"AnyKernel3 executable {member} reproducibility",
        )
        if (
            reproduction.get("status") != "official-release-match-byte-rebuild-not-verified"
            or not isinstance(reproduction.get("note"), str)
            or not reproduction["note"]
        ):
            raise BuildToolError(f"AnyKernel3 executable {member} reproducibility claim differs")
        expected[member] = record

    if set(expected) != set(tool_contracts):
        raise BuildToolError("AnyKernel3 executable provenance record set differs")

    actual: dict[str, Path] = {}
    for candidate in package_tree.rglob("*"):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if candidate.read_bytes()[:4] != b"\x7fELF":
            continue
        member = candidate.relative_to(package_tree).as_posix()
        actual[member] = candidate
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        opaque = sorted(set(actual) - set(expected))
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if opaque:
            details.append("opaque/unrecorded: " + ", ".join(opaque))
        raise BuildToolError(
            "AnyKernel3 executable payload differs from its provenance manifest"
            + (f" ({'; '.join(details)})" if details else "")
        )
    for member, candidate in actual.items():
        record = expected[member]
        data_bytes = candidate.read_bytes()
        if candidate.stat().st_size != record["size"] or sha256_bytes(data_bytes) != record["sha256"]:
            raise BuildToolError(
                f"AnyKernel3 executable {member} differs from its audited provenance record"
            )
        descriptor = _elf_descriptor(data_bytes, where=f"AnyKernel3 executable {member}")
        if record.get("elf") != descriptor:
            raise BuildToolError(f"AnyKernel3 executable {member} ELF identity differs")
    return data


def _prepare_anykernel_tree(
    *,
    root: Path,
    source: Path,
    magisk_asset: Path,
    destination: Path,
    anykernel_dependency: Any,
    magisk_dependency: Any,
    tool_contracts: Mapping[str, Mapping[str, Any]] = ANYKERNEL_TOOL_CONTRACTS,
    upstream_contracts: tuple[Mapping[str, Any], ...] = (
        ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS
    ),
) -> dict[str, Any]:
    if destination.exists():
        raise BuildToolError(f"packaging work directory already exists: {destination}")
    destination.mkdir(parents=True)
    for member in ANYKERNEL_UPSTREAM_MEMBERS:
        _copy_anykernel_member(source, destination, member, normalize_lf=True)

    overlay = root / "packaging" / "anykernel3"
    for member in (
        "anykernel.sh",
        ANYKERNEL_EXECUTABLE_PROVENANCE,
        ANYKERNEL_SOURCE_CONVEYANCE,
    ):
        _copy_anykernel_member(overlay, destination, member)
    for output_member, source_member in sorted(ANYKERNEL_LICENSE_MEMBERS.items()):
        _copy_anykernel_member(
            overlay,
            destination,
            source_member,
            output_member=output_member,
        )

    _extract_magisk_anykernel_tools(
        asset=magisk_asset,
        destination=destination,
        dependency=magisk_dependency,
        tool_contracts=tool_contracts,
    )

    _validate_anykernel_script(destination / "anykernel.sh")
    _validate_lf_file(
        destination / ANYKERNEL_EXECUTABLE_PROVENANCE,
        role="AnyKernel3 executable provenance manifest",
    )
    return _validate_anykernel_executable_provenance(
        destination / ANYKERNEL_EXECUTABLE_PROVENANCE,
        package_tree=destination,
        anykernel_dependency=anykernel_dependency,
        magisk_dependency=magisk_dependency,
        tool_contracts=tool_contracts,
        upstream_contracts=upstream_contracts,
    )


def _anykernel_tree_records(package_tree: Path) -> list[dict[str, Any]]:
    paths = sorted(
        package_tree.rglob("*"),
        key=lambda candidate: candidate.relative_to(package_tree).as_posix(),
    )
    if any(path.is_symlink() for path in paths):
        raise BuildToolError("AnyKernel3 package tree contains a symbolic link")
    files = [path for path in paths if path.is_file()]
    actual_members = {path.relative_to(package_tree).as_posix() for path in files}
    if actual_members != set(ANYKERNEL_ZIP_MODES):
        raise BuildToolError("AnyKernel3 package tree differs from its exact member policy")
    return [
        {
            "path": path.relative_to(package_tree).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "mode": ANYKERNEL_ZIP_MODES[path.relative_to(package_tree).as_posix()],
        }
        for path in files
    ]


def _verify_anykernel_zip(archive_path: Path, records: object) -> None:
    _verify_zip_file_manifest(archive_path, records, role="AnyKernel3")
    if not isinstance(records, list):
        raise BuildToolError("AnyKernel3 file manifest is absent")
    expected_modes = {
        str(record["path"]): int(record["mode"])
        for record in records
        if isinstance(record, dict) and "path" in record and "mode" in record
    }
    if expected_modes != dict(ANYKERNEL_ZIP_MODES):
        raise BuildToolError("AnyKernel3 file manifest mode map differs")
    actual_elfs: dict[str, dict[str, str]] = {}
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        for info in infos:
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if (
                info.create_system != 3
                or not stat.S_ISREG(unix_mode)
                or stat.S_IMODE(unix_mode) != expected_modes[info.filename]
            ):
                raise BuildToolError(
                    f"AnyKernel3 ZIP member mode differs from policy: {info.filename}"
                )
            data = archive.read(info)
            if data[:4] == b"\x7fELF":
                actual_elfs[info.filename] = _elf_descriptor(
                    data,
                    where=f"AnyKernel3 ZIP member {info.filename}",
                )
    if set(actual_elfs) != set(ANYKERNEL_TOOL_CONTRACTS):
        raise BuildToolError("AnyKernel3 ZIP ELF inventory differs from its arm64 policy")


def _validate_zip_asset(path: Path, *, role: str) -> None:
    if not path.is_file():
        raise BuildToolError(f"missing {role}: {path}")
    if not zipfile.is_zipfile(path):
        raise BuildToolError(f"{role} is not a ZIP archive: {path}")
    with zipfile.ZipFile(path, "r") as archive:
        for member in archive.infolist():
            normalized = member.filename.replace("\\", "/")
            parts = Path(normalized).parts
            if normalized.startswith("/") or ".." in parts:
                raise BuildToolError(f"{role} contains an unsafe member: {member.filename}")
        bad = archive.testzip()
        if bad is not None:
            raise BuildToolError(f"{role} contains a corrupt member: {bad}")


def _safe_wireless_member(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BuildToolError(f"{where} has an unsafe member path")
    member = PurePosixPath(value)
    if (
        member.is_absolute()
        or ".." in member.parts
        or member.as_posix() != value
        or value.endswith("/")
    ):
        raise BuildToolError(f"{where} has an unsafe member path")
    return value


def _wireless_source_identity(dependency: Any) -> dict[str, str]:
    repository = dependency.raw.get("repository")
    version = dependency.raw.get("version")
    license_classification = dependency.raw.get("license")
    if (
        dependency.id != "nethunter_wireless_firmware"
        or dependency.kind != "release_asset"
        or not isinstance(dependency.url, str)
        or not dependency.url
        or not isinstance(dependency.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", dependency.sha256) is None
        or not isinstance(repository, str)
        or not repository
        or not isinstance(dependency.ref, str)
        or not dependency.ref.startswith("refs/tags/")
        or not isinstance(dependency.commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", dependency.commit) is None
        or not isinstance(version, str)
        or not version
        or license_classification != WIRELESS_FIRMWARE_LOCK_LICENSE
    ):
        raise BuildToolError(
            "nethunter_wireless_firmware lacks an immutable release/source identity"
        )
    return {
        "dependency": dependency.id,
        "asset_uri": dependency.url,
        "asset_sha256": dependency.sha256,
        "repository": repository,
        "release_ref": dependency.ref,
        "release_commit": dependency.commit,
        "release_version": version,
        "license_classification": license_classification,
    }


def _wireless_policy_record(
    record: object,
    *,
    index: int,
    attribution: bool,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise BuildToolError(f"wireless firmware policy record {index} is invalid")
    member = _safe_wireless_member(
        record.get("path"),
        where=f"wireless firmware policy record {index}",
    )
    size = record.get("size")
    digest = record.get("sha256")
    family = record.get("family")
    kind = record.get("kind")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(family, str)
        or not family
        or kind not in {"firmware", "attribution"}
    ):
        raise BuildToolError(f"wireless firmware policy record {member} is incomplete")
    if attribution:
        expected_output = WIRELESS_FIRMWARE_UPSTREAM_ATTRIBUTION_OUTPUTS.get(member)
        if (
            kind != "attribution"
            or family != "attribution"
            or expected_output is None
            or record.get("output_path") != expected_output
            or not isinstance(record.get("license"), str)
            or not record["license"]
        ):
            raise BuildToolError(
                f"wireless firmware attribution record {member} is invalid"
            )
    elif (
        kind != "firmware"
        or family not in WIRELESS_FIRMWARE_FAMILIES
        or not member.startswith("system/etc/firmware/")
    ):
        raise BuildToolError(f"wireless firmware payload record {member} is invalid")
    return dict(record)


def _wireless_authoritative_source(value: object, *, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BuildToolError(f"{where} authoritative source is invalid")
    repository = value.get("repository")
    commit = value.get("commit")
    if (
        not isinstance(repository, str)
        or not repository.startswith("https://")
        or not repository.endswith(".git")
        or not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        raise BuildToolError(f"{where} authoritative source is invalid")
    return {
        "repository": repository,
        "commit": commit,
    }


def _wireless_rtw88_source_identity(dependency: Any) -> dict[str, str]:
    if (
        dependency.id != "rtw88"
        or dependency.kind != "git"
        or not isinstance(dependency.url, str)
        or not dependency.url.startswith("https://")
        or not dependency.url.endswith(".git")
        or not isinstance(dependency.commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", dependency.commit) is None
    ):
        raise BuildToolError("rtw88 lacks an immutable source identity")
    return {
        "repository": dependency.url,
        "commit": dependency.commit,
    }


def _wireless_authoritative_repository_path(member: str, *, family: str) -> str:
    prefix = "system/etc/firmware/"
    if not member.startswith(prefix):
        raise BuildToolError("wireless firmware member lacks its runtime prefix")
    relative = member[len(prefix) :]
    if member == "system/etc/firmware/mt7601u.bin":
        repository_path = "mediatek/mt7601u.bin"
    elif family == "rtw88":
        if not relative.startswith("rtw88/"):
            raise BuildToolError("RTW88 firmware member has an unexpected runtime path")
        repository_path = f"firmware/{PurePosixPath(relative).name}"
    else:
        repository_path = relative
    return _safe_wireless_member(
        repository_path,
        where=f"wireless firmware authoritative repository path for {member}",
    )


def _wireless_license_evidence(value: object, *, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise BuildToolError(f"{where} license evidence is absent")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise BuildToolError(f"{where} license evidence {index} is invalid")
        uri = item.get("uri")
        digest = item.get("sha256")
        commit = item.get("source_commit")
        size = item.get("size")
        packaged_path = item.get("packaged_path")
        repository_path = item.get("repository_path")
        repository_encoding = item.get("repository_encoding")
        if (
            not isinstance(uri, str)
            or not uri.startswith("https://")
            or uri in seen
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", commit) is None
            or commit not in uri
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or not isinstance(packaged_path, str)
            or not packaged_path.startswith(f"{WIRELESS_FIRMWARE_LICENSE_DIRECTORY}/")
            or _safe_wireless_member(
                packaged_path,
                where=f"{where} license evidence {index} packaged path",
            )
            != packaged_path
            or not isinstance(repository_path, str)
            or not repository_path.startswith("packaging/wireless-firmware/licenses/")
            or _safe_wireless_member(
                repository_path,
                where=f"{where} license evidence {index} repository path",
            )
            != repository_path
            or repository_encoding not in {"identity", "base64"}
        ):
            raise BuildToolError(f"{where} license evidence {index} is invalid")
        seen.add(uri)
        result.append(
            {
                "uri": uri,
                "sha256": digest,
                "source_commit": commit,
                "size": size,
                "packaged_path": packaged_path,
                "repository_path": repository_path,
                "repository_encoding": repository_encoding,
            }
        )
    return result


def _wireless_license_payload(
    *,
    root: Path,
    evidence: Mapping[str, Any],
) -> bytes:
    repository_path = root.joinpath(*PurePosixPath(str(evidence["repository_path"])).parts)
    try:
        repository_path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise BuildToolError("wireless firmware license source escapes the repository") from exc
    if repository_path.is_symlink() or not repository_path.is_file():
        raise BuildToolError(
            f"repository-owned wireless firmware license is missing: {evidence['repository_path']}"
        )
    transport = repository_path.read_bytes()
    if b"\r" in transport:
        raise BuildToolError(
            f"wireless firmware license transport must use LF: {evidence['repository_path']}"
        )
    encoding = evidence["repository_encoding"]
    if encoding == "identity":
        try:
            transport.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuildToolError(
                f"wireless firmware license text is not UTF-8: {evidence['repository_path']}"
            ) from exc
        payload = transport
    else:
        try:
            payload = base64.b64decode(b"".join(transport.split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BuildToolError(
                f"wireless firmware license base64 is invalid: {evidence['repository_path']}"
            ) from exc
    if len(payload) != evidence["size"] or sha256_bytes(payload) != evidence["sha256"]:
        raise BuildToolError(
            f"wireless firmware license differs from policy: {evidence['repository_path']}"
        )
    if payload[:4] == b"\x7fELF":
        raise BuildToolError("wireless firmware license payload is unexpectedly ELF")
    return payload


def _wireless_known_gaps(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise BuildToolError("wireless firmware known-gap policy is absent")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_gap in enumerate(value):
        gap = _exact_object_keys(
            raw_gap,
            {"id", "classification", "reason"},
            where=f"wireless firmware known gap {index}",
        )
        gap_id = gap.get("id")
        classification = gap.get("classification")
        reason = gap.get("reason")
        if (
            not isinstance(gap_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]*", gap_id) is None
            or gap_id in seen
            or not isinstance(classification, str)
            or not classification
            or not isinstance(reason, str)
            or not reason
        ):
            raise BuildToolError(f"wireless firmware known gap {index} is invalid")
        seen.add(gap_id)
        result.append(
            {
                "id": gap_id,
                "classification": classification,
                "reason": reason,
            }
        )
    missing = sorted(WIRELESS_FIRMWARE_REQUIRED_GAP_IDS - seen)
    if missing:
        raise BuildToolError("wireless firmware known-gap policy omits " + ", ".join(missing))
    return result


def _load_wireless_firmware_policy(
    *,
    root: Path,
    dependency: Any,
    rtw88_dependency: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    policy_path = root / WIRELESS_FIRMWARE_POLICY
    try:
        policy_path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise BuildToolError("wireless firmware policy escapes the repository") from exc
    if policy_path.is_symlink() or not policy_path.is_file():
        raise BuildToolError("repository-owned wireless firmware policy is missing")
    try:
        policy = strict_json_loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BuildToolError("wireless firmware policy is invalid") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise BuildToolError("wireless firmware policy schema is unsupported")

    source_identity = _wireless_source_identity(dependency)
    rtw88_source_identity = _wireless_rtw88_source_identity(rtw88_dependency)
    if policy.get("source") != source_identity:
        raise BuildToolError(
            "wireless firmware policy source differs from dependencies/lock.yml"
        )
    source_member_count = policy.get("source_member_count")
    retained_member_count = policy.get("retained_member_count")
    if (
        not isinstance(source_member_count, int)
        or isinstance(source_member_count, bool)
        or source_member_count < 1
        or not isinstance(retained_member_count, int)
        or isinstance(retained_member_count, bool)
        or retained_member_count < 1
        or retained_member_count > source_member_count
    ):
        raise BuildToolError("wireless firmware policy member counts are invalid")

    classifications = policy.get("classifications")
    expected_classifications = {
        "attribution": {
            "provenance_status": "audited-upstream-text-at-pinned-source-commit",
            "reproducible_source": True,
        },
        "firmware": {
            "license": WIRELESS_FIRMWARE_LICENSE,
            "provenance_status": WIRELESS_FIRMWARE_PROVENANCE_STATUS,
            "reproducible_source": False,
        },
    }
    if classifications != expected_classifications:
        raise BuildToolError("wireless firmware policy classifications differ")

    raw_records = policy.get("members")
    if not isinstance(raw_records, list):
        raise BuildToolError("wireless firmware policy member records are absent")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    family_counts: Counter[str] = Counter()
    for index, raw_record in enumerate(raw_records):
        is_attribution = (
            isinstance(raw_record, dict) and raw_record.get("kind") == "attribution"
        )
        record = _wireless_policy_record(
            raw_record,
            index=index,
            attribution=is_attribution,
        )
        member = str(record["path"])
        if member in seen:
            raise BuildToolError(f"wireless firmware policy repeats {member}")
        seen.add(member)
        family_counts[str(record["family"])] += 1
        records.append(record)
    output_members = [
        str(record.get("output_path", record["path"])) for record in records
    ]
    if len(output_members) != len(set(output_members)):
        raise BuildToolError("wireless firmware policy repeats an output member")
    if len(records) != retained_member_count:
        raise BuildToolError(
            "wireless firmware policy retained count differs from its member records"
        )
    expected_family_counts = {
        key: value for key, value in sorted(family_counts.items())
    }
    if policy.get("family_counts") != expected_family_counts:
        raise BuildToolError("wireless firmware policy family counts differ")

    raw_family_provenance = policy.get("family_provenance")
    expected_families = set(expected_family_counts) - {"attribution"}
    if (
        not isinstance(raw_family_provenance, dict)
        or set(raw_family_provenance) != expected_families
    ):
        raise BuildToolError("wireless firmware family provenance differs")
    record_by_path = {str(record["path"]): record for record in records}
    family_provenance: dict[str, dict[str, Any]] = {}
    for family in sorted(expected_families):
        raw_family = raw_family_provenance[family]
        if not isinstance(raw_family, dict):
            raise BuildToolError(
                f"wireless firmware family provenance {family} is invalid"
            )
        license_value = raw_family.get("license")
        status = raw_family.get("provenance_status")
        reproducible = raw_family.get("reproducible_source")
        if (
            not isinstance(license_value, str)
            or not license_value
            or not isinstance(status, str)
            or not status
            or not isinstance(reproducible, bool)
        ):
            raise BuildToolError(
                f"wireless firmware family provenance {family} is incomplete"
            )
        source = _wireless_authoritative_source(
            raw_family.get("source"),
            where=f"wireless firmware family {family}",
        )
        if family == "rtw88" and source != rtw88_source_identity:
            raise BuildToolError(
                "wireless firmware RTW88 source differs from dependencies/lock.yml"
            )
        license_evidence = _wireless_license_evidence(
            raw_family.get("license_evidence"),
            where=f"wireless firmware family {family}",
        )
        raw_overrides = raw_family.get("source_overrides", {})
        if not isinstance(raw_overrides, dict):
            raise BuildToolError(
                f"wireless firmware family {family} source overrides are invalid"
            )
        overrides: dict[str, dict[str, str]] = {}
        for member, raw_source in sorted(raw_overrides.items()):
            member = _safe_wireless_member(
                member,
                where=f"wireless firmware family {family} source override",
            )
            record = record_by_path.get(member)
            if record is None or record.get("family") != family:
                raise BuildToolError(
                    f"wireless firmware family {family} overrides an unknown member"
                )
            overrides[member] = _wireless_authoritative_source(
                raw_source,
                where=f"wireless firmware family {family} source override {member}",
            )
        family_provenance[family] = {
            "license": license_value,
            "provenance_status": status,
            "reproducible_source": reproducible,
            "source": source,
            "license_evidence": license_evidence,
            "source_overrides": overrides,
        }

    license_payloads: dict[str, dict[str, Any]] = {}
    for family in sorted(family_provenance):
        for evidence in family_provenance[family]["license_evidence"]:
            packaged_path = str(evidence["packaged_path"])
            payload = _wireless_license_payload(root=root, evidence=evidence)
            existing = license_payloads.get(packaged_path)
            identity = {
                "evidence": evidence,
                "payload": payload,
            }
            if existing is not None:
                if existing != identity:
                    raise BuildToolError(
                        f"wireless firmware license path {packaged_path} has conflicting evidence"
                    )
                continue
            if packaged_path in output_members:
                raise BuildToolError(
                    f"wireless firmware license path collides with retained member {packaged_path}"
                )
            license_payloads[packaged_path] = identity

    raw_excluded_elf = policy.get("known_excluded_elf_members")
    if not isinstance(raw_excluded_elf, list):
        raise BuildToolError("wireless firmware excluded-ELF records are absent")
    excluded_elf: list[dict[str, Any]] = []
    excluded_paths: set[str] = set()
    for index, raw_record in enumerate(raw_excluded_elf):
        if not isinstance(raw_record, dict):
            raise BuildToolError(
                f"wireless firmware excluded-ELF record {index} is invalid"
            )
        member = _safe_wireless_member(
            raw_record.get("path"),
            where=f"wireless firmware excluded-ELF record {index}",
        )
        size = raw_record.get("size")
        digest = raw_record.get("sha256")
        reason = raw_record.get("reason")
        if (
            member in seen
            or member in excluded_paths
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 4
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(reason, str)
            or not reason
        ):
            raise BuildToolError(
                f"wireless firmware excluded-ELF record {member} is incomplete"
            )
        excluded_paths.add(member)
        excluded_elf.append(dict(raw_record))

    raw_aliases = policy.get("generated_aliases")
    if not isinstance(raw_aliases, list):
        raise BuildToolError("wireless firmware generated-alias records are absent")
    aliases: list[dict[str, Any]] = []
    alias_paths: set[str] = set()
    for index, raw_alias in enumerate(raw_aliases):
        if not isinstance(raw_alias, dict):
            raise BuildToolError(
                f"wireless firmware generated-alias record {index} is invalid"
            )
        source_member = _safe_wireless_member(
            raw_alias.get("source_path"),
            where=f"wireless firmware generated-alias record {index}",
        )
        output_member = _safe_wireless_member(
            raw_alias.get("path"),
            where=f"wireless firmware generated-alias record {index}",
        )
        source_record = record_by_path.get(source_member)
        size = raw_alias.get("size")
        digest = raw_alias.get("sha256")
        reason = raw_alias.get("reason")
        if (
            source_record is None
            or source_record.get("kind") != "firmware"
            or source_record.get("family") != "mt76"
            or output_member in seen
            or output_member in alias_paths
            or not output_member.startswith("system/etc/firmware/")
            or size != source_record.get("size")
            or digest != source_record.get("sha256")
            or not isinstance(reason, str)
            or not reason
        ):
            raise BuildToolError(
                f"wireless firmware generated-alias record {output_member} is invalid"
            )
        alias_paths.add(output_member)
        aliases.append(dict(raw_alias))

    final_members: dict[str, str] = {}
    final_member_groups = (
        ("retained output", output_members),
        ("generated alias", sorted(alias_paths)),
        ("license evidence", sorted(license_payloads)),
        (
            "curation metadata",
            [
                WIRELESS_FIRMWARE_README,
                WIRELESS_FIRMWARE_WHENCE,
                WIRELESS_FIRMWARE_PROVENANCE,
            ],
        ),
    )
    for role, members in final_member_groups:
        for member in members:
            existing = final_members.get(member)
            if existing is not None:
                raise BuildToolError(
                    f"wireless firmware final member {member} collides between "
                    f"{existing} and {role}"
                )
            final_members[member] = role
    result = dict(policy)
    result["family_provenance"] = family_provenance
    result["known_excluded_elf_members"] = excluded_elf
    result["generated_aliases"] = sorted(
        aliases,
        key=lambda record: str(record["path"]),
    )
    result["known_gaps"] = _wireless_known_gaps(policy.get("known_gaps"))
    result["_license_payloads"] = license_payloads
    return result, sorted(records, key=lambda record: str(record["path"]))


def _wireless_archive_files(
    archive: zipfile.ZipFile,
    *,
    role: str,
) -> dict[str, zipfile.ZipInfo]:
    files: dict[str, zipfile.ZipInfo] = {}
    names: set[str] = set()
    for info in archive.infolist():
        value = info.filename
        if "\\" in value or "\x00" in value:
            raise BuildToolError(f"{role} contains an unsafe member: {value}")
        normalized = PurePosixPath(value)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise BuildToolError(f"{role} contains an unsafe member: {value}")
        if value in names:
            raise BuildToolError(f"{role} repeats ZIP member {value}")
        names.add(value)
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if info.create_system == 3 and stat.S_ISLNK(unix_mode):
            raise BuildToolError(f"{role} contains a symlink: {value}")
        if info.flag_bits & 0x1:
            raise BuildToolError(f"{role} contains an encrypted member: {value}")
        if info.is_dir():
            continue
        member = _safe_wireless_member(value, where=role)
        files[member] = info
    return files


def _curate_wireless_firmware_tree(
    *,
    root: Path,
    source: Path,
    destination: Path,
    dependency: Any,
    rtw88_dependency: Any,
) -> dict[str, Any]:
    if destination.exists():
        raise BuildToolError(
            f"wireless firmware packaging work directory already exists: {destination}"
        )
    _validate_zip_asset(source, role="pinned wireless firmware bundle")
    if sha256_file(source) != dependency.sha256:
        raise BuildToolError("wireless firmware digest differs from dependencies/lock.yml")
    policy, records = _load_wireless_firmware_policy(
        root=root,
        dependency=dependency,
        rtw88_dependency=rtw88_dependency,
    )
    destination.mkdir(parents=True)

    selected = {str(record["path"]): record for record in records}
    selected_bytes: dict[str, bytes] = {}
    actual_elf: dict[str, tuple[int, str]] = {}
    with zipfile.ZipFile(source, "r") as archive:
        files = _wireless_archive_files(
            archive,
            role="pinned wireless firmware bundle",
        )
        if len(files) != policy["source_member_count"]:
            raise BuildToolError(
                "pinned wireless firmware source member count differs from policy"
            )
        missing = sorted(set(selected) - set(files))
        if missing:
            raise BuildToolError(
                "pinned wireless firmware bundle lacks audited members: "
                + ", ".join(missing)
            )
        for member, info in sorted(files.items()):
            data = archive.read(info)
            if len(data) != info.file_size:
                raise BuildToolError(
                    f"pinned wireless firmware member {member} has an invalid size"
                )
            if data[:4] == b"\x7fELF":
                actual_elf[member] = (len(data), sha256_bytes(data))
            if member not in selected:
                continue
            record = selected[member]
            if (
                len(data) != record["size"]
                or sha256_bytes(data) != record["sha256"]
            ):
                raise BuildToolError(
                    f"pinned wireless firmware member {member} differs from policy"
                )
            if data[:4] == b"\x7fELF":
                raise BuildToolError(
                    f"wireless firmware allowlist selects forbidden ELF member {member}"
                )
            selected_bytes[member] = data

    expected_elf = {
        str(record["path"]): (int(record["size"]), str(record["sha256"]))
        for record in policy["known_excluded_elf_members"]
    }
    if actual_elf != expected_elf:
        missing = sorted(set(expected_elf) - set(actual_elf))
        undeclared = sorted(set(actual_elf) - set(expected_elf))
        changed = sorted(
            member
            for member in set(actual_elf) & set(expected_elf)
            if actual_elf[member] != expected_elf[member]
        )
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if undeclared:
            details.append("undeclared: " + ", ".join(undeclared))
        if changed:
            details.append("changed: " + ", ".join(changed))
        raise BuildToolError(
            "pinned wireless firmware ELF inventory differs from policy"
            + (f" ({'; '.join(details)})" if details else "")
        )

    source_identity = _wireless_source_identity(dependency)
    classifications = policy["classifications"]
    expanded_records: list[dict[str, Any]] = []
    for member, record in sorted(selected.items()):
        output_member = str(record.get("output_path", member))
        destination_path = destination.joinpath(*PurePosixPath(output_member).parts)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(selected_bytes[member])
        destination_path.chmod(0o644)
        classification = classifications[str(record["kind"])]
        expanded_record = {
            "path": output_member,
            "source_path": member,
            "size": record["size"],
            "sha256": record["sha256"],
            "kind": record["kind"],
            "family": record["family"],
            "source": source_identity,
        }
        if record["kind"] == "attribution":
            expanded_record.update(
                {
                    "license": record["license"],
                    "provenance_status": classification["provenance_status"],
                    "reproducible_source": classification["reproducible_source"],
                }
            )
        else:
            family_provenance = policy["family_provenance"][record["family"]]
            authoritative_source = family_provenance["source_overrides"].get(
                member,
                family_provenance["source"],
            )
            authoritative_source = {
                **authoritative_source,
                "path": _wireless_authoritative_repository_path(
                    member,
                    family=str(record["family"]),
                ),
            }
            expanded_record.update(
                {
                    "license": family_provenance["license"],
                    "license_evidence": family_provenance["license_evidence"],
                    "provenance_status": family_provenance["provenance_status"],
                    "reproducible_source": family_provenance[
                        "reproducible_source"
                    ],
                    "authoritative_source": authoritative_source,
                }
            )
        expanded_records.append(expanded_record)

    generated_alias_records: list[dict[str, Any]] = []
    mt76_provenance = policy["family_provenance"]["mt76"]
    for alias in policy["generated_aliases"]:
        member = str(alias["path"])
        source_member = str(alias["source_path"])
        destination_path = destination.joinpath(*PurePosixPath(member).parts)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(selected_bytes[source_member])
        destination_path.chmod(0o644)
        generated_alias_records.append(
            {
                "path": member,
                "source_path": source_member,
                "size": alias["size"],
                "sha256": alias["sha256"],
                "kind": "generated-alias",
                "family": "mt76",
                "license": mt76_provenance["license"],
                "license_evidence": mt76_provenance["license_evidence"],
                "provenance_status": mt76_provenance["provenance_status"],
                "reproducible_source": mt76_provenance["reproducible_source"],
                "authoritative_source": {
                    **mt76_provenance["source"],
                    "path": _wireless_authoritative_repository_path(
                        source_member,
                        family="mt76",
                    ),
                },
                "reason": alias["reason"],
                "source": source_identity,
            }
        )

    license_text_records: list[dict[str, Any]] = []
    for member, license_record in sorted(policy["_license_payloads"].items()):
        destination_path = destination.joinpath(*PurePosixPath(member).parts)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        payload = license_record["payload"]
        destination_path.write_bytes(payload)
        destination_path.chmod(0o644)
        evidence = license_record["evidence"]
        license_text_records.append(
            {
                "path": member,
                "size": len(payload),
                "sha256": sha256_bytes(payload),
                "kind": "license-text",
                "source": {
                    "uri": evidence["uri"],
                    "commit": evidence["source_commit"],
                    "repository_path": evidence["repository_path"],
                    "repository_encoding": evidence["repository_encoding"],
                },
            }
        )

    curation_readme_source = root / WIRELESS_FIRMWARE_CURATION_README
    if curation_readme_source.is_symlink() or not curation_readme_source.is_file():
        raise BuildToolError("repository-owned wireless firmware curation README is missing")
    curation_readme = destination / WIRELESS_FIRMWARE_README
    _validate_lf_file(curation_readme_source, role="wireless firmware curation README")
    shutil.copy2(curation_readme_source, curation_readme)
    curation_readme.chmod(0o644)

    whence_source = root / WIRELESS_FIRMWARE_WHENCE_SOURCE
    if whence_source.is_symlink() or not whence_source.is_file():
        raise BuildToolError("repository-owned wireless firmware WHENCE is missing")
    whence_destination = destination / WIRELESS_FIRMWARE_WHENCE
    _validate_lf_file(whence_source, role="wireless firmware WHENCE")
    shutil.copy2(whence_source, whence_destination)
    whence_destination.chmod(0o644)
    curated_attribution_records = [
        {
            "path": WIRELESS_FIRMWARE_README,
            "size": curation_readme.stat().st_size,
            "sha256": sha256_file(curation_readme),
            "kind": "curated-attribution",
            "source": {
                "repository_path": WIRELESS_FIRMWARE_CURATION_README,
            },
        },
        {
            "path": WIRELESS_FIRMWARE_WHENCE,
            "size": whence_destination.stat().st_size,
            "sha256": sha256_file(whence_destination),
            "kind": "curated-attribution",
            "source": {
                "repository_path": WIRELESS_FIRMWARE_WHENCE_SOURCE,
            },
        }
    ] + license_text_records

    policy_path = root / WIRELESS_FIRMWARE_POLICY
    excluded_count = policy["source_member_count"] - len(expanded_records)
    provenance = {
        "schema_version": 1,
        "source": source_identity,
        "policy": {
            "path": WIRELESS_FIRMWARE_POLICY,
            "sha256": sha256_file(policy_path),
        },
        "source_member_count": policy["source_member_count"],
        "retained_upstream_member_count": len(expanded_records),
        "excluded_source_member_count": excluded_count,
        "firmware_member_count": sum(
            1 for record in expanded_records if record["kind"] == "firmware"
        ),
        "generated_alias_count": len(generated_alias_records),
        "attribution_member_count": sum(
            1 for record in expanded_records if record["kind"] == "attribution"
        ),
        "family_counts": policy["family_counts"],
        "members": expanded_records,
        "generated_aliases": generated_alias_records,
        "curated_attribution_members": curated_attribution_records,
        "known_excluded_elf_members": policy["known_excluded_elf_members"],
        "known_gaps": policy["known_gaps"],
    }
    provenance_path = destination / WIRELESS_FIRMWARE_PROVENANCE
    atomic_write_json(provenance_path, provenance)
    provenance_path.chmod(0o644)

    output_records = [
        {
            "path": path.relative_to(destination).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(
            (candidate for candidate in destination.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(destination).as_posix(),
        )
    ]
    provenance_record = next(
        record
        for record in output_records
        if record["path"] == WIRELESS_FIRMWARE_PROVENANCE
    )
    return {
        "output_records": output_records,
        "policy_path": WIRELESS_FIRMWARE_POLICY,
        "policy_sha256": sha256_file(policy_path),
        "provenance_member": WIRELESS_FIRMWARE_PROVENANCE,
        "provenance_sha256": provenance_record["sha256"],
        "source_member_count": policy["source_member_count"],
        "retained_upstream_member_count": len(expanded_records),
        "excluded_source_member_count": excluded_count,
        "firmware_member_count": provenance["firmware_member_count"],
        "generated_alias_count": provenance["generated_alias_count"],
        "attribution_member_count": provenance["attribution_member_count"],
        "curated_attribution_member_count": len(curated_attribution_records),
        "license_text_member_count": len(license_text_records),
        "known_gap_count": len(policy["known_gaps"]),
        "lock_license_classification": source_identity["license_classification"],
        "output_member_count": len(output_records),
        "family_counts": policy["family_counts"],
        "elf_member_count": 0,
    }


def _verify_curated_wireless_firmware_zip(
    archive_path: Path,
    records: object,
) -> None:
    _verify_zip_file_manifest(
        archive_path,
        records,
        role="curated wireless firmware",
    )
    forbidden_fragments = (
        "system/xbin/",
        "/scp.img",
        "/vpu_",
        "/sof/",
        "/sof-tplg/",
    )
    with zipfile.ZipFile(archive_path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if any(fragment in info.filename for fragment in forbidden_fragments):
                raise BuildToolError(
                    f"curated wireless firmware contains forbidden member {info.filename}"
                )
            data = archive.read(info)
            if data[:4] == b"\x7fELF":
                raise BuildToolError(
                    f"curated wireless firmware contains forbidden ELF {info.filename}"
                )
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if (
                info.create_system != 3
                or not stat.S_ISREG(unix_mode)
                or stat.S_IMODE(unix_mode) != 0o644
            ):
                raise BuildToolError(
                    f"curated wireless firmware member mode is not 0644: {info.filename}"
                )


def _write_checksums(output_dir: Path) -> Path:
    checksum_path = output_dir / "SHA256SUMS"
    files = sorted(
        (
            path
            for path in output_dir.iterdir()
            if path.is_file() and path.name != checksum_path.name
        ),
        key=lambda path: path.name.encode("utf-8", "strict"),
    )
    lines = [f"{sha256_file(path)}  {path.name}" for path in files]
    if not lines:
        raise BuildToolError("no artifacts were produced")
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return checksum_path


def _orchestrator_identity() -> dict[str, Any]:
    repository = os.environ.get("GITHUB_REPOSITORY", "Hipuu/OnePlus13-KernelBuilder").strip()
    revision = os.environ.get("GITHUB_SHA", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise BuildToolError("GITHUB_REPOSITORY has an invalid repository name")
    if revision and not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise BuildToolError("GITHUB_SHA must be a full commit SHA when set")
    if run_id and not run_id.isdigit():
        raise BuildToolError("GITHUB_RUN_ID must be numeric when set")
    return {
        "repository": repository,
        "revision": revision.lower() or None,
        "workflow_run_id": run_id or None,
    }


def _portable_repository_path(path: Path, root: Path, *, role: str) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise BuildToolError(f"{role} is outside the repository root: {path}") from exc


def _portable_provenance_string(value: str, workspace_root: Path) -> str:
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return value
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if not (Path(value).is_absolute() or windows_path.is_absolute() or posix_path.is_absolute()):
        return value
    candidate = Path(value)
    try:
        relative = candidate.resolve().relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise BuildToolError(
            f"packaged provenance path escapes the workspace: {value}"
        ) from exc
    portable = relative.as_posix()
    return portable or "."


PROVENANCE_PATH_FIELDS = frozenset(
    {
        "argv",
        "cwd",
        "destination",
        "expected_outputs",
        "fragments",
        "kernel_kit",
        "path",
        "source",
        "staging",
        "target",
    }
)


def _provenance_path_field(field: str | None) -> bool:
    return bool(
        field
        and (
            field in PROVENANCE_PATH_FIELDS
            or field.endswith("_path")
            or field.endswith("_paths")
            or field.endswith("_dir")
        )
    )


def _portable_provenance_document(
    value: Any,
    workspace_root: Path,
    *,
    field: str | None = None,
) -> Any:
    """Remove machine-specific workspace prefixes from packaged lineage."""

    if isinstance(value, dict):
        return {
            str(key): _portable_provenance_document(
                item,
                workspace_root,
                field=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _portable_provenance_document(item, workspace_root, field=field)
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _portable_provenance_document(item, workspace_root, field=field)
            for item in value
        ]
    if isinstance(value, str):
        if _provenance_path_field(field):
            return _portable_provenance_string(value, workspace_root)
        # Convert a workspace-absolute value even when a newly added record
        # field has not yet been classified as path-bearing. Absolute strings
        # outside the workspace are rejected only for declared path fields so
        # values such as Kconfig strings and shell snippets remain data.
        try:
            return _portable_provenance_string(value, workspace_root)
        except BuildToolError:
            return value
    return value


def _dependency_inventory(lock: DependencyLock) -> list[dict[str, Any]]:
    """Return the immutable dependency identity recorded in release metadata."""

    records: list[dict[str, Any]] = []
    for dependency_id, dependency in sorted(lock.dependencies.items()):
        record: dict[str, Any] = {
            "id": dependency_id,
            "kind": dependency.kind,
            "required_for": sorted(dependency.required_for),
        }
        if dependency.ref is not None:
            record["ref"] = dependency.ref
        version = dependency.raw.get("version")
        if isinstance(version, str) and version:
            record["version"] = version
        if dependency.kind == "git":
            record["source"] = {
                "uri": dependency.url,
                "commit": dependency.commit,
            }
        else:
            record["resource"] = {
                "uri": dependency.url,
                "sha256": dependency.sha256,
            }
            source_uri = dependency.raw.get("repository") or dependency.raw.get("repo_url")
            source_commit = dependency.commit or dependency.raw.get("repo_commit")
            if source_uri is not None:
                if not isinstance(source_uri, str) or not isinstance(source_commit, str):
                    raise BuildToolError(
                        f"dependency {dependency_id} has incomplete source provenance"
                    )
                record["source"] = {
                    "uri": source_uri,
                    "commit": source_commit,
                }
        records.append(record)
    return records


def package_build(
    *,
    root: Path,
    input_dir: Path,
    output_dir: Path,
    cache_root: Path,
    profile: Profile,
    feature: FeatureProfile,
    lock: DependencyLock,
    root_variant: str,
    build_target: str,
    debug: bool,
    pre_release: bool,
    smoke: bool,
) -> list[dict[str, Any]]:
    verify_build_output(
        output_dir=input_dir,
        profile=profile,
        feature=feature,
        lock=lock,
        root_variant=root_variant,
        build_target=build_target,
        smoke=smoke,
    )
    context_path = input_dir / ".op13" / "build-context.json"
    context = load_context(context_path)
    wireless_led_kmi_required = False
    if not smoke:
        wireless_led_kmi_required = wireless_led_exports_required(
            context.get("features"),
            feature_profile=feature.id,
        )
    kernel = context["kernel"]
    branding = str(kernel["branding"])
    epoch = int(kernel["source_date_epoch"])
    suffix = "-SMOKE" if smoke else ("-prerelease" if pre_release else "")
    base_name = f"OnePlus13-{profile.id}-{feature.id}-{root_variant}-{branding}{suffix}"
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise BuildToolError(f"package output directory must be empty: {output_dir}")
    records: list[dict[str, Any]] = []
    packaged_build_evidence: dict[str, Any] | None = None
    if not smoke:
        packaged_build_evidence = copy_preserved_build_evidence(
            input_dir=input_dir,
            destination=output_dir,
            evidence_value=context.get("build_evidence"),
            base=profile.id,
            resolved_manifest_sha256=str(context["manifest"]["sha256"]),
            wireless_led_exports_required=wireless_led_kmi_required,
        )
        evidence_files = [
            (TOOLCHAIN_NAME, "build-toolchain-provenance"),
            (KMI_NAME, "kmi-symbol-exports"),
        ]
        if wireless_led_kmi_required:
            evidence_files.append(
                (
                    WIRELESS_LED_KMI_NAME,
                    "wireless-led-kmi-symbol-exports",
                )
            )
        records.extend(
            record_for_file(output_dir / name, role=role)
            for name, role in evidence_files
        )
    image_destination = output_dir / f"{base_name}-Image"
    shutil.copy2(input_dir / "Image", image_destination)
    records.append(record_for_file(image_destination, role="kernel-image"))
    with tempfile.TemporaryDirectory(prefix="op13-package-", dir=output_dir.parent) as temporary_name:
        temporary = Path(temporary_name)
        anykernel_work = temporary / "anykernel3"
        if smoke:
            anykernel_work.mkdir()
            (anykernel_work / "anykernel.sh").write_text(
                "#!/sbin/sh\n# SMOKE ONLY\ndevice.name1=dodge\n", encoding="utf-8", newline="\n"
            )
            anykernel_provenance: dict[str, Any] | None = None
        else:
            state = fetch_dependencies(
                lock,
                cache_root,
                selected=(
                    "anykernel3",
                    "magisk_release_apk",
                ),
                dry_run=False,
                offline=False,
            )
            source_state = _fetch_anykernel_corresponding_source_dependencies(
                lock,
                cache_root,
                offline=False,
            )
            state["dependencies"].update(source_state["dependencies"])
            anykernel_dependency = lock.dependencies["anykernel3"]
            magisk_dependency = lock.dependencies["magisk_release_apk"]
            anykernel_source = Path(str(state["dependencies"]["anykernel3"]["path"]))
            magisk_asset = Path(str(state["dependencies"]["magisk_release_apk"]["path"]))
            anykernel_provenance = _prepare_anykernel_tree(
                root=root,
                source=anykernel_source,
                magisk_asset=magisk_asset,
                destination=anykernel_work,
                anykernel_dependency=anykernel_dependency,
                magisk_dependency=magisk_dependency,
            )
        shutil.copy2(input_dir / "Image", anykernel_work / "Image")
        anykernel_zip = output_dir / f"{base_name}-AnyKernel3.zip"
        if smoke:
            smoke_modes = {"anykernel.sh": 0o755, "Image": 0o644}
            deterministic_zip(
                anykernel_work,
                anykernel_zip,
                epoch=epoch,
                member_modes=smoke_modes,
            )
            anykernel_record = record_for_file(anykernel_zip, role="anykernel3-zip")
            anykernel_record["smoke_placeholder"] = True
        else:
            anykernel_files = _anykernel_tree_records(anykernel_work)
            deterministic_zip(
                anykernel_work,
                anykernel_zip,
                epoch=epoch,
                member_modes=ANYKERNEL_ZIP_MODES,
            )
            _verify_anykernel_zip(anykernel_zip, anykernel_files)
            if anykernel_provenance is None:
                raise BuildToolError("AnyKernel3 executable provenance was not sealed")
            provenance_path = anykernel_work / ANYKERNEL_EXECUTABLE_PROVENANCE
            anykernel_record = record_for_file(anykernel_zip, role="anykernel3-zip")
            anykernel_record.update(
                {
                    "dependencies": ["anykernel3", "magisk_release_apk"],
                    "member_count": len(anykernel_files),
                    "member_mode_policy": "explicit-host-independent",
                    "elf_class": "ELFCLASS64",
                    "elf_machine": "EM_AARCH64",
                    "executable_provenance_member": ANYKERNEL_EXECUTABLE_PROVENANCE,
                    "executable_provenance_sha256": sha256_file(provenance_path),
                    "magisk_release_sha256": anykernel_provenance["release_asset"]["sha256"],
                }
            )
        records.append(anykernel_record)
        if not smoke:
            corresponding_source_work = temporary / "anykernel-corresponding-source"
            corresponding_source = _prepare_anykernel_corresponding_source_tree(
                root=root,
                destination=corresponding_source_work,
                state=state,
                lock=lock,
                magisk_dependency=magisk_dependency,
            )
            corresponding_source_zip = (
                output_dir / f"{base_name}-corresponding-source.zip"
            )
            deterministic_zip(
                corresponding_source_work,
                corresponding_source_zip,
                epoch=epoch,
                member_modes={
                    record["path"]: 0o644
                    for record in corresponding_source["output_records"]
                },
                compression=zipfile.ZIP_STORED,
            )
            _verify_zip_file_manifest(
                corresponding_source_zip,
                corresponding_source["output_records"],
                role="AnyKernel corresponding source",
            )
            corresponding_source_record = record_for_file(
                corresponding_source_zip,
                role="corresponding-source",
            )
            corresponding_source_record.update(
                {
                    "dependencies": corresponding_source[
                        "archive_dependencies"
                    ],
                    "archive_count": corresponding_source["archive_count"],
                    "member_count": len(corresponding_source["output_records"]),
                    "member_mode_policy": "all-regular-0644",
                    "source_manifest_member": corresponding_source[
                        "manifest_member"
                    ],
                    "source_manifest_sha256": corresponding_source[
                        "manifest_sha256"
                    ],
                    "source_policy_member": corresponding_source["policy_member"],
                    "source_policy_sha256": corresponding_source["policy_sha256"],
                    "scope": corresponding_source["scope"],
                    "reproducible_build_proof": False,
                }
            )
            records.append(corresponding_source_record)
        if build_target in {"modules", "mixed"}:
            module_staging = input_dir / "modules" / "staging"
            if not module_staging.is_dir():
                raise BuildToolError("module packaging requested but module staging is missing")
            module_zip = output_dir / f"{base_name}-modules.zip"
            deterministic_zip(module_staging, module_zip, epoch=epoch)
            modules_context = context.get("modules")
            if not isinstance(modules_context, dict):
                raise BuildToolError("module packaging context is absent")
            _verify_zip_file_manifest(
                module_zip,
                modules_context.get("staging_files"),
                role="module",
            )
            records.append(record_for_file(module_zip, role="module-zip"))
        if feature.flags.get("artifact.wireless_firmware", False):
            firmware_zip = output_dir / f"{base_name}-wireless-firmware.zip"
            if smoke:
                firmware_work = temporary / "wireless-firmware-smoke"
                firmware_work.mkdir()
                (firmware_work / "SMOKE-ONLY.txt").write_text(
                    "Synthetic placeholder; no wireless firmware is included.\n",
                    encoding="utf-8",
                    newline="\n",
                )
                deterministic_zip(firmware_work, firmware_zip, epoch=epoch)
                firmware_record = record_for_file(firmware_zip, role="wireless-firmware")
                firmware_record["smoke_placeholder"] = True
            else:
                state = fetch_dependencies(
                    lock,
                    cache_root,
                    selected=("nethunter_wireless_firmware",),
                    dry_run=False,
                    offline=False,
                )
                dependency = lock.dependencies["nethunter_wireless_firmware"]
                rtw88_dependency = lock.dependencies["rtw88"]
                source_record = state["dependencies"]["nethunter_wireless_firmware"]
                firmware_source = Path(str(source_record["path"]))
                firmware_work = temporary / "wireless-firmware"
                curation = _curate_wireless_firmware_tree(
                    root=root,
                    source=firmware_source,
                    destination=firmware_work,
                    dependency=dependency,
                    rtw88_dependency=rtw88_dependency,
                )
                deterministic_zip(firmware_work, firmware_zip, epoch=epoch)
                _verify_curated_wireless_firmware_zip(
                    firmware_zip,
                    curation["output_records"],
                )
                firmware_record = record_for_file(firmware_zip, role="wireless-firmware")
                firmware_record.update(
                    {
                        "dependency": dependency.id,
                        "version": dependency.raw.get("version"),
                        "upstream_sha256": dependency.sha256,
                        "license_classification": dependency.raw.get("license"),
                        "source": {
                            "uri": dependency.raw.get("repository"),
                            "commit": dependency.commit,
                        },
                        "curation": {
                            key: value
                            for key, value in curation.items()
                            if key != "output_records"
                        },
                    }
                )
            records.append(firmware_record)
        if debug:
            debug_work = temporary / "debug"
            debug_work.mkdir()
            if not smoke:
                copy_preserved_build_evidence(
                    input_dir=input_dir,
                    destination=debug_work,
                    evidence_value=context.get("build_evidence"),
                    base=profile.id,
                    resolved_manifest_sha256=str(context["manifest"]["sha256"]),
                    wireless_led_exports_required=wireless_led_kmi_required,
                )
            candidates = [
                input_dir / ".config",
                input_dir / "Module.symvers",
                input_dir / "System.map",
                input_dir / "vmlinux",
                input_dir / ".op13" / "build-context.json",
                input_dir / ".op13" / "resolved-manifest.xml",
                input_dir / ".op13" / "kernel-build.log",
                input_dir / "modules" / ".op13" / "modules-build.log",
            ]
            for candidate in candidates:
                if candidate.is_file():
                    shutil.copy2(candidate, debug_work / candidate.name)
            debug_zip = output_dir / f"{base_name}-debug.zip"
            deterministic_zip(debug_work, debug_zip, epoch=epoch)
            records.append(record_for_file(debug_zip, role="debug-zip"))
    manifest_copy = output_dir / f"{base_name}-manifest.xml"
    shutil.copy2(Path(context["manifest"]["resolved_path"]), manifest_copy)
    records.append(record_for_file(manifest_copy, role="resolved-manifest"))
    provenance_path = output_dir / "BUILD-MANIFEST.json"
    source = dict(context["manifest"])
    source["locked_path"] = _portable_repository_path(
        profile.locked_manifest,
        root,
        role="locked OnePlus manifest",
    )
    source["resolved_path"] = manifest_copy.name
    dependency_lock = {
        "path": _portable_repository_path(
            lock.source_path,
            root,
            role="dependency lock",
        ),
        "sha256": sha256_file(lock.source_path),
        "canonical_sha256": context["dependency_lock"]["sha256"],
    }
    provenance = {
        "schema_version": 2,
        "builder": _orchestrator_identity(),
        "device": profile.device,
        "target": profile.target,
        "arch": profile.arch,
        "kmi": profile.kmi,
        "base": profile.id,
        "profile": profile.id,
        "feature_profile": feature.id,
        "features": context["features"],
        "root_variant": root_variant,
        "build_target": build_target,
        "debug": bool(debug),
        "pre_release": bool(pre_release),
        "smoke": bool(smoke),
        "source": source,
        "build_evidence": packaged_build_evidence,
        "dependency_lock": dependency_lock,
        "dependencies": _dependency_inventory(lock),
        "patches": context["patches"],
        "configuration": context["configuration"],
        "kernel": context["kernel"],
        "modules": context.get("modules"),
        "artifacts": records,
    }
    provenance = _portable_provenance_document(provenance, root)
    atomic_write_json(provenance_path, provenance)
    records.append(record_for_file(provenance_path, role="provenance"))
    checksum_path = _write_checksums(output_dir)
    records.append(record_for_file(checksum_path, role="checksums"))
    _assert_no_partition_images(output_dir)
    portable_records = _portable_provenance_document(records, root)
    updated = advance_context(context, "packaged", {"packages": portable_records})
    write_context(context_path, updated)
    return portable_records
