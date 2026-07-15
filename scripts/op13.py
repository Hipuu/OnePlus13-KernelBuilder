#!/usr/bin/env python3
"""OnePlus 13 deterministic kernel build command line interface."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lib.artifacts import package_build, verify_build_output
from lib.build import BUILD_TARGETS, build_external_modules, build_kernel, configure_kernel
from lib.config import (
    SECRET_PATTERNS,
    discover_configs,
    load_dependency_lock,
    resolve_root_selection,
    select_feature,
    select_profile,
    validate_repository,
)
from lib.context import load_context
from lib.errors import BuildToolError, SourceChanged
from lib.patches import apply_patch_series, validate_series_documents
from lib.runtime import fetch_dependencies, monitor_or_raise, sync_sources


def _default_root() -> Path:
    return SCRIPT_DIR.parent


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _add_repo_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=_path, default=_default_root(), help="repository root")


def _add_cache(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache", type=_path, help="dependency cache (default: REPO/.cache/op13)")


def _cache(args: argparse.Namespace, root: Path) -> Path:
    return (args.cache if args.cache is not None else root / ".cache" / "op13").resolve()


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _scan_pipeline_sources(root: Path) -> None:
    pipe_to_shell = re.compile(r"(?:curl|wget)[^\n|]*\|\s*(?:ba)?sh\b", re.IGNORECASE)
    scan_roots = [root / "scripts", root / ".github" / "workflows"]
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for path in scan_root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    raise BuildToolError(f"embedded credential detected in {path}")
            if pipe_to_shell.search(text):
                raise BuildToolError(f"network-to-shell pipeline detected in {path}")
            if path.suffix == ".py":
                try:
                    tree = ast.parse(text, filename=str(path))
                except SyntaxError as exc:
                    raise BuildToolError(f"invalid Python syntax in {path}: {exc}") from exc
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    if any(
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                        for keyword in node.keywords
                    ):
                        raise BuildToolError(f"subprocess shell execution is forbidden in {path}")


def command_validate(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    summary = validate_repository(root)
    device, lock, profiles, features = discover_configs(root)
    summary["patch_series"] = validate_series_documents(root, profiles, features, lock)
    _scan_pipeline_sources(root)
    summary["pipeline_source_scan"] = "passed"
    _print(summary)


def command_fetch(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    lock_path = args.lock.resolve() if args.lock else root / "dependencies" / "lock.yml"
    lock = load_dependency_lock(lock_path)
    state = fetch_dependencies(
        lock,
        _cache(args, root),
        selected=args.dependency or None,
        dry_run=args.dry_run,
        offline=args.offline,
    )
    _print(state)


def command_resolve_root(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    lock_path = args.lock.resolve() if args.lock else root / "dependencies" / "lock.yml"
    lock = load_dependency_lock(lock_path)
    _print(
        resolve_root_selection(
            lock,
            args.root,
            requested_kernelsu_commit=args.kernelsu_commit,
            requested_susfs_commit=args.susfs_commit,
        )
    )


def command_sync(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    _, lock, profiles, _ = discover_configs(root)
    profile = select_profile(profiles, args.base)
    output = args.output.resolve()
    if args.check_only:
        monitor_or_raise(profile, output)
        _print({"profile": profile.id, "changed": False, "report": str(output / "source-changes.md")})
        return
    source, context = sync_sources(
        profile,
        lock,
        output,
        _cache(args, root),
        jobs=args.jobs,
        dry_run=args.dry_run,
        smoke=args.smoke,
    )
    _print({"profile": profile.id, "source_dir": str(source), "context": str(context), "smoke": args.smoke})


def command_apply(args: argparse.Namespace) -> None:
    repo_root = args.repo_root.resolve()
    _, lock, profiles, features = discover_configs(repo_root)
    profile = select_profile(profiles, args.base)
    feature = select_feature(features, args.profile)
    source_dir = args.source_dir.resolve()
    context = args.context.resolve() if args.context else source_dir / ".op13" / "build-context.json"
    records = apply_patch_series(
        root=repo_root,
        source_dir=source_dir,
        cache_root=_cache(args, repo_root),
        context_path=context,
        profile=profile,
        feature=feature,
        lock=lock,
        root_variant=args.root,
        check_only=args.dry_run,
        smoke=args.smoke,
        log_dir=args.log.resolve(),
    )
    _print({"operations": len(records), "check_only": args.dry_run, "smoke": args.smoke})


def command_configure(args: argparse.Namespace) -> None:
    repo_root = args.repo_root.resolve()
    device, lock, profiles, features = discover_configs(repo_root)
    profile = select_profile(profiles, args.base)
    feature = select_feature(features, args.profile)
    source_dir = args.source_dir.resolve()
    context = args.context.resolve() if args.context else source_dir / ".op13" / "build-context.json"
    config = configure_kernel(
        root=repo_root,
        source_dir=source_dir,
        output_dir=args.output.resolve(),
        context_path=context,
        profile=profile,
        feature=feature,
        device=device,
        lock=lock,
        root_variant=args.root,
        optimization=args.optimization,
        lto=args.lto,
        build_target=args.build_target,
        smoke=args.smoke,
        check_only=args.dry_run,
    )
    _print({"config": str(config), "check_only": args.dry_run, "smoke": args.smoke})


def _selection_from_context(root: Path, context_path: Path):
    device, lock, profiles, features = discover_configs(root)
    context = load_context(context_path)
    profile = select_profile(profiles, str(context.get("profile")))
    configuration = context.get("configuration")
    if not isinstance(configuration, dict):
        raise BuildToolError("build context has no configuration selection")
    feature = select_feature(features, str(configuration.get("feature_profile")))
    return device, lock, profile, feature, context


def command_build_kernel(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    source_dir = args.source_dir.resolve()
    context_path = args.context.resolve() if args.context else source_dir / ".op13" / "build-context.json"
    device, lock, profile, _, _ = _selection_from_context(root, context_path)
    record = build_kernel(
        source_dir=source_dir,
        output_dir=args.output.resolve(),
        context_path=context_path,
        profile=profile,
        device=device,
        lock=lock,
        clean=args.clean,
        debug=args.debug,
        smoke=args.smoke,
        dry_run=args.dry_run,
        branding=args.branding,
        build_timestamp=args.timestamp,
    )
    _print(record)


def command_build_modules(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    source_dir = args.source_dir.resolve()
    context_path = args.context.resolve() if args.context else source_dir / ".op13" / "build-context.json"
    device, lock, profile, feature, _ = _selection_from_context(root, context_path)
    record = build_external_modules(
        source_dir=source_dir,
        kernel_output=args.kernel_output.resolve(),
        output_dir=args.output.resolve(),
        source_context_path=context_path,
        profile=profile,
        feature=feature,
        device=device,
        lock=lock,
        cache_root=_cache(args, root),
        clean=args.clean,
        debug=args.debug,
        smoke=args.smoke,
        dry_run=args.dry_run,
    )
    _print(record)


def command_verify(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    _, lock, profiles, features = discover_configs(root)
    report = verify_build_output(
        output_dir=args.output.resolve(),
        profile=select_profile(profiles, args.base),
        feature=select_feature(features, args.profile),
        lock=lock,
        root_variant=args.root,
        build_target=args.build_target,
        smoke=args.smoke,
    )
    _print(report)


def command_package(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    _, lock, profiles, features = discover_configs(root)
    records = package_build(
        root=root,
        input_dir=args.input.resolve(),
        output_dir=args.output.resolve(),
        cache_root=_cache(args, root),
        profile=select_profile(profiles, args.base),
        feature=select_feature(features, args.profile),
        lock=lock,
        root_variant=args.root,
        build_target=args.build_target,
        debug=args.debug,
        pre_release=args.pre_release,
        smoke=args.smoke,
    )
    _print({"artifacts": records})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="op13.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate all repository inputs and invariants")
    _add_repo_root(validate)
    validate.set_defaults(handler=command_validate)

    fetch = subparsers.add_parser("fetch-dependencies", help="fetch and verify pinned dependencies")
    _add_repo_root(fetch)
    _add_cache(fetch)
    fetch.add_argument("--lock", type=_path)
    fetch.add_argument("--dependency", action="append", help="lock ID (repeatable)")
    fetch.add_argument("--offline", action="store_true", help="verify cache without network")
    fetch.add_argument("--dry-run", action="store_true")
    fetch.set_defaults(handler=command_fetch)

    resolve_root = subparsers.add_parser(
        "resolve-root-lock",
        help="resolve optional KernelSU/SUSFS commit assertions against the audited lock",
    )
    _add_repo_root(resolve_root)
    resolve_root.add_argument("--lock", type=_path)
    resolve_root.add_argument(
        "--root",
        required=True,
        choices=("kernelsu", "kernelsu-next", "none"),
    )
    resolve_root.add_argument("--kernelsu-commit", default="")
    resolve_root.add_argument("--susfs-commit", default="")
    resolve_root.set_defaults(handler=command_resolve_root)

    sync = subparsers.add_parser("sync-sources", help="sync an exact official OnePlus source lock")
    _add_repo_root(sync)
    _add_cache(sync)
    sync.add_argument("--base", required=True, choices=("oos15-cn", "oos15-global", "oos16"))
    sync.add_argument("--output", type=_path, default=Path("out/source"))
    sync.add_argument("--jobs", type=int, default=4)
    sync.add_argument("--check-only", action="store_true", help="monitor upstream; exit 2 on drift")
    sync.add_argument("--dry-run", action="store_true", help="print source sync without executing")
    sync.add_argument("--smoke", action="store_true", help="create a non-releasable source fixture")
    sync.set_defaults(handler=command_sync)

    apply = subparsers.add_parser("apply-series", help="check/apply ordered integration series")
    _add_repo_root(apply)
    _add_cache(apply)
    apply.add_argument("--base", required=True)
    apply.add_argument("--profile", required=True)
    apply.add_argument("--root", required=True, choices=("kernelsu", "kernelsu-next", "none"))
    apply.add_argument("--source-dir", required=True, type=_path)
    apply.add_argument("--context", type=_path)
    apply.add_argument("--log", type=_path, default=Path("out/debug"))
    apply.add_argument("--dry-run", action="store_true", help="run applicability checks without edits")
    apply.add_argument("--smoke", action="store_true")
    apply.set_defaults(handler=command_apply)

    configure = subparsers.add_parser("configure", help="merge, normalize, and assert Kconfig")
    _add_repo_root(configure)
    configure.add_argument("--base", required=True)
    configure.add_argument("--profile", required=True)
    configure.add_argument("--root", required=True, choices=("kernelsu", "kernelsu-next", "none"))
    configure.add_argument("--optimization", required=True, choices=("O2", "O3"))
    configure.add_argument("--lto", required=True, choices=("thin", "full"))
    configure.add_argument("--build-target", required=True, choices=tuple(sorted(BUILD_TARGETS)))
    configure.add_argument("--source-dir", required=True, type=_path)
    configure.add_argument("--output", required=True, type=_path)
    configure.add_argument("--context", type=_path)
    configure.add_argument("--dry-run", action="store_true")
    configure.add_argument("--smoke", action="store_true")
    configure.set_defaults(handler=command_configure)

    kernel = subparsers.add_parser("build-kernel", help="run the official sun perf build")
    _add_repo_root(kernel)
    kernel.add_argument("--source-dir", required=True, type=_path)
    kernel.add_argument("--output", required=True, type=_path)
    kernel.add_argument("--context", type=_path)
    kernel.add_argument("--clean", action="store_true")
    kernel.add_argument("--debug", action="store_true")
    kernel.add_argument("--dry-run", action="store_true")
    kernel.add_argument("--smoke", action="store_true")
    kernel.add_argument("--branding")
    kernel.add_argument("--timestamp")
    kernel.set_defaults(handler=command_build_kernel)

    modules = subparsers.add_parser("build-modules", help="build external modules against a kernel artifact")
    _add_repo_root(modules)
    _add_cache(modules)
    modules.add_argument("--source-dir", required=True, type=_path)
    modules.add_argument("--kernel-output", required=True, type=_path)
    modules.add_argument("--output", required=True, type=_path)
    modules.add_argument("--context", type=_path)
    modules.add_argument("--clean", action="store_true")
    modules.add_argument("--debug", action="store_true")
    modules.add_argument("--dry-run", action="store_true")
    modules.add_argument("--smoke", action="store_true")
    modules.set_defaults(handler=command_build_modules)

    verify = subparsers.add_parser("verify", help="verify build lineage and artifacts")
    _add_repo_root(verify)
    verify.add_argument("--base", required=True)
    verify.add_argument("--profile", required=True)
    verify.add_argument("--root", required=True, choices=("kernelsu", "kernelsu-next", "none"))
    verify.add_argument("--build-target", required=True, choices=tuple(sorted(BUILD_TARGETS)))
    verify.add_argument("--output", required=True, type=_path)
    verify.add_argument("--smoke", action="store_true")
    verify.set_defaults(handler=command_verify)

    package = subparsers.add_parser("package", help="package only approved initial artifacts")
    _add_repo_root(package)
    _add_cache(package)
    package.add_argument("--base", required=True)
    package.add_argument("--profile", required=True)
    package.add_argument("--root", required=True, choices=("kernelsu", "kernelsu-next", "none"))
    package.add_argument("--build-target", required=True, choices=tuple(sorted(BUILD_TARGETS)))
    package.add_argument("--input", required=True, type=_path)
    package.add_argument("--output", required=True, type=_path)
    package.add_argument("--debug", action="store_true")
    package.add_argument("--pre-release", action="store_true")
    package.add_argument("--smoke", action="store_true")
    package.set_defaults(handler=command_package)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
        return 0
    except SourceChanged as exc:
        print(f"source update: {exc}", file=sys.stderr)
        return 2
    except BuildToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
