#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; [[ "${1:-}" == "--apply" || -z "${1:-}" ]] || { printf 'Unknown argument\n' >&2; exit 2; }
"$ROOT/scripts/verify.sh"
[[ "${1:-}" == "--apply" ]] || { printf 'Dry run passed; use --apply to enable through the official CLI.\n'; exit 0; }
hermes plugins enable slack-delegation-status
hermes plugins list --plain --no-bundled
