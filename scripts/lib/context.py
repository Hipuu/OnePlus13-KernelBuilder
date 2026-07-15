"""Tamper-evident build lineage shared by every pipeline phase."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import (
    ARCH,
    KMI,
    SCHEMA_VERSION,
    TARGET,
    DependencyLock,
    FeatureProfile,
    Profile,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from .errors import BuildToolError


CONTEXT_KIND = "oneplus13-build-context"
STAGE_ORDER = {
    "sources-synced": 1,
    "patches-applied": 2,
    "configured": 3,
    "kernel-built": 4,
    "modules-built": 5,
    "packaged": 6,
    "verified": 7,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def context_digest(document: Mapping[str, Any]) -> str:
    unsigned = dict(document)
    unsigned.pop("context_sha256", None)
    return sha256_bytes(canonical_json_bytes(unsigned))


def _seal(document: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(document))
    result["context_sha256"] = context_digest(result)
    return result


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(document)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_context(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    sealed = _seal(document)
    atomic_write_json(path, sealed)
    return sealed


def load_context(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildToolError(f"cannot read build context {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BuildToolError(f"invalid build context {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BuildToolError(f"{path}: build context must be an object")
    if raw.get("kind") != CONTEXT_KIND or raw.get("schema_version") != SCHEMA_VERSION:
        raise BuildToolError(f"{path}: unsupported build context")
    expected = context_digest(raw)
    if raw.get("context_sha256") != expected:
        raise BuildToolError(f"{path}: build context digest mismatch")
    if raw.get("target") != TARGET or raw.get("arch") != ARCH or raw.get("kmi") != KMI:
        raise BuildToolError(f"{path}: build context is not for sun/android15-6.6 arm64")
    if raw.get("stage") not in STAGE_ORDER:
        raise BuildToolError(f"{path}: unknown build stage")
    return raw


def new_context(
    profile: Profile,
    lock: DependencyLock,
    resolved_manifest: Path,
    *,
    smoke: bool,
) -> dict[str, Any]:
    manifest_sha = sha256_file(resolved_manifest)
    return _seal(
        {
            "kind": CONTEXT_KIND,
            "schema_version": SCHEMA_VERSION,
            "stage": "sources-synced",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "smoke": bool(smoke),
            "device": profile.device,
            "profile": profile.id,
            "target": profile.target,
            "arch": profile.arch,
            "kmi": profile.kmi,
            "manifest": {
                "url": profile.manifest_url,
                "branch": profile.manifest_branch,
                "file": profile.manifest_file,
                "revision": profile.manifest_revision,
                "locked_path": str(profile.locked_manifest),
                "locked_sha256": sha256_file(profile.locked_manifest),
                "resolved_path": str(resolved_manifest.resolve()),
                "sha256": manifest_sha,
            },
            "dependency_lock": {
                "path": str(lock.source_path),
                "sha256": lock.digest,
            },
            "features": [],
            "patches": [],
            "configuration": None,
            "kernel": None,
            "modules": None,
            "packages": [],
            "history": [{"stage": "sources-synced", "at": utc_now()}],
        }
    )


def validate_lineage(
    context: Mapping[str, Any],
    profile: Profile,
    lock: DependencyLock,
    *,
    minimum_stage: str = "sources-synced",
) -> None:
    if STAGE_ORDER[context["stage"]] < STAGE_ORDER[minimum_stage]:
        raise BuildToolError(
            f"build context stage {context['stage']} is before required stage {minimum_stage}"
        )
    if context.get("profile") != profile.id:
        raise BuildToolError(
            f"cross-profile mixing rejected: context={context.get('profile')}, requested={profile.id}"
        )
    if (
        context.get("target"),
        context.get("arch"),
        context.get("kmi"),
    ) != (profile.target, profile.arch, profile.kmi):
        raise BuildToolError("platform lineage mismatch")
    manifest = context.get("manifest")
    if not isinstance(manifest, dict):
        raise BuildToolError("build context manifest is missing")
    expected_manifest = (
        profile.manifest_url,
        profile.manifest_file,
        profile.manifest_revision,
    )
    actual_manifest = (
        manifest.get("url"),
        manifest.get("file"),
        manifest.get("revision"),
    )
    if actual_manifest != expected_manifest:
        raise BuildToolError("source manifest lineage does not match the selected profile")
    if (
        manifest.get("locked_path") != str(profile.locked_manifest)
        or manifest.get("locked_sha256") != sha256_file(profile.locked_manifest)
    ):
        raise BuildToolError("locked manifest changed after source synchronization")
    locked = context.get("dependency_lock")
    if not isinstance(locked, dict) or locked.get("sha256") != lock.digest:
        raise BuildToolError("dependency lock changed after source synchronization")
    manifest_path = Path(str(manifest.get("resolved_path", "")))
    if not manifest_path.is_file() or sha256_file(manifest_path) != manifest.get("sha256"):
        raise BuildToolError("resolved manifest is missing or changed")


def advance_context(
    context: Mapping[str, Any],
    stage: str,
    updates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if stage not in STAGE_ORDER:
        raise BuildToolError(f"unknown stage {stage}")
    previous = str(context.get("stage"))
    if previous not in STAGE_ORDER:
        raise BuildToolError(f"unknown previous stage {previous}")
    if STAGE_ORDER[stage] < STAGE_ORDER[previous]:
        raise BuildToolError(f"stage regression rejected: {previous} -> {stage}")
    result = deepcopy(dict(context))
    result.pop("context_sha256", None)
    if updates:
        result.update(deepcopy(dict(updates)))
    result["stage"] = stage
    result["updated_at"] = utc_now()
    history = list(result.get("history", []))
    if not history or history[-1].get("stage") != stage:
        history.append({"stage": stage, "at": utc_now()})
    result["history"] = history
    return _seal(result)


def record_for_file(path: Path, *, role: str, root: Path | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    display = str(resolved)
    if root is not None:
        try:
            display = resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return {
        "role": role,
        "path": display,
        "size": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def validate_record(record: Mapping[str, Any], base: Path | None = None) -> Path:
    value = record.get("path")
    if not isinstance(value, str) or not value:
        raise BuildToolError("artifact record has no path")
    path = Path(value)
    if not path.is_absolute():
        if base is None:
            raise BuildToolError(f"relative artifact path has no base: {value}")
        path = base / path
    if not path.is_file():
        raise BuildToolError(f"recorded artifact is missing: {path}")
    if path.stat().st_size != record.get("size"):
        raise BuildToolError(f"recorded artifact size changed: {path}")
    if sha256_file(path) != record.get("sha256"):
        raise BuildToolError(f"recorded artifact digest changed: {path}")
    return path


def assert_symvers_lineage(context: Mapping[str, Any], symvers: Path) -> None:
    kernel = context.get("kernel")
    if not isinstance(kernel, dict):
        raise BuildToolError("kernel build record is absent")
    record = kernel.get("module_symvers")
    if not isinstance(record, dict):
        raise BuildToolError("kernel build did not record Module.symvers")
    if sha256_file(symvers) != record.get("sha256"):
        raise BuildToolError("Module.symvers does not belong to this kernel build")


def feature_selection(feature: FeatureProfile, root_variant: str) -> dict[str, Any]:
    if root_variant != "none" and root_variant not in feature.root_variants:
        raise BuildToolError(f"root variant {root_variant!r} is not supported by {feature.id}")
    return {
        "profile": feature.id,
        "root_variant": root_variant,
        "flags": dict(sorted(feature.flags.items())),
    }
