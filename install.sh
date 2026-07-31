#!/bin/sh
set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

for python_command in python3 python; do
    if command -v "$python_command" >/dev/null 2>&1 && \
        "$python_command" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
    then
        exec "$python_command" "$script_dir/install.py" "$@"
    fi
done

echo "[ERROR] Python 3.10 or newer was not found." >&2
exit 1
