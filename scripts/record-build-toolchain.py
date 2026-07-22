#!/usr/bin/env python3
"""Record and verify the pinned compiler selected by the OnePlus build."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lib.errors import BuildToolError
from lib.toolchain_provenance import record_build_toolchain


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--resolved-manifest", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        action="append",
        required=True,
        help="canonical JSON destination; may be repeated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = record_build_toolchain(
            args.source_dir,
            args.resolved_manifest,
            args.output,
        )
    except BuildToolError as exc:
        print(f"build toolchain provenance: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
