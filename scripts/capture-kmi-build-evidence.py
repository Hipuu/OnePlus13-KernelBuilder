#!/usr/bin/env python3
"""Validate and capture feature-gated KMI patch evidence before compilation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lib.build_evidence import (
    capture_source_kmi_evidence,
    wireless_led_exports_required,
)
from lib.context import load_context
from lib.errors import BuildToolError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        action="append",
        required=True,
        help="evidence directory; may be repeated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = load_context(args.context)
        if context.get("profile") != args.base:
            raise BuildToolError("KMI evidence base differs from build context")
        required = wireless_led_exports_required(context.get("features"))
        result = capture_source_kmi_evidence(
            source_dir=args.source_dir,
            base=args.base,
            wireless_led_exports_required=required,
            destinations=args.output,
        )
    except BuildToolError as exc:
        print(f"KMI build evidence: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "base": result["base"],
                "wireless_led_exports_required": result[
                    "wireless_led_exports_required"
                ],
                "outputs": [str(path) for path in args.output],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
