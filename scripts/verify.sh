#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_SRC="${HERMES_SRC:-/opt/hermes}"
PY="${HERMES_PYTHON:-$HERMES_SRC/.venv/bin/python}"
[[ -x "$PY" ]] || { printf 'Hermes Python missing: %s\n' "$PY" >&2; exit 2; }
python3 -m py_compile "$ROOT/__init__.py" "$ROOT/tests/test_status.py"
PYTHONPATH="$HERMES_SRC:${HERMES_SRC}/.venv/lib/python3.13/site-packages" uv run --no-project --with pytest python -m pytest -q "$ROOT/tests/test_status.py"
PYTHONPATH="$HERMES_SRC:${HERMES_SRC}/.venv/lib/python3.13/site-packages" "$PY" - <<'PY'
import inspect
from gateway.run import _gateway_runner_ref
from gateway.session_context import get_session_env
from plugins.platforms.slack.adapter import SlackAdapter
assert callable(get_session_env) and callable(_gateway_runner_ref)
for name in ("set_status_text", "send_typing", "stop_typing", "register_post_delivery_callback"):
    assert callable(getattr(SlackAdapter, name, None)), name
assert callable(getattr(SlackAdapter, "_get_client", None))
assert "metadata" in inspect.signature(SlackAdapter.send_typing).parameters
assert "metadata" in inspect.signature(SlackAdapter.stop_typing).parameters
assert "generation" in inspect.signature(SlackAdapter.register_post_delivery_callback).parameters
print("installed Slack Assistant-status adapter contract: PASS")
PY
printf 'verification: PASS\n'
