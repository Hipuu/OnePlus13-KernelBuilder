"""Generate deterministic release checksums and SLSA v1 provenance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit


BUILD_MANIFEST_NAME = "BUILD-MANIFEST.json"
PROVENANCE_NAME = "provenance.intoto.jsonl"
RELEASE_CHECKSUM_NAME = "RELEASE_SHA256SUMS"
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEPENDENCY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


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
    if value.isdigit():
        return int(value)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReleaseProvenanceError("buildTimestamp must be RFC3339 or an epoch integer") from exc
    if parsed.tzinfo is None:
        raise ReleaseProvenanceError("buildTimestamp must include a timezone")
    return int(parsed.timestamp())


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
    requested_epoch = _requested_epoch(str(external_parameters.get("buildTimestamp", "")))
    if requested_epoch is not None and kernel.get("source_date_epoch") != requested_epoch:
        raise ReleaseProvenanceError(
            "release buildTimestamp differs from the packaged kernel source epoch"
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
        manifest = json.loads(build_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
        lock_document = json.loads(checked_out_lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
