#!/usr/bin/env bash
set -euo pipefail
exec python3 "$(dirname "$0")/op13.py" apply-series "$@"
