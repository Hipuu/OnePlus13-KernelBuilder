"""Deterministic Kleaf declarations for profile-selected in-tree modules.

The OnePlus common kernel's arm64 Kleaf targets declare every loadable module
as a Bazel implicit output.  Enabling another ``=m`` Kconfig symbol without
extending those declarations either drops the module from the official
distribution or makes the Kleaf action fail.  This module provides a small,
fail-closed source integrator for the exact common trees locked by this
repository.  OxygenOS 16 builds both 4 KiB and 16 KiB-page common targets, so
both declarations must be extended together.

Only callers that have already resolved the final Kconfig state should invoke
``integrate_common_kleaf_module_outputs``.  The symbol allowlist below is an
auditable Kconfig-to-Kbuild contract; no filename is inferred from user input.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .config import sha256_bytes
from .errors import BuildToolError


SCHEMA_VERSION = 1

# Full-byte preimages of common/modules.bzl at the three commits pinned in
# manifests/lockfiles.  Formatting-only differences make the hashes distinct,
# even though their module lists and Starlark functions are equivalent.
MODULES_BZL_PREIMAGE_SHA256: Mapping[str, str] = MappingProxyType(
    {
        "3b361b863d337b6d215ddca9747371025b241b72a1e6dce020b85875113c0f88": "oos16",
        "df523fc074baae9496a1593147b8781e37e0260e73f465813d1cfe2125f724ba": "oos15-global",
        "33f709345f2bee3470b1905098fdc789d64f2e97ab5b2f6cea8fad727b67d17a": "oos15-cn",
    }
)

# Full-byte preimages of common/BUILD.bazel at the same locked commits.  The
# profile labels must pair with MODULES_BZL_PREIMAGE_SHA256; mixing files from
# two source bases is rejected before either source file is changed.
BUILD_BAZEL_PREIMAGE_SHA256: Mapping[str, str] = MappingProxyType(
    {
        "f0c9c372a0e5f107dc07f18a800376a9165e8db9e58532da01ba4be237ce0456": "oos16",
        "b3ddc9e0ecccb91b4e291001d4f12e8b207c43c782b9e214ad5936b531dd8bbd": "oos15-global",
        "a04b08e432dbcc3c73869a922eb52dd410eeb7747862fa3f3321a4a0e68f97a6": "oos15-cn",
    }
)

# Full-byte preimages of msm-kernel/msm_kernel_la.bzl.  The two OxygenOS 15
# bases share one byte-identical file; the profile allowlist still prevents a
# common/MSM cross-base mix.
MSM_DIST_BZL_PREIMAGE_PROFILES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "e5f7bddde1f41266fd72b33b52fb3c7c5e5e4ba1979c945cdf6e6a042caaef58": frozenset(
            {"oos16"}
        ),
        "7faf46339672923d7ab8e12acb7a46e19ca8cab4a4adcc32ae92130ee15b2a9e": frozenset(
            {"oos15-global", "oos15-cn"}
        ),
    }
)
MSM_DIST_BZL_POSTIMAGE_BY_PREIMAGE: Mapping[str, str] = MappingProxyType(
    {
        "e5f7bddde1f41266fd72b33b52fb3c7c5e5e4ba1979c945cdf6e6a042caaef58":
            "96fa43b81387873201c53a873644427db6d570a038e70b2967464386b0de3f4e",
        "7faf46339672923d7ab8e12acb7a46e19ca8cab4a4adcc32ae92130ee15b2a9e":
            "a9be9148143b738b9210fd82d78de98a5f690af7c524d65b048c195fcc4889ea",
    }
)

# Exact Kbuild mappings from the three locked common trees and the pinned
# MemKernel source.  Symbols selected indirectly by Kconfig are included so a
# caller can pass the canonical final =m set rather than only fragment text.
MODULE_OUTPUT_BY_SYMBOL: Mapping[str, str] = MappingProxyType(
    {
        "CONFIG_ATH10K": "drivers/net/wireless/ath/ath10k/ath10k_core.ko",
        "CONFIG_ATH10K_USB": "drivers/net/wireless/ath/ath10k/ath10k_usb.ko",
        "CONFIG_ATH11K": "drivers/net/wireless/ath/ath11k/ath11k.ko",
        "CONFIG_ATH11K_PCI": "drivers/net/wireless/ath/ath11k/ath11k_pci.ko",
        "CONFIG_ATH9K": "drivers/net/wireless/ath/ath9k/ath9k.ko",
        "CONFIG_ATH9K_COMMON": "drivers/net/wireless/ath/ath9k/ath9k_common.ko",
        "CONFIG_ATH9K_HTC": "drivers/net/wireless/ath/ath9k/ath9k_htc.ko",
        "CONFIG_ATH9K_HW": "drivers/net/wireless/ath/ath9k/ath9k_hw.ko",
        "CONFIG_ATH_COMMON": "drivers/net/wireless/ath/ath.ko",
        "CONFIG_BT": "net/bluetooth/bluetooth.ko",
        "CONFIG_BT_BCM": "drivers/bluetooth/btbcm.ko",
        "CONFIG_BT_HCIBCM203X": "drivers/bluetooth/bcm203x.ko",
        "CONFIG_BT_HCIBFUSB": "drivers/bluetooth/bfusb.ko",
        "CONFIG_BT_HCIBPA10X": "drivers/bluetooth/bpa10x.ko",
        "CONFIG_BT_HCIBTUSB": "drivers/bluetooth/btusb.ko",
        "CONFIG_BT_HCIVHCI": "drivers/bluetooth/hci_vhci.ko",
        "CONFIG_BT_INTEL": "drivers/bluetooth/btintel.ko",
        "CONFIG_BT_RTL": "drivers/bluetooth/btrtl.ko",
        "CONFIG_CAN": "net/can/can.ko",
        "CONFIG_CAN_8DEV_USB": "drivers/net/can/usb/usb_8dev.ko",
        "CONFIG_CAN_CC770": "drivers/net/can/cc770/cc770.ko",
        "CONFIG_CAN_CC770_PLATFORM": "drivers/net/can/cc770/cc770_platform.ko",
        "CONFIG_CAN_C_CAN": "drivers/net/can/c_can/c_can.ko",
        "CONFIG_CAN_C_CAN_PCI": "drivers/net/can/c_can/c_can_pci.ko",
        "CONFIG_CAN_C_CAN_PLATFORM": "drivers/net/can/c_can/c_can_platform.ko",
        "CONFIG_CAN_DEV": "drivers/net/can/dev/can-dev.ko",
        "CONFIG_CAN_EMS_USB": "drivers/net/can/usb/ems_usb.ko",
        "CONFIG_CAN_ESD_USB": "drivers/net/can/usb/esd_usb.ko",
        "CONFIG_CAN_GS_USB": "drivers/net/can/usb/gs_usb.ko",
        "CONFIG_CAN_HI311X": "drivers/net/can/spi/hi311x.ko",
        "CONFIG_CAN_KVASER_USB": "drivers/net/can/usb/kvaser_usb/kvaser_usb.ko",
        "CONFIG_CAN_MCP251X": "drivers/net/can/spi/mcp251x.ko",
        "CONFIG_CAN_M_CAN": "drivers/net/can/m_can/m_can.ko",
        "CONFIG_CAN_M_CAN_PCI": "drivers/net/can/m_can/m_can_pci.ko",
        "CONFIG_CAN_M_CAN_PLATFORM": "drivers/net/can/m_can/m_can_platform.ko",
        "CONFIG_CAN_M_CAN_TCAN4X5X": "drivers/net/can/m_can/tcan4x5x.ko",
        "CONFIG_CAN_PEAK_USB": "drivers/net/can/usb/peak_usb/peak_usb.ko",
        "CONFIG_CAN_SLCAN": "drivers/net/can/slcan/slcan.ko",
        "CONFIG_CAN_VCAN": "drivers/net/can/vcan.ko",
        "CONFIG_CAN_XILINXCAN": "drivers/net/can/xilinx_can.ko",
        "CONFIG_CFG80211": "net/wireless/cfg80211.ko",
        "CONFIG_CRYPTO_MICHAEL_MIC": "crypto/michael_mic.ko",
        "CONFIG_MAC80211": "net/mac80211/mac80211.ko",
        "CONFIG_MEMKERNEL": "drivers/memkernel/memkernel.ko",
        "CONFIG_MHI_BUS": "drivers/bus/mhi/host/mhi.ko",
        "CONFIG_MT7601U": "drivers/net/wireless/mediatek/mt7601u/mt7601u.ko",
        "CONFIG_MT7603E": "drivers/net/wireless/mediatek/mt76/mt7603/mt7603e.ko",
        "CONFIG_MT7615E": "drivers/net/wireless/mediatek/mt76/mt7615/mt7615e.ko",
        "CONFIG_MT7615_COMMON": "drivers/net/wireless/mediatek/mt76/mt7615/mt7615-common.ko",
        "CONFIG_MT7663U": "drivers/net/wireless/mediatek/mt76/mt7615/mt7663u.ko",
        "CONFIG_MT7663_USB_SDIO_COMMON": "drivers/net/wireless/mediatek/mt76/mt7615/mt7663-usb-sdio-common.ko",
        "CONFIG_MT76_CORE": "drivers/net/wireless/mediatek/mt76/mt76.ko",
        "CONFIG_MT76_USB": "drivers/net/wireless/mediatek/mt76/mt76-usb.ko",
        "CONFIG_MT76_CONNAC_LIB": "drivers/net/wireless/mediatek/mt76/mt76-connac-lib.ko",
        "CONFIG_MT76x0U": "drivers/net/wireless/mediatek/mt76/mt76x0/mt76x0u.ko",
        "CONFIG_MT76x0_COMMON": "drivers/net/wireless/mediatek/mt76/mt76x0/mt76x0-common.ko",
        "CONFIG_MT76x02_LIB": "drivers/net/wireless/mediatek/mt76/mt76x02-lib.ko",
        "CONFIG_MT76x02_USB": "drivers/net/wireless/mediatek/mt76/mt76x02-usb.ko",
        "CONFIG_MT76x2U": "drivers/net/wireless/mediatek/mt76/mt76x2/mt76x2u.ko",
        "CONFIG_MT76x2_COMMON": "drivers/net/wireless/mediatek/mt76/mt76x2/mt76x2-common.ko",
        "CONFIG_MT7915E": "drivers/net/wireless/mediatek/mt76/mt7915/mt7915e.ko",
        "CONFIG_MT7921E": "drivers/net/wireless/mediatek/mt76/mt7921/mt7921e.ko",
        "CONFIG_MT7921U": "drivers/net/wireless/mediatek/mt76/mt7921/mt7921u.ko",
        "CONFIG_MT7921_COMMON": "drivers/net/wireless/mediatek/mt76/mt7921/mt7921-common.ko",
        "CONFIG_MT792x_LIB": "drivers/net/wireless/mediatek/mt76/mt792x-lib.ko",
        "CONFIG_MT792x_USB": "drivers/net/wireless/mediatek/mt76/mt792x-usb.ko",
        "CONFIG_QCOM_QMI_HELPERS": "drivers/soc/qcom/qmi_helpers.ko",
        "CONFIG_QRTR": "net/qrtr/qrtr.ko",
        "CONFIG_QRTR_MHI": "net/qrtr/qrtr-mhi.ko",
        "CONFIG_USB_SERIAL": "drivers/usb/serial/usbserial.ko",
        "CONFIG_USB_SERIAL_CH341": "drivers/usb/serial/ch341.ko",
        "CONFIG_USB_SERIAL_FTDI_SIO": "drivers/usb/serial/ftdi_sio.ko",
        "CONFIG_USB_SERIAL_PL2303": "drivers/usb/serial/pl2303.ko",
    }
)

# Mapped outputs already present in get_gki_modules_list("arm64").  They are
# retained in the resolution record but must not be repeated in the custom
# constant passed to module_implicit_outs.
OFFICIAL_GKI_MODULE_OUTPUTS = frozenset(
    {
        "drivers/bluetooth/btbcm.ko",
        "drivers/net/can/dev/can-dev.ko",
        "drivers/net/can/slcan/slcan.ko",
        "drivers/net/can/vcan.ko",
        "drivers/usb/serial/ftdi_sio.ko",
        "drivers/usb/serial/usbserial.ko",
        "net/bluetooth/bluetooth.ko",
        "net/can/can.ko",
    }
)

_SYMBOL_RE = re.compile(r"^CONFIG_[A-Za-z0-9_]+$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._+/-]+\.ko$")
_OP13_TOKEN = "OP13_MODULE_IMPLICIT_OUTS"
_MODULES_INSERT_ANCHOR = (
    "COMMON_GKI_MODULES_LIST = _COMMON_GKI_MODULES_LIST\n\n"
    "_ARM_GKI_MODULES_LIST = ["
)
_BUILD_LOAD_ANCHOR = """load(
    ":modules.bzl",
    "get_gki_modules_list",
    "get_gki_protected_modules_list",
    "get_kunit_modules_list",
)"""
_KLEAF_TARGETS_BY_PROFILE: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "oos16": ("kernel_aarch64", "kernel_aarch64_16k"),
        "oos15-global": ("kernel_aarch64",),
        "oos15-cn": ("kernel_aarch64",),
    }
)
_MODULE_IMPLICIT_OUTS_LINE = (
    '        "module_implicit_outs": get_gki_modules_list("arm64") + '
    'get_kunit_modules_list("arm64"),'
)
_MODULE_IMPLICIT_OUTS_REPLACEMENT = """        "module_implicit_outs": (
            get_gki_modules_list("arm64") +
            get_kunit_modules_list("arm64") +
            OP13_MODULE_IMPLICIT_OUTS
        ),"""
_MSM_MODULES_INSTALL_ANCHOR = """    kernel_modules_install(
        name = "{}_modules_install".format(target),
        kernel_build = ":{}".format(target),
    )"""
_MSM_MODULES_INSTALL_REPLACEMENT = _MSM_MODULES_INSTALL_ANCHOR + """

    # OP13_MODULE_STAGING_ARCHIVE:BEGIN
    native.filegroup(
        name = "{}_op13_modules_staging_archive".format(target),
        srcs = [":{}".format(target)],
        output_group = "modules_staging_archive",
    )
    # OP13_MODULE_STAGING_ARCHIVE:END"""
_MSM_DIST_ANCHOR = "    msm_dist_targets = [base_kernel]"
_MSM_DIST_REPLACEMENT = """    msm_dist_targets = [
        base_kernel,
        ":{}_op13_modules_staging_archive".format(target),
    ]"""


def _validate_relative_ko_path(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildToolError(f"{where}: module output path must be a non-empty string")
    if "\\" in value or "\x00" in value or "//" in value or not _SAFE_PATH_RE.fullmatch(value):
        raise BuildToolError(f"{where}: unsafe module output path {value!r}")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise BuildToolError(f"{where}: unsafe module output path {value!r}")
    return value


def _unique_sorted_paths(values: Iterable[object], *, where: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise BuildToolError(f"{where}: expected an iterable of relative .ko paths")
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        path = _validate_relative_ko_path(value, where=f"{where}[{index}]")
        if path in seen:
            raise BuildToolError(f"{where}: repeated module output path {path!r}")
        seen.add(path)
        result.append(path)
    return tuple(sorted(result))


def _path_set_sha256(paths: Iterable[str]) -> str:
    ordered = tuple(paths)
    payload = "" if not ordered else "\n".join(ordered) + "\n"
    return sha256_bytes(payload.encode("utf-8"))


def resolve_module_outputs(active_symbols: Iterable[object]) -> dict[str, object]:
    """Resolve canonical ``=m`` symbols to audited relative module paths.

    Unknown or repeated symbols are rejected.  ``active_paths`` contains only
    paths that need the OP13 Kleaf extension; ``official_paths`` records mapped
    outputs already supplied by the locked GKI declaration.
    """

    if isinstance(active_symbols, (str, bytes)):
        raise BuildToolError("active module symbols must be an iterable, not a string")
    symbols: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(active_symbols):
        if not isinstance(value, str) or not _SYMBOL_RE.fullmatch(value):
            raise BuildToolError(f"active module symbol {index} is invalid: {value!r}")
        if value in seen:
            raise BuildToolError(f"repeated active module symbol {value}")
        if value not in MODULE_OUTPUT_BY_SYMBOL:
            raise BuildToolError(f"active module symbol is not allowlisted: {value}")
        seen.add(value)
        symbols.append(value)

    requested_paths = _unique_sorted_paths(
        (MODULE_OUTPUT_BY_SYMBOL[symbol] for symbol in symbols),
        where="resolved module outputs",
    )
    official_paths = tuple(path for path in requested_paths if path in OFFICIAL_GKI_MODULE_OUTPUTS)
    active_paths = tuple(path for path in requested_paths if path not in OFFICIAL_GKI_MODULE_OUTPUTS)
    return {
        "active_symbols": sorted(symbols),
        "requested_paths": list(requested_paths),
        "official_paths": list(official_paths),
        "active_paths": list(active_paths),
        "requested_paths_sha256": _path_set_sha256(requested_paths),
        "active_paths_sha256": _path_set_sha256(active_paths),
    }


def _read_source_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink():
        raise BuildToolError(f"{label} must not be a symbolic link: {path}")
    try:
        if not path.is_file():
            raise BuildToolError(f"{label} is missing: {path}")
        return path.read_bytes()
    except OSError as exc:
        raise BuildToolError(f"cannot read {label} {path}: {exc}") from exc


def _decode_source(raw: bytes, *, path: Path) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise BuildToolError(f"{path}: UTF-8 BOM is not permitted")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildToolError(f"{path}: source must be UTF-8") from exc


def _validate_pristine_sources(
    modules_path: Path,
    modules_raw: bytes,
    build_path: Path,
    build_raw: bytes,
) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    modules_digest = sha256_bytes(modules_raw)
    profile = MODULES_BZL_PREIMAGE_SHA256.get(modules_digest)
    if profile is None:
        raise BuildToolError(
            f"{modules_path}: unrecognized or already modified modules.bzl preimage {modules_digest}"
        )
    build_digest = sha256_bytes(build_raw)
    build_profile = BUILD_BAZEL_PREIMAGE_SHA256.get(build_digest)
    if build_profile is None:
        raise BuildToolError(
            f"{build_path}: unrecognized or already modified BUILD.bazel preimage {build_digest}"
        )
    if build_profile != profile:
        raise BuildToolError(
            f"{build_path}: BUILD.bazel profile {build_profile} does not match modules.bzl profile {profile}"
        )
    modules_text = _decode_source(modules_raw, path=modules_path)
    build_text = _decode_source(build_raw, path=build_path)
    if _OP13_TOKEN in modules_text or _OP13_TOKEN in build_text:
        raise BuildToolError("OP13 Kleaf module-output integration is already present or tampered")
    if modules_text.count(_MODULES_INSERT_ANCHOR) != 1:
        raise BuildToolError(f"{modules_path}: expected one stable module-list insertion anchor")
    if build_text.count(_BUILD_LOAD_ANCHOR) != 1:
        raise BuildToolError(f"{build_path}: expected one exact modules.bzl load anchor")
    target_names = _KLEAF_TARGETS_BY_PROFILE.get(profile)
    if target_names is None:
        raise BuildToolError(f"{build_path}: no audited Kleaf target set for profile {profile}")

    target_blocks: list[tuple[str, str]] = []
    for target_name in target_names:
        target_anchor = f'    "{target_name}": {{'
        if build_text.count(target_anchor) != 1:
            raise BuildToolError(f"{build_path}: expected one {target_name} target anchor")
        target_start = build_text.index(target_anchor)
        target_end = build_text.find("\n    },", target_start)
        if target_end < 0:
            raise BuildToolError(
                f"{build_path}: {target_name} target has no deterministic closing anchor"
            )
        target_end += len("\n    },")
        target_block = build_text[target_start:target_end]
        if target_block.count(_MODULE_IMPLICIT_OUTS_LINE) != 1:
            raise BuildToolError(
                f"{build_path}: {target_name} module_implicit_outs declaration changed"
            )
        target_blocks.append((target_name, target_block))
    return profile, modules_text, build_text, tuple(target_blocks)


def _render_module_constant(paths: Iterable[str]) -> str:
    lines = ["# OP13_MODULE_IMPLICIT_OUTS:BEGIN", "OP13_MODULE_IMPLICIT_OUTS = ["]
    lines.extend(f'    "{path}",' for path in paths)
    lines.extend(["]", "# OP13_MODULE_IMPLICIT_OUTS:END"])
    return "\n".join(lines)


def _patched_sources(
    modules_text: str,
    build_text: str,
    target_blocks: tuple[tuple[str, str], ...],
    active_paths: tuple[str, ...],
) -> tuple[bytes, bytes]:
    constant = _render_module_constant(active_paths)
    modules_replacement = (
        "COMMON_GKI_MODULES_LIST = _COMMON_GKI_MODULES_LIST\n\n"
        f"{constant}\n\n"
        "_ARM_GKI_MODULES_LIST = ["
    )
    patched_modules = modules_text.replace(_MODULES_INSERT_ANCHOR, modules_replacement, 1)

    patched_load = _BUILD_LOAD_ANCHOR.replace(
        '    "get_gki_modules_list",',
        '    "OP13_MODULE_IMPLICIT_OUTS",\n    "get_gki_modules_list",',
        1,
    )
    patched_build = build_text.replace(_BUILD_LOAD_ANCHOR, patched_load, 1)
    for _target_name, target_block in target_blocks:
        patched_target = target_block.replace(
            _MODULE_IMPLICIT_OUTS_LINE,
            _MODULE_IMPLICIT_OUTS_REPLACEMENT,
            1,
        )
        patched_build = patched_build.replace(target_block, patched_target, 1)

    if patched_modules.count("OP13_MODULE_IMPLICIT_OUTS = [") != 1:
        raise BuildToolError("postcondition failed: OP13 module constant is not unique")
    if patched_build.count('    "OP13_MODULE_IMPLICIT_OUTS",') != 1:
        raise BuildToolError("postcondition failed: OP13 modules.bzl load is not unique")
    if patched_build.count("            OP13_MODULE_IMPLICIT_OUTS") != len(target_blocks):
        raise BuildToolError("postcondition failed: OP13 target extension count is wrong")
    return patched_modules.encode("utf-8"), patched_build.encode("utf-8")


def _write_source_pair(
    modules_path: Path,
    modules_before: bytes,
    modules_after: bytes,
    build_path: Path,
    build_before: bytes,
    build_after: bytes,
) -> None:
    try:
        modules_path.write_bytes(modules_after)
        build_path.write_bytes(build_after)
    except OSError as exc:
        # Keep a failed two-file transformation from leaving a valid first half.
        rollback_failures: list[str] = []
        for path, content in ((modules_path, modules_before), (build_path, build_before)):
            try:
                path.write_bytes(content)
            except OSError as rollback_exc:
                rollback_failures.append(f"{path}: {rollback_exc}")
        suffix = "" if not rollback_failures else "; rollback failed: " + "; ".join(rollback_failures)
        raise BuildToolError(f"cannot write Kleaf module-output integration: {exc}{suffix}") from exc


def integrate_common_kleaf_module_outputs(
    common_kernel: Path,
    active_symbols: Iterable[object],
) -> dict[str, object]:
    """Patch the locked common Kleaf targets for an allowlisted active module set.

    Empty/custom-free sets are a validated no-op.  A successful non-empty call
    is intentionally not idempotent: a repeat sees a non-pristine preimage and
    fails instead of silently accepting source tampering.
    """

    resolution = resolve_module_outputs(active_symbols)
    active_paths = tuple(resolution["active_paths"])
    common_kernel = Path(common_kernel)
    if not common_kernel.is_dir():
        raise BuildToolError(f"common kernel directory is missing: {common_kernel}")
    modules_path = common_kernel / "modules.bzl"
    build_path = common_kernel / "BUILD.bazel"
    modules_before = _read_source_file(modules_path, label="common/modules.bzl")
    build_before = _read_source_file(build_path, label="common/BUILD.bazel")
    profile, modules_text, build_text, target_blocks = _validate_pristine_sources(
        modules_path,
        modules_before,
        build_path,
        build_before,
    )

    modules_pre_digest = sha256_bytes(modules_before)
    build_pre_digest = sha256_bytes(build_before)
    if active_paths:
        modules_after, build_after = _patched_sources(
            modules_text,
            build_text,
            target_blocks,
            active_paths,
        )
        _write_source_pair(
            modules_path,
            modules_before,
            modules_after,
            build_path,
            build_before,
            build_after,
        )
        changed = True
    else:
        modules_after = modules_before
        build_after = build_before
        changed = False

    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "locked_profile": profile,
        "changed": changed,
        "extended_targets": [name for name, _block in target_blocks] if changed else [],
        **resolution,
        "modules_bzl": {
            "path": str(modules_path.resolve()),
            "pre_sha256": modules_pre_digest,
            "post_sha256": sha256_bytes(modules_after),
        },
        "build_bazel": {
            "path": str(build_path.resolve()),
            "pre_sha256": build_pre_digest,
            "post_sha256": sha256_bytes(build_after),
        },
    }
    return record


def integrate_msm_kleaf_module_dist(
    msm_kernel: Path,
    *,
    expected_profile: str,
) -> dict[str, object]:
    """Export the final mixed module-staging archive into sun/perf dist."""

    msm_kernel = Path(msm_kernel)
    if not msm_kernel.is_dir():
        raise BuildToolError(f"MSM kernel directory is missing: {msm_kernel}")
    path = msm_kernel / "msm_kernel_la.bzl"
    before = _read_source_file(path, label="msm-kernel/msm_kernel_la.bzl")
    digest = sha256_bytes(before)
    compatible_profiles = MSM_DIST_BZL_PREIMAGE_PROFILES.get(digest)
    if compatible_profiles is None:
        raise BuildToolError(
            f"{path}: unrecognized or already modified msm_kernel_la.bzl preimage {digest}"
        )
    if expected_profile not in compatible_profiles:
        raise BuildToolError(
            f"{path}: MSM dist profile does not match common profile {expected_profile}"
        )
    text = _decode_source(before, path=path)
    if text.count(_MSM_MODULES_INSTALL_ANCHOR) != 1:
        raise BuildToolError(f"{path}: expected one locked kernel_modules_install anchor")
    if text.count(_MSM_DIST_ANCHOR) != 1:
        raise BuildToolError(f"{path}: expected one locked MSM dist target anchor")
    if "OP13_MODULE_STAGING_" in text:
        raise BuildToolError("OP13 module staging integration is already present or tampered")
    patched = text.replace(
        _MSM_MODULES_INSTALL_ANCHOR,
        _MSM_MODULES_INSTALL_REPLACEMENT,
        1,
    )
    after = patched.replace(_MSM_DIST_ANCHOR, _MSM_DIST_REPLACEMENT, 1).encode("utf-8")
    if after.count(b'output_group = "modules_staging_archive"') != 1:
        raise BuildToolError("postcondition failed: module staging filegroup is not unique")
    if after.count(b'":{}_op13_modules_staging_archive".format(target)') != 1:
        raise BuildToolError("postcondition failed: module staging dist target is not unique")
    post_digest = sha256_bytes(after)
    if post_digest != MSM_DIST_BZL_POSTIMAGE_BY_PREIMAGE[digest]:
        raise BuildToolError("postcondition failed: MSM module staging integration digest changed")
    try:
        path.write_bytes(after)
    except OSError as exc:
        raise BuildToolError(f"cannot write MSM module dist integration {path}: {exc}") from exc
    return {
        "path": str(path.resolve()),
        "locked_profile": expected_profile,
        "pre_sha256": digest,
        "post_sha256": post_digest,
        "changed": True,
    }


def verify_produced_module_outputs(
    declared_paths: Iterable[object],
    produced_paths: Iterable[object],
) -> dict[str, object]:
    """Require exact equality between declared and produced relative ``.ko`` paths."""

    declared = _unique_sorted_paths(declared_paths, where="declared module outputs")
    produced = _unique_sorted_paths(produced_paths, where="produced module outputs")
    declared_set = set(declared)
    produced_set = set(produced)
    missing = sorted(declared_set - produced_set)
    unexpected = sorted(produced_set - declared_set)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise BuildToolError("produced module outputs differ from Kleaf declarations (" + "; ".join(details) + ")")
    return {
        "schema_version": SCHEMA_VERSION,
        "count": len(declared),
        "paths": list(declared),
        "declared_paths_sha256": _path_set_sha256(declared),
        "produced_paths_sha256": _path_set_sha256(produced),
    }


# Validate programmer-owned constants at import time.  User input never reaches
# this path; a bad mapping is a repository defect and should stop every caller.
_allowlisted_paths = list(MODULE_OUTPUT_BY_SYMBOL.values())
if len(_allowlisted_paths) != len(set(_allowlisted_paths)):
    raise RuntimeError("MODULE_OUTPUT_BY_SYMBOL contains repeated .ko paths")
for _symbol, _path in MODULE_OUTPUT_BY_SYMBOL.items():
    if not _SYMBOL_RE.fullmatch(_symbol):
        raise RuntimeError(f"invalid allowlisted Kconfig symbol: {_symbol}")
    try:
        _validate_relative_ko_path(_path, where=f"MODULE_OUTPUT_BY_SYMBOL[{_symbol}]")
    except BuildToolError as exc:
        raise RuntimeError(str(exc)) from exc
if not OFFICIAL_GKI_MODULE_OUTPUTS.issubset(set(_allowlisted_paths)):
    raise RuntimeError("OFFICIAL_GKI_MODULE_OUTPUTS contains a path absent from the symbol allowlist")
