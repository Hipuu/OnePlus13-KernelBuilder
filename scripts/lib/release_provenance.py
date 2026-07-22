"""Generate deterministic release checksums and SLSA v1 provenance."""

from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from .artifacts import (
    ANYKERNEL_CARGO_GIT_ARCHIVE,
    ANYKERNEL_CARGO_GIT_SOURCE,
    ANYKERNEL_CARGO_REGISTRY_SOURCE,
    ANYKERNEL_GIT_SOURCE_TREE_IDS,
    ANYKERNEL_MAGISK_GITLINKS,
    ANYKERNEL_TOOL_CONTRACTS,
    ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS,
    _git_object_digest,
    _zip_datetime,
    _verify_cargo_source_archive,
    _verify_corresponding_source_tarball,
    _verify_magisk_source_closure,
)
from .build_evidence import (
    validate_packaged_build_evidence,
    wireless_led_exports_required,
)
from .config import (
    ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS,
    ANYKERNEL_CARGO_CRATE_IDENTITIES,
    ANYKERNEL_CARGO_GIT_DEPENDENCY_ID,
    ANYKERNEL_PROGRAM_SOURCE_DEPENDENCY_IDS,
    ANYKERNEL_SOURCE_DEPENDENCY_IDS,
    strict_json_loads,
)
from .errors import BuildToolError


BUILD_MANIFEST_NAME = "BUILD-MANIFEST.json"
PROVENANCE_NAME = "provenance.intoto.jsonl"
RELEASE_CHECKSUM_NAME = "RELEASE_SHA256SUMS"
PACKAGE_CHECKSUM_NAME = "SHA256SUMS"
CORRESPONDING_SOURCE_FORMAT = "oneplus13-anykernel-corresponding-source"
CORRESPONDING_SOURCE_MANIFEST = "SOURCE-MANIFEST.json"
CORRESPONDING_SOURCE_POLICY = "SOURCE-POLICY.json"
CHECKED_IN_SOURCE_POLICY = "packaging/anykernel3/CORRESPONDING-SOURCE.json"
CHECKED_IN_EXECUTABLE_POLICY = "packaging/anykernel3/EXECUTABLE-PROVENANCE.json"
CHECKED_IN_ANYKERNEL_OVERLAY = "packaging/anykernel3"
ANYKERNEL_RELEASE_MODES = {
    "EXECUTABLE-PROVENANCE.json": 0o644,
    "Image": 0o644,
    "LICENSE": 0o644,
    "LICENSES/GPL-2.0-only": 0o644,
    "LICENSES/GPL-3.0-or-later": 0o644,
    "META-INF/com/google/android/update-binary": 0o755,
    "META-INF/com/google/android/updater-script": 0o644,
    "SOURCE-CONVEYANCE.md": 0o644,
    "anykernel.sh": 0o755,
    "tools/ak3-core.sh": 0o755,
    "tools/busybox": 0o755,
    "tools/magiskboot": 0o755,
}
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEPENDENCY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
RELEASE_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")


class ReleaseProvenanceError(ValueError):
    """Raised when packaged lineage is incomplete, mutable, or inconsistent."""


def _sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseProvenanceError(f"{where} must be an object")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReleaseProvenanceError(f"{where} must be a non-empty trimmed string")
    return value


def _full_commit(value: Any, where: str) -> str:
    commit = _string(value, where).lower()
    if HEX40_RE.fullmatch(commit) is None:
        raise ReleaseProvenanceError(f"{where} must be a full lowercase Git commit")
    return commit


def _sha256(value: Any, where: str) -> str:
    digest = _string(value, where).lower()
    if SHA256_RE.fullmatch(digest) is None:
        raise ReleaseProvenanceError(f"{where} must be a lowercase SHA-256 digest")
    return digest


def _https_url(value: Any, where: str) -> str:
    url = _string(value, where)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseProvenanceError(f"{where} must be a credential-free HTTPS URL")
    return url


def _portable_path(
    value: Any,
    where: str,
    *,
    one_component: bool = False,
    allow_dot: bool = False,
) -> str:
    text = _string(value, where)
    if "\\" in text:
        raise ReleaseProvenanceError(f"{where} must use portable separators")
    path = PurePosixPath(text)
    normalized = path.as_posix()
    if (
        path.is_absolute()
        or PureWindowsPath(text).is_absolute()
        or bool(PureWindowsPath(text).drive)
        or ".." in path.parts
        or normalized == ""
        or (normalized == "." and not allow_dot)
    ):
        raise ReleaseProvenanceError(f"{where} must be a repository-relative path")
    if one_component and len(path.parts) != 1:
        raise ReleaseProvenanceError(f"{where} must name one release asset")
    return normalized


def _release_file(root: Path, relative: str, *, role: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ReleaseProvenanceError(f"{role} escapes the release directory") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ReleaseProvenanceError(f"{role} is missing or is not a regular file: {relative}")
    return candidate


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _git_descriptor(*, name: str, uri: str, commit: str) -> dict[str, Any]:
    commit = _full_commit(commit, f"{name}.commit")
    return {
        "name": name,
        "uri": f"git+{_https_url(uri, f'{name}.uri')}@{commit}",
        "digest": {"gitCommit": commit},
    }


def _resource_descriptor(*, name: str, uri: str, sha256: str) -> dict[str, Any]:
    return {
        "name": name,
        "uri": _https_url(uri, f"{name}.uri"),
        "digest": {"sha256": _sha256(sha256, f"{name}.sha256")},
    }


def _dependency_descriptors(
    dependencies: Any,
) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    if not isinstance(dependencies, list) or not dependencies:
        raise ReleaseProvenanceError("build manifest dependencies must be a non-empty array")
    descriptors: list[dict[str, Any]] = []
    identities: list[str] = []
    records: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(dependencies):
        record = _mapping(raw, f"dependencies[{index}]")
        dependency_id = _string(record.get("id"), f"dependencies[{index}].id")
        if DEPENDENCY_ID_RE.fullmatch(dependency_id) is None or dependency_id in records:
            raise ReleaseProvenanceError(f"invalid or duplicate dependency id: {dependency_id}")
        identities.append(dependency_id)
        records[dependency_id] = record
        kind = _string(record.get("kind"), f"dependencies.{dependency_id}.kind")
        required_for = record.get("required_for")
        if (
            not isinstance(required_for, list)
            or not required_for
            or not all(isinstance(value, str) and value for value in required_for)
            or required_for != sorted(set(required_for))
        ):
            raise ReleaseProvenanceError(
                f"dependencies.{dependency_id}.required_for must be sorted and unique"
            )
        source = record.get("source")
        resource = record.get("resource")
        if kind == "git":
            source_record = _mapping(source, f"dependencies.{dependency_id}.source")
            descriptors.append(
                _git_descriptor(
                    name=f"locked-dependency:{dependency_id}",
                    uri=_string(source_record.get("uri"), f"dependencies.{dependency_id}.source.uri"),
                    commit=_string(
                        source_record.get("commit"),
                        f"dependencies.{dependency_id}.source.commit",
                    ),
                )
            )
            if resource is not None:
                raise ReleaseProvenanceError(
                    f"Git dependency {dependency_id} must not declare a separate resource"
                )
        elif kind in {"file", "archive", "release_asset"}:
            resource_record = _mapping(resource, f"dependencies.{dependency_id}.resource")
            descriptors.append(
                _resource_descriptor(
                    name=f"locked-dependency:{dependency_id}",
                    uri=_string(
                        resource_record.get("uri"),
                        f"dependencies.{dependency_id}.resource.uri",
                    ),
                    sha256=_string(
                        resource_record.get("sha256"),
                        f"dependencies.{dependency_id}.resource.sha256",
                    ),
                )
            )
            if source is not None:
                source_record = _mapping(source, f"dependencies.{dependency_id}.source")
                descriptors.append(
                    _git_descriptor(
                        name=f"locked-dependency-source:{dependency_id}",
                        uri=_string(
                            source_record.get("uri"),
                            f"dependencies.{dependency_id}.source.uri",
                        ),
                        commit=_string(
                            source_record.get("commit"),
                            f"dependencies.{dependency_id}.source.commit",
                        ),
                    )
                )
        else:
            raise ReleaseProvenanceError(f"unsupported dependency kind for {dependency_id}: {kind}")
    if identities != sorted(identities):
        raise ReleaseProvenanceError("build manifest dependencies are not deterministically ordered")
    return descriptors, records


def _inventory_from_lock(document: Any) -> list[dict[str, Any]]:
    lock = _mapping(document, "dependencies/lock.yml")
    dependencies = _mapping(lock.get("dependencies"), "dependencies/lock.yml.dependencies")
    inventory: list[dict[str, Any]] = []
    for dependency_id, raw in sorted(dependencies.items()):
        if not isinstance(dependency_id, str):
            raise ReleaseProvenanceError("dependency lock contains a non-string id")
        item = _mapping(raw, f"dependencies/lock.yml.dependencies.{dependency_id}")
        kind = _string(item.get("kind"), f"dependency lock {dependency_id}.kind")
        required_for = item.get("required_for")
        if (
            not isinstance(required_for, list)
            or not required_for
            or not all(isinstance(value, str) and value for value in required_for)
        ):
            raise ReleaseProvenanceError(
                f"dependency lock {dependency_id}.required_for must contain strings"
            )
        record: dict[str, Any] = {
            "id": dependency_id,
            "kind": kind,
            "required_for": sorted(required_for),
        }
        ref = item.get("ref")
        if ref is not None:
            record["ref"] = ref
        version = item.get("version")
        if isinstance(version, str) and version:
            record["version"] = version
        if kind == "git":
            record["source"] = {
                "uri": item.get("url"),
                "commit": item.get("commit") or item.get("revision"),
            }
        else:
            record["resource"] = {
                "uri": item.get("url"),
                "sha256": item.get("sha256"),
            }
            source_uri = item.get("repository") or item.get("repo_url")
            source_commit = item.get("commit") or item.get("revision") or item.get(
                "repo_commit"
            )
            if source_uri is not None:
                record["source"] = {
                    "uri": source_uri,
                    "commit": source_commit,
                }
        inventory.append(record)
    if not inventory:
        raise ReleaseProvenanceError("dependency lock contains no dependencies")
    return inventory


def _manifest_project_descriptors(path: Path) -> list[dict[str, Any]]:
    payload = path.read_bytes()
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ReleaseProvenanceError("resolved OnePlus manifest contains a prohibited declaration")
    try:
        document = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ReleaseProvenanceError(f"resolved OnePlus manifest is invalid XML: {exc}") from exc
    if document.tag != "manifest":
        raise ReleaseProvenanceError("resolved OnePlus manifest has the wrong root element")
    remotes: dict[str, str] = {}
    for index, remote in enumerate(document.findall("remote")):
        name = _string(remote.get("name"), f"manifest.remote[{index}].name")
        if name in remotes:
            raise ReleaseProvenanceError(f"resolved OnePlus manifest repeats remote {name}")
        remotes[name] = _https_url(remote.get("fetch"), f"manifest.remote[{name}].fetch").rstrip("/")
    default = document.find("default")
    default_remote = default.get("remote") if default is not None else None
    descriptors: list[dict[str, Any]] = []
    project_paths: set[str] = set()
    for index, project in enumerate(document.findall("project")):
        project_name = _portable_path(
            project.get("name"),
            f"manifest.project[{index}].name",
        )
        project_path = _portable_path(
            project.get("path") or project_name,
            f"manifest.project[{index}].path",
            allow_dot=True,
        )
        if project_path in project_paths:
            raise ReleaseProvenanceError(
                f"resolved OnePlus manifest repeats project path {project_path}"
            )
        project_paths.add(project_path)
        remote_name = project.get("remote") or default_remote
        if not isinstance(remote_name, str) or remote_name not in remotes:
            raise ReleaseProvenanceError(
                f"resolved OnePlus manifest project {project_path} has no known remote"
            )
        commit = _full_commit(
            project.get("revision"),
            f"manifest.project[{project_path}].revision",
        )
        repository_url = f"{remotes[remote_name]}/{project_name}"
        if not repository_url.endswith(".git"):
            repository_url += ".git"
        descriptor = _git_descriptor(
            name=f"oneplus-project:{project_path}",
            uri=repository_url,
            commit=commit,
        )
        descriptor["annotations"] = {
            "oneplus13_manifestName": project_name,
            "oneplus13_manifestPath": project_path,
        }
        descriptors.append(descriptor)
    if not descriptors:
        raise ReleaseProvenanceError("resolved OnePlus manifest contains no projects")
    return sorted(descriptors, key=_descriptor_sort_key)


def _descriptor_sort_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("name", "")),
        str(record.get("uri", "")),
        json.dumps(record.get("digest", {}), sort_keys=True, separators=(",", ":")),
    )


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ReleaseProvenanceError(f"{where} must be a boolean")
    return value


def _requested_epoch(value: str) -> int | None:
    if not value:
        return None
    configured = value.strip()
    if configured.isdigit():
        return int(configured)
    normalized = (
        configured[:-1] + "+00:00"
        if configured.endswith("Z")
        else configured
    )
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReleaseProvenanceError("buildTimestamp must be RFC3339 or an epoch integer") from exc
    if parsed.tzinfo is None:
        raise ReleaseProvenanceError("buildTimestamp must include a timezone")
    return int(parsed.timestamp())


def _release_timestamp_record(raw: Any, source_date_epoch: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ReleaseProvenanceError("buildTimestamp must be a string")
    try:
        encoded = raw.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ReleaseProvenanceError("buildTimestamp must be valid UTF-8") from exc
    if len(encoded) > 1024:
        raise ReleaseProvenanceError("buildTimestamp exceeds the size limit")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ReleaseProvenanceError("buildTimestamp must be a single-line value")
    if raw and not raw.strip():
        raise ReleaseProvenanceError("buildTimestamp must not contain only whitespace")
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or source_date_epoch < 0
        or source_date_epoch
        > int(datetime(2107, 12, 31, 23, 59, 58, tzinfo=timezone.utc).timestamp())
    ):
        raise ReleaseProvenanceError("packaged kernel source epoch is invalid")
    requested_epoch = _requested_epoch(raw)
    if requested_epoch is not None and requested_epoch != source_date_epoch:
        raise ReleaseProvenanceError(
            "release buildTimestamp differs from the packaged kernel source epoch"
        )
    if not raw:
        return {
            "artifact_key": "default",
            "mode": "default",
            "requested": None,
            "requested_sha256": None,
            "source_date_epoch": source_date_epoch,
        }
    digest = hashlib.sha256(encoded).hexdigest()
    return {
        "artifact_key": digest,
        "mode": "explicit",
        "requested": raw,
        "requested_sha256": digest,
        "source_date_epoch": source_date_epoch,
    }


def _validate_build_identity(
    manifest: Mapping[str, Any],
    *,
    repository: str,
    revision: str,
    external_parameters: Mapping[str, Any],
) -> None:
    if manifest.get("schema_version") != 2:
        raise ReleaseProvenanceError("BUILD-MANIFEST.json must use schema version 2")
    builder = _mapping(manifest.get("builder"), "builder")
    if builder.get("repository") != repository or builder.get("revision") != revision:
        raise ReleaseProvenanceError("packaged builder identity differs from the release revision")
    if _bool(manifest.get("smoke"), "smoke"):
        raise ReleaseProvenanceError("smoke packages cannot be published as releases")
    configuration = _mapping(manifest.get("configuration"), "configuration")
    kernel = _mapping(manifest.get("kernel"), "kernel")
    expected = {
        "base": manifest.get("base"),
        "root": manifest.get("root_variant"),
        "profile": manifest.get("feature_profile"),
        "target": manifest.get("build_target"),
        "optimization": configuration.get("optimization"),
        "lto": configuration.get("lto"),
        "debug": manifest.get("debug"),
        "preRelease": manifest.get("pre_release"),
        "branding": kernel.get("branding"),
    }
    for field, packaged_value in expected.items():
        if external_parameters.get(field) != packaged_value:
            raise ReleaseProvenanceError(
                f"release parameter {field} differs from the packaged build manifest"
            )
    timestamp_record = _release_timestamp_record(
        external_parameters.get("buildTimestamp", ""),
        kernel.get("source_date_epoch"),
    )
    if kernel.get("build_timestamp") != timestamp_record:
        raise ReleaseProvenanceError(
            "release buildTimestamp raw identity differs from the packaged build manifest"
        )


def _artifact_asset_name(record: Mapping[str, Any], index: int) -> str:
    portable = _portable_path(record.get("path"), f"artifacts[{index}].path")
    name = PurePosixPath(portable).name
    if RELEASE_ASSET_NAME_RE.fullmatch(name) is None:
        raise ReleaseProvenanceError(
            f"artifacts[{index}].path has an unsafe release filename"
        )
    return name


def _read_json_bytes(payload: bytes, where: str) -> Mapping[str, Any]:
    try:
        value = strict_json_loads(payload.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseProvenanceError(f"{where} is invalid JSON") from exc
    return _mapping(value, where)


def _validate_aarch64_elf(payload: bytes, where: str) -> None:
    if (
        len(payload) < 20
        or payload[:4] != b"\x7fELF"
        or payload[4] != 2
        or payload[5] != 1
        or int.from_bytes(payload[16:18], "little") != 2
        or int.from_bytes(payload[18:20], "little") != 183
    ):
        raise ReleaseProvenanceError(f"{where} is not an ELF64 little-endian AArch64 executable")


def _validate_anykernel_release_zip(
    *,
    archive_path: Path,
    artifact_record: Mapping[str, Any],
    image_record: Mapping[str, Any],
    repository_root: Path,
    lock_document: Mapping[str, Any],
) -> dict[str, str]:
    executable_policy_path = _release_file(
        repository_root,
        CHECKED_IN_EXECUTABLE_POLICY,
        role="checked-out AnyKernel executable policy",
    )
    executable_policy_bytes = executable_policy_path.read_bytes()
    executable_policy = _read_json_bytes(
        executable_policy_bytes,
        "checked-out AnyKernel executable policy",
    )
    if executable_policy.get("schema_version") != 2:
        raise ReleaseProvenanceError("checked-out AnyKernel executable policy is unsupported")

    lock_dependencies = _mapping(
        lock_document.get("dependencies"),
        "dependencies/lock.yml.dependencies",
    )
    anykernel_dependency = _mapping(
        lock_dependencies.get("anykernel3"),
        "dependencies/lock.yml.dependencies.anykernel3",
    )
    magisk_dependency = _mapping(
        lock_dependencies.get("magisk_release_apk"),
        "dependencies/lock.yml.dependencies.magisk_release_apk",
    )
    policy_anykernel = _exact_mapping_keys(
        executable_policy.get("anykernel3"),
        {
            "dependency",
            "repository",
            "commit",
            "license_classification",
            "license_member",
            "template_members",
        },
        "AnyKernel executable policy anykernel3",
    )
    policy_release = _exact_mapping_keys(
        executable_policy.get("release_asset"),
        {
            "dependency",
            "uri",
            "sha256",
            "repository",
            "ref",
            "source_commit",
            "version",
            "license_classification",
            "archive_format",
            "abi",
        },
        "AnyKernel executable policy release_asset",
    )
    if (
        anykernel_dependency.get("kind") != "git"
        or policy_anykernel.get("dependency") != "anykernel3"
        or policy_anykernel.get("repository") != anykernel_dependency.get("url")
        or policy_anykernel.get("commit") != anykernel_dependency.get("commit")
        or policy_anykernel.get("license_classification")
        != anykernel_dependency.get("license")
        or policy_anykernel.get("license_member") != "LICENSE"
        or policy_anykernel.get("template_members")
        != [dict(record) for record in ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS]
        or magisk_dependency.get("kind") != "release_asset"
        or policy_release.get("dependency") != "magisk_release_apk"
        or policy_release.get("uri") != magisk_dependency.get("url")
        or policy_release.get("sha256") != magisk_dependency.get("sha256")
        or policy_release.get("repository") != magisk_dependency.get("repository")
        or policy_release.get("source_commit") != magisk_dependency.get("commit")
        or policy_release.get("version") != magisk_dependency.get("version")
    ):
        raise ReleaseProvenanceError(
            "checked-out AnyKernel executable policy differs from the dependency lock"
        )

    expected_policy_sha = hashlib.sha256(executable_policy_bytes).hexdigest()
    if (
        artifact_record.get("dependencies") != ["anykernel3", "magisk_release_apk"]
        or artifact_record.get("member_count") != len(ANYKERNEL_RELEASE_MODES)
        or artifact_record.get("member_mode_policy") != "explicit-host-independent"
        or artifact_record.get("elf_class") != "ELFCLASS64"
        or artifact_record.get("elf_machine") != "EM_AARCH64"
        or artifact_record.get("executable_provenance_member")
        != "EXECUTABLE-PROVENANCE.json"
        or artifact_record.get("executable_provenance_sha256") != expected_policy_sha
        or artifact_record.get("magisk_release_sha256")
        != magisk_dependency.get("sha256")
    ):
        raise ReleaseProvenanceError(
            "AnyKernel artifact record differs from the exact release contract"
        )

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            expected_names = sorted(ANYKERNEL_RELEASE_MODES)
            if names != expected_names or len(names) != len(set(names)):
                raise ReleaseProvenanceError(
                    "AnyKernel ZIP member set or order differs from the exact policy"
                )
            if (
                archive_path.stat().st_size > 512 * 1024 * 1024
                or sum(info.file_size for info in infos) > 512 * 1024 * 1024
            ):
                raise ReleaseProvenanceError("AnyKernel ZIP declares excessive content")
            members: dict[str, bytes] = {}
            for info in infos:
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.is_dir()
                    or info.flag_bits & 0x1
                    or info.create_system != 3
                    or info.compress_type
                    not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or not stat.S_ISREG(unix_mode)
                    or stat.S_IMODE(unix_mode)
                    != ANYKERNEL_RELEASE_MODES[info.filename]
                ):
                    raise ReleaseProvenanceError(
                        f"AnyKernel ZIP member metadata is invalid: {info.filename}"
                    )
                members[info.filename] = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError) as exc:
        raise ReleaseProvenanceError(
            "AnyKernel release artifact is not a readable policy-conforming ZIP"
        ) from exc

    if members["EXECUTABLE-PROVENANCE.json"] != executable_policy_bytes:
        raise ReleaseProvenanceError(
            "packaged AnyKernel executable policy differs from the release revision"
        )
    for record in ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS:
        member = str(record["path"])
        payload = members[member]
        expected_mode = (
            "100755" if ANYKERNEL_RELEASE_MODES[member] == 0o755 else "100644"
        )
        if (
            record.get("git_mode") != expected_mode
            or len(payload) != record.get("size")
            or hashlib.sha256(payload).hexdigest() != record.get("sha256")
            or _git_object_digest(b"blob", payload).hex() != record.get("git_blob")
        ):
            raise ReleaseProvenanceError(
                f"packaged AnyKernel template differs from its pinned Git blob: {member}"
            )
    checked_overlay_members = {
        "anykernel.sh": "anykernel.sh",
        "SOURCE-CONVEYANCE.md": "SOURCE-CONVEYANCE.md",
        "LICENSES/GPL-2.0-only": "licenses/GPL-2.0-only",
        "LICENSES/GPL-3.0-or-later": "licenses/GPL-3.0-or-later",
    }
    for member, relative in checked_overlay_members.items():
        checked_path = _release_file(
            repository_root,
            f"{CHECKED_IN_ANYKERNEL_OVERLAY}/{relative}",
            role=f"checked-out AnyKernel overlay {member}",
        )
        if members[member] != checked_path.read_bytes():
            raise ReleaseProvenanceError(
                f"packaged AnyKernel overlay differs from the release revision: {member}"
            )

    image_size = image_record.get("size")
    image_sha = _sha256(image_record.get("sha256"), "kernel-image.sha256")
    if (
        not isinstance(image_size, int)
        or isinstance(image_size, bool)
        or len(members["Image"]) != image_size
        or hashlib.sha256(members["Image"]).hexdigest() != image_sha
    ):
        raise ReleaseProvenanceError(
            "AnyKernel Image differs from the separately sealed kernel image"
        )

    raw_executables = executable_policy.get("executables")
    if not isinstance(raw_executables, list) or len(raw_executables) != 2:
        raise ReleaseProvenanceError(
            "checked-out AnyKernel executable policy lacks the exact tool records"
        )
    executable_digests: dict[str, str] = {}
    for index, raw_record in enumerate(raw_executables):
        record = _mapping(raw_record, f"AnyKernel executable record {index}")
        member = _portable_path(
            record.get("path"),
            f"AnyKernel executable record {index}.path",
        )
        if member not in {"tools/busybox", "tools/magiskboot"} or member in executable_digests:
            raise ReleaseProvenanceError("AnyKernel executable policy path set differs")
        size = record.get("size")
        digest = _sha256(
            record.get("sha256"), f"AnyKernel executable record {member}.sha256"
        )
        payload = members[member]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or len(payload) != size
            or hashlib.sha256(payload).hexdigest() != digest
        ):
            raise ReleaseProvenanceError(
                f"packaged AnyKernel executable differs from its audited policy: {member}"
            )
        _validate_aarch64_elf(payload, f"packaged AnyKernel executable {member}")
        executable_digests[member] = digest
    if set(executable_digests) != {"tools/busybox", "tools/magiskboot"}:
        raise ReleaseProvenanceError("AnyKernel executable policy path set differs")
    return executable_digests


def _exact_mapping_keys(
    value: object,
    expected: set[str],
    where: str,
) -> Mapping[str, Any]:
    record = _mapping(value, where)
    if set(record) != expected:
        raise ReleaseProvenanceError(
            f"{where} schema differs; missing={sorted(expected - set(record))}, "
            f"unexpected={sorted(set(record) - expected)}"
        )
    return record


def _source_root(record: Mapping[str, Any]) -> str:
    if record.get("relationship") == "magisk-cargo-registry":
        packages = record.get("cargo_packages")
        if not isinstance(packages, list) or len(packages) != 1:
            raise ReleaseProvenanceError("Cargo source record lacks one package")
        package = _mapping(packages[0], "Cargo source package")
        return f"{package.get('name')}-{package.get('version')}"
    repository = _https_url(
        record.get("repository"),
        f"corresponding-source {record.get('dependency')}.repository",
    )
    repository_name = PurePosixPath(urlsplit(repository).path).name
    if not repository_name.endswith(".git"):
        raise ReleaseProvenanceError(
            f"corresponding-source repository is not a Git URL: {record.get('dependency')}"
        )
    return f"{repository_name[:-4]}-{record.get('commit')}"


def _validate_source_policy(
    policy: object,
    *,
    dependency_ids: list[str],
    lock_dependencies: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    policy = _exact_mapping_keys(
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
        "checked-out corresponding-source policy",
    )
    if (
        policy.get("schema_version") != 1
        or policy.get("format") != CORRESPONDING_SOURCE_FORMAT
        or not isinstance(policy.get("scope"), str)
        or not str(policy["scope"]).strip()
    ):
        raise ReleaseProvenanceError(
            "checked-out corresponding-source policy identity is invalid"
        )

    raw_archives = policy.get("archives")
    if not isinstance(raw_archives, list) or len(raw_archives) != len(dependency_ids):
        raise ReleaseProvenanceError(
            "checked-out corresponding-source policy archive count differs"
        )
    archive_keys = {
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
    package_keys = {"name", "version", "source", "checksum", "manifest_path"}
    crate_identities = dict(
        zip(ANYKERNEL_CARGO_CRATE_DEPENDENCY_IDS, ANYKERNEL_CARGO_CRATE_IDENTITIES)
    )
    quick_commit = ANYKERNEL_CARGO_GIT_SOURCE.rsplit("#", 1)[1]
    expected_quick_packages = [
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
    records: list[Mapping[str, Any]] = []
    paths: set[str] = set()
    for index, raw_record in enumerate(raw_archives):
        where = f"corresponding-source policy archives[{index}]"
        record = _mapping(raw_record, where)
        actual_keys = set(record)
        allowed_keys = {frozenset(archive_keys), frozenset({*archive_keys, "tree"})}
        if frozenset(actual_keys) not in allowed_keys:
            raise ReleaseProvenanceError(f"{where} schema differs")
        dependency_id = _string(record.get("dependency"), f"{where}.dependency")
        if dependency_id != dependency_ids[index]:
            raise ReleaseProvenanceError(
                "corresponding-source policy dependency order differs"
            )
        archive_path = _portable_path(
            record.get("archive_path"), f"{where}.archive_path"
        )
        if not archive_path.startswith("sources/") or archive_path in paths:
            raise ReleaseProvenanceError(
                f"corresponding-source policy path is invalid: {dependency_id}"
            )
        paths.add(archive_path)
        url = _https_url(record.get("url"), f"{where}.url")
        size = record.get("size")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or size > 512 * 1024 * 1024
        ):
            raise ReleaseProvenanceError(
                f"corresponding-source policy size is invalid: {dependency_id}"
            )
        digest = _sha256(record.get("sha256"), f"{where}.sha256")
        license_value = _string(record.get("license"), f"{where}.license")
        packages = record.get("cargo_packages")
        if not isinstance(packages, list):
            raise ReleaseProvenanceError(f"{where}.cargo_packages must be an array")
        validated_packages = [
            dict(_exact_mapping_keys(package, package_keys, f"{where}.cargo_packages[{package_index}]"))
            for package_index, package in enumerate(packages)
        ]

        expected_tree = ANYKERNEL_GIT_SOURCE_TREE_IDS.get(dependency_id)
        if expected_tree is None:
            if "tree" in record:
                raise ReleaseProvenanceError(
                    f"non-Git corresponding-source record declares a tree: {dependency_id}"
                )
        elif record.get("tree") != expected_tree:
            raise ReleaseProvenanceError(
                f"corresponding-source Git tree identity differs: {dependency_id}"
            )

        crate_identity = crate_identities.get(dependency_id)
        if dependency_id in ANYKERNEL_PROGRAM_SOURCE_DEPENDENCY_IDS:
            program_index = ANYKERNEL_PROGRAM_SOURCE_DEPENDENCY_IDS.index(dependency_id)
            expected_relationship = (
                "magisk-root"
                if program_index == 0
                else "busybox-root"
                if program_index == 1
                else "magisk-git-submodule"
            )
            if (
                not archive_path.endswith(".tar.gz")
                or record.get("source_registry") is not None
                or not isinstance(record.get("repository"), str)
                or not str(record["repository"]).startswith("https://github.com/")
                or not str(record["repository"]).endswith(".git")
                or HEX40_RE.fullmatch(str(record.get("commit"))) is None
                or url
                != (
                    f"{str(record.get('repository'))[:-4]}/archive/"
                    f"{record.get('commit')}.tar.gz"
                )
                or record.get("relationship") != expected_relationship
                or validated_packages
                or (
                    program_index < 2
                    and record.get("submodule_path") is not None
                )
                or (
                    program_index >= 2
                    and _portable_path(
                        record.get("submodule_path"), f"{where}.submodule_path"
                    )
                    != record.get("submodule_path")
                )
            ):
                raise ReleaseProvenanceError(
                    f"program corresponding-source identity differs: {dependency_id}"
                )
        elif dependency_id == ANYKERNEL_CARGO_GIT_DEPENDENCY_ID:
            if (
                archive_path
                != f"sources/cargo/git/quick-protobuf-{quick_commit[:8]}.tar.gz"
                or record.get("repository") != ANYKERNEL_CARGO_GIT_ARCHIVE["repository"]
                or record.get("source_registry") is not None
                or record.get("commit") != quick_commit
                or url
                != (
                    f"{str(ANYKERNEL_CARGO_GIT_ARCHIVE['repository'])[:-4]}/archive/"
                    f"{quick_commit}.tar.gz"
                )
                or record.get("license") != "MIT"
                or record.get("relationship") != "magisk-cargo-git"
                or record.get("submodule_path") is not None
                or validated_packages != expected_quick_packages
            ):
                raise ReleaseProvenanceError(
                    "Magisk Cargo Git corresponding-source identity differs"
                )
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
                or record.get("repository") is not None
                or record.get("source_registry")
                != f"https://crates.io/crates/{crate_name}"
                or url
                != (
                    f"https://static.crates.io/crates/{crate_name}/"
                    f"{crate_name}-{crate_version}.crate"
                )
                or record.get("commit") is not None
                or record.get("relationship") != "magisk-cargo-registry"
                or record.get("submodule_path") is not None
                or validated_packages != [expected_package]
            ):
                raise ReleaseProvenanceError(
                    f"Magisk Cargo registry corresponding-source identity differs: {dependency_id}"
                )
        else:
            raise ReleaseProvenanceError(
                f"unclassified corresponding-source dependency: {dependency_id}"
            )

        lock_record = _mapping(
            lock_dependencies.get(dependency_id),
            f"dependencies/lock.yml.dependencies.{dependency_id}",
        )
        if (
            lock_record.get("kind") != "file"
            or lock_record.get("url") != url
            or lock_record.get("repository") != record.get("repository")
            or lock_record.get("commit") != record.get("commit")
            or lock_record.get("size") != size
            or lock_record.get("sha256") != digest
            or lock_record.get("license") != license_value
            or "package-anykernel3-source"
            not in lock_record.get("required_for", [])
        ):
            raise ReleaseProvenanceError(
                f"corresponding-source policy differs from the checked-out lock: {dependency_id}"
            )
        records.append(record)

    by_dependency = {str(record["dependency"]): record for record in records}
    raw_gitlinks = policy.get("magisk_gitlinks")
    expected_gitlink_ids = list(ANYKERNEL_PROGRAM_SOURCE_DEPENDENCY_IDS[2:])
    expected_gitlinks = [
        {"dependency": dependency_id, **dict(ANYKERNEL_MAGISK_GITLINKS[dependency_id])}
        for dependency_id in expected_gitlink_ids
    ]
    if raw_gitlinks != expected_gitlinks:
        raise ReleaseProvenanceError("Magisk Gitlink inventory differs")
    for index, dependency_id in enumerate(expected_gitlink_ids):
        link = _exact_mapping_keys(
            raw_gitlinks[index],
            {"dependency", "path", "repository", "commit"},
            f"Magisk Gitlink {index}",
        )
        record = by_dependency[dependency_id]
        if (
            link.get("dependency") != dependency_id
            or link.get("path") != record.get("submodule_path")
            or link.get("repository") != record.get("repository")
            or link.get("commit") != record.get("commit")
        ):
            raise ReleaseProvenanceError(
                f"Magisk Gitlink identity differs: {dependency_id}"
            )

    magisk_record = records[0]
    magisk_release = _mapping(
        lock_dependencies.get("magisk_release_apk"),
        "dependencies/lock.yml.dependencies.magisk_release_apk",
    )
    busybox_record = records[1]
    busybox_source = ANYKERNEL_TOOL_CONTRACTS["tools/busybox"]["source"]
    if (
        magisk_record.get("repository") != magisk_release.get("repository")
        or magisk_record.get("commit") != magisk_release.get("commit")
        or busybox_record.get("repository") != busybox_source.get("repository")
        or busybox_record.get("commit") != busybox_source.get("commit")
    ):
        raise ReleaseProvenanceError(
            "retained executable and corresponding root source identities differ"
        )
    magisk_root = _source_root(magisk_record)
    gitmodules = _exact_mapping_keys(
        policy.get("magisk_gitmodules"),
        {"dependency", "archive_member", "size", "sha256"},
        "Magisk .gitmodules identity",
    )
    cargo_lock = _exact_mapping_keys(
        policy.get("cargo_lock"),
        {
            "dependency",
            "archive_member",
            "format_version",
            "size",
            "sha256",
            "package_count",
            "local_package_count",
            "registry_package_count",
            "git_package_count",
            "registry_archive_count",
            "git_archive_count",
            "registry_source",
            "git_source",
        },
        "Magisk Cargo.lock identity",
    )
    for identity, expected_member, where in (
        (gitmodules, f"{magisk_root}/.gitmodules", "Magisk .gitmodules"),
        (cargo_lock, f"{magisk_root}/native/src/Cargo.lock", "Magisk Cargo.lock"),
    ):
        if (
            identity.get("dependency") != "magisk_source"
            or identity.get("archive_member") != expected_member
            or not isinstance(identity.get("size"), int)
            or isinstance(identity.get("size"), bool)
            or int(identity["size"]) < 1
            or SHA256_RE.fullmatch(str(identity.get("sha256"))) is None
        ):
            raise ReleaseProvenanceError(f"{where} identity differs")
    if (
        cargo_lock.get("format_version") != 4
        or cargo_lock.get("package_count") != 155
        or cargo_lock.get("local_package_count") != 13
        or cargo_lock.get("registry_package_count") != 140
        or cargo_lock.get("git_package_count") != 2
        or cargo_lock.get("registry_archive_count") != 140
        or cargo_lock.get("git_archive_count") != 1
        or cargo_lock.get("registry_source") != ANYKERNEL_CARGO_REGISTRY_SOURCE
        or cargo_lock.get("git_source") != ANYKERNEL_CARGO_GIT_SOURCE
    ):
        raise ReleaseProvenanceError("Magisk Cargo.lock closure counts differ")
    return records


def _validate_corresponding_source_companion(
    *,
    archive_path: Path,
    artifact_record: Mapping[str, Any],
    repository_root: Path,
    lock_document: Mapping[str, Any],
    lock_canonical_sha256: str,
    source_date_epoch: int,
) -> None:
    dependency_ids = list(ANYKERNEL_SOURCE_DEPENDENCY_IDS)
    if (
        artifact_record.get("dependencies") != dependency_ids
        or artifact_record.get("archive_count") != len(dependency_ids)
        or artifact_record.get("member_count") != len(dependency_ids) + 2
        or artifact_record.get("member_mode_policy") != "all-regular-0644"
        or artifact_record.get("source_manifest_member")
        != CORRESPONDING_SOURCE_MANIFEST
        or artifact_record.get("source_policy_member") != CORRESPONDING_SOURCE_POLICY
        or artifact_record.get("reproducible_build_proof") is not False
        or not isinstance(artifact_record.get("scope"), str)
        or not str(artifact_record["scope"]).strip()
    ):
        raise ReleaseProvenanceError(
            "corresponding-source artifact record differs from the exact release contract"
        )
    expected_manifest_digest = _sha256(
        artifact_record.get("source_manifest_sha256"),
        "corresponding-source.source_manifest_sha256",
    )
    expected_policy_digest = _sha256(
        artifact_record.get("source_policy_sha256"),
        "corresponding-source.source_policy_sha256",
    )

    checked_policy_path = _release_file(
        repository_root,
        CHECKED_IN_SOURCE_POLICY,
        role="checked-out corresponding-source policy",
    )
    checked_policy_bytes = checked_policy_path.read_bytes()
    if _sha256_file(checked_policy_path) != expected_policy_digest:
        raise ReleaseProvenanceError(
            "corresponding-source policy digest differs from the checked-out release revision"
        )
    checked_policy = _read_json_bytes(
        checked_policy_bytes,
        "checked-out corresponding-source policy",
    )
    if checked_policy.get("scope") != artifact_record["scope"]:
        raise ReleaseProvenanceError(
            "checked-out corresponding-source policy identity is invalid"
        )

    lock_dependencies = _mapping(
        lock_document.get("dependencies"),
        "dependencies/lock.yml.dependencies",
    )
    policy_archives = _validate_source_policy(
        checked_policy,
        dependency_ids=dependency_ids,
        lock_dependencies=lock_dependencies,
    )
    archive_paths = [str(record["archive_path"]) for record in policy_archives]
    source_sizes = {
        str(record["archive_path"]): int(record["size"])
        for record in policy_archives
    }
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or source_date_epoch < 0
    ):
        raise ReleaseProvenanceError(
            "corresponding-source ZIP timestamp epoch is invalid"
        )
    try:
        expected_zip_datetime = _zip_datetime(source_date_epoch)
    except BuildToolError as exc:
        raise ReleaseProvenanceError(
            "corresponding-source ZIP timestamp epoch is invalid"
        ) from exc

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            if archive.comment != b"":
                raise ReleaseProvenanceError(
                    "corresponding-source ZIP archive comment is forbidden"
                )
            infos = archive.infolist()
            names = [info.filename for info in infos]
            expected_names = sorted(
                [
                    CORRESPONDING_SOURCE_MANIFEST,
                    CORRESPONDING_SOURCE_POLICY,
                    *archive_paths,
                ]
            )
            if names != expected_names or len(names) != len(set(names)):
                raise ReleaseProvenanceError(
                    "corresponding-source ZIP member set or order differs"
                )
            maximum_declared_size = (
                sum(source_sizes.values()) + len(checked_policy_bytes) + 4 * 1024 * 1024
            )
            if (
                archive_path.stat().st_size > sum(source_sizes.values()) + 32 * 1024 * 1024
                or sum(info.file_size for info in infos) > maximum_declared_size
            ):
                raise ReleaseProvenanceError(
                    "corresponding-source ZIP declares excessive content"
                )
            members: dict[str, bytes] = {}
            for info in infos:
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.is_dir()
                    or info.date_time != expected_zip_datetime
                    or info.extra != b""
                    or info.comment != b""
                    or info.create_system != 3
                    or info.create_version != 20
                    or info.extract_version != 20
                    or info.reserved != 0
                    or info.flag_bits != 0
                    or info.volume != 0
                    or info.internal_attr != 0
                    or info.external_attr != (stat.S_IFREG | 0o644) << 16
                    or not stat.S_ISREG(unix_mode)
                    or stat.S_IMODE(unix_mode) != 0o644
                    or info.compress_type != zipfile.ZIP_STORED
                    or info.compress_size != info.file_size
                ):
                    raise ReleaseProvenanceError(
                        f"corresponding-source ZIP member metadata is invalid: {info.filename}"
                    )
                if info.filename in source_sizes:
                    if info.file_size != source_sizes[info.filename]:
                        raise ReleaseProvenanceError(
                            f"corresponding-source ZIP member size differs: {info.filename}"
                        )
                elif info.filename == CORRESPONDING_SOURCE_POLICY:
                    if info.file_size != len(checked_policy_bytes):
                        raise ReleaseProvenanceError(
                            "embedded corresponding-source policy size differs"
                        )
                elif info.file_size < 1 or info.file_size > 4 * 1024 * 1024:
                    raise ReleaseProvenanceError(
                        "embedded corresponding-source manifest size is invalid"
                    )
                members[info.filename] = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReleaseProvenanceError(
            "corresponding-source release artifact is not a readable ZIP"
        ) from exc

    if members[CORRESPONDING_SOURCE_POLICY] != checked_policy_bytes:
        raise ReleaseProvenanceError(
            "embedded corresponding-source policy differs from the release revision"
        )
    manifest_bytes = members[CORRESPONDING_SOURCE_MANIFEST]
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_digest:
        raise ReleaseProvenanceError(
            "embedded corresponding-source manifest digest differs"
        )
    source_manifest = _read_json_bytes(
        manifest_bytes,
        "embedded corresponding-source manifest",
    )
    expected_manifest_keys = {
        "schema_version",
        "format",
        "scope",
        "dependency_lock_sha256",
        "source_policy",
        "release_asset",
        "binary_relationships",
        "archives",
    }
    if set(source_manifest) != expected_manifest_keys or (
        source_manifest.get("schema_version") != 1
        or source_manifest.get("format") != CORRESPONDING_SOURCE_FORMAT
        or source_manifest.get("scope") != checked_policy.get("scope")
        or source_manifest.get("dependency_lock_sha256")
        != lock_canonical_sha256
        or source_manifest.get("archives") != policy_archives
    ):
        raise ReleaseProvenanceError(
            "embedded corresponding-source manifest identity differs"
        )
    if source_manifest.get("source_policy") != {
        "repository_path": CHECKED_IN_SOURCE_POLICY,
        "member": CORRESPONDING_SOURCE_POLICY,
        "sha256": expected_policy_digest,
    }:
        raise ReleaseProvenanceError(
            "embedded corresponding-source policy binding differs"
        )

    magisk_dependency = _mapping(
        lock_dependencies.get("magisk_release_apk"),
        "dependencies/lock.yml.dependencies.magisk_release_apk",
    )
    if source_manifest.get("release_asset") != {
        "dependency": "magisk_release_apk",
        "uri": magisk_dependency.get("url"),
        "sha256": magisk_dependency.get("sha256"),
        "repository": magisk_dependency.get("repository"),
        "commit": magisk_dependency.get("commit"),
        "version": magisk_dependency.get("version"),
    }:
        raise ReleaseProvenanceError(
            "embedded corresponding-source Magisk release binding differs"
        )

    executable_policy_path = _release_file(
        repository_root,
        CHECKED_IN_EXECUTABLE_POLICY,
        role="checked-out AnyKernel executable policy",
    )
    executable_policy = _read_json_bytes(
        executable_policy_path.read_bytes(),
        "checked-out AnyKernel executable policy",
    )
    executable_records = executable_policy.get("executables")
    if not isinstance(executable_records, list):
        raise ReleaseProvenanceError(
            "checked-out AnyKernel executable policy lacks executable records"
        )
    executable_digests = {
        _string(record.get("path"), "AnyKernel executable path"): _sha256(
            record.get("sha256"),
            "AnyKernel executable sha256",
        )
        for record in (
            _mapping(item, "AnyKernel executable record")
            for item in executable_records
        )
    }
    expected_relationships = [
        {
            "path": "tools/busybox",
            "sha256": executable_digests.get("tools/busybox"),
            "source_dependencies": ["magisk_busybox_source"],
        },
        {
            "path": "tools/magiskboot",
            "sha256": executable_digests.get("tools/magiskboot"),
            "source_dependencies": [
                "magisk_source",
                *sorted(ANYKERNEL_SOURCE_DEPENDENCY_IDS[2:]),
            ],
        },
    ]
    if (
        len(executable_records) != 2
        or set(executable_digests) != {"tools/busybox", "tools/magiskboot"}
        or source_manifest.get("binary_relationships") != expected_relationships
    ):
        raise ReleaseProvenanceError(
            "embedded corresponding-source binary relationships differ"
        )

    for record in policy_archives:
        member = str(record["archive_path"])
        payload = members[member]
        if (
            len(payload) != record.get("size")
            or hashlib.sha256(payload).hexdigest() != record.get("sha256")
        ):
            raise ReleaseProvenanceError(
                f"corresponding-source archive bytes differ: {record.get('dependency')}"
            )

    try:
        with tempfile.TemporaryDirectory(
            prefix="oneplus13-release-source-validation-"
        ) as temporary_name:
            validation_root = Path(temporary_name)
            magisk_source_path: Path | None = None
            for index, record in enumerate(policy_archives):
                dependency_id = str(record["dependency"])
                archive_path = validation_root / f"{index:03d}.source"
                _atomic_write(archive_path, members[str(record["archive_path"])])
                source_root = _verify_corresponding_source_tarball(
                    archive_path,
                    record,
                )
                _verify_cargo_source_archive(archive_path, source_root, record)
                if dependency_id == "magisk_source":
                    magisk_source_path = archive_path
            if magisk_source_path is None:
                raise ReleaseProvenanceError(
                    "corresponding-source companion lacks the Magisk root source"
                )
            _verify_magisk_source_closure(magisk_source_path, checked_policy)
    except BuildToolError as exc:
        raise ReleaseProvenanceError(
            f"corresponding-source deep source validation failed: {exc}"
        ) from exc
    except OSError as exc:
        raise ReleaseProvenanceError(
            f"corresponding-source temporary validation failed: {exc}"
        ) from exc


def _validate_packaged_release_assets(
    *,
    root: Path,
    repository_root: Path,
    manifest: Mapping[str, Any],
    lock_document: Mapping[str, Any],
    lock_canonical_sha256: str,
) -> None:
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ReleaseProvenanceError(
            "BUILD-MANIFEST.json artifacts must be a non-empty array"
        )
    records: dict[str, Mapping[str, Any]] = {}
    roles: dict[str, Mapping[str, Any]] = {}
    for index, raw_record in enumerate(raw_artifacts):
        record = _mapping(raw_record, f"artifacts[{index}]")
        name = _artifact_asset_name(record, index)
        role = _string(record.get("role"), f"artifacts[{index}].role")
        if (
            name in records
            or role in roles
            or name
            in {
                BUILD_MANIFEST_NAME,
                PACKAGE_CHECKSUM_NAME,
                PROVENANCE_NAME,
                RELEASE_CHECKSUM_NAME,
            }
        ):
            raise ReleaseProvenanceError(
                f"duplicate or reserved packaged artifact identity: {name}/{role}"
            )
        size = record.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ReleaseProvenanceError(
                f"packaged artifact size is invalid: {name}"
            )
        digest = _sha256(record.get("sha256"), f"artifacts[{index}].sha256")
        path = _release_file(root, name, role=f"packaged artifact {role}")
        if path.stat().st_size != size or _sha256_file(path) != digest:
            raise ReleaseProvenanceError(
                f"packaged artifact differs from BUILD-MANIFEST.json: {name}"
            )
        records[name] = record
        roles[role] = record

    actual: set[str] = set()
    for child in root.iterdir():
        if child.is_symlink() or not child.is_file():
            raise ReleaseProvenanceError(
                f"release-assets must be a flat plain-file directory: {child.name}"
            )
        actual.add(child.name)
    allowed_generated = {
        name for name in (PROVENANCE_NAME, RELEASE_CHECKSUM_NAME) if name in actual
    }
    expected = {
        *records,
        BUILD_MANIFEST_NAME,
        PACKAGE_CHECKSUM_NAME,
        *allowed_generated,
    }
    if actual != expected:
        raise ReleaseProvenanceError(
            "release asset coverage differs from BUILD-MANIFEST.json; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )

    checksum_path = _release_file(
        root,
        PACKAGE_CHECKSUM_NAME,
        role="packaged SHA256SUMS",
    )
    checksummed_names = sorted(
        [*records, BUILD_MANIFEST_NAME],
        key=lambda name: name.encode("utf-8", "strict"),
    )
    expected_checksums = "".join(
        f"{_sha256_file(root / name)}  {name}\n" for name in checksummed_names
    ).encode("ascii")
    if checksum_path.read_bytes() != expected_checksums:
        raise ReleaseProvenanceError(
            "packaged SHA256SUMS is not the exact canonical asset inventory"
        )

    for role in (
        "kernel-image",
        "anykernel3-zip",
        "corresponding-source",
        "resolved-manifest",
    ):
        if role not in roles:
            raise ReleaseProvenanceError(f"required packaged artifact is absent: {role}")
    target = _string(manifest.get("build_target"), "build_target")
    if (target in {"modules", "mixed"}) != ("module-zip" in roles):
        raise ReleaseProvenanceError(
            "module package presence differs from the selected build target"
        )
    if _bool(manifest.get("debug"), "debug") != ("debug-zip" in roles):
        raise ReleaseProvenanceError(
            "debug package presence differs from the selected release state"
        )
    features = manifest.get("features")
    if not isinstance(features, list) or len(features) != 1:
        raise ReleaseProvenanceError(
            "release manifest must contain one sealed feature selection"
        )
    flags = _mapping(features[0], "features[0]").get("flags")
    firmware_required = _bool(
        _mapping(flags, "features[0].flags").get("artifact.wireless_firmware"),
        "features[0].flags.artifact.wireless_firmware",
    )
    if firmware_required != ("wireless-firmware" in roles):
        raise ReleaseProvenanceError(
            "wireless firmware presence differs from the sealed feature selection"
        )

    anykernel_record = roles["anykernel3-zip"]
    anykernel_name = PurePosixPath(str(anykernel_record["path"])).name
    if not anykernel_name.endswith("-AnyKernel3.zip"):
        raise ReleaseProvenanceError(
            "AnyKernel release filename differs from the contract"
        )
    _validate_anykernel_release_zip(
        archive_path=root / anykernel_name,
        artifact_record=anykernel_record,
        image_record=roles["kernel-image"],
        repository_root=repository_root,
        lock_document=lock_document,
    )

    companion_record = roles["corresponding-source"]
    companion_name = PurePosixPath(str(companion_record["path"])).name
    if not companion_name.endswith("-corresponding-source.zip"):
        raise ReleaseProvenanceError(
            "corresponding-source release filename differs from the contract"
        )
    _validate_corresponding_source_companion(
        archive_path=root / companion_name,
        artifact_record=companion_record,
        repository_root=repository_root,
        lock_document=lock_document,
        lock_canonical_sha256=lock_canonical_sha256,
        source_date_epoch=_mapping(manifest.get("kernel"), "kernel").get(
            "source_date_epoch"
        ),
    )


def generate_release_provenance(
    *,
    assets_dir: Path,
    repository_root: Path,
    repository: str,
    revision: str,
    run_id: str,
    run_attempt: str,
    external_parameters: Mapping[str, Any],
) -> tuple[Path, Path]:
    """Validate packaged lineage and write the release statement/checksums."""

    repository = _string(repository, "repository")
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise ReleaseProvenanceError("repository must be an owner/name pair")
    revision = _full_commit(revision, "revision")
    if not run_id.isdigit() or not run_attempt.isdigit():
        raise ReleaseProvenanceError("run id and attempt must be numeric")
    root = assets_dir.resolve()
    if not root.is_dir():
        raise ReleaseProvenanceError(f"release asset directory does not exist: {root}")
    checked_out_repository = repository_root.resolve()
    if not checked_out_repository.is_dir():
        raise ReleaseProvenanceError(
            f"checked-out repository directory does not exist: {checked_out_repository}"
        )
    build_manifest_path = _release_file(root, BUILD_MANIFEST_NAME, role="build manifest")
    try:
        manifest = strict_json_loads(build_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseProvenanceError(f"cannot read BUILD-MANIFEST.json: {exc}") from exc
    manifest = _mapping(manifest, "BUILD-MANIFEST.json")
    _validate_build_identity(
        manifest,
        repository=repository,
        revision=revision,
        external_parameters=external_parameters,
    )

    source = _mapping(manifest.get("source"), "source")
    base = _string(manifest.get("base"), "base")
    locked_path = _portable_path(source.get("locked_path"), "source.locked_path")
    expected_locked_path = f"manifests/lockfiles/{base}.xml"
    if locked_path != expected_locked_path:
        raise ReleaseProvenanceError(
            f"source.locked_path must be {expected_locked_path} for profile {base}"
        )
    resolved_name = _portable_path(
        source.get("resolved_path"),
        "source.resolved_path",
        one_component=True,
    )
    resolved_manifest = _release_file(root, resolved_name, role="resolved OnePlus manifest")
    resolved_sha256 = _sha256(source.get("sha256"), "source.sha256")
    if _sha256_file(resolved_manifest) != resolved_sha256:
        raise ReleaseProvenanceError("resolved OnePlus manifest digest differs from build lineage")
    try:
        wireless_led_kmi_required = wireless_led_exports_required(
            manifest.get("features"),
            feature_profile=_string(
                manifest.get("feature_profile"),
                "feature_profile",
            ),
        )
        validate_packaged_build_evidence(
            assets_dir=root,
            evidence_value=manifest.get("build_evidence"),
            base=base,
            resolved_manifest_sha256=resolved_sha256,
            wireless_led_exports_required=wireless_led_kmi_required,
        )
    except BuildToolError as exc:
        raise ReleaseProvenanceError(f"packaged build evidence is invalid: {exc}") from exc
    locked_sha256 = _sha256(source.get("locked_sha256"), "source.locked_sha256")
    checked_out_manifest = _release_file(
        checked_out_repository,
        locked_path,
        role="checked-out locked OnePlus manifest",
    )
    if _sha256_file(checked_out_manifest) != locked_sha256:
        raise ReleaseProvenanceError(
            "checked-out OnePlus manifest digest differs from packaged build lineage"
        )
    manifest_repository = _https_url(source.get("url"), "source.url")
    manifest_revision = _full_commit(source.get("revision"), "source.revision")

    lock_record = _mapping(manifest.get("dependency_lock"), "dependency_lock")
    lock_path = _portable_path(lock_record.get("path"), "dependency_lock.path")
    if lock_path != "dependencies/lock.yml":
        raise ReleaseProvenanceError("dependency lock path must be dependencies/lock.yml")
    lock_sha256 = _sha256(lock_record.get("sha256"), "dependency_lock.sha256")
    lock_canonical_sha256 = _sha256(
        lock_record.get("canonical_sha256"),
        "dependency_lock.canonical_sha256",
    )
    checked_out_lock = _release_file(
        checked_out_repository,
        lock_path,
        role="checked-out dependency lock",
    )
    if _sha256_file(checked_out_lock) != lock_sha256:
        raise ReleaseProvenanceError(
            "checked-out dependency lock digest differs from packaged build lineage"
        )
    try:
        lock_document = strict_json_loads(checked_out_lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseProvenanceError(f"cannot read checked-out dependency lock: {exc}") from exc
    if _canonical_json_sha256(lock_document) != lock_canonical_sha256:
        raise ReleaseProvenanceError(
            "checked-out dependency lock canonical digest differs from build lineage"
        )
    expected_inventory = _inventory_from_lock(lock_document)
    if manifest.get("dependencies") != expected_inventory:
        raise ReleaseProvenanceError(
            "packaged dependency inventory differs from the checked-out dependency lock"
        )
    dependency_descriptors, dependency_records = _dependency_descriptors(
        manifest.get("dependencies")
    )
    _validate_packaged_release_assets(
        root=root,
        repository_root=checked_out_repository,
        manifest=manifest,
        lock_document=lock_document,
        lock_canonical_sha256=lock_canonical_sha256,
    )
    oneplus_dependency = _mapping(
        dependency_records.get("oneplus_manifest"),
        "dependencies.oneplus_manifest",
    )
    oneplus_source = _mapping(
        oneplus_dependency.get("source"),
        "dependencies.oneplus_manifest.source",
    )
    if (
        oneplus_source.get("uri") != manifest_repository
        or oneplus_source.get("commit") != manifest_revision
    ):
        raise ReleaseProvenanceError(
            "OnePlus manifest repository identity differs from the dependency lock"
        )

    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    repository_url = f"https://github.com/{repository}"
    resolved_dependencies = [
        _git_descriptor(
            name="orchestrator",
            uri=f"{repository_url}.git",
            commit=revision,
        ),
        {
            "name": "dependency-lock",
            "uri": f"{repository_url}/blob/{revision}/{lock_path}",
            "digest": {"sha256": lock_sha256},
            "annotations": {
                "oneplus13_canonicalSha256": lock_canonical_sha256,
            },
        },
        {
            "name": f"oneplus-manifest-lock:{base}",
            "uri": f"{repository_url}/blob/{revision}/{locked_path}",
            "digest": {"sha256": locked_sha256},
        },
        {
            "name": f"oneplus-manifest-resolved:{base}",
            "uri": f"{run_url}#resolved-manifest-{base}",
            "digest": {"sha256": resolved_sha256},
        },
        *dependency_descriptors,
        *_manifest_project_descriptors(resolved_manifest),
    ]
    resolved_dependencies.sort(key=_descriptor_sort_key)
    names = [str(record["name"]) for record in resolved_dependencies]
    if len(names) != len(set(names)):
        raise ReleaseProvenanceError("resolved dependency names are not unique")

    provenance_path = root / PROVENANCE_NAME
    checksum_path = root / RELEASE_CHECKSUM_NAME
    subjects = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ReleaseProvenanceError(f"release assets contain a symbolic link: {path}")
        if path.is_file() and path not in {provenance_path, checksum_path}:
            subjects.append(
                {
                    "name": path.relative_to(root).as_posix(),
                    "digest": {"sha256": _sha256_file(path)},
                }
            )

    build_manifest_sha256 = _sha256_file(build_manifest_path)
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": f"{repository_url}/blob/{revision}/.github/workflows/release.yml",
                "externalParameters": dict(external_parameters),
                "internalParameters": {
                    "runId": run_id,
                    "runAttempt": run_attempt,
                    "buildManifestSha256": build_manifest_sha256,
                    "dependencyLockCanonicalSha256": lock_canonical_sha256,
                    "lockedDependencyCount": len(dependency_records),
                    "onePlusProjectCount": sum(
                        1
                        for record in resolved_dependencies
                        if str(record["name"]).startswith("oneplus-project:")
                    ),
                },
                "resolvedDependencies": resolved_dependencies,
            },
            "runDetails": {
                "builder": {"id": run_url},
                "metadata": {"invocationId": f"{run_id}/{run_attempt}"},
            },
        },
    }
    _atomic_write(
        provenance_path,
        (json.dumps(statement, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
    )
    checksum_files = [
        path
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.is_file() and path != checksum_path
    ]
    checksum_payload = "".join(
        f"{_sha256_file(path)}  {path.relative_to(root).as_posix()}\n"
        for path in checksum_files
    ).encode("utf-8")
    _atomic_write(checksum_path, checksum_payload)
    return provenance_path, checksum_path


def parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ReleaseProvenanceError(f"expected true or false, got {value!r}")
