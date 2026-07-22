"""Static, machine-readable evidence for advertised feature flags."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .build import parse_fragment
from .config import (
    FeatureProfile,
    Profile,
    load_json_yaml,
    resolve_inside,
)
from .errors import BuildToolError
from .patches import _load_series, _operation_enabled, _series_paths


EVIDENCE_SCHEMA_VERSION = 1
SELECTION_SEMANTICS = "profile-base-capability"
EVIDENCE_KINDS = frozenset(
    {
        "external-module",
        "kconfig-request",
        "patch-operation",
        "source-reference",
    }
)
KCONFIG_RE = re.compile(r"^CONFIG_[A-Za-z0-9_]+$")


def _mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BuildToolError(f"{where}: expected an object")
    return value


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildToolError(f"{where}: expected a non-empty string")
    return value


def _profile_kconfig_requests(root: Path, feature: FeatureProfile) -> dict[str, str]:
    requested: dict[str, str] = {}
    for fragment in feature.kconfig_fragments:
        path = resolve_inside(
            root,
            fragment.path,
            f"feature evidence fragment for {feature.id}",
            must_exist=fragment.required,
        )
        if path.exists():
            requested.update(parse_fragment(path))
    requested.update(feature.required_symbols)
    return requested


def _known_patch_operations(root: Path) -> set[str]:
    series_root = root / "patches" / "series"
    if not series_root.is_dir():
        raise BuildToolError("feature evidence: patches/series is missing")
    operations: set[str] = set()
    for path in sorted(series_root.glob("*.yml")):
        series_id, entries = _load_series(path)
        for entry in entries:
            qualified = f"{series_id}:{entry['id']}"
            if qualified in operations:
                raise BuildToolError(
                    f"feature evidence: duplicate logical patch operation {qualified}"
                )
            operations.add(qualified)
    return operations


def _selected_patch_operations(
    root: Path,
    *,
    profile: Profile,
    feature: FeatureProfile,
) -> set[str]:
    """Return capability evidence across every root variant supported by a profile."""

    selected: set[str] = set()
    for root_variant in feature.root_variants:
        for path in _series_paths(root, feature, root_variant):
            series_id, operations = _load_series(path)
            for operation in operations:
                if _operation_enabled(
                    operation,
                    feature,
                    profile.id,
                    root_variant,
                ):
                    selected.add(f"{series_id}:{operation['id']}")
    return selected


def _load_contract(
    root: Path,
    evidence_path: Path | None,
) -> tuple[Path, Mapping[str, object]]:
    path = (
        evidence_path.resolve()
        if evidence_path is not None
        else (root / "configs" / "feature-evidence.yml").resolve()
    )
    raw = _mapping(load_json_yaml(path), str(path))
    allowed_top_level = {
        "schema_version",
        "selection_semantics",
        "feature_flags",
    }
    unexpected = sorted(set(raw) - allowed_top_level)
    if unexpected:
        raise BuildToolError(
            f"{path}: unknown top-level fields: {', '.join(unexpected)}"
        )
    if raw.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise BuildToolError(
            f"{path}: schema_version must be {EVIDENCE_SCHEMA_VERSION}"
        )
    if raw.get("selection_semantics") != SELECTION_SEMANTICS:
        raise BuildToolError(
            f"{path}: selection_semantics must be {SELECTION_SEMANTICS!r}"
        )
    return path, _mapping(raw.get("feature_flags"), f"{path}:feature_flags")


def _validate_evidence_item(
    root: Path,
    item: object,
    *,
    where: str,
    known_operations: set[str],
    known_kconfig_requests: set[tuple[str, str]],
    known_external_modules: set[str],
) -> dict[str, str]:
    evidence = _mapping(item, where)
    kind = _string(evidence.get("kind"), f"{where}.kind")
    if kind not in EVIDENCE_KINDS:
        raise BuildToolError(f"{where}: unknown evidence kind {kind!r}")

    if kind == "external-module":
        allowed = {"kind", "dependency"}
        unexpected = sorted(set(evidence) - allowed)
        if unexpected:
            raise BuildToolError(
                f"{where}: unknown external-module fields: {', '.join(unexpected)}"
            )
        dependency = _string(evidence.get("dependency"), f"{where}.dependency")
        if dependency not in known_external_modules:
            raise BuildToolError(
                f"{where}: unknown external module reference {dependency!r}"
            )
        return {"kind": kind, "dependency": dependency}

    if kind == "patch-operation":
        allowed = {"kind", "operation"}
        unexpected = sorted(set(evidence) - allowed)
        if unexpected:
            raise BuildToolError(
                f"{where}: unknown patch-operation fields: {', '.join(unexpected)}"
            )
        operation = _string(evidence.get("operation"), f"{where}.operation")
        if operation not in known_operations:
            raise BuildToolError(
                f"{where}: unknown patch operation reference {operation!r}"
            )
        return {"kind": kind, "operation": operation}

    if kind == "kconfig-request":
        allowed = {"kind", "symbol", "value"}
        unexpected = sorted(set(evidence) - allowed)
        if unexpected:
            raise BuildToolError(
                f"{where}: unknown kconfig-request fields: {', '.join(unexpected)}"
            )
        symbol = _string(evidence.get("symbol"), f"{where}.symbol")
        value = _string(evidence.get("value"), f"{where}.value")
        if not KCONFIG_RE.fullmatch(symbol):
            raise BuildToolError(f"{where}: invalid Kconfig symbol {symbol!r}")
        if (symbol, value) not in known_kconfig_requests:
            raise BuildToolError(
                f"{where}: unknown Kconfig request reference {symbol}={value}"
            )
        return {"kind": kind, "symbol": symbol, "value": value}

    allowed = {"kind", "path", "contains"}
    unexpected = sorted(set(evidence) - allowed)
    if unexpected:
        raise BuildToolError(
            f"{where}: unknown source-reference fields: {', '.join(unexpected)}"
        )
    relative = _string(evidence.get("path"), f"{where}.path")
    needle = _string(evidence.get("contains"), f"{where}.contains")
    path = resolve_inside(root, relative, f"{where}.path")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildToolError(f"{where}: cannot read source reference {path}: {exc}") from exc
    if needle not in source:
        raise BuildToolError(
            f"{where}: source reference {relative!r} does not contain {needle!r}"
        )
    return {"kind": kind, "path": relative, "contains": needle}


def _evidence_selected(
    evidence: Mapping[str, str],
    *,
    selected_operations: set[str],
    requested_symbols: Mapping[str, str],
    external_modules: tuple[str, ...],
) -> bool:
    kind = evidence["kind"]
    if kind == "external-module":
        return evidence["dependency"] in external_modules
    if kind == "patch-operation":
        return evidence["operation"] in selected_operations
    if kind == "kconfig-request":
        return requested_symbols.get(evidence["symbol"]) == evidence["value"]
    return True


def validate_feature_evidence(
    root: Path,
    profiles: Mapping[str, Profile],
    features: Mapping[str, FeatureProfile],
    *,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    """Validate evidence coverage and selection for every shipped feature flag."""

    root = root.resolve()
    path, raw_contract = _load_contract(root, evidence_path)
    if not features:
        raise BuildToolError("feature evidence: no feature profiles were loaded")

    catalog: set[str] | None = None
    for feature in features.values():
        keys = set(feature.flags)
        if catalog is None:
            catalog = keys
        elif keys != catalog:
            raise BuildToolError(
                "feature evidence: feature profiles do not share one flag catalog"
            )
    assert catalog is not None

    contract_keys = set(raw_contract)
    if contract_keys != catalog:
        missing = sorted(catalog - contract_keys)
        unknown = sorted(contract_keys - catalog)
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unknown:
            details.append("unknown: " + ", ".join(unknown))
        raise BuildToolError(
            f"{path}: feature evidence catalog mismatch ({'; '.join(details)})"
        )

    known_operations = _known_patch_operations(root)
    kconfig_by_feature = {
        feature.id: _profile_kconfig_requests(root, feature)
        for feature in features.values()
    }
    known_kconfig_requests = {
        item
        for requests in kconfig_by_feature.values()
        for item in requests.items()
    }
    known_external_modules = {
        dependency
        for feature in features.values()
        for dependency in feature.external_modules
    }

    contract: dict[str, tuple[dict[str, str], ...]] = {}
    reference_count = 0
    for flag in sorted(catalog):
        entries = raw_contract[flag]
        if not isinstance(entries, list):
            raise BuildToolError(f"{path}:feature_flags.{flag}: expected an array")
        normalized: list[dict[str, str]] = []
        fingerprints: set[str] = set()
        for index, item in enumerate(entries):
            evidence = _validate_evidence_item(
                root,
                item,
                where=f"{path}:feature_flags.{flag}[{index}]",
                known_operations=known_operations,
                known_kconfig_requests=known_kconfig_requests,
                known_external_modules=known_external_modules,
            )
            fingerprint = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
            if fingerprint in fingerprints:
                raise BuildToolError(
                    f"{path}:feature_flags.{flag}: duplicate evidence reference"
                )
            fingerprints.add(fingerprint)
            normalized.append(evidence)
        contract[flag] = tuple(normalized)
        reference_count += len(normalized)

    combinations = 0
    enabled_checks = 0
    for feature in features.values():
        requested_symbols = kconfig_by_feature[feature.id]
        for profile in profiles.values():
            combinations += 1
            selected_operations = _selected_patch_operations(
                root,
                profile=profile,
                feature=feature,
            )
            for flag, enabled in feature.flags.items():
                if not enabled:
                    continue
                enabled_checks += 1
                if not any(
                    _evidence_selected(
                        evidence,
                        selected_operations=selected_operations,
                        requested_symbols=requested_symbols,
                        external_modules=feature.external_modules,
                    )
                    for evidence in contract[flag]
                ):
                    raise BuildToolError(
                        f"{path}: {feature.id}/{profile.id} enables {flag} "
                        "but selects no declared evidence"
                    )

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "selection_semantics": SELECTION_SEMANTICS,
        "catalog_size": len(catalog),
        "evidence_references": reference_count,
        "profile_base_combinations": combinations,
        "enabled_checks": enabled_checks,
    }
