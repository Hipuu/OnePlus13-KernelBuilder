"""Safe artifact verification, deterministic packaging, and provenance."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .build import (
    BUILD_TARGETS,
    ROOT_VARIANTS,
    _validate_official_module_payload_records,
    assert_symbols,
    expected_symbols,
    parse_dotconfig,
)
from .config import DependencyLock, FeatureProfile, Profile, sha256_bytes, sha256_file
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


def _record_matches(path: Path, record: Mapping[str, Any], role: str) -> None:
    if not path.is_file():
        raise BuildToolError(f"missing {role}: {path}")
    if path.stat().st_size != record.get("size"):
        raise BuildToolError(f"{role} size differs from build lineage")
    if sha256_file(path) != record.get("sha256"):
        raise BuildToolError(f"{role} digest differs from build lineage")


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
    if build_target not in BUILD_TARGETS:
        raise BuildToolError(f"unsupported build target {build_target!r}")
    context_path = output_dir / ".op13" / "build-context.json"
    context = load_context(context_path)
    required_stage = "modules-built" if build_target in {"modules", "mixed", "monolithic"} else "kernel-built"
    validate_lineage(context, profile, lock, minimum_stage=required_stage)
    if bool(context.get("smoke")) != bool(smoke):
        raise BuildToolError("smoke output must be verified explicitly and cannot be released as real")
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
    required = expected_symbols(
        feature,
        root_variant=root_variant,
        optimization=str(configuration.get("optimization")),
        lto=str(configuration.get("lto")),
    )
    assert_symbols(config_path, required)
    kernel = context.get("kernel")
    if not isinstance(kernel, dict):
        raise BuildToolError("kernel record is absent")
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
        in_tree = modules.get("in_tree_modules")
        if not isinstance(in_tree, dict):
            raise BuildToolError("in-tree module record is absent")
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


def deterministic_zip(source: Path, destination: Path, *, epoch: int) -> None:
    if not source.is_dir():
        raise BuildToolError(f"ZIP source directory is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
            if path.is_symlink():
                raise BuildToolError(f"symlinks are forbidden in release ZIPs: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            if relative.startswith("../") or relative.startswith("/"):
                raise BuildToolError(f"unsafe ZIP member {relative}")
            info = zipfile.ZipInfo(relative, date_time=_zip_datetime(epoch))
            info.create_system = 3
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            mode = 0o755 if executable else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
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


def _copy_tree_without_git(source: Path, destination: Path) -> None:
    if destination.exists():
        raise BuildToolError(f"packaging work directory already exists: {destination}")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", ".github"))


def _configure_anykernel(path: Path, codename: str) -> None:
    if not path.is_file():
        raise BuildToolError("pinned AnyKernel3 checkout lacks anykernel.sh")
    text = path.read_text(encoding="utf-8")
    replacements = {
        r"(?m)^device\.name1=.*$": f"device.name1={codename}",
        r"(?m)^is_slot_device=.*$": "is_slot_device=1",
        r"(?m)^do\.devicecheck=.*$": "do.devicecheck=1",
    }
    for pattern, replacement in replacements.items():
        matches = re.findall(pattern, text)
        if len(matches) != 1:
            raise BuildToolError(f"AnyKernel3 template field did not match exactly once: {pattern}")
        text = re.sub(pattern, replacement, text)
    path.write_text(text, encoding="utf-8", newline="\n")


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


def _write_checksums(output_dir: Path) -> Path:
    checksum_path = output_dir / "SHA256SUMS"
    files = sorted(
        path for path in output_dir.iterdir() if path.is_file() and path.name != checksum_path.name
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
    kernel = context["kernel"]
    branding = str(kernel["branding"])
    epoch = int(kernel["source_date_epoch"])
    suffix = "-SMOKE" if smoke else ("-prerelease" if pre_release else "")
    base_name = f"OnePlus13-{profile.id}-{feature.id}-{root_variant}-{branding}{suffix}"
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise BuildToolError(f"package output directory must be empty: {output_dir}")
    records: list[dict[str, Any]] = []
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
        else:
            fetch_dependencies(lock, cache_root, selected=("anykernel3",), dry_run=False, offline=False)
            anykernel_source = cache_root / "git" / "anykernel3"
            _copy_tree_without_git(anykernel_source, anykernel_work)
            overlay = root / "packaging" / "anykernel3"
            if overlay.is_dir():
                shutil.copytree(overlay, anykernel_work, dirs_exist_ok=True)
            _configure_anykernel(anykernel_work / "anykernel.sh", "dodge")
        shutil.copy2(input_dir / "Image", anykernel_work / "Image")
        anykernel_zip = output_dir / f"{base_name}-AnyKernel3.zip"
        deterministic_zip(anykernel_work, anykernel_zip, epoch=epoch)
        records.append(record_for_file(anykernel_zip, role="anykernel3-zip"))
        if build_target in {"modules", "mixed", "monolithic"}:
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
                source_record = state["dependencies"]["nethunter_wireless_firmware"]
                firmware_source = Path(str(source_record["path"]))
                _validate_zip_asset(firmware_source, role="pinned wireless firmware bundle")
                if sha256_file(firmware_source) != dependency.sha256:
                    raise BuildToolError("wireless firmware digest differs from dependencies/lock.yml")
                shutil.copy2(firmware_source, firmware_zip)
                firmware_record = record_for_file(firmware_zip, role="wireless-firmware")
                firmware_record.update(
                    {
                        "dependency": dependency.id,
                        "version": dependency.raw.get("version"),
                        "upstream_sha256": dependency.sha256,
                    }
                )
            records.append(firmware_record)
        if debug:
            debug_work = temporary / "debug"
            debug_work.mkdir()
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
        "root_variant": root_variant,
        "build_target": build_target,
        "debug": bool(debug),
        "pre_release": bool(pre_release),
        "smoke": bool(smoke),
        "source": source,
        "dependency_lock": dependency_lock,
        "dependencies": _dependency_inventory(lock),
        "patches": context["patches"],
        "configuration": context["configuration"],
        "kernel": context["kernel"],
        "modules": context.get("modules"),
        "artifacts": records,
    }
    atomic_write_json(provenance_path, provenance)
    records.append(record_for_file(provenance_path, role="provenance"))
    checksum_path = _write_checksums(output_dir)
    records.append(record_for_file(checksum_path, role="checksums"))
    _assert_no_partition_images(output_dir)
    updated = advance_context(context, "packaged", {"packages": records})
    write_context(context_path, updated)
    return records
