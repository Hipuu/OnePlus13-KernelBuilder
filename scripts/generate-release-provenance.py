#!/usr/bin/env python3
"""Command-line entry point for deterministic release provenance."""

from __future__ import annotations

import argparse
from pathlib import Path

from lib.release_provenance import (
    ReleaseProvenanceError,
    generate_release_provenance,
    parse_bool,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--optimization", required=True)
    parser.add_argument("--lto", required=True)
    parser.add_argument("--clean", type=parse_bool, required=True)
    parser.add_argument("--debug", type=parse_bool, required=True)
    parser.add_argument("--pre-release", type=parse_bool, required=True)
    parser.add_argument("--branding", required=True)
    parser.add_argument("--build-timestamp", default="")
    args = parser.parse_args()
    try:
        provenance_path, checksum_path = generate_release_provenance(
            assets_dir=args.assets_dir,
            repository_root=args.repository_root,
            repository=args.repository,
            revision=args.revision,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            external_parameters={
                "tag": args.tag,
                "base": args.base,
                "root": args.root,
                "profile": args.profile,
                "target": args.target,
                "optimization": args.optimization,
                "lto": args.lto,
                "clean": args.clean,
                "debug": args.debug,
                "preRelease": args.pre_release,
                "branding": args.branding,
                "buildTimestamp": args.build_timestamp,
            },
        )
    except ReleaseProvenanceError as exc:
        parser.error(str(exc))
    print(provenance_path)
    print(checksum_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
