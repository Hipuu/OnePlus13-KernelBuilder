#!/bin/sh
# Compile-and-check the injection reclaim path.  See run.py for what it does.
exec python3 "$(dirname "$0")/run.py" "$@"
