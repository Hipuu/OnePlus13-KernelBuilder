"""Preserve and verify compiler/KMI evidence across build artifacts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .config import sha256_file
from .errors import BuildToolError


EVIDENCE_SCHEMA_VERSION = 1
TOOLCHAIN_KIND = "oneplus13-build-toolchain-provenance"
TOOLCHAIN_SCHEMA_VERSION = 2
TOOLCHAIN_NAME = "build-toolchain-provenance.json"
KMI_NAME = "kmi-symbol-exports.json"
WIRELESS_LED_KMI_NAME = "kmi-wireless-led-exports.json"
TOOLCHAIN_SOURCE_RELATIVE = f".op13/{TOOLCHAIN_NAME}"
KMI_SOURCE_RELATIVE = "kernel_platform/common/.op13-kmi-symbol-exports.json"
WIRELESS_LED_KMI_SOURCE_RELATIVE = (
    "kernel_platform/common/.op13-kmi-wireless-led-exports.json"
)
WIRELESS_LED_FEATURE = "nethunter.wifi_ath"
WIRELESS_LED_TARGET = "android/abi_gki_aarch64_qcom"
EXPECTED_COMPILER_TOOLS = frozenset(
    {
        "clang",
        "clang++",
        "ld.lld",
        "llvm-ar",
        "llvm-nm",
        "llvm-objcopy",
        "llvm-objdump",
        "llvm-readelf",
        "llvm-size",
        "llvm-strip",
    }
)
EXPECTED_BAZEL_ROLES = (
    "entrypoint-and-oneplus-wrapper",
    "upstream-launcher",
    "repository-discovery-helper",
    "launcher-python-interpreter",
    "bazel-python-driver",
    "bazel-binary",
)
EXPECTED_KMI_IDENTITIES = frozenset(
    {
        ("from_kuid", "oplus_bsp_mm_osvelte.ko"),
        ("from_kuid_munged", "msm_sysstats.ko"),
    }
)
EXPECTED_WIRELESS_LED_SYMBOLS = (
    (
        "__ieee80211_get_radio_led_name",
        ("ath9k.ko", "ath9k_htc.ko"),
    ),
    (
        "__ieee80211_create_tpt_led_trigger",
        ("ath9k.ko", "ath9k_htc.ko", "mt76.ko"),
    ),
)
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BuildToolError(f"{where} must be an object")
    return value


def _portable(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BuildToolError(f"{where} must be a portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise BuildToolError(f"{where} must be a portable relative path")
    return value


def _load_json(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BuildToolError(f"{role} is missing or is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildToolError(f"{role} is invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise BuildToolError(f"{role} must be a JSON object")
    return value


def wireless_led_exports_required(
    features_value: object,
    *,
    feature_profile: str | None = None,
) -> bool:
    """Return the sealed Wi-Fi ATH feature state from one feature selection."""

    if not isinstance(features_value, list) or len(features_value) != 1:
        raise BuildToolError(
            "build evidence requires exactly one sealed feature selection"
        )
    selection = _mapping(features_value[0], "features[0]")
    if feature_profile is not None and selection.get("profile") != feature_profile:
        raise BuildToolError("sealed feature selection profile differs from build lineage")
    flags = _mapping(selection.get("flags"), "features[0].flags")
    required = flags.get(WIRELESS_LED_FEATURE)
    if not isinstance(required, bool):
        raise BuildToolError(
            f"sealed feature selection lacks boolean {WIRELESS_LED_FEATURE} state"
        )
    return required


def _source_path_from_binding(project_path: str, binding_path: str) -> str:
    return binding_path if project_path == "." else f"{project_path}/{binding_path}"


def _validate_git_binding(
    binding_value: object,
    *,
    project: Mapping[str, Any],
    source_path: str,
    where: str,
    selected: bool,
    selected_is_symlink: bool,
) -> None:
    binding = _mapping(binding_value, where)
    project_path = _portable(project.get("path"), f"{where}.manifest_project.path")
    commit = project.get("commit")
    if not isinstance(commit, str) or HEX40_RE.fullmatch(commit) is None:
        raise BuildToolError(f"{where} manifest commit is invalid")
    binding_path = _portable(binding.get("path"), f"{where}.path")
    if (
        binding.get("project_path") != project_path
        or binding.get("manifest_commit") != commit
        or binding.get("checkout_head") != commit
        or binding.get("status") != "exact-manifest-tree"
        or binding.get("tree_type") != "blob"
        or binding.get("tree_mode") not in {"100644", "100755", "120000"}
        or not isinstance(binding.get("tree_object"), str)
        or HEX40_RE.fullmatch(str(binding.get("tree_object"))) is None
        or binding.get("worktree_object") != binding.get("tree_object")
        or _source_path_from_binding(project_path, binding_path) != source_path
    ):
        raise BuildToolError(f"{where} does not prove the manifest Git tree")
    if selected:
        if (binding.get("tree_mode") == "120000") != selected_is_symlink:
            raise BuildToolError(f"{where} symlink kind differs from its selected path")
    elif binding.get("tree_mode") == "120000":
        raise BuildToolError(f"{where} canonical path must resolve to a regular file")


def _validate_component(value: object, *, where: str) -> Mapping[str, Any]:
    component = _mapping(value, where)
    selected_path = _portable(component.get("selected_path"), f"{where}.selected_path")
    canonical_path = _portable(component.get("canonical_path"), f"{where}.canonical_path")
    project = _mapping(component.get("manifest_project"), f"{where}.manifest_project")
    selected_is_symlink = component.get("selected_path_is_symlink")
    if not isinstance(selected_is_symlink, bool):
        raise BuildToolError(f"{where}.selected_path_is_symlink must be boolean")
    size = component.get("size")
    digest = component.get("sha256")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
        or not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None
    ):
        raise BuildToolError(f"{where} byte identity is invalid")
    _validate_git_binding(
        component.get("selected_git_tree"),
        project=project,
        source_path=selected_path,
        where=f"{where}.selected_git_tree",
        selected=True,
        selected_is_symlink=selected_is_symlink,
    )
    _validate_git_binding(
        component.get("canonical_git_tree"),
        project=project,
        source_path=canonical_path,
        where=f"{where}.canonical_git_tree",
        selected=False,
        selected_is_symlink=False,
    )
    return component


def validate_toolchain_document(
    document: Mapping[str, Any],
    *,
    resolved_manifest_sha256: str,
) -> None:
    if (
        document.get("kind") != TOOLCHAIN_KIND
        or document.get("schema_version") != TOOLCHAIN_SCHEMA_VERSION
        or document.get("path_scope") != "synced-source-relative"
    ):
        raise BuildToolError("build toolchain provenance schema is unsupported")
    manifest = _mapping(document.get("resolved_manifest"), "toolchain.resolved_manifest")
    if (
        manifest.get("sha256") != resolved_manifest_sha256
        or not isinstance(manifest.get("size"), int)
        or isinstance(manifest.get("size"), bool)
        or int(manifest["size"]) < 1
    ):
        raise BuildToolError(
            "build toolchain provenance differs from the resolved manifest lineage"
        )
    selection = _mapping(document.get("selection"), "toolchain.selection")
    declaration = _validate_component(
        {
            **dict(_mapping(selection.get("declaration"), "toolchain.selection.declaration")),
            "selected_path": _mapping(
                selection.get("declaration"), "toolchain.selection.declaration"
            ).get("path"),
            "selected_path_is_symlink": (
                _mapping(
                    selection.get("declaration"), "toolchain.selection.declaration"
                ).get("selected_git_tree", {})
                if isinstance(
                    _mapping(
                        selection.get("declaration"),
                        "toolchain.selection.declaration",
                    ).get("selected_git_tree"),
                    dict,
                )
                else {}
            ).get("tree_mode")
            == "120000",
        },
        where="toolchain.selection.declaration",
    )
    if declaration.get("manifest_project", {}).get("path") != "kernel_platform/common":
        raise BuildToolError("Clang declaration is not bound to the common project")

    compiler_tools = document.get("compiler_tools")
    if not isinstance(compiler_tools, list):
        raise BuildToolError("build toolchain compiler inventory is absent")
    compiler_names: list[str] = []
    for index, value in enumerate(compiler_tools):
        component = _validate_component(value, where=f"toolchain.compiler_tools[{index}]")
        name = component.get("name")
        if not isinstance(name, str):
            raise BuildToolError("build toolchain compiler record lacks a name")
        compiler_names.append(name)
    if set(compiler_names) != EXPECTED_COMPILER_TOOLS or len(compiler_names) != len(
        EXPECTED_COMPILER_TOOLS
    ):
        raise BuildToolError("build toolchain compiler inventory is incomplete or repeated")

    bazel = _mapping(document.get("bazel_launcher"), "toolchain.bazel_launcher")
    components = bazel.get("components")
    if not isinstance(components, list):
        raise BuildToolError("Bazel launcher component inventory is absent")
    roles: list[str] = []
    for index, value in enumerate(components):
        component = _validate_component(
            value,
            where=f"toolchain.bazel_launcher.components[{index}]",
        )
        role = component.get("role")
        if not isinstance(role, str):
            raise BuildToolError("Bazel launcher component lacks a role")
        roles.append(role)
    if tuple(roles) != EXPECTED_BAZEL_ROLES:
        raise BuildToolError("Bazel launcher component inventory is incomplete or reordered")

    host_tools = document.get("host_environment_tools")
    if not isinstance(host_tools, list):
        raise BuildToolError("host build-tool evidence is absent")
    names: set[str] = set()
    for index, value in enumerate(host_tools):
        record = _mapping(value, f"toolchain.host_environment_tools[{index}]")
        name = record.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or record.get("provenance") != "environment-provided"
            or record.get("immutable") is not False
        ):
            raise BuildToolError("host build-tool evidence is invalid")
        names.add(name)
    if not {"bash", "git", "make", "python3"}.issubset(names):
        raise BuildToolError("host build-tool evidence omits a core command")
    runner_image = _mapping(document.get("github_runner_image"), "toolchain.github_runner_image")
    if (
        runner_image.get("provenance") != "github-actions-environment"
        or runner_image.get("immutable") is not False
        or runner_image.get("status") not in {"recorded", "partial", "not-provided"}
    ):
        raise BuildToolError("GitHub runner-image evidence is invalid")


def validate_kmi_document(
    document: Mapping[str, Any],
    *,
    base: str,
    common_root: Path | None = None,
    source_successors: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    if (
        document.get("schema_version") != 1
        or document.get("integration")
        != "minimal-vendor-module-kmi-symbol-closure"
        or document.get("base") != base
        or document.get("strict_mode") is not True
    ):
        raise BuildToolError("KMI symbol-export evidence has invalid lineage")
    symbols = document.get("symbols")
    if not isinstance(symbols, list):
        raise BuildToolError("KMI symbol-export evidence has no symbol records")
    identities: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for index, value in enumerate(symbols):
        record = _mapping(value, f"kmi.symbols[{index}]")
        path_text = _portable(record.get("path"), f"kmi.symbols[{index}].path")
        symbol = record.get("symbol")
        consumer = record.get("consumer")
        identities.append((str(symbol), str(consumer)))
        seen_paths.add(path_text)
        if (
            record.get("status") not in {"integrated", "already-integrated"}
            or not isinstance(record.get("pre_size"), int)
            or isinstance(record.get("pre_size"), bool)
            or not isinstance(record.get("post_size"), int)
            or isinstance(record.get("post_size"), bool)
            or int(record["pre_size"]) < 1
            or int(record["post_size"]) <= int(record["pre_size"])
            or not isinstance(record.get("pre_sha256"), str)
            or SHA256_RE.fullmatch(str(record.get("pre_sha256"))) is None
            or not isinstance(record.get("post_sha256"), str)
            or SHA256_RE.fullmatch(str(record.get("post_sha256"))) is None
        ):
            raise BuildToolError(f"KMI symbol-export record {index} is invalid")
        expected_size = record["post_size"]
        expected_sha256 = record["post_sha256"]
        successor = (source_successors or {}).get(path_text)
        if successor is not None:
            if (
                successor.get("pre_size") != record["post_size"]
                or successor.get("pre_sha256") != record["post_sha256"]
                or not isinstance(successor.get("post_size"), int)
                or isinstance(successor.get("post_size"), bool)
                or int(successor["post_size"]) <= int(successor["pre_size"])
                or not isinstance(successor.get("post_sha256"), str)
                or SHA256_RE.fullmatch(str(successor["post_sha256"])) is None
            ):
                raise BuildToolError(
                    f"KMI symbol-export successor lineage differs for {path_text}"
                )
            expected_size = successor["post_size"]
            expected_sha256 = successor["post_sha256"]
        if common_root is not None:
            candidate = common_root.joinpath(*PurePosixPath(path_text).parts)
            if candidate.is_symlink() or not candidate.is_file():
                raise BuildToolError(f"preserved KMI source file is missing: {path_text}")
            if (
                candidate.stat().st_size != expected_size
                or sha256_file(candidate) != expected_sha256
                or candidate.read_bytes().splitlines().count(
                    f"  {symbol}".encode("ascii")
                )
                != 1
            ):
                raise BuildToolError(
                    f"KMI symbol-export evidence differs from {path_text}"
                )
    if set(identities) != EXPECTED_KMI_IDENTITIES or len(identities) != len(
        EXPECTED_KMI_IDENTITIES
    ):
        raise BuildToolError("KMI symbol-export evidence is incomplete or repeated")
    if source_successors is not None and set(source_successors) - seen_paths:
        raise BuildToolError("KMI symbol-export successor names an unknown source path")


def validate_wireless_led_kmi_document(
    document: Mapping[str, Any],
    *,
    base: str,
    common_root: Path | None = None,
) -> None:
    if (
        set(document)
        != {
            "schema_version",
            "integration",
            "feature",
            "base",
            "strict_mode",
            "pre_size",
            "pre_sha256",
            "post_size",
            "post_sha256",
            "symbols",
        }
        or document.get("schema_version") != 1
        or document.get("integration")
        != "nethunter-mac80211-led-kmi-symbol-closure"
        or document.get("feature") != WIRELESS_LED_FEATURE
        or document.get("base") != base
        or document.get("strict_mode") is not True
    ):
        raise BuildToolError("wireless LED KMI evidence has invalid lineage")
    pre_size = document.get("pre_size")
    post_size = document.get("post_size")
    pre_sha256 = document.get("pre_sha256")
    post_sha256 = document.get("post_sha256")
    expected_delta = sum(
        len(f"  {symbol}\n".encode("ascii"))
        for symbol, _consumers in EXPECTED_WIRELESS_LED_SYMBOLS
    )
    if (
        not isinstance(pre_size, int)
        or isinstance(pre_size, bool)
        or pre_size < 1
        or not isinstance(post_size, int)
        or isinstance(post_size, bool)
        or post_size - pre_size != expected_delta
        or not isinstance(pre_sha256, str)
        or SHA256_RE.fullmatch(pre_sha256) is None
        or not isinstance(post_sha256, str)
        or SHA256_RE.fullmatch(post_sha256) is None
        or pre_sha256 == post_sha256
    ):
        raise BuildToolError("wireless LED KMI byte lineage is invalid")
    symbols = document.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != len(
        EXPECTED_WIRELESS_LED_SYMBOLS
    ):
        raise BuildToolError("wireless LED KMI symbol inventory is incomplete")
    observed: list[tuple[str, tuple[str, ...]]] = []
    statuses: set[str] = set()
    for index, value in enumerate(symbols):
        record = _mapping(value, f"wireless_led_kmi.symbols[{index}]")
        consumers = record.get("consumers")
        if (
            set(record) != {"path", "symbol", "consumers", "status"}
            or record.get("path") != WIRELESS_LED_TARGET
            or not isinstance(record.get("symbol"), str)
            or not isinstance(consumers, list)
            or any(not isinstance(consumer, str) for consumer in consumers)
            or record.get("status") not in {"integrated", "already-integrated"}
        ):
            raise BuildToolError(f"wireless LED KMI symbol record {index} is invalid")
        observed.append((str(record["symbol"]), tuple(consumers)))
        statuses.add(str(record["status"]))
    if tuple(observed) != EXPECTED_WIRELESS_LED_SYMBOLS or len(statuses) != 1:
        raise BuildToolError(
            "wireless LED KMI symbols or exact consumer sets differ"
        )
    if common_root is not None:
        candidate = common_root.joinpath(*PurePosixPath(WIRELESS_LED_TARGET).parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise BuildToolError(
                f"preserved wireless LED KMI source file is missing: {WIRELESS_LED_TARGET}"
            )
        payload = candidate.read_bytes()
        if (
            candidate.stat().st_size != post_size
            or sha256_file(candidate) != post_sha256
            or any(
                payload.splitlines().count(f"  {symbol}".encode("ascii")) != 1
                for symbol, _consumers in EXPECTED_WIRELESS_LED_SYMBOLS
            )
        ):
            raise BuildToolError(
                f"wireless LED KMI evidence differs from {WIRELESS_LED_TARGET}"
            )


def _wireless_successor(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        WIRELESS_LED_TARGET: {
            "pre_size": document["pre_size"],
            "pre_sha256": document["pre_sha256"],
            "post_size": document["post_size"],
            "post_sha256": document["post_sha256"],
        }
    }


def _validated_source_kmi_documents(
    *,
    source_dir: Path,
    base: str,
    wireless_led_exports_required: bool,
) -> tuple[Path, dict[str, Any], Path, dict[str, Any] | None]:
    if not isinstance(wireless_led_exports_required, bool):
        raise BuildToolError("wireless LED KMI requirement must be boolean")
    kmi_source = source_dir.joinpath(*PurePosixPath(KMI_SOURCE_RELATIVE).parts)
    wireless_source = source_dir.joinpath(
        *PurePosixPath(WIRELESS_LED_KMI_SOURCE_RELATIVE).parts
    )
    kmi = _load_json(kmi_source, "KMI symbol-export evidence")
    wireless: dict[str, Any] | None = None
    common_root = source_dir / "kernel_platform" / "common"
    if wireless_led_exports_required:
        wireless = _load_json(wireless_source, "wireless LED KMI symbol-export evidence")
        validate_wireless_led_kmi_document(
            wireless,
            base=base,
            common_root=common_root,
        )
    elif wireless_source.exists() or wireless_source.is_symlink():
        raise BuildToolError(
            f"{WIRELESS_LED_FEATURE} is disabled but its KMI evidence stamp is present"
        )
    validate_kmi_document(
        kmi,
        base=base,
        common_root=common_root,
        source_successors=_wireless_successor(wireless) if wireless is not None else None,
    )
    return kmi_source, kmi, wireless_source, wireless


def _validate_kleaf_repo_manifest_record(
    value: object,
    *,
    base: str,
    resolved_manifest_sha256: str,
) -> dict[str, Any]:
    record = dict(_mapping(value, "build_evidence.kleaf_repo_manifest"))
    manifest_path = _portable(
        record.get("resolved_manifest"),
        "build_evidence.kleaf_repo_manifest.resolved_manifest",
    )
    revision = record.get("manifest_revision")
    if (
        record.get("schema_version") != 1
        or record.get("environment_variable") != "KLEAF_REPO_MANIFEST"
        or record.get("status") != "applied"
        or record.get("path_scope") != "synced-source-relative"
        or record.get("base") != base
        or record.get("repository_root") != "."
        or manifest_path == "."
        or record.get("resolved_manifest_sha256") != resolved_manifest_sha256
        or not isinstance(record.get("manifest_url"), str)
        or not str(record.get("manifest_url")).startswith("https://")
        or not isinstance(record.get("manifest_file"), str)
        or not record.get("manifest_file")
        or not isinstance(revision, str)
        or HEX40_RE.fullmatch(revision) is None
    ):
        raise BuildToolError("Kleaf repository-manifest evidence is invalid")
    return record


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def capture_source_kmi_evidence(
    *,
    source_dir: Path,
    base: str,
    wireless_led_exports_required: bool,
    destinations: Sequence[Path],
) -> dict[str, Any]:
    """Validate and copy patch-stage KMI stamps before compilation can fail."""

    if not destinations:
        raise BuildToolError("at least one KMI evidence destination is required")
    kmi_source, kmi, wireless_source, wireless = _validated_source_kmi_documents(
        source_dir=source_dir,
        base=base,
        wireless_led_exports_required=wireless_led_exports_required,
    )
    written: set[Path] = set()
    for raw_destination in destinations:
        if raw_destination.is_symlink():
            raise BuildToolError(
                f"KMI evidence destination must not be a symlink: {raw_destination}"
            )
        raw_destination.mkdir(parents=True, exist_ok=True)
        try:
            destination = raw_destination.resolve(strict=True)
        except OSError as exc:
            raise BuildToolError(
                f"cannot resolve KMI evidence destination: {raw_destination}"
            ) from exc
        if not destination.is_dir():
            raise BuildToolError(
                f"KMI evidence destination is not a directory: {destination}"
            )
        if destination in written:
            continue
        written.add(destination)
        wireless_destination = destination / WIRELESS_LED_KMI_NAME
        if not wireless_led_exports_required and (
            wireless_destination.exists() or wireless_destination.is_symlink()
        ):
            raise BuildToolError(
                "disabled wireless LED KMI feature has stale captured evidence"
            )
        _atomic_copy(kmi_source, destination / KMI_NAME)
        if wireless is not None:
            _atomic_copy(wireless_source, wireless_destination)
    return {
        "base": base,
        "wireless_led_exports_required": wireless_led_exports_required,
        "kmi_symbol_exports": kmi,
        "wireless_led_symbol_exports": wireless,
    }


def _record(path: Path, *, role: str, relative_path: str, **extra: object) -> dict[str, Any]:
    return {
        "role": role,
        "path": relative_path,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        **extra,
    }


def preserve_source_build_evidence(
    *,
    source_dir: Path,
    output_dir: Path,
    base: str,
    resolved_manifest: Path,
    kleaf_repo_manifest: object,
    wireless_led_exports_required: bool,
) -> dict[str, Any]:
    manifest_sha256 = sha256_file(resolved_manifest)
    toolchain_source = source_dir.joinpath(*PurePosixPath(TOOLCHAIN_SOURCE_RELATIVE).parts)
    toolchain = _load_json(toolchain_source, "build toolchain provenance")
    kmi_source, kmi, wireless_source, wireless = _validated_source_kmi_documents(
        source_dir=source_dir,
        base=base,
        wireless_led_exports_required=wireless_led_exports_required,
    )
    validate_toolchain_document(
        toolchain,
        resolved_manifest_sha256=manifest_sha256,
    )
    kleaf_record = _validate_kleaf_repo_manifest_record(
        kleaf_repo_manifest,
        base=base,
        resolved_manifest_sha256=manifest_sha256,
    )

    metadata = output_dir / ".op13"
    toolchain_destination = metadata / TOOLCHAIN_NAME
    kmi_destination = metadata / KMI_NAME
    wireless_destination = metadata / WIRELESS_LED_KMI_NAME
    _atomic_copy(toolchain_source, toolchain_destination)
    _atomic_copy(kmi_source, kmi_destination)
    if wireless is not None:
        _atomic_copy(wireless_source, wireless_destination)
    result: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "resolved_manifest_sha256": manifest_sha256,
        "kleaf_repo_manifest": kleaf_record,
        "wireless_led_exports_required": wireless_led_exports_required,
        "toolchain": _record(
            toolchain_destination,
            role="build-toolchain-provenance",
            relative_path=f".op13/{TOOLCHAIN_NAME}",
            source_path=TOOLCHAIN_SOURCE_RELATIVE,
            document_kind=TOOLCHAIN_KIND,
            document_schema_version=TOOLCHAIN_SCHEMA_VERSION,
        ),
        "kmi_symbol_exports": _record(
            kmi_destination,
            role="kmi-symbol-exports",
            relative_path=f".op13/{KMI_NAME}",
            source_path=KMI_SOURCE_RELATIVE,
            base=base,
            strict_mode=True,
        ),
        "wireless_led_symbol_exports": None,
    }
    if wireless is not None:
        result["wireless_led_symbol_exports"] = _record(
            wireless_destination,
            role="wireless-led-kmi-symbol-exports",
            relative_path=f".op13/{WIRELESS_LED_KMI_NAME}",
            source_path=WIRELESS_LED_KMI_SOURCE_RELATIVE,
            base=base,
            feature=WIRELESS_LED_FEATURE,
            strict_mode=True,
        )
    return result


def _validated_record_file(
    root: Path,
    record_value: object,
    *,
    expected_path: str,
    where: str,
) -> tuple[Path, Mapping[str, Any]]:
    record = _mapping(record_value, where)
    if record.get("path") != expected_path:
        raise BuildToolError(f"{where}.path must be {expected_path}")
    candidate = root.joinpath(*PurePosixPath(expected_path).parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise BuildToolError(f"{where} file is missing: {candidate}")
    if (
        candidate.stat().st_size != record.get("size")
        or sha256_file(candidate) != record.get("sha256")
    ):
        raise BuildToolError(f"{where} file differs from its sealed record")
    return candidate, record


def validate_preserved_build_evidence(
    *,
    output_dir: Path,
    evidence_value: object,
    base: str,
    resolved_manifest_sha256: str,
    wireless_led_exports_required: bool,
) -> dict[str, dict[str, Any]]:
    if not isinstance(wireless_led_exports_required, bool):
        raise BuildToolError("wireless LED KMI requirement must be boolean")
    evidence = _mapping(evidence_value, "build_evidence")
    if (
        evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("resolved_manifest_sha256") != resolved_manifest_sha256
        or evidence.get("wireless_led_exports_required")
        is not wireless_led_exports_required
    ):
        raise BuildToolError("preserved build evidence has invalid manifest lineage")
    _validate_kleaf_repo_manifest_record(
        evidence.get("kleaf_repo_manifest"),
        base=base,
        resolved_manifest_sha256=resolved_manifest_sha256,
    )
    toolchain_path, toolchain_record = _validated_record_file(
        output_dir,
        evidence.get("toolchain"),
        expected_path=f".op13/{TOOLCHAIN_NAME}",
        where="build_evidence.toolchain",
    )
    kmi_path, kmi_record = _validated_record_file(
        output_dir,
        evidence.get("kmi_symbol_exports"),
        expected_path=f".op13/{KMI_NAME}",
        where="build_evidence.kmi_symbol_exports",
    )
    wireless_path: Path | None = None
    wireless_record: Mapping[str, Any] | None = None
    if wireless_led_exports_required:
        wireless_path, wireless_record = _validated_record_file(
            output_dir,
            evidence.get("wireless_led_symbol_exports"),
            expected_path=f".op13/{WIRELESS_LED_KMI_NAME}",
            where="build_evidence.wireless_led_symbol_exports",
        )
    else:
        if evidence.get("wireless_led_symbol_exports") is not None:
            raise BuildToolError(
                "disabled wireless LED KMI evidence contains an unexpected record"
            )
        unexpected = output_dir / ".op13" / WIRELESS_LED_KMI_NAME
        if unexpected.exists() or unexpected.is_symlink():
            raise BuildToolError(
                "disabled wireless LED KMI evidence contains an unexpected file"
            )
    if (
        toolchain_record.get("source_path") != TOOLCHAIN_SOURCE_RELATIVE
        or toolchain_record.get("document_kind") != TOOLCHAIN_KIND
        or toolchain_record.get("document_schema_version") != TOOLCHAIN_SCHEMA_VERSION
        or kmi_record.get("source_path") != KMI_SOURCE_RELATIVE
        or kmi_record.get("base") != base
        or kmi_record.get("strict_mode") is not True
    ):
        raise BuildToolError("preserved build evidence record metadata is invalid")
    toolchain = _load_json(toolchain_path, "preserved build toolchain provenance")
    kmi = _load_json(kmi_path, "preserved KMI symbol-export evidence")
    wireless: dict[str, Any] | None = None
    if wireless_path is not None and wireless_record is not None:
        if (
            wireless_record.get("source_path") != WIRELESS_LED_KMI_SOURCE_RELATIVE
            or wireless_record.get("base") != base
            or wireless_record.get("feature") != WIRELESS_LED_FEATURE
            or wireless_record.get("strict_mode") is not True
        ):
            raise BuildToolError(
                "preserved wireless LED KMI evidence record metadata is invalid"
            )
        wireless = _load_json(
            wireless_path,
            "preserved wireless LED KMI symbol-export evidence",
        )
        validate_wireless_led_kmi_document(wireless, base=base)
    validate_toolchain_document(
        toolchain,
        resolved_manifest_sha256=resolved_manifest_sha256,
    )
    validate_kmi_document(
        kmi,
        base=base,
        source_successors=_wireless_successor(wireless) if wireless is not None else None,
    )
    result = {"toolchain": toolchain, "kmi_symbol_exports": kmi}
    if wireless is not None:
        result["wireless_led_symbol_exports"] = wireless
    return result


def copy_preserved_build_evidence(
    *,
    input_dir: Path,
    destination: Path,
    evidence_value: object,
    base: str,
    resolved_manifest_sha256: str,
    wireless_led_exports_required: bool,
) -> dict[str, Any]:
    validate_preserved_build_evidence(
        output_dir=input_dir,
        evidence_value=evidence_value,
        base=base,
        resolved_manifest_sha256=resolved_manifest_sha256,
        wireless_led_exports_required=wireless_led_exports_required,
    )
    if destination.is_symlink() or not destination.is_dir():
        raise BuildToolError(f"build-evidence destination is not a directory: {destination}")
    source_toolchain = input_dir / ".op13" / TOOLCHAIN_NAME
    source_kmi = input_dir / ".op13" / KMI_NAME
    source_wireless = input_dir / ".op13" / WIRELESS_LED_KMI_NAME
    destination_toolchain = destination / TOOLCHAIN_NAME
    destination_kmi = destination / KMI_NAME
    destination_wireless = destination / WIRELESS_LED_KMI_NAME
    destination_paths = [destination_toolchain, destination_kmi]
    if wireless_led_exports_required:
        destination_paths.append(destination_wireless)
    elif destination_wireless.exists() or destination_wireless.is_symlink():
        raise BuildToolError(
            "disabled wireless LED KMI evidence has a stale destination file"
        )
    for path in destination_paths:
        if path.exists() or path.is_symlink():
            raise BuildToolError(f"build-evidence destination already exists: {path}")
    _atomic_copy(source_toolchain, destination_toolchain)
    _atomic_copy(source_kmi, destination_kmi)
    if wireless_led_exports_required:
        _atomic_copy(source_wireless, destination_wireless)
    result: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "resolved_manifest_sha256": resolved_manifest_sha256,
        "kleaf_repo_manifest": dict(
            _mapping(evidence_value, "build_evidence")["kleaf_repo_manifest"]
        ),
        "wireless_led_exports_required": wireless_led_exports_required,
        "toolchain": _record(
            destination_toolchain,
            role="build-toolchain-provenance",
            relative_path=TOOLCHAIN_NAME,
            document_kind=TOOLCHAIN_KIND,
            document_schema_version=TOOLCHAIN_SCHEMA_VERSION,
        ),
        "kmi_symbol_exports": _record(
            destination_kmi,
            role="kmi-symbol-exports",
            relative_path=KMI_NAME,
            base=base,
            strict_mode=True,
        ),
        "wireless_led_symbol_exports": None,
    }
    if wireless_led_exports_required:
        result["wireless_led_symbol_exports"] = _record(
            destination_wireless,
            role="wireless-led-kmi-symbol-exports",
            relative_path=WIRELESS_LED_KMI_NAME,
            base=base,
            feature=WIRELESS_LED_FEATURE,
            strict_mode=True,
        )
    return result


def validate_packaged_build_evidence(
    *,
    assets_dir: Path,
    evidence_value: object,
    base: str,
    resolved_manifest_sha256: str,
    wireless_led_exports_required: bool,
) -> None:
    if not isinstance(wireless_led_exports_required, bool):
        raise BuildToolError("wireless LED KMI requirement must be boolean")
    evidence = _mapping(evidence_value, "build_evidence")
    if (
        evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("resolved_manifest_sha256") != resolved_manifest_sha256
        or evidence.get("wireless_led_exports_required")
        is not wireless_led_exports_required
    ):
        raise BuildToolError("packaged build evidence has invalid manifest lineage")
    _validate_kleaf_repo_manifest_record(
        evidence.get("kleaf_repo_manifest"),
        base=base,
        resolved_manifest_sha256=resolved_manifest_sha256,
    )
    toolchain_path, toolchain_record = _validated_record_file(
        assets_dir,
        evidence.get("toolchain"),
        expected_path=TOOLCHAIN_NAME,
        where="build_evidence.toolchain",
    )
    kmi_path, kmi_record = _validated_record_file(
        assets_dir,
        evidence.get("kmi_symbol_exports"),
        expected_path=KMI_NAME,
        where="build_evidence.kmi_symbol_exports",
    )
    wireless_path: Path | None = None
    wireless_record: Mapping[str, Any] | None = None
    if wireless_led_exports_required:
        wireless_path, wireless_record = _validated_record_file(
            assets_dir,
            evidence.get("wireless_led_symbol_exports"),
            expected_path=WIRELESS_LED_KMI_NAME,
            where="build_evidence.wireless_led_symbol_exports",
        )
    else:
        if evidence.get("wireless_led_symbol_exports") is not None:
            raise BuildToolError(
                "disabled packaged wireless LED KMI evidence has an unexpected record"
            )
        unexpected = assets_dir / WIRELESS_LED_KMI_NAME
        if unexpected.exists() or unexpected.is_symlink():
            raise BuildToolError(
                "disabled packaged wireless LED KMI evidence has an unexpected file"
            )
    if (
        toolchain_record.get("document_kind") != TOOLCHAIN_KIND
        or toolchain_record.get("document_schema_version") != TOOLCHAIN_SCHEMA_VERSION
        or kmi_record.get("base") != base
        or kmi_record.get("strict_mode") is not True
    ):
        raise BuildToolError("packaged build evidence record metadata is invalid")
    validate_toolchain_document(
        _load_json(toolchain_path, "packaged build toolchain provenance"),
        resolved_manifest_sha256=resolved_manifest_sha256,
    )
    wireless: dict[str, Any] | None = None
    if wireless_path is not None and wireless_record is not None:
        if (
            wireless_record.get("base") != base
            or wireless_record.get("feature") != WIRELESS_LED_FEATURE
            or wireless_record.get("strict_mode") is not True
        ):
            raise BuildToolError(
                "packaged wireless LED KMI evidence record metadata is invalid"
            )
        wireless = _load_json(
            wireless_path,
            "packaged wireless LED KMI symbol-export evidence",
        )
        validate_wireless_led_kmi_document(wireless, base=base)
    validate_kmi_document(
        _load_json(kmi_path, "packaged KMI symbol-export evidence"),
        base=base,
        source_successors=_wireless_successor(wireless) if wireless is not None else None,
    )
