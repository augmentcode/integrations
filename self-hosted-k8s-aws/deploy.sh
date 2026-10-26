#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$bundle_dir/deploy.py" "$@"
