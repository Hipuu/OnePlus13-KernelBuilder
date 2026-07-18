"""Kernel configuration, compilation, and external-module compilation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .config import (
    Device,
    DependencyLock,
    FeatureProfile,
    Profile,
    resolve_inside,
    sha256_file,
)
from .context import (
    advance_context,
    assert_symvers_lineage,
    atomic_write_json,
    load_context,
    record_for_file,
    validate_lineage,
    write_context,
)
from .errors import BuildToolError
from .module_outputs import (
    MODULE_OUTPUT_BY_SYMBOL,
    integrate_common_kleaf_module_outputs,
    integrate_msm_kleaf_module_dist,
    mapped_module_output_paths,
    resolve_module_outputs,
    verify_produced_module_outputs,
)
from .runtime import CommandRunner, fetch_dependencies


BUILD_TARGETS = {"kernel", "modules", "mixed", "monolithic"}
KERNEL_PHASE_TARGETS = {"kernel", "mixed"}
MODULE_PHASE_TARGETS = {"modules", "mixed"}
ROOT_VARIANTS = {"kernelsu", "kernelsu-next", "none"}
KERNEL_TREE_NAMES = ("common", "msm-kernel")
BRANDING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SYMBOL_LINE_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
SYMBOL_UNSET_RE = re.compile(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$")
CLANG_VERSION_RE = re.compile(r"^CLANG_VERSION=([A-Za-z0-9][A-Za-z0-9._-]{0,63})$")
MAX_BUILD_EPOCH = int(datetime(2107, 12, 31, 23, 59, 58, tzinfo=timezone.utc).timestamp())


def assert_build_target_contract(build_target: str) -> None:
    """Reject unknown targets and targets with no pinned implementation path."""

    if build_target not in BUILD_TARGETS:
        raise BuildToolError(f"unsupported build target {build_target!r}")
    if build_target == "monolithic":
        raise BuildToolError(
            "build target 'monolithic' is disabled: the pinned OnePlus 13 "
            "sun/perf entry point is a mixed GKI pipeline and exposes no "
            "monolithic source target"
        )


def _configuration_build_target(context: Mapping[str, Any], *, where: str) -> str:
    configuration = context.get("configuration")
    if not isinstance(configuration, dict):
        raise BuildToolError(f"{where} configuration record is absent")
    build_target = configuration.get("build_target")
    if not isinstance(build_target, str):
        raise BuildToolError(f"{where} build target is absent")
    assert_build_target_contract(build_target)
    return build_target


def parse_fragment(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BuildToolError(f"cannot read Kconfig fragment {path}: {exc}") from exc
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or (line.startswith("#") and " is not set" not in line):
            continue
        match = SYMBOL_LINE_RE.fullmatch(line)
        if match:
            name, value = match.groups()
        else:
            match = SYMBOL_UNSET_RE.fullmatch(line)
            if not match:
                raise BuildToolError(f"{path}:{line_number}: malformed Kconfig assignment")
            name, value = match.group(1), "n"
        if name in result:
            raise BuildToolError(f"{path}:{line_number}: duplicate Kconfig symbol {name}")
        result[name] = value
    return result


def parse_dotconfig(path: Path) -> dict[str, str]:
    return parse_fragment(path)


def expected_symbols(
    feature: FeatureProfile,
    *,
    root_variant: str,
    optimization: str,
    lto: str,
) -> dict[str, str]:
    if root_variant not in ROOT_VARIANTS:
        raise BuildToolError(f"unsupported root variant {root_variant!r}")
    expected = dict(feature.required_symbols)
    if root_variant == "none":
        for symbol in list(expected):
            if symbol.startswith("CONFIG_KSU"):
                expected[symbol] = "n"
    if optimization == "O2":
        expected["CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE"] = "y"
        expected["CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE_O3"] = "n"
    elif optimization == "O3":
        expected["CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE"] = "n"
        expected["CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE_O3"] = "y"
    else:
        raise BuildToolError("optimization must be O2 or O3")
    if lto == "thin":
        expected["CONFIG_LTO_CLANG_THIN"] = "y"
        expected["CONFIG_LTO_CLANG_FULL"] = "n"
    elif lto == "full":
        expected["CONFIG_LTO_CLANG_THIN"] = "n"
        expected["CONFIG_LTO_CLANG_FULL"] = "y"
    else:
        raise BuildToolError("LTO mode must be thin or full")
    return expected


def assert_symbols(config_path: Path, expected: Mapping[str, str]) -> None:
    actual = parse_dotconfig(config_path)
    failures: list[str] = []
    for symbol, wanted in sorted(expected.items()):
        observed = actual.get(symbol, "n")
        if observed != wanted:
            failures.append(f"{symbol}: expected {wanted}, got {observed}")
    if failures:
        preview = "\n  ".join(failures[:30])
        suffix = f"\n  ... and {len(failures) - 30} more" if len(failures) > 30 else ""
        raise BuildToolError(f"final Kconfig assertions failed:\n  {preview}{suffix}")


def _config_command(config_tool: Path, config_path: Path, symbol: str, value: str) -> list[str]:
    if value == "y":
        action = ["--enable", symbol]
    elif value == "m":
        action = ["--module", symbol]
    elif value == "n":
        action = ["--disable", symbol]
    elif value.startswith('"') and value.endswith('"'):
        action = ["--set-str", symbol, value[1:-1]]
    elif re.fullmatch(r"-?[0-9]+", value):
        action = ["--set-val", symbol, value]
    else:
        raise BuildToolError(f"unsupported Kconfig value for {symbol}: {value!r}")
    # scripts/config upper-cases symbols unless --keep-case precedes them.
    # Several audited in-tree symbols (for example CONFIG_MT76x0U) contain a
    # lower-case character, so the default behavior silently writes an unknown
    # CONFIG_MT76X0U entry that olddefconfig then discards.
    return [str(config_tool), "--file", str(config_path), "--keep-case", *action]


def _fragment_paths(
    root: Path,
    feature: FeatureProfile,
    build_target: str,
    kernel_tree: str = "common",
) -> list[Path]:
    if kernel_tree not in KERNEL_TREE_NAMES:
        raise BuildToolError(f"unsupported Kconfig kernel tree {kernel_tree!r}")
    include_modules = build_target in MODULE_PHASE_TARGETS
    result: list[Path] = []
    for fragment in feature.kconfig_fragments:
        if kernel_tree not in fragment.kernel_trees:
            continue
        if fragment.scope == "modules" and not include_modules:
            continue
        path = resolve_inside(root, fragment.path, f"feature {feature.id} fragment", must_exist=fragment.required)
        if path.exists():
            result.append(path)
        elif fragment.required:
            raise BuildToolError(f"required Kconfig fragment is absent: {path}")
    return result


def _common_gki_defconfig(source_dir: Path, device: Device) -> Path:
    """Return the defconfig consumed by the pinned Kleaf arm64 targets."""

    return source_dir / device.common_kernel / "arch" / device.arch / "configs" / "gki_defconfig"


def _kernel_tree_path(source_dir: Path, device: Device, kernel_tree: str) -> Path:
    if kernel_tree == "common":
        relative = device.common_kernel
    elif kernel_tree == "msm-kernel":
        relative = device.vendor_kernel
    else:
        raise BuildToolError(f"unsupported Kconfig kernel tree {kernel_tree!r}")
    return source_dir / relative


def _kernel_tree_gki_defconfig(
    source_dir: Path,
    device: Device,
    kernel_tree: str,
) -> Path:
    return (
        _kernel_tree_path(source_dir, device, kernel_tree)
        / "arch"
        / device.arch
        / "configs"
        / "gki_defconfig"
    )


def _official_build_paths(source_dir: Path, device: Device) -> tuple[Path, Path]:
    """Return the one exact output tree and kernel kit produced by sun/perf."""

    target, variant = device.official_args[:2]
    official_output = source_dir / "kernel_platform" / "out" / f"msm-kernel-{target}-{variant}"
    kernel_kit = source_dir / "device" / "qcom" / f"{target}-kernel"
    return official_output, kernel_kit


def _official_cache_path(source_dir: Path, device: Device) -> Path:
    """Return the device-declared Bazel cache after containing it in kernel_platform."""

    kernel_platform = source_dir / "kernel_platform"
    relative = Path(device.official_cache_dir)
    candidate = kernel_platform / relative
    cursor = kernel_platform
    if cursor.is_symlink():
        raise BuildToolError(f"refusing symlinked official build cache component: {cursor}")
    for component in relative.parts:
        cursor /= component
        if cursor.is_symlink():
            raise BuildToolError(f"refusing symlinked official build cache component: {cursor}")
    resolved_kernel_platform = kernel_platform.resolve()
    resolved = candidate.resolve()
    if not _inside(resolved, resolved_kernel_platform) or resolved == resolved_kernel_platform:
        raise BuildToolError(f"refusing unsafe official build cache path: {resolved}")
    return candidate


def _run_kconfig_make(
    runner: CommandRunner,
    kernel_tree: Path,
    config_output: Path,
    arch: str,
    target: str,
    toolchain_env: Mapping[str, str],
) -> None:
    env = dict(toolchain_env)
    env["KCONFIG_CONFIG"] = str((config_output / ".config").resolve())
    runner.run(
        ["make", "-C", str(kernel_tree), f"O={config_output}", f"ARCH={arch}", target],
        env=env,
    )


def _kconfig_toolchain_env(
    source_dir: Path,
    device: Device,
    kernel_tree: str = "common",
) -> dict[str, str]:
    """Resolve the exact Clang toolchain declared by one locked kernel tree."""

    selected_tree = _kernel_tree_path(source_dir, device, kernel_tree)
    constants = selected_tree / "build.config.constants"
    if not constants.is_file():
        raise BuildToolError(f"locked Clang version declaration is missing: {constants}")
    matches = [
        match.group(1)
        for line in constants.read_text(encoding="utf-8").splitlines()
        if (match := CLANG_VERSION_RE.fullmatch(line)) is not None
    ]
    if len(matches) != 1:
        raise BuildToolError(
            f"locked {kernel_tree} tree must declare exactly one CLANG_VERSION"
        )
    clang_bin = (
        source_dir
        / "kernel_platform"
        / "prebuilts"
        / "clang"
        / "host"
        / "linux-x86"
        / f"clang-{matches[0]}"
        / "bin"
    )
    required_tools = (
        "clang",
        "ld.lld",
        "llvm-ar",
        "llvm-nm",
        "llvm-objcopy",
        "llvm-objdump",
        "llvm-readelf",
        "llvm-strip",
    )
    missing = [tool for tool in required_tools if not (clang_bin / tool).is_file()]
    if missing:
        raise BuildToolError(
            "locked Clang toolchain is incomplete: " + ", ".join(sorted(missing))
        )
    inherited_path = os.environ.get("PATH", "")
    return {
        "LLVM": "1",
        "LLVM_IAS": "1",
        "PATH": str(clang_bin) + (os.pathsep + inherited_path if inherited_path else ""),
    }


def _configure_gki_defconfig(
    *,
    source_dir: Path,
    metadata_dir: Path,
    device: Device,
    kernel_tree: str,
    fragments: Iterable[Path],
    forced: Mapping[str, str],
    work_name: str,
) -> tuple[Path, Path]:
    """Merge and canonicalize one checked-in GKI defconfig used by Kleaf.

    ``prepare_vendor.sh`` does not consume ``KCONFIG_CONFIG`` from its caller.
    Both the base ``//common`` build and the mixed ``//msm-kernel`` build load
    their own arch/arm64/configs/gki_defconfig. Canonicalizing each tree through
    savedefconfig, then rebuilding a full .config, makes the request independent
    of incidental output files from an earlier build.
    """

    selected_tree = _kernel_tree_path(source_dir, device, kernel_tree)
    source_defconfig = _kernel_tree_gki_defconfig(source_dir, device, kernel_tree)
    config_tool = selected_tree / "scripts" / "config"
    if not source_defconfig.is_file():
        raise BuildToolError(f"Kleaf GKI defconfig is missing: {source_defconfig}")
    if not config_tool.is_file():
        raise BuildToolError(f"kernel scripts/config is missing: {config_tool}")
    config_output = metadata_dir / work_name
    if config_output.exists():
        shutil.rmtree(config_output)
    config_output.mkdir(parents=True)
    requested_config = config_output / ".config"
    shutil.copy2(source_defconfig, requested_config)
    runner = CommandRunner()
    toolchain_env = _kconfig_toolchain_env(source_dir, device, kernel_tree)
    for fragment in fragments:
        for symbol, value in parse_fragment(fragment).items():
            runner.run(
                _config_command(config_tool, requested_config, symbol, value),
                cwd=selected_tree,
            )
    for symbol, value in forced.items():
        runner.run(
            _config_command(config_tool, requested_config, symbol, value),
            cwd=selected_tree,
        )
    _run_kconfig_make(
        runner,
        selected_tree,
        config_output,
        device.arch,
        "olddefconfig",
        toolchain_env,
    )
    _run_kconfig_make(
        runner,
        selected_tree,
        config_output,
        device.arch,
        "savedefconfig",
        toolchain_env,
    )
    saved_defconfig = config_output / "defconfig"
    if not saved_defconfig.is_file():
        raise BuildToolError("savedefconfig did not produce a canonical GKI defconfig")
    shutil.copy2(saved_defconfig, source_defconfig)

    # Recreate the full config exactly as Kleaf will: load the now-canonical
    # source gki_defconfig, then resolve defaults once more.
    requested_config.unlink(missing_ok=True)
    _run_kconfig_make(
        runner,
        selected_tree,
        config_output,
        device.arch,
        "gki_defconfig",
        toolchain_env,
    )
    _run_kconfig_make(
        runner,
        selected_tree,
        config_output,
        device.arch,
        "olddefconfig",
        toolchain_env,
    )
    if not requested_config.is_file():
        raise BuildToolError("Kleaf GKI defconfig did not produce a full .config")
    return requested_config, source_defconfig


def _configure_common_gki_defconfig(
    *,
    source_dir: Path,
    metadata_dir: Path,
    device: Device,
    fragments: Iterable[Path],
    forced: Mapping[str, str],
) -> tuple[Path, Path]:
    """Compatibility wrapper for the base common-kernel configuration."""

    return _configure_gki_defconfig(
        source_dir=source_dir,
        metadata_dir=metadata_dir,
        device=device,
        kernel_tree="common",
        fragments=fragments,
        forced=forced,
        work_name="config-work",
    )


def configure_kernel(
    *,
    root: Path,
    source_dir: Path,
    output_dir: Path,
    context_path: Path,
    profile: Profile,
    feature: FeatureProfile,
    device: Device,
    lock: DependencyLock,
    root_variant: str,
    optimization: str,
    lto: str,
    build_target: str,
    smoke: bool,
    check_only: bool,
) -> Path:
    assert_build_target_contract(build_target)
    context = load_context(context_path)
    minimum = "sources-synced" if smoke else "patches-applied"
    validate_lineage(context, profile, lock, minimum_stage=minimum)
    selections = context.get("features")
    if not smoke:
        if not isinstance(selections, list) or len(selections) != 1:
            raise BuildToolError("patch phase did not record exactly one feature selection")
        selection = selections[0]
        if selection.get("profile") != feature.id or selection.get("root_variant") != root_variant:
            raise BuildToolError("configuration selection differs from applied patch selection")
    if bool(context.get("smoke")) != bool(smoke):
        raise BuildToolError("smoke and real build contexts must never be mixed")
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = output_dir / ".op13"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    downloaded_kernel_context: dict[str, Any] | None = None
    downloaded_context_path = metadata_dir / "build-context.json"
    if build_target == "modules":
        if not downloaded_context_path.is_file():
            raise BuildToolError("modules-only configuration requires a downloaded kernel artifact context")
        downloaded_kernel_context = load_context(downloaded_context_path)
        validate_lineage(downloaded_kernel_context, profile, lock, minimum_stage="kernel-built")
        prerequisite_target = _configuration_build_target(
            downloaded_kernel_context,
            where="downloaded kernel",
        )
        if prerequisite_target != "mixed":
            raise BuildToolError(
                "modules-only configuration requires a mixed-target kernel artifact"
            )
    config_path = output_dir / ".config"
    fragments_by_tree = {
        kernel_tree: _fragment_paths(root, feature, build_target, kernel_tree)
        for kernel_tree in KERNEL_TREE_NAMES
    }
    merged_by_tree: dict[str, dict[str, str]] = {}
    for kernel_tree, tree_fragments in fragments_by_tree.items():
        merged: dict[str, str] = {}
        for fragment in tree_fragments:
            merged.update(parse_fragment(fragment))
        merged_by_tree[kernel_tree] = merged
    explicit_expectations = expected_symbols(
        feature,
        root_variant=root_variant,
        optimization=optimization,
        lto=lto,
    )
    if build_target == "kernel":
        omitted_module_symbols: set[str] = set()
        for fragment in feature.kconfig_fragments:
            if fragment.scope != "modules":
                continue
            path = resolve_inside(
                root,
                fragment.path,
                f"feature {feature.id} module fragment",
                must_exist=fragment.required,
            )
            if path.exists():
                omitted_module_symbols.update(parse_fragment(path))
        for symbol in omitted_module_symbols - set(merged_by_tree["common"]):
            explicit_expectations.pop(symbol, None)
    # A fragment is a requested feature surface, not a best-effort hint.  Assert
    # every merged value after olddefconfig so unknown symbols and unmet Kconfig
    # dependencies fail closed instead of disappearing from the final config.
    tuning_symbols = (
        "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE",
        "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE_O3",
        "CONFIG_LTO_CLANG_THIN",
        "CONFIG_LTO_CLANG_FULL",
    )
    overrides_by_tree: dict[str, dict[str, str]] = {}
    common_overrides = dict(merged_by_tree["common"])
    common_overrides.update(explicit_expectations)
    overrides_by_tree["common"] = common_overrides
    msm_overrides = dict(merged_by_tree["msm-kernel"])
    msm_requested_symbols = set(msm_overrides)
    msm_requested_symbols.update(tuning_symbols)
    msm_overrides.update(
        {
            symbol: value
            for symbol, value in explicit_expectations.items()
            if symbol in msm_requested_symbols
        }
    )
    overrides_by_tree["msm-kernel"] = msm_overrides
    if root_variant == "none":
        for tree_overrides in overrides_by_tree.values():
            tree_overrides.update(
                {
                    symbol: "n"
                    for symbol in tree_overrides
                    if symbol.startswith("CONFIG_KSU")
                }
            )
    # Only build-mode and explicit no-root selections are forced.  Feature
    # symbols remain assertions so a missing patch/Kconfig definition fails.
    forced_by_tree: dict[str, dict[str, str]] = {}
    for kernel_tree, tree_overrides in overrides_by_tree.items():
        forced = {symbol: tree_overrides[symbol] for symbol in tuning_symbols}
        if root_variant == "none":
            forced.update(
                {
                    symbol: "n"
                    for symbol in tree_overrides
                    if symbol.startswith("CONFIG_KSU")
                }
            )
        forced_by_tree[kernel_tree] = forced
    tree_requests = {
        kernel_tree: {
            "fragments": [
                {"path": str(path.resolve()), "sha256": sha256_file(path)}
                for path in fragments_by_tree[kernel_tree]
            ],
            "forced_symbols": dict(sorted(forced_by_tree[kernel_tree].items())),
            "required_symbols": dict(sorted(overrides_by_tree[kernel_tree].items())),
        }
        for kernel_tree in KERNEL_TREE_NAMES
    }
    request = {
        "schema_version": 1,
        "profile": profile.id,
        "feature_profile": feature.id,
        "root_variant": root_variant,
        "optimization": optimization,
        "lto": lto,
        "build_target": build_target,
        # Keep the common-tree aliases stable for artifact consumers while the
        # explicit per-tree records bind both Kleaf build inputs.
        **tree_requests["common"],
        "kernel_tree_requests": tree_requests,
    }
    atomic_write_json(metadata_dir / "config-request.json", request)
    if check_only:
        return config_path
    requested_configs: dict[str, Path] = {}
    source_defconfigs: dict[str, Path] = {}
    if smoke:
        for kernel_tree in KERNEL_TREE_NAMES:
            simulated = dict(merged_by_tree[kernel_tree])
            simulated.update(forced_by_tree[kernel_tree])
            # Smoke mode models defined feature Kconfig entries. It never
            # yields a releasable artifact and exists only for invariants.
            for symbol, value in overrides_by_tree[kernel_tree].items():
                simulated.setdefault(symbol, value)
            lines = [
                f"# {symbol} is not set" if value == "n" else f"{symbol}={value}"
                for symbol, value in sorted(simulated.items())
            ]
            if kernel_tree == "common":
                tree_config_path = config_path
            else:
                tree_config_path = metadata_dir / "config-work-msm-kernel" / ".config"
                tree_config_path.parent.mkdir(parents=True, exist_ok=True)
            tree_config_path.write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            requested_configs[kernel_tree] = tree_config_path
    else:
        requested_configs["common"], source_defconfigs["common"] = (
            _configure_common_gki_defconfig(
                source_dir=source_dir,
                metadata_dir=metadata_dir,
                device=device,
                fragments=fragments_by_tree["common"],
                forced=forced_by_tree["common"],
            )
        )
        requested_configs["msm-kernel"], source_defconfigs["msm-kernel"] = (
            _configure_gki_defconfig(
                source_dir=source_dir,
                metadata_dir=metadata_dir,
                device=device,
                kernel_tree="msm-kernel",
                fragments=fragments_by_tree["msm-kernel"],
                forced=forced_by_tree["msm-kernel"],
                work_name="config-work-msm-kernel",
            )
        )
        if downloaded_kernel_context is None:
            shutil.copy2(requested_configs["common"], config_path)
        elif not config_path.is_file():
            raise BuildToolError("downloaded mixed kernel artifact lacks its Image .config")
    for kernel_tree in KERNEL_TREE_NAMES:
        assert_symbols(
            requested_configs[kernel_tree],
            overrides_by_tree[kernel_tree],
        )
    requested_config_path = requested_configs["common"]
    overrides = overrides_by_tree["common"]
    explicit_module_symbols = sorted(
        symbol for symbol, value in overrides.items() if value == "m"
    )
    unmapped_module_symbols = sorted(
        set(explicit_module_symbols) - set(MODULE_OUTPUT_BY_SYMBOL)
    )
    if unmapped_module_symbols:
        raise BuildToolError(
            "requested module symbols lack an audited Kleaf output mapping: "
            + ", ".join(unmapped_module_symbols)
        )
    resolved_config = parse_dotconfig(requested_config_path)
    active_mapped_symbols = sorted(
        symbol
        for symbol in MODULE_OUTPUT_BY_SYMBOL
        if resolved_config.get(symbol) == "m"
    )
    if smoke:
        module_outputs_record = {
            "schema_version": 1,
            "locked_profile": "smoke",
            "changed": bool(
                resolve_module_outputs(active_mapped_symbols)["active_paths"]
            ),
            **resolve_module_outputs(active_mapped_symbols),
            "smoke": True,
        }
    else:
        module_outputs_record = integrate_common_kleaf_module_outputs(
            source_dir / device.common_kernel,
            active_mapped_symbols,
        )
        module_outputs_record["msm_dist_bzl"] = integrate_msm_kleaf_module_dist(
            source_dir / device.vendor_kernel,
            expected_profile=str(module_outputs_record["locked_profile"]),
        )
    requested_config_sha256 = sha256_file(requested_config_path)
    if downloaded_kernel_context is not None:
        downloaded_configuration = downloaded_kernel_context.get("configuration")
        if not isinstance(downloaded_configuration, dict):
            raise BuildToolError("downloaded kernel configuration record is absent")
        if sha256_file(config_path) != downloaded_configuration.get("config_sha256"):
            raise BuildToolError("downloaded Image .config differs from its recorded lineage")
        # A modules-only run consumes the exact Image and module kit from the
        # prerequisite mixed build.  It still reconstructs gki_defconfig in the
        # fresh source tree and proves that the request is identical.
        assert_symbols(config_path, overrides)
        config_sha256 = str(downloaded_configuration["config_sha256"])
    else:
        config_sha256 = sha256_file(config_path)
    kernel_tree_configs: dict[str, dict[str, Any]] = {}
    for kernel_tree in KERNEL_TREE_NAMES:
        tree_record: dict[str, Any] = {
            **tree_requests[kernel_tree],
            "requested_config_path": str(requested_configs[kernel_tree].resolve()),
            "requested_config_sha256": sha256_file(requested_configs[kernel_tree]),
        }
        source_defconfig = source_defconfigs.get(kernel_tree)
        if source_defconfig is not None:
            tree_record.update(
                {
                    "source_defconfig_path": str(source_defconfig.resolve()),
                    "source_defconfig_sha256": sha256_file(source_defconfig),
                }
            )
        kernel_tree_configs[kernel_tree] = tree_record
    configuration_record = {
        **request,
        "config_path": str(config_path.resolve()),
        "config_sha256": config_sha256,
        "requested_config_path": str(requested_config_path.resolve()),
        "requested_config_sha256": requested_config_sha256,
        "kernel_tree_configs": kernel_tree_configs,
        "module_outputs": module_outputs_record,
    }
    source_defconfig = source_defconfigs.get("common")
    if source_defconfig is not None:
        configuration_record.update(
            {
                "source_defconfig_path": str(source_defconfig.resolve()),
                "source_defconfig_sha256": sha256_file(source_defconfig),
            }
        )
    updated = advance_context(context, "configured", {"configuration": configuration_record})
    write_context(context_path, updated)
    if downloaded_kernel_context is not None:
        _compare_kernel_lineage(updated, downloaded_kernel_context)
        write_context(metadata_dir / "configuration-context.json", updated)
    else:
        write_context(metadata_dir / "build-context.json", updated)
    return config_path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _clean_official_output(source_dir: Path, output_dir: Path, device: Device, *, clean: bool) -> None:
    candidate = (source_dir / "kernel_platform" / "out").resolve()
    cache_dir = _official_cache_path(source_dir, device)
    if not _inside(candidate, source_dir) or candidate == source_dir.resolve():
        raise BuildToolError("refusing to clean an unsafe source output path")
    if _inside(output_dir, candidate):
        raise BuildToolError("build output must not be nested in the cleaned source output")
    if clean and candidate.exists():
        shutil.rmtree(candidate)
    if clean and cache_dir.exists():
        shutil.rmtree(cache_dir)
    official_output, kernel_kit = _official_build_paths(source_dir, device)
    generated_paths = (official_output / "dist", kernel_kit)
    for generated in generated_paths:
        resolved = generated.resolve()
        if not _inside(resolved, source_dir) or resolved == source_dir.resolve():
            raise BuildToolError(f"refusing to clean unsafe official output: {resolved}")
        if resolved.exists():
            shutil.rmtree(resolved)


def _validate_build_epoch(epoch: int) -> int:
    if epoch < 0 or epoch > MAX_BUILD_EPOCH:
        raise BuildToolError(
            "build timestamp must be between 1970-01-01 and 2107-12-31T23:59:58Z"
        )
    return epoch


def _build_epoch(source_dir: Path, common_kernel: str, requested: str | None) -> int:
    configured_timestamp = (requested or "").strip()
    if not configured_timestamp:
        configured_timestamp = (os.environ.get("BUILD_TIMESTAMP") or "").strip()
    if configured_timestamp:
        if configured_timestamp.isdigit():
            epoch = int(configured_timestamp)
        else:
            normalized = (
                configured_timestamp[:-1] + "+00:00"
                if configured_timestamp.endswith("Z")
                else configured_timestamp
            )
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise BuildToolError("BUILD_TIMESTAMP must be RFC3339 or an epoch integer") from exc
            if parsed.tzinfo is None:
                raise BuildToolError("BUILD_TIMESTAMP must include a timezone")
            epoch = int(parsed.timestamp())
        return _validate_build_epoch(epoch)
    source_date_epoch = (os.environ.get("SOURCE_DATE_EPOCH") or "").strip()
    if source_date_epoch:
        if not source_date_epoch.isdigit():
            raise BuildToolError("SOURCE_DATE_EPOCH must be an epoch integer")
        return _validate_build_epoch(int(source_date_epoch))
    runner = CommandRunner(verbose=False)
    result = runner.run(
        ["git", "log", "-1", "--format=%ct"],
        cwd=source_dir / common_kernel,
        capture=True,
    )
    text = result.stdout.strip()
    if not text.isdigit():
        raise BuildToolError("could not derive SOURCE_DATE_EPOCH from the common kernel commit")
    return _validate_build_epoch(int(text))


def _run_logged(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print("+ " + CommandRunner._display(argv), flush=True)
    merged_env = os.environ.copy()
    merged_env.update({str(key): str(value) for key, value in env.items()})
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=merged_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            return_code = process.wait()
    except FileNotFoundError as exc:
        raise BuildToolError(f"required build command not found: {argv[0]}") from exc
    if return_code != 0:
        raise BuildToolError(f"kernel build failed with exit code {return_code}; see {log_path}")


def _copy_artifact(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    return destination


def _require_exact_artifact(root: Path, name: str, *, where: str) -> Path:
    path = root / name
    if not path.is_file():
        raise BuildToolError(f"official {where} lacks {name}: {path}")
    return path


def _copy_tree_fresh(source: Path, destination: Path) -> Path:
    if not source.is_dir():
        raise BuildToolError(f"official kernel tree is missing: {source}")
    source_root = source.resolve()
    for path in source.rglob("*"):
        if not path.is_symlink():
            continue
        link_text = os.readlink(path)
        if os.path.isabs(link_text):
            raise BuildToolError(f"official kernel kit contains an absolute symlink: {path}")
        resolved_target = (path.parent / link_text).resolve()
        if not _inside(resolved_target, source_root):
            raise BuildToolError(f"official kernel kit symlink escapes its tree: {path}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True)
    return destination


def _copy_declared_dist_module_payload(
    source: Path,
    destination: Path,
    declared_paths: Iterable[object],
) -> tuple[list[Path], dict[str, Any]]:
    """Materialize audited paths from the mixed modules-staging archive."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    if isinstance(declared_paths, (str, bytes)):
        raise BuildToolError("declared module paths must be an iterable, not a string")
    declared_values = list(declared_paths)
    declared_record = verify_produced_module_outputs(declared_values, declared_values)
    ordered_paths = [PurePosixPath(value) for value in declared_record["paths"]]
    archive_path = source / "modules_staging_dir.tar.gz"
    if archive_path.is_symlink() or not archive_path.is_file():
        raise BuildToolError(
            "official sun/perf dist lacks the pinned modules_staging_archive output: "
            + str(archive_path)
        )
    copied: list[Path] = []
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            regular_members: dict[str, tarfile.TarInfo] = {}
            seen_members: set[str] = set()
            releases: set[str] = set()
            for member in archive.getmembers():
                raw_name = member.name
                if "\x00" in raw_name or "\\" in raw_name:
                    raise BuildToolError(
                        f"modules staging archive has an unsafe member: {raw_name!r}"
                    )
                while raw_name.startswith("./"):
                    raw_name = raw_name[2:]
                if raw_name in {"", "."}:
                    continue
                relative = PurePosixPath(raw_name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise BuildToolError(
                        f"modules staging archive has an unsafe member: {member.name!r}"
                    )
                normalized = relative.as_posix()
                if normalized in seen_members:
                    raise BuildToolError(
                        f"modules staging archive repeats member {normalized}"
                    )
                seen_members.add(normalized)
                parts = relative.parts
                if len(parts) >= 3 and parts[:2] == ("lib", "modules"):
                    releases.add(parts[2])
                if member.isdir():
                    continue
                if member.issym() and relative.name == "build":
                    # The generated build link is metadata and is never extracted.
                    continue
                if not member.isfile():
                    raise BuildToolError(
                        f"modules staging archive contains unsupported member {normalized}"
                    )
                regular_members[normalized] = member
            if len(releases) != 1:
                raise BuildToolError(
                    "modules staging archive must contain exactly one kernel release"
                )
            kernel_release = next(iter(releases))
            for relative in ordered_paths:
                member_name = (
                    f"lib/modules/{kernel_release}/kernel/{relative.as_posix()}"
                )
                member = regular_members.get(member_name)
                if member is None:
                    raise BuildToolError(
                        "modules staging archive lacks declared output "
                        + relative.as_posix()
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise BuildToolError(
                        f"cannot read modules staging archive member {member_name}"
                    )
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                if target.stat().st_size != member.size:
                    raise BuildToolError(
                        f"extracted module size differs for {relative.as_posix()}"
                    )
                copied.append(target)
    except (OSError, tarfile.TarError) as exc:
        raise BuildToolError(f"cannot read modules staging archive {archive_path}: {exc}") from exc
    (destination / "modules.order").write_text(
        "".join(f"{path.as_posix()}\n" for path in ordered_paths),
        encoding="utf-8",
        newline="\n",
    )
    archive_record = {
        "name": archive_path.name,
        "size": archive_path.stat().st_size,
        "sha256": sha256_file(archive_path),
        "kernel_release": kernel_release,
        "requested_paths_sha256": declared_record["declared_paths_sha256"],
    }
    return copied, archive_record


def _extract_image_config(common_kernel: Path, image: Path, destination: Path) -> Path:
    extractor = common_kernel / "scripts" / "extract-ikconfig"
    if not extractor.is_file():
        raise BuildToolError(f"kernel Image config extractor is missing: {extractor}")
    result = CommandRunner(verbose=False).run([str(extractor), str(image)], capture=True)
    payload = result.stdout
    if not payload or "CONFIG_" not in payload:
        raise BuildToolError("the exact sun/perf Image has no extractable IKCONFIG payload")
    destination.write_text(payload, encoding="utf-8", newline="\n")
    return destination


def build_kernel(
    *,
    source_dir: Path,
    output_dir: Path,
    context_path: Path,
    profile: Profile,
    device: Device,
    lock: DependencyLock,
    clean: bool,
    debug: bool,
    smoke: bool,
    dry_run: bool,
    branding: str | None,
    build_timestamp: str | None,
) -> dict[str, Any]:
    context = load_context(context_path)
    validate_lineage(context, profile, lock, minimum_stage="configured")
    if bool(context.get("smoke")) != bool(smoke):
        raise BuildToolError("smoke and real build contexts must never be mixed")
    configuration = context.get("configuration")
    if not isinstance(configuration, dict):
        raise BuildToolError("kernel configuration record is absent")
    build_target = _configuration_build_target(context, where="kernel")
    if build_target not in KERNEL_PHASE_TARGETS:
        raise BuildToolError(
            f"kernel compilation is not part of build target {build_target!r}"
        )
    config_path = output_dir / ".config"
    expected_requested_config = configuration.get(
        "requested_config_sha256", configuration.get("config_sha256")
    )
    if not config_path.is_file() or sha256_file(config_path) != expected_requested_config:
        raise BuildToolError("build .config differs from the configured lineage")
    selected_branding = branding or os.environ.get("KERNEL_BRANDING") or "OnePlus13-KernelBuilder"
    if not BRANDING_RE.fullmatch(selected_branding):
        raise BuildToolError("kernel branding contains unsupported characters")
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = output_dir / ".op13"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        command = [str(source_dir / device.official_script), *device.official_args]
        print("+ " + CommandRunner._display(command))
        return {"dry_run": True, "command": command}
    if not smoke:
        # Always recreate the public dist and kernel kit so an incremental
        # local build cannot retain an obsolete .ko. `clean=false` preserves
        # the official host/Bazel caches outside those generated surfaces.
        _clean_official_output(source_dir, output_dir, device, clean=clean)
    if smoke:
        epoch = 0
        image = output_dir / "Image"
        symvers = output_dir / "Module.symvers"
        image.write_bytes(b"OP13-SMOKE-IMAGE\n")
        symvers.write_text(
            "0000000000000000\tsmoke_symbol\tvmlinux\tEXPORT_SYMBOL\n",
            encoding="utf-8",
            newline="\n",
        )
        (output_dir / "System.map").write_text(
            "0000000000000000 T smoke_symbol\n", encoding="utf-8", newline="\n"
        )
        (output_dir / "vmlinux").write_bytes(b"OP13-SMOKE-VMLINUX\n")
        (metadata_dir / "kernel-build.log").write_text("smoke build\n", encoding="utf-8", newline="\n")
    else:
        epoch = _build_epoch(source_dir, device.common_kernel, build_timestamp)
        timestamp = datetime.fromtimestamp(epoch, timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
        script = source_dir / device.official_script
        if not script.is_file():
            raise BuildToolError(f"official OnePlus build script is missing: {script}")
        env = {
            "ARCH": device.arch,
            "RECOMPILE_KERNEL": "1",
            "COPY_NEEDED": "1",
            "LTO": str(configuration.get("lto")),
            "SOURCE_DATE_EPOCH": str(epoch),
            "KBUILD_BUILD_TIMESTAMP": timestamp,
            "KBUILD_BUILD_USER": "Hipuu",
            "KBUILD_BUILD_HOST": "github-actions",
            "LOCALVERSION": f"-{selected_branding}",
        }
        if debug:
            env["V"] = "1"
        _run_logged(
            [str(script), *device.official_args],
            cwd=source_dir,
            env=env,
            log_path=metadata_dir / "kernel-build.log",
        )
        official_output, source_kernel_kit = _official_build_paths(source_dir, device)
        official_dist = official_output / "dist"
        if not official_dist.is_dir():
            raise BuildToolError(f"official sun/perf dist directory is missing: {official_dist}")
        if not source_kernel_kit.is_dir():
            raise BuildToolError(f"official sun/perf kernel kit is missing: {source_kernel_kit}")
        exact_artifacts: dict[str, Path] = {}
        for name in ("Image", "Module.symvers", "System.map", ".config", "vmlinux"):
            dist_artifact = _require_exact_artifact(official_dist, name, where="sun/perf dist")
            kit_artifact = _require_exact_artifact(source_kernel_kit, name, where="sun kernel kit")
            if sha256_file(dist_artifact) != sha256_file(kit_artifact):
                raise BuildToolError(
                    f"official sun/perf dist and kernel kit disagree for {name}"
                )
            exact_artifacts[name] = dist_artifact
        _require_exact_artifact(source_kernel_kit, "build_opts.txt", where="sun kernel kit")

        preserved_kernel_kit = _copy_tree_fresh(source_kernel_kit, output_dir / "kernel-kit")
        preserved_dist_root = output_dir / "kernel-dist-modules"
        configured_module_outputs = configuration.get("module_outputs")
        if not isinstance(configured_module_outputs, dict):
            raise BuildToolError("configured Kleaf module-output record is absent")
        declared_module_paths = configured_module_outputs.get("requested_paths")
        if not isinstance(declared_module_paths, list):
            raise BuildToolError("configured Kleaf module-output paths are absent")
        preserved_dist_modules, module_staging_archive = _copy_declared_dist_module_payload(
            official_dist,
            preserved_dist_root,
            declared_module_paths,
        )
        official_module_paths, produced_module_outputs = _verify_official_module_payload(
            preserved_dist_root,
            declared_module_paths,
        )
        official_modules_order_record, official_module_records = (
            _record_official_module_payload(
                preserved_dist_root,
                official_module_paths,
            )
        )
        image = _copy_artifact(exact_artifacts["Image"], output_dir / "Image")
        symvers = _copy_artifact(exact_artifacts["Module.symvers"], output_dir / "Module.symvers")
        _copy_artifact(exact_artifacts["System.map"], output_dir / "System.map")
        _copy_artifact(exact_artifacts["vmlinux"], output_dir / "vmlinux")
        if image.stat().st_size < 1024 * 1024:
            raise BuildToolError("built Image is implausibly small")
        _extract_image_config(source_dir / device.common_kernel, image, config_path)
        required_symbols = configuration.get("required_symbols")
        if not isinstance(required_symbols, dict):
            raise BuildToolError("configured symbol request is absent")
        assert_symbols(config_path, required_symbols)
        module_config = preserved_kernel_kit / ".config"
        module_symvers = preserved_kernel_kit / "Module.symvers"
        configured_tree_records = configuration.get("kernel_tree_configs")
        if not isinstance(configured_tree_records, dict):
            raise BuildToolError("configured per-tree Kconfig lineage is absent")
        common_tree_record = configured_tree_records.get("common")
        msm_tree_record = configured_tree_records.get("msm-kernel")
        if not isinstance(common_tree_record, dict) or not isinstance(msm_tree_record, dict):
            raise BuildToolError("configured common/MSM Kconfig lineage is absent")
        msm_required_symbols = msm_tree_record.get("required_symbols")
        if not isinstance(msm_required_symbols, dict):
            raise BuildToolError("configured MSM symbol request is absent")
        assert_symbols(module_config, msm_required_symbols)
        if sha256_file(module_symvers) != sha256_file(symvers):
            raise BuildToolError("preserved kernel kit Module.symvers differs from the exact dist")
        built_tree_records = {
            "common": {
                **common_tree_record,
                "built_config_path": str(config_path.resolve()),
                "built_config_sha256": sha256_file(config_path),
            },
            "msm-kernel": {
                **msm_tree_record,
                "built_config_path": str(module_config.resolve()),
                "built_config_sha256": sha256_file(module_config),
            },
        }
        configuration = {
            **configuration,
            "config_path": str(config_path.resolve()),
            "config_sha256": sha256_file(config_path),
            "image_config_path": str(config_path.resolve()),
            "image_config_sha256": sha256_file(config_path),
            "module_config_path": str(module_config.resolve()),
            "module_config_sha256": sha256_file(module_config),
            "kernel_kit_path": str(preserved_kernel_kit.resolve()),
            "kernel_tree_configs": built_tree_records,
            "module_outputs": {
                **configured_module_outputs,
                "produced": produced_module_outputs,
            },
        }
        context = dict(context)
        context["configuration"] = configuration
    image = output_dir / "Image"
    symvers = output_dir / "Module.symvers"
    kernel_record: dict[str, Any] = {
        "build_target": build_target,
        "branding": selected_branding,
        "source_date_epoch": epoch,
        "debug": bool(debug),
        "image": record_for_file(image, role="kernel-image"),
        "module_symvers": record_for_file(symvers, role="module-symvers"),
        "build_log": record_for_file(metadata_dir / "kernel-build.log", role="kernel-build-log"),
    }
    if not smoke:
        kernel_record.update(
            {
                "image_config": record_for_file(config_path, role="image-config"),
                "module_config": record_for_file(
                    output_dir / "kernel-kit" / ".config", role="kernel-kit-config"
                ),
                "kernel_kit": str((output_dir / "kernel-kit").resolve()),
                "official_dist_module_count": len(official_module_paths),
                "official_dist_payload_count": len(preserved_dist_modules) + 1,
                "module_staging_archive": module_staging_archive,
                "official_modules_order": official_modules_order_record,
                "official_modules": official_module_records,
                "module_outputs": produced_module_outputs,
            }
        )
    for name, role in (("System.map", "system-map"), ("vmlinux", "vmlinux")):
        candidate = output_dir / name
        if candidate.is_file():
            kernel_record[name.lower().replace(".", "_")] = record_for_file(candidate, role=role)
    resolved_source = Path(str(context["manifest"]["resolved_path"]))
    embedded_manifest = metadata_dir / "resolved-manifest.xml"
    shutil.copy2(resolved_source, embedded_manifest)
    portable_context = dict(context)
    portable_context.pop("context_sha256", None)
    portable_context["manifest"] = dict(context["manifest"])
    portable_context["manifest"]["resolved_path"] = str(embedded_manifest.resolve())
    portable_context["manifest"]["sha256"] = sha256_file(embedded_manifest)
    updated = advance_context(portable_context, "kernel-built", {"kernel": kernel_record})
    write_context(context_path, updated)
    write_context(metadata_dir / "build-context.json", updated)
    return kernel_record


def _compare_kernel_lineage(source_context: Mapping[str, Any], kernel_context: Mapping[str, Any]) -> None:
    fields = ("profile", "target", "arch", "kmi")
    for field in fields:
        if source_context.get(field) != kernel_context.get(field):
            raise BuildToolError(f"kernel artifact lineage differs at {field}")
    source_manifest = source_context.get("manifest")
    kernel_manifest = kernel_context.get("manifest")
    if not isinstance(source_manifest, dict) or not isinstance(kernel_manifest, dict):
        raise BuildToolError("manifest lineage is absent")
    for field in ("url", "file", "revision", "sha256", "locked_sha256"):
        if source_manifest.get(field) != kernel_manifest.get(field):
            raise BuildToolError(f"kernel artifact source lock differs at manifest.{field}")
    if source_context.get("features") != kernel_context.get("features"):
        raise BuildToolError("kernel artifact feature selection differs from this module build")
    source_config = source_context.get("configuration")
    kernel_config = kernel_context.get("configuration")
    if not isinstance(source_config, dict) or not isinstance(kernel_config, dict):
        raise BuildToolError("configuration lineage is absent")
    source_target = _configuration_build_target(source_context, where="module source")
    kernel_target = _configuration_build_target(kernel_context, where="kernel artifact")
    if source_target not in MODULE_PHASE_TARGETS:
        raise BuildToolError(
            f"module compilation is not part of build target {source_target!r}"
        )
    if kernel_target != "mixed":
        raise BuildToolError(
            "module compilation requires a mixed-target kernel artifact"
        )
    for field in (
        "profile",
        "feature_profile",
        "root_variant",
        "optimization",
        "lto",
        "requested_config_sha256",
        "config_sha256",
    ):
        if source_config.get(field) != kernel_config.get(field):
            raise BuildToolError(f"kernel artifact configuration differs at {field}")
    source_trees = source_config.get("kernel_tree_configs")
    kernel_trees = kernel_config.get("kernel_tree_configs")
    if not isinstance(source_trees, dict) or not isinstance(kernel_trees, dict):
        raise BuildToolError("kernel artifact per-tree Kconfig lineage is absent")
    for kernel_tree in KERNEL_TREE_NAMES:
        source_tree = source_trees.get(kernel_tree)
        kernel_tree_record = kernel_trees.get(kernel_tree)
        if not isinstance(source_tree, dict) or not isinstance(kernel_tree_record, dict):
            raise BuildToolError(
                f"kernel artifact Kconfig lineage is absent for {kernel_tree}"
            )
        for field in (
            "forced_symbols",
            "required_symbols",
            "requested_config_sha256",
            "source_defconfig_sha256",
        ):
            if source_tree.get(field) != kernel_tree_record.get(field):
                raise BuildToolError(
                    f"kernel artifact Kconfig lineage differs at {kernel_tree}.{field}"
                )
        source_fragment_digests = [
            item.get("sha256")
            for item in source_tree.get("fragments", [])
            if isinstance(item, dict)
        ]
        kernel_fragment_digests = [
            item.get("sha256")
            for item in kernel_tree_record.get("fragments", [])
            if isinstance(item, dict)
        ]
        if source_fragment_digests != kernel_fragment_digests:
            raise BuildToolError(
                f"kernel artifact Kconfig lineage differs at {kernel_tree}.fragments"
            )
    source_outputs = source_config.get("module_outputs")
    kernel_outputs = kernel_config.get("module_outputs")
    if not isinstance(source_outputs, dict) or not isinstance(kernel_outputs, dict):
        raise BuildToolError("Kleaf module-output lineage is absent")
    for field in (
        "locked_profile",
        "changed",
        "active_symbols",
        "requested_paths",
        "official_paths",
        "active_paths",
        "requested_paths_sha256",
        "active_paths_sha256",
    ):
        if source_outputs.get(field) != kernel_outputs.get(field):
            raise BuildToolError(f"kernel artifact module-output lineage differs at {field}")
    for field in ("modules_bzl", "build_bazel", "msm_dist_bzl"):
        source_file = source_outputs.get(field)
        kernel_file = kernel_outputs.get(field)
        if not isinstance(source_file, dict) or not isinstance(kernel_file, dict):
            # Smoke contexts intentionally model the declaration without
            # synthesizing locked source files.
            if bool(source_context.get("smoke")) and bool(kernel_context.get("smoke")):
                continue
            raise BuildToolError(f"kernel artifact module-output {field} lineage is absent")
        for digest_field in ("pre_sha256", "post_sha256"):
            if source_file.get(digest_field) != kernel_file.get(digest_field):
                raise BuildToolError(
                    f"kernel artifact module-output lineage differs at {field}.{digest_field}"
                )


def _clean_module_output(output_dir: Path, kernel_output: Path) -> None:
    if output_dir.resolve() == kernel_output.resolve():
        raise BuildToolError("module output must not equal the kernel output root")
    if not _inside(output_dir, kernel_output.parent) and not _inside(output_dir, kernel_output):
        raise BuildToolError("refusing to clean module output outside the build tree")
    if output_dir.exists():
        shutil.rmtree(output_dir)


def _module_vermagic(runner: CommandRunner, module: Path) -> str:
    result = runner.run(["modinfo", "-F", "vermagic", str(module)], capture=True)
    vermagic = result.stdout.strip()
    release = vermagic.split()[0] if vermagic else ""
    if not release or any(character.isspace() for character in release):
        raise BuildToolError(f"invalid module vermagic for {module}: {vermagic!r}")
    return release


def _official_modules_order_path(root: Path) -> Path:
    candidates = sorted(root.rglob("modules.order")) if root.is_dir() else []
    if len(candidates) != 1:
        raise BuildToolError(
            "preserved common-module payload must contain one deterministic "
            f"modules.order, found {len(candidates)}"
        )
    if candidates[0].is_symlink():
        raise BuildToolError("preserved modules.order must not be a symbolic link")
    return candidates[0]


def _official_modules_order(root: Path) -> list[Path]:
    order_path = _official_modules_order_path(root)
    ordered: list[Path] = []
    seen: set[str] = set()
    for line_number, line in enumerate(order_path.read_text(encoding="utf-8").splitlines(), 1):
        value = line.strip()
        if not value:
            continue
        if value.startswith("./"):
            value = value[2:]
        if "\\" in value:
            raise BuildToolError(f"modules.order line {line_number} uses a non-portable path")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".ko":
            raise BuildToolError(f"modules.order line {line_number} is unsafe: {value!r}")
        normalized = relative.as_posix()
        if normalized in seen:
            raise BuildToolError(f"modules.order contains duplicate path {normalized}")
        seen.add(normalized)
        ordered.append(relative)
    if not ordered:
        raise BuildToolError("exact sun/perf modules.order is empty")
    return ordered


def _match_dist_module(root: Path, relative: Path) -> Path:
    exact = root / relative
    if exact.is_file() and not exact.is_symlink():
        return exact
    candidates = [
        path for path in sorted(root.rglob(relative.name))
        if path.is_file() and not path.is_symlink()
    ]
    if candidates:
        minimum_depth = min(len(path.relative_to(root).parts) for path in candidates)
        shallow = [
            path for path in candidates if len(path.relative_to(root).parts) == minimum_depth
        ]
        if len(shallow) == 1:
            return shallow[0]
    if len(candidates) != 1:
        raise BuildToolError(
            f"official dist module is missing or ambiguous for {relative.as_posix()}"
        )
    return candidates[0]


def _verify_official_module_payload(
    root: Path,
    declared_paths: Iterable[object],
) -> tuple[list[Path], dict[str, object]]:
    """Bind audited Kleaf declarations to one exact official dist payload."""

    ordered_paths = _official_modules_order(root)
    used_sources: set[Path] = set()
    for relative in ordered_paths:
        module = _match_dist_module(root, relative)
        source_key = module.resolve()
        if source_key in used_sources:
            raise BuildToolError(
                f"one official .ko was matched to multiple modules.order paths: {module}"
            )
        used_sources.add(source_key)
    mapped_paths = set(mapped_module_output_paths())
    produced_paths = [
        relative.as_posix()
        for relative in ordered_paths
        if relative.as_posix() in mapped_paths
    ]
    verification = verify_produced_module_outputs(declared_paths, produced_paths)
    return ordered_paths, verification


def _record_official_module_payload(
    root: Path,
    ordered_paths: Iterable[Path],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    order_record = record_for_file(
        _official_modules_order_path(root),
        role="official-modules-order",
        root=root,
    )
    module_records: list[dict[str, Any]] = []
    for relative in ordered_paths:
        module = _match_dist_module(root, relative)
        record = record_for_file(module, role="official-dist-module", root=root)
        record["official_path"] = relative.as_posix()
        module_records.append(record)
    return order_record, module_records


def _resolve_official_payload_record(
    root: Path,
    record: Mapping[str, Any],
    *,
    role: str,
) -> Path:
    value = record.get("path")
    if not isinstance(value, str) or not value or "\\" in value:
        raise BuildToolError(f"recorded {role} has no portable dist-relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise BuildToolError(f"recorded {role} path escapes the official dist")
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise BuildToolError(f"recorded {role} path escapes the official dist") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise BuildToolError(f"recorded {role} is missing: {relative.as_posix()}")
    if candidate.stat().st_size != record.get("size"):
        raise BuildToolError(f"recorded {role} size differs from kernel lineage")
    if sha256_file(candidate) != record.get("sha256"):
        raise BuildToolError(f"recorded {role} digest differs from kernel lineage")
    return candidate


def _validate_official_module_payload_records(
    root: Path,
    order_record: Mapping[str, Any],
    module_records: Iterable[object],
) -> list[Path]:
    """Validate preserved official module bytes before reuse or packaging."""

    expected_order = _resolve_official_payload_record(
        root,
        order_record,
        role="modules.order",
    )
    if expected_order.resolve() != _official_modules_order_path(root).resolve():
        raise BuildToolError("recorded modules.order is not the selected official order file")
    ordered_paths = _official_modules_order(root)
    if isinstance(module_records, (str, bytes)):
        raise BuildToolError("official module records must be a list")
    records = list(module_records)
    if len(records) != len(ordered_paths):
        raise BuildToolError("official module record count differs from modules.order")
    observed_official_paths: list[str] = []
    observed_record_paths: set[Path] = set()
    for index, (record, relative) in enumerate(zip(records, ordered_paths, strict=True)):
        if not isinstance(record, dict):
            raise BuildToolError(f"official module record {index} is invalid")
        official_path = record.get("official_path")
        if official_path != relative.as_posix():
            raise BuildToolError("official module record order differs from modules.order")
        module = _resolve_official_payload_record(root, record, role="official module")
        resolved = module.resolve()
        if resolved in observed_record_paths:
            raise BuildToolError("official module records repeat one dist file")
        if resolved != _match_dist_module(root, relative).resolve():
            raise BuildToolError(
                f"recorded official module does not resolve {relative.as_posix()}"
            )
        observed_record_paths.add(resolved)
        observed_official_paths.append(str(official_path))
    if observed_official_paths != [path.as_posix() for path in ordered_paths]:
        raise BuildToolError("official module records differ from modules.order")
    return ordered_paths


def _stage_official_modules(
    *,
    kernel_output: Path,
    staging: Path,
    runner: CommandRunner,
    requested_symbols: list[str],
    expected_paths: Iterable[str],
    expected_order_record: Mapping[str, Any],
    expected_module_records: Iterable[object],
    memkernel_enabled: bool,
) -> tuple[str, list[dict[str, Any]], dict[str, object]]:
    dist_root = kernel_output / "kernel-dist-modules"
    if requested_symbols and not dist_root.is_dir():
        raise BuildToolError(
            "requested in-tree modules are absent from the exact sun/perf output; "
            "export the final device modules_staging_archive through the pinned "
            "sun_perf Kleaf dist target and extract the audited .ko paths"
        )
    _validate_official_module_payload_records(
        dist_root,
        expected_order_record,
        expected_module_records,
    )
    ordered_paths, output_verification = _verify_official_module_payload(
        dist_root,
        expected_paths,
    )
    ordered_names = {path.as_posix() for path in ordered_paths}
    releases: set[str] = set()
    resolved: list[tuple[Path, Path]] = []
    used_sources: set[Path] = set()
    for relative in ordered_paths:
        module = _match_dist_module(dist_root, relative)
        source_key = module.resolve()
        if source_key in used_sources:
            raise BuildToolError(
                f"one official .ko was matched to multiple modules.order paths: {module}"
            )
        used_sources.add(source_key)
        resolved.append((relative, module))
        releases.add(_module_vermagic(runner, module))
    if len(releases) != 1:
        raise BuildToolError(
            "official sun/perf module outputs do not identify one exact kernel release"
        )
    kernel_release = next(iter(releases))
    destination_root = staging / "lib" / "modules" / kernel_release / "extra" / "official"
    records: list[dict[str, Any]] = []
    for relative, module in resolved:
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(module, destination)
        record = record_for_file(destination, role="in-tree-module", root=staging)
        record["official_path"] = relative.as_posix()
        records.append(record)
    if memkernel_enabled and "drivers/memkernel/memkernel.ko" not in ordered_names:
        raise BuildToolError(
            "CONFIG_MEMKERNEL=m but exact sun/perf emitted no memkernel.ko; add the "
            "MemKernel output to the audited common Kleaf arm64 module_implicit_outs"
        )
    return kernel_release, records, output_verification


def _external_module_commands(
    source_dir: Path,
    output_dir: Path,
    device: Device,
    dependencies: Iterable[str],
) -> list[list[str]]:
    kernel_platform = source_dir / "kernel_platform"
    target, variant = device.official_args[:2]
    commands: list[list[str]] = [
        [str(kernel_platform / "build" / "brunch"), target, variant]
    ]
    commands.extend(
        ["bash", str(kernel_platform / "build" / "build_module.sh")]
        for _ in dependencies
    )
    return commands


def _record_module_staging(staging: Path) -> list[dict[str, Any]]:
    """Record every packaged staging file and reject link-based path aliases."""

    if not staging.is_dir():
        raise BuildToolError(f"module staging directory is missing: {staging}")
    records: list[dict[str, Any]] = []
    for path in sorted(staging.rglob("*")):
        if path.is_symlink():
            raise BuildToolError(f"module staging contains a symbolic link: {path}")
        if path.is_file():
            records.append(record_for_file(path, role="module-staging-file", root=staging))
    return records


def build_external_modules(
    *,
    source_dir: Path,
    kernel_output: Path,
    output_dir: Path,
    source_context_path: Path,
    profile: Profile,
    feature: FeatureProfile,
    device: Device,
    lock: DependencyLock,
    cache_root: Path,
    clean: bool,
    debug: bool,
    smoke: bool,
    dry_run: bool,
) -> dict[str, Any]:
    source_context = load_context(source_context_path)
    validate_lineage(source_context, profile, lock, minimum_stage="configured")
    kernel_context_path = kernel_output / ".op13" / "build-context.json"
    kernel_context = load_context(kernel_context_path)
    validate_lineage(kernel_context, profile, lock, minimum_stage="kernel-built")
    source_target = _configuration_build_target(source_context, where="module source")
    _compare_kernel_lineage(source_context, kernel_context)
    if bool(source_context.get("smoke")) != bool(smoke) or bool(kernel_context.get("smoke")) != bool(smoke):
        raise BuildToolError("smoke and real build contexts must never be mixed")
    symvers = kernel_output / "Module.symvers"
    if not symvers.is_file():
        raise BuildToolError(f"kernel artifact lacks Module.symvers: {symvers}")
    assert_symvers_lineage(kernel_context, symvers)
    if output_dir.exists() and not dry_run:
        _clean_module_output(output_dir, kernel_output)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = output_dir / ".op13"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    selected_dependencies = list(feature.external_modules)
    memkernel_enabled = bool(feature.flags.get("nethunter.memkernel", False))
    config_path = kernel_output / ".config"
    source_configuration = source_context.get("configuration")
    kernel_configuration = kernel_context.get("configuration")
    if not isinstance(source_configuration, dict) or not isinstance(kernel_configuration, dict):
        raise BuildToolError("module configuration lineage is absent")
    if not config_path.is_file() or sha256_file(config_path) != source_configuration.get("config_sha256"):
        raise BuildToolError("module build Image .config differs from the configured lineage")
    requested_module_symbols = sorted(
        symbol for symbol, value in parse_dotconfig(config_path).items() if value == "m"
    )
    commands = _external_module_commands(source_dir, output_dir, device, selected_dependencies)
    if dry_run:
        for command in commands:
            print("+ " + CommandRunner._display(command))
        return {
            "dry_run": True,
            "commands": commands,
            "requested_module_symbols": requested_module_symbols,
            "kernel_kit": str((kernel_output / "kernel-kit").resolve()),
        }

    records: list[dict[str, Any]] = []
    staging = output_dir / "staging"
    in_tree_record: dict[str, Any] = {
        "requested_symbols": requested_module_symbols,
        "modules": [],
        "memkernel_commit": (
            lock.dependencies["memkernel"].commit if memkernel_enabled else None
        ),
    }
    log_path = metadata_dir / "modules-build.log"
    if smoke:
        kernel_release = "6.6.0-op13-smoke"
        module_root = staging / "lib" / "modules" / kernel_release / "extra"
        module_root.mkdir(parents=True, exist_ok=True)
        if requested_module_symbols:
            in_tree_module = module_root / "in-tree-smoke.ko"
            in_tree_module.write_bytes(b"OP13-SMOKE-IN-TREE-MODULES\n")
            in_tree_record.update(
                {
                    "modules": [record_for_file(in_tree_module, role="in-tree-module", root=staging)],
                    "vermagic": kernel_release,
                    "smoke": True,
                }
            )
        for dependency_id in selected_dependencies:
            dependency = lock.dependencies[dependency_id]
            if dependency.kind != "git":
                raise BuildToolError(
                    f"external module {dependency_id} must be a pinned Git dependency"
                )
            module = module_root / f"{dependency_id}-smoke.ko"
            module.write_bytes(f"OP13-SMOKE-MODULE:{dependency_id}\n".encode("ascii"))
            records.append(
                {
                    "dependency": dependency_id,
                    "locked_commit": dependency.commit,
                    "modules": [record_for_file(module, role="external-module", root=staging)],
                    "vermagic": kernel_release,
                    "smoke": True,
                }
            )
        log_path.write_text("smoke modules build\n", encoding="utf-8", newline="\n")
    else:
        kernel_kit = kernel_output / "kernel-kit"
        for name in (".config", "Module.symvers", "Image", "System.map", "build_opts.txt"):
            _require_exact_artifact(kernel_kit, name, where="preserved kernel kit")
        expected_module_config = kernel_configuration.get("module_config_sha256")
        if not isinstance(expected_module_config, str) or sha256_file(kernel_kit / ".config") != expected_module_config:
            raise BuildToolError("preserved kernel-kit .config differs from the kernel lineage")
        if sha256_file(kernel_kit / "Module.symvers") != sha256_file(symvers):
            raise BuildToolError("preserved kernel-kit Module.symvers differs from the kernel lineage")
        fetch_dependencies(lock, cache_root, selected=selected_dependencies, dry_run=False, offline=False)
        runner = CommandRunner()
        kernel_release, installed_in_tree, output_verification = _stage_official_modules(
            kernel_output=kernel_output,
            staging=staging,
            runner=runner,
            requested_symbols=requested_module_symbols,
            expected_paths=(
                kernel_configuration.get("module_outputs", {}).get("requested_paths", [])
                if isinstance(kernel_configuration.get("module_outputs"), dict)
                else []
            ),
            expected_order_record=(
                kernel_context.get("kernel", {}).get("official_modules_order", {})
                if isinstance(kernel_context.get("kernel"), dict)
                else {}
            ),
            expected_module_records=(
                kernel_context.get("kernel", {}).get("official_modules", [])
                if isinstance(kernel_context.get("kernel"), dict)
                else []
            ),
            memkernel_enabled=memkernel_enabled,
        )
        in_tree_record.update(
            {
                "modules": installed_in_tree,
                "vermagic": kernel_release,
                "module_config_sha256": sha256_file(kernel_kit / ".config"),
                "module_symvers_sha256": sha256_file(kernel_kit / "Module.symvers"),
                "module_outputs": output_verification,
            }
        )
        kernel_platform = source_dir / "kernel_platform"
        # Invoke the repository's top-level symlink. The helper derives
        # ROOT_DIR from $0; calling its kernel/ target directly shifts the root
        # into kernel_platform/build and breaks the pinned build environment.
        module_helper = kernel_platform / "build" / "build_module.sh"
        brunch = kernel_platform / "build" / "brunch"
        if not module_helper.is_file():
            raise BuildToolError(f"pinned external-module helper is missing: {module_helper}")
        if not brunch.is_file():
            raise BuildToolError(f"pinned brunch helper is missing: {brunch}")
        target, variant = device.official_args[:2]
        work_root = output_dir / "work"
        work_root.mkdir(parents=True, exist_ok=True)
        brunch_out = work_root / "brunch"
        runner.run(
            [str(brunch), target, variant],
            cwd=kernel_platform,
            env={"OUT_DIR": str(brunch_out.resolve())},
        )
        build_config = kernel_platform / "build.config"
        if not build_config.is_file():
            raise BuildToolError("pinned brunch did not generate kernel_platform/build.config")
        log_path.write_text(
            "+ " + CommandRunner._display([str(brunch), target, variant]) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        external_root = kernel_platform / "msm-kernel" / "op13_external"
        external_root.mkdir(parents=True, exist_ok=True)
        for dependency_id in selected_dependencies:
            dependency = lock.dependencies[dependency_id]
            if dependency.kind != "git":
                raise BuildToolError(f"external module {dependency_id} must be a pinned Git dependency")
            cached = cache_root / "git" / dependency_id
            if not cached.is_dir():
                raise BuildToolError(f"external module checkout is absent: {cached}")
            work = external_root / dependency_id
            if work.exists():
                shutil.rmtree(work)
            shutil.copytree(cached, work, ignore=shutil.ignore_patterns(".git"))
            for stale_module in work.rglob("*.ko"):
                stale_module.unlink()
            module_out = work_root / dependency_id
            module_dist = module_out / "dist"
            relative_module = work.relative_to(kernel_platform).as_posix()
            env = {
                "BUILD_CONFIG": "build.config",
                "EXT_MODULES": relative_module,
                "OUT_DIR": str(module_out.resolve()),
                "DIST_DIR": str(module_dist.resolve()),
                "KERNEL_KIT": str(kernel_kit.resolve()),
                "TARGET_BOARD_PLATFORM": target,
                "VARIANT": variant,
            }
            if debug:
                env["V"] = "1"
            try:
                runner.run(["bash", str(module_helper)], cwd=kernel_platform, env=env)
                candidates: list[Path] = []
                seen: set[Path] = set()
                for root in (module_dist, module_out, work):
                    if not root.is_dir():
                        continue
                    for module in sorted(root.rglob("*.ko")):
                        resolved = module.resolve()
                        if resolved not in seen:
                            seen.add(resolved)
                            candidates.append(module)
                if not candidates:
                    raise BuildToolError(f"external module {dependency_id} produced no .ko files")
                installed: list[dict[str, Any]] = []
                destination_root = staging / "lib" / "modules" / kernel_release / "extra" / dependency_id
                destination_root.mkdir(parents=True, exist_ok=True)
                installed_names: dict[str, str] = {}
                for module in candidates:
                    digest = sha256_file(module)
                    if module.name in installed_names:
                        if installed_names[module.name] != digest:
                            raise BuildToolError(
                                f"external module {dependency_id} emitted conflicting {module.name} files"
                            )
                        continue
                    destination = destination_root / module.name
                    shutil.copy2(module, destination)
                    observed_release = _module_vermagic(runner, destination)
                    if observed_release != kernel_release:
                        raise BuildToolError(
                            f"module vermagic mismatch for {destination}: "
                            f"{observed_release!r}, expected {kernel_release}"
                        )
                    installed_names[module.name] = digest
                    installed.append(record_for_file(destination, role="external-module", root=staging))
                records.append(
                    {
                        "dependency": dependency_id,
                        "locked_commit": dependency.commit,
                        "modules": installed,
                        "vermagic": kernel_release,
                        "builder": "kernel_platform/build/build_module.sh",
                    }
                )
                with log_path.open("a", encoding="utf-8", newline="\n") as log:
                    log.write("+ " + CommandRunner._display(["bash", str(module_helper)]) + "\n")
            finally:
                if work.exists():
                    shutil.rmtree(work)
        system_map = kernel_output / "System.map"
        if not system_map.is_file():
            raise BuildToolError("kernel artifact lacks System.map for unresolved-symbol validation")
        depmod = runner.run(
            ["depmod", "-e", "-F", str(system_map), "-b", str(staging), kernel_release],
            capture=True,
        )
        depmod_output = (depmod.stdout or "") + (depmod.stderr or "")
        if "needs unknown symbol" in depmod_output.lower():
            raise BuildToolError(f"depmod found unresolved symbols:\n{depmod_output.strip()}")
        with log_path.open("a", encoding="utf-8", newline="\n") as log:
            log.write(depmod_output)
    modules_record = {
        "build_target": source_target,
        "kernel_release": kernel_release,
        "module_symvers_sha256": sha256_file(symvers),
        "in_tree_modules": in_tree_record,
        "external_dependency_ids": list(selected_dependencies),
        "external_modules": records,
        "staging": str(staging.resolve()),
        "staging_files": _record_module_staging(staging),
        "build_log": record_for_file(log_path, role="modules-build-log"),
        "debug": bool(debug),
        "clean_requested": bool(clean),
    }
    combined_context = dict(source_context)
    combined_context["kernel"] = kernel_context["kernel"]
    updated = advance_context(combined_context, "modules-built", {"modules": modules_record})
    write_context(source_context_path, updated)
    write_context(kernel_context_path, updated)
    write_context(metadata_dir / "build-context.json", updated)
    return modules_record
