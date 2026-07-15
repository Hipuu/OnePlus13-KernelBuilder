"""Strict repository configuration and dependency-lock validation.

Repository .yml files deliberately contain JSON.  JSON is a strict subset of
YAML, which keeps local and GitHub Actions builds independent of PyYAML while
still allowing editors and schema tooling to treat the files as YAML.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .errors import BuildToolError


SCHEMA_VERSION = 1
DEVICE_ID = "oneplus13"
CODENAME = "dodge"
TARGET = "sun"
ARCH = "arm64"
KMI = "android15-6.6"
SOC = "sm8750"
MANIFEST_URL = "https://github.com/OnePlusOSS/kernel_manifest.git"
MANIFEST_BRANCH = "oneplus/sm8750"
MANIFEST_FILES = {
    "oos15-cn": "oneplus_13.xml",
    "oos15-global": "oneplus_13_global.xml",
    "oos16": "oneplus_13_b.xml",
}
ROOT_DEPENDENCY_IDS = {
    "kernelsu": "kernelsu",
    "kernelsu-next": "kernelsu_next",
}

HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KCONFIG_RE = re.compile(r"^CONFIG_[A-Za-z0-9_]+$")
SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
)
MUTABLE_REFS = {
    "main",
    "master",
    "dev",
    "develop",
    "development",
    "next",
    "latest",
    "nightly",
    "HEAD",
}
ALLOWED_DEPENDENCY_KINDS = {"git", "file", "archive", "release_asset"}
ALLOWED_SYMBOL_VALUES = {"y", "m", "n"}


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json_yaml(path: Path) -> Any:
    """Load a repository YAML file without an optional YAML dependency."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BuildToolError(f"cannot read {path}: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise BuildToolError(f"{path}: UTF-8 BOM is not permitted")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildToolError(f"{path}: configuration must be UTF-8") from exc
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise BuildToolError(f"{path}: embedded credential detected")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BuildToolError(
            f"{path}:{exc.lineno}:{exc.colno}: expected JSON-compatible YAML ({exc.msg})"
        ) from exc
    return value


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BuildToolError(f"{where}: expected an object")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuildToolError(f"{where}: expected a non-empty string")
    if value != value.strip():
        raise BuildToolError(f"{where}: leading/trailing whitespace is not permitted")
    return value


def _list(value: Any, where: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise BuildToolError(f"{where}: expected an array")
    return value


def _schema(document: Mapping[str, Any], where: str) -> None:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise BuildToolError(f"{where}: schema_version must be {SCHEMA_VERSION}")


def validate_https_url(url: str, where: str, *, allow_file: bool = False) -> None:
    parsed = urlsplit(url)
    if allow_file and parsed.scheme == "file":
        return
    if parsed.scheme != "https" or not parsed.netloc:
        raise BuildToolError(f"{where}: URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise BuildToolError(f"{where}: credentials in URLs are forbidden")
    if parsed.query or parsed.fragment:
        raise BuildToolError(f"{where}: query strings and fragments are forbidden")


def normalize_manifest_url(url: str) -> str:
    return url.rstrip("/") + ("" if url.rstrip("/").endswith(".git") else ".git")


def is_full_commit(value: Any) -> bool:
    return isinstance(value, str) and HEX40_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class Device:
    id: str
    codename: str
    soc: str
    target: str
    arch: str
    kmi: str
    official_script: str
    official_args: tuple[str, ...]
    common_kernel: str
    vendor_kernel: str
    modules_and_devicetree: str
    defconfig: str
    source_path: Path


@dataclass(frozen=True)
class Profile:
    id: str
    device: str
    target: str
    arch: str
    kmi: str
    manifest_url: str
    manifest_branch: str
    manifest_file: str
    manifest_revision: str
    locked_manifest: Path
    build_variant: str
    susfs_status: str
    raw_partition_images: bool
    source_path: Path


@dataclass(frozen=True)
class KconfigFragment:
    path: str
    scope: str
    required: bool


@dataclass(frozen=True)
class FeatureProfile:
    id: str
    root_variants: tuple[str, ...]
    default_root_variant: str
    flags: Mapping[str, bool]
    patch_series: tuple[Any, ...]
    kconfig_fragments: tuple[KconfigFragment, ...]
    required_symbols: Mapping[str, str]
    external_modules: tuple[str, ...]
    optimization: str
    lto: str
    source_path: Path


@dataclass(frozen=True)
class Dependency:
    id: str
    kind: str
    url: str
    commit: str | None
    ref: str | None
    sha256: str | None
    required_for: tuple[str, ...]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class DependencyLock:
    dependencies: Mapping[str, Dependency]
    digest: str
    source_path: Path
    raw: Mapping[str, Any]


def load_device(path: Path) -> Device:
    raw = _mapping(load_json_yaml(path), str(path))
    _schema(raw, str(path))
    official = _mapping(raw.get("official_build"), f"{path}:official_build")
    layout = _mapping(raw.get("source_layout"), f"{path}:source_layout")
    args = tuple(_string(v, f"{path}:official_build.args") for v in _list(official.get("args"), f"{path}:official_build.args"))
    device = Device(
        id=_string(raw.get("device"), f"{path}:device"),
        codename=_string(raw.get("codename"), f"{path}:codename"),
        soc=_string(raw.get("soc"), f"{path}:soc").lower(),
        target=_string(raw.get("target"), f"{path}:target"),
        arch=_string(raw.get("arch"), f"{path}:arch"),
        kmi=_string(raw.get("kmi"), f"{path}:kmi"),
        official_script=_string(official.get("script"), f"{path}:official_build.script"),
        official_args=args,
        common_kernel=_string(layout.get("common_kernel"), f"{path}:source_layout.common_kernel"),
        vendor_kernel=_string(layout.get("vendor_kernel"), f"{path}:source_layout.vendor_kernel"),
        modules_and_devicetree=_string(layout.get("modules_and_devicetree"), f"{path}:source_layout.modules_and_devicetree"),
        defconfig=_string(layout.get("defconfig"), f"{path}:source_layout.defconfig"),
        source_path=path.resolve(),
    )
    expected = (DEVICE_ID, CODENAME, SOC, TARGET, ARCH, KMI)
    actual = (device.id, device.codename, device.soc, device.target, device.arch, device.kmi)
    if actual != expected:
        raise BuildToolError(
            f"{path}: platform identity mismatch; expected {expected}, got {actual}"
        )
    if device.official_script.startswith("/") or ".." in Path(device.official_script).parts:
        raise BuildToolError(f"{path}: official build script must be repository-relative")
    if device.official_args[:2] != (TARGET, "perf"):
        raise BuildToolError(f"{path}: official build args must begin with ['sun', 'perf']")
    return device


def load_profile(path: Path, device: Device, manifest_commit: str | None = None) -> Profile:
    raw = _mapping(load_json_yaml(path), str(path))
    _schema(raw, str(path))
    manifest = _mapping(raw.get("manifest"), f"{path}:manifest")
    build = _mapping(raw.get("build"), f"{path}:build")
    compatibility = _mapping(raw.get("compatibility"), f"{path}:compatibility")
    profile_id = _string(raw.get("id"), f"{path}:id")
    revision_value = manifest.get("revision") or manifest_commit
    revision = _string(revision_value, f"{path}:manifest.revision")
    profile = Profile(
        id=profile_id,
        device=_string(raw.get("device"), f"{path}:device"),
        target=_string(raw.get("target"), f"{path}:target"),
        arch=_string(raw.get("arch"), f"{path}:arch"),
        kmi=_string(raw.get("kmi"), f"{path}:kmi"),
        manifest_url=normalize_manifest_url(_string(manifest.get("url"), f"{path}:manifest.url")),
        manifest_branch=_string(manifest.get("branch"), f"{path}:manifest.branch"),
        manifest_file=_string(manifest.get("file"), f"{path}:manifest.file"),
        manifest_revision=revision,
        locked_manifest=resolve_inside(
            path.parents[2],
            _string(raw.get("locked_manifest"), f"{path}:locked_manifest"),
            f"{path}:locked_manifest",
        ),
        build_variant=_string(build.get("variant"), f"{path}:build.variant"),
        susfs_status=_string(compatibility.get("susfs"), f"{path}:compatibility.susfs"),
        raw_partition_images=compatibility.get("raw_partition_images"),
        source_path=path.resolve(),
    )
    if profile.id not in MANIFEST_FILES:
        raise BuildToolError(f"{path}: unsupported profile id {profile.id!r}")
    if path.stem != profile.id:
        raise BuildToolError(f"{path}: profile id must match its filename")
    if (profile.device, profile.target, profile.arch, profile.kmi) != (
        device.id,
        device.target,
        device.arch,
        device.kmi,
    ):
        raise BuildToolError(f"{path}: profile/device platform mismatch")
    if profile.manifest_url != MANIFEST_URL:
        raise BuildToolError(f"{path}: manifest must be the official OnePlusOSS SM8750 repository")
    validate_https_url(profile.manifest_url, f"{path}:manifest.url")
    if profile.manifest_branch != MANIFEST_BRANCH:
        raise BuildToolError(f"{path}: manifest branch must be {MANIFEST_BRANCH}")
    if profile.manifest_file != MANIFEST_FILES[profile.id]:
        raise BuildToolError(f"{path}: wrong manifest file for {profile.id}")
    if not is_full_commit(profile.manifest_revision):
        raise BuildToolError(f"{path}: manifest revision must be a full 40-character commit")
    if profile.susfs_status not in {"supported", "experimental"}:
        raise BuildToolError(f"{path}: compatibility.susfs must be supported or experimental")
    if profile.raw_partition_images is not False:
        raise BuildToolError(f"{path}: raw partition packaging must remain disabled")
    return profile


def _symbol_value(value: Any, where: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value:
        raise BuildToolError(f"{where}: expected y, m, n, quoted string, or numeric string")
    if value in ALLOWED_SYMBOL_VALUES or re.fullmatch(r"-?[0-9]+", value):
        return value
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value
    raise BuildToolError(f"{where}: invalid Kconfig value {value!r}")


def load_feature_profile(path: Path) -> FeatureProfile:
    raw = _mapping(load_json_yaml(path), str(path))
    _schema(raw, str(path))
    root = _mapping(raw.get("root"), f"{path}:root")
    variants = tuple(
        _string(v, f"{path}:root.supported_variants")
        for v in _list(root.get("supported_variants"), f"{path}:root.supported_variants")
    )
    if not variants or len(set(variants)) != len(variants):
        raise BuildToolError(f"{path}: root variants must be non-empty and unique")
    allowed_variants = {"kernelsu", "kernelsu-next"}
    if not set(variants).issubset(allowed_variants):
        raise BuildToolError(f"{path}: unsupported root variant")
    default_variant = _string(root.get("default_variant"), f"{path}:root.default_variant")
    if default_variant not in variants:
        raise BuildToolError(f"{path}: default root variant is not supported")
    flags_raw = _mapping(raw.get("feature_flags"), f"{path}:feature_flags")
    flags: dict[str, bool] = {}
    for key, value in flags_raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", key):
            raise BuildToolError(f"{path}: invalid feature flag name {key!r}")
        if not isinstance(value, bool):
            raise BuildToolError(f"{path}: feature flag {key} must be boolean")
        flags[key] = value
    fragments: list[KconfigFragment] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(_list(raw.get("kconfig_fragments"), f"{path}:kconfig_fragments")):
        entry = _mapping(item, f"{path}:kconfig_fragments[{index}]")
        fragment_path = _string(entry.get("path"), f"{path}:kconfig_fragments[{index}].path")
        if fragment_path in seen_paths:
            raise BuildToolError(f"{path}: duplicate Kconfig fragment {fragment_path}")
        seen_paths.add(fragment_path)
        scope = _string(entry.get("scope"), f"{path}:kconfig_fragments[{index}].scope")
        if scope not in {"common", "modules"}:
            raise BuildToolError(f"{path}: invalid Kconfig scope {scope}")
        required = entry.get("required")
        if not isinstance(required, bool):
            raise BuildToolError(f"{path}: Kconfig fragment required must be boolean")
        fragments.append(KconfigFragment(fragment_path, scope, required))
    symbols_raw = _mapping(raw.get("required_symbols"), f"{path}:required_symbols")
    symbols: dict[str, str] = {}
    for name, value in symbols_raw.items():
        if not isinstance(name, str) or not KCONFIG_RE.fullmatch(name):
            raise BuildToolError(f"{path}: invalid Kconfig symbol {name!r}")
        symbols[name] = _symbol_value(value, f"{path}:required_symbols.{name}")
    external_modules = tuple(
        _string(v, f"{path}:external_modules")
        for v in _list(raw.get("external_modules"), f"{path}:external_modules")
    )
    if len(set(external_modules)) != len(external_modules):
        raise BuildToolError(f"{path}: external modules must be unique")
    defaults = _mapping(raw.get("defaults"), f"{path}:defaults")
    optimization = _string(defaults.get("optimization"), f"{path}:defaults.optimization")
    lto = _string(defaults.get("lto"), f"{path}:defaults.lto")
    if optimization not in {"O2", "O3"}:
        raise BuildToolError(f"{path}: optimization must be O2 or O3")
    if lto not in {"thin", "full"}:
        raise BuildToolError(f"{path}: lto must be thin or full")
    patch_series = tuple(_list(raw.get("patch_series"), f"{path}:patch_series"))
    for index, entry in enumerate(patch_series):
        if isinstance(entry, str):
            _string(entry, f"{path}:patch_series[{index}]")
        elif isinstance(entry, dict):
            _string(entry.get("path") or entry.get("id"), f"{path}:patch_series[{index}].path")
        else:
            raise BuildToolError(f"{path}: patch series entries must be strings or objects")
    return FeatureProfile(
        id=_string(raw.get("id"), f"{path}:id"),
        root_variants=variants,
        default_root_variant=default_variant,
        flags=flags,
        patch_series=patch_series,
        kconfig_fragments=tuple(fragments),
        required_symbols=symbols,
        external_modules=external_modules,
        optimization=optimization,
        lto=lto,
        source_path=path.resolve(),
    )


def load_dependency_lock(path: Path) -> DependencyLock:
    raw = _mapping(load_json_yaml(path), str(path))
    _schema(raw, str(path))
    platform = _mapping(raw.get("platform"), f"{path}:platform")
    expected_platform = {
        "codename": CODENAME,
        "target": TARGET,
        "architecture": ARCH,
        "kmi": KMI,
    }
    for key, expected in expected_platform.items():
        actual = str(platform.get(key, "")).lower() if key == "codename" else platform.get(key)
        if actual != expected:
            raise BuildToolError(f"{path}: platform.{key} must be {expected!r}")
    policy = _mapping(raw.get("policy"), f"{path}:policy")
    required_policy = {
        "allow_mutable_checkout": False,
        "require_full_git_commit": True,
        "require_archive_sha256": True,
        "allow_pipe_to_shell": False,
    }
    for key, expected in required_policy.items():
        if policy.get(key) is not expected:
            raise BuildToolError(f"{path}: policy.{key} must be {expected}")
    dependencies_raw = _mapping(raw.get("dependencies"), f"{path}:dependencies")
    dependencies: dict[str, Dependency] = {}
    for dep_id, value in sorted(dependencies_raw.items()):
        if not isinstance(dep_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", dep_id):
            raise BuildToolError(f"{path}: invalid dependency id {dep_id!r}")
        item = _mapping(value, f"{path}:dependencies.{dep_id}")
        kind = _string(item.get("kind"), f"{path}:dependencies.{dep_id}.kind")
        if kind not in ALLOWED_DEPENDENCY_KINDS:
            raise BuildToolError(f"{path}: unsupported dependency kind {kind!r}")
        url = _string(item.get("url"), f"{path}:dependencies.{dep_id}.url")
        validate_https_url(url, f"{path}:dependencies.{dep_id}.url")
        commit_value = item.get("commit") or item.get("revision")
        commit = str(commit_value) if commit_value is not None else None
        ref_value = item.get("ref")
        ref = str(ref_value) if ref_value is not None else None
        sha_value = item.get("sha256")
        sha = str(sha_value) if sha_value is not None else None
        if kind == "git":
            if not is_full_commit(commit):
                raise BuildToolError(f"{path}: git dependency {dep_id} needs a full commit SHA")
            if sha is not None and not SHA256_RE.fullmatch(sha):
                raise BuildToolError(f"{path}: invalid sha256 for {dep_id}")
        else:
            if sha is None or not SHA256_RE.fullmatch(sha):
                raise BuildToolError(f"{path}: {kind} dependency {dep_id} needs sha256")
            if commit is not None and not is_full_commit(commit):
                raise BuildToolError(f"{path}: dependency {dep_id} commit must be a full SHA")
        if ref in MUTABLE_REFS or (isinstance(ref, str) and ref.startswith("refs/heads/")):
            raise BuildToolError(f"{path}: mutable branch ref is forbidden for {dep_id}")
        if ref is not None and not (is_full_commit(ref) or ref.startswith("refs/tags/")):
            raise BuildToolError(
                f"{path}: dependency {dep_id} ref must be a full commit or immutable tag"
            )
        required_for = tuple(
            _string(v, f"{path}:dependencies.{dep_id}.required_for")
            for v in _list(item.get("required_for"), f"{path}:dependencies.{dep_id}.required_for")
        )
        if not required_for:
            raise BuildToolError(f"{path}: dependency {dep_id} must declare required_for")
        dependencies[dep_id] = Dependency(dep_id, kind, url, commit, ref, sha, required_for, item)
    required_ids = {"repo_launcher", "oneplus_manifest", "kernelsu", "kernelsu_next", "anykernel3"}
    missing = sorted(required_ids - dependencies.keys())
    if missing:
        raise BuildToolError(f"{path}: missing required dependencies: {', '.join(missing)}")
    manifest = dependencies["oneplus_manifest"]
    if manifest.kind != "git" or normalize_manifest_url(manifest.url) != MANIFEST_URL:
        raise BuildToolError(f"{path}: oneplus_manifest must pin the official repository")
    digest = sha256_bytes(canonical_json_bytes(raw))
    return DependencyLock(dependencies, digest, path.resolve(), raw)


def resolve_root_selection(
    lock: DependencyLock,
    root_variant: str,
    requested_kernelsu_commit: str | None = None,
    requested_susfs_commit: str | None = None,
) -> dict[str, Any]:
    """Resolve optional workflow commit assertions against the audited lock."""

    requested = {
        "KernelSU": "" if requested_kernelsu_commit is None else requested_kernelsu_commit,
        "SUSFS": "" if requested_susfs_commit is None else requested_susfs_commit,
    }
    for label, value in requested.items():
        if not isinstance(value, str) or value != value.strip():
            raise BuildToolError(f"{label} commit assertion must be a single trimmed string")
    if root_variant == "none":
        if any(requested.values()):
            raise BuildToolError("root=none does not accept KernelSU or SUSFS commit assertions")
        return {
            "schema_version": 1,
            "root_variant": "none",
            "dependency_lock_sha256": lock.digest,
            "root": None,
            "susfs": None,
        }
    dependency_id = ROOT_DEPENDENCY_IDS.get(root_variant)
    if dependency_id is None:
        raise BuildToolError(f"unsupported root variant {root_variant!r}")
    required_ids = {dependency_id, "susfs"}
    if root_variant == "kernelsu-next":
        required_ids.add("wild_kernel_patches")
    missing = sorted(required_ids - set(lock.dependencies))
    if missing:
        raise BuildToolError(f"root compatibility dependencies are absent: {', '.join(missing)}")

    root_dependency = lock.dependencies[dependency_id]
    susfs_dependency = lock.dependencies["susfs"]
    if root_dependency.kind != "git" or not is_full_commit(root_dependency.commit):
        raise BuildToolError(f"dependency {dependency_id} lacks an immutable Git commit")
    if susfs_dependency.kind != "git" or not is_full_commit(susfs_dependency.commit):
        raise BuildToolError("dependency susfs lacks an immutable Git commit")
    expected = {
        "KernelSU": root_dependency.commit,
        "SUSFS": susfs_dependency.commit,
    }
    for label, value in requested.items():
        if not value:
            continue
        if not is_full_commit(value):
            raise BuildToolError(f"{label} commit assertion must be a lowercase 40-character SHA")
        if value != expected[label]:
            raise BuildToolError(
                f"{label} commit {value} is not the audited lock {expected[label]}; "
                "update the lock and compatibility fingerprints first"
            )

    result: dict[str, Any] = {
        "schema_version": 1,
        "root_variant": root_variant,
        "dependency_lock_sha256": lock.digest,
        "root": {
            "dependency": dependency_id,
            "uri": root_dependency.url,
            "commit": root_dependency.commit,
        },
        "susfs": {
            "dependency": "susfs",
            "uri": susfs_dependency.url,
            "commit": susfs_dependency.commit,
        },
    }
    if root_variant == "kernelsu-next":
        wild = lock.dependencies["wild_kernel_patches"]
        if wild.kind != "git" or not is_full_commit(wild.commit):
            raise BuildToolError("dependency wild_kernel_patches lacks an immutable Git commit")
        result["compatibility_patches"] = {
            "dependency": "wild_kernel_patches",
            "uri": wild.url,
            "commit": wild.commit,
        }
    return result


def validate_feature_dependencies(feature: FeatureProfile, lock: DependencyLock) -> None:
    required = set(feature.external_modules)
    if feature.flags.get("artifact.wireless_firmware", False):
        required.add("nethunter_wireless_firmware")
    missing = sorted(required - set(lock.dependencies))
    if missing:
        raise BuildToolError(
            f"{feature.source_path}: feature dependencies absent from dependency lock: {', '.join(missing)}"
        )


def resolve_inside(root: Path, relative: str, where: str, *, must_exist: bool = True) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise BuildToolError(f"{where}: path escapes repository root: {relative}") from exc
    if must_exist and not candidate.exists():
        raise BuildToolError(f"{where}: missing path {relative}")
    return candidate


def discover_configs(root: Path) -> tuple[Device, DependencyLock, dict[str, Profile], dict[str, FeatureProfile]]:
    lock = load_dependency_lock(root / "dependencies" / "lock.yml")
    device = load_device(root / "configs" / "devices" / "oneplus13.yml")
    manifest_commit = lock.dependencies["oneplus_manifest"].commit
    profiles: dict[str, Profile] = {}
    for path in sorted((root / "configs" / "profiles").glob("*.yml")):
        profile = load_profile(path, device, manifest_commit)
        if profile.id in profiles:
            raise BuildToolError(f"duplicate profile id {profile.id}")
        if profile.manifest_revision != manifest_commit:
            raise BuildToolError(f"{path}: manifest revision disagrees with dependencies/lock.yml")
        profiles[profile.id] = profile
    if set(profiles) != set(MANIFEST_FILES):
        missing = sorted(set(MANIFEST_FILES) - set(profiles))
        extra = sorted(set(profiles) - set(MANIFEST_FILES))
        raise BuildToolError(f"release profile set mismatch; missing={missing}, extra={extra}")
    features: dict[str, FeatureProfile] = {}
    for path in sorted((root / "configs" / "features").glob("*.yml")):
        feature = load_feature_profile(path)
        if path.stem != feature.id:
            raise BuildToolError(f"{path}: feature id must match its filename")
        if feature.id in features:
            raise BuildToolError(f"duplicate feature id {feature.id}")
        validate_feature_dependencies(feature, lock)
        features[feature.id] = feature
    if not features:
        raise BuildToolError("no feature profiles found")
    return device, lock, profiles, features


def select_profile(profiles: Mapping[str, Profile], base: str) -> Profile:
    try:
        return profiles[base]
    except KeyError as exc:
        raise BuildToolError(f"unknown base profile {base!r}; choose {', '.join(sorted(profiles))}") from exc


def select_feature(features: Mapping[str, FeatureProfile], name: str) -> FeatureProfile:
    try:
        return features[name]
    except KeyError as exc:
        raise BuildToolError(f"unknown feature profile {name!r}; choose {', '.join(sorted(features))}") from exc


def enabled_patch_entries(feature: FeatureProfile, base: str) -> list[Mapping[str, Any]]:
    """Normalize and filter an ordered patch series without silently dropping required patches."""

    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(feature.patch_series):
        if isinstance(raw, str):
            entry: dict[str, Any] = {"id": raw, "path": raw, "required": True}
        else:
            entry = dict(raw)
            entry.setdefault("id", entry.get("path"))
            entry.setdefault("path", entry.get("id"))
            entry.setdefault("required", True)
        patch_id = _string(entry.get("id"), f"{feature.source_path}:patch_series[{index}].id")
        path = _string(entry.get("path"), f"{feature.source_path}:patch_series[{index}].path")
        if patch_id in seen:
            raise BuildToolError(f"{feature.source_path}: duplicate patch id {patch_id}")
        seen.add(patch_id)
        bases = entry.get("bases") or entry.get("profiles")
        if bases is not None:
            allowed = {_string(v, f"patch {patch_id}.bases") for v in _list(bases, f"patch {patch_id}.bases")}
            if base not in allowed:
                continue
        flag = entry.get("when") or entry.get("feature_flag")
        if flag is not None:
            flag_name = _string(flag, f"patch {patch_id}.when")
            if flag_name not in feature.flags:
                raise BuildToolError(f"patch {patch_id}: unknown feature flag {flag_name}")
            if not feature.flags[flag_name]:
                continue
        if not isinstance(entry.get("required"), bool):
            raise BuildToolError(f"patch {patch_id}: required must be boolean")
        entry["id"] = patch_id
        entry["path"] = path
        result.append(entry)
    return result


def validate_repository(root: Path) -> dict[str, Any]:
    device, lock, profiles, features = discover_configs(root)
    for feature in features.values():
        for base in profiles:
            for entry in enabled_patch_entries(feature, base):
                # Patch identifiers may be supplied by a locked patch repository.
                # Explicit paths, however, must exist now and are never optional.
                path_value = str(entry["path"])
                if "/" in path_value or path_value.endswith((".patch", ".diff")):
                    if entry["required"]:
                        resolve_inside(root, path_value, f"patch {entry['id']}")
        for fragment in feature.kconfig_fragments:
            if fragment.required:
                resolve_inside(root, fragment.path, f"feature {feature.id} Kconfig fragment")
    return {
        "schema_version": SCHEMA_VERSION,
        "device": device.id,
        "target": device.target,
        "arch": device.arch,
        "kmi": device.kmi,
        "profiles": sorted(profiles),
        "features": sorted(features),
        "dependency_lock_sha256": lock.digest,
    }
