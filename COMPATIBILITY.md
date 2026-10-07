# Compatibility

| Status plugin | Hermes | Routing plugin | Evidence |
|---|---|---|---|
| 0.7.1 | 0.21.1, `fef0e16f` | 0.2.9 | sync/background + real-enum + authenticated completion-chain + heartbeat bridge + per-turn/finalize regression tests |
| 0.6.3 | 0.20.6 (2026.8.27), `01740f35` | 0.2.8 | installed adapter/post-delivery/async-registry contract probes + 22 dynamic status/lifecycle/privacy/fallback tests |

## Activation

Tagged and tested is not the same as loaded by the running gateway (see `AGENTS.md`).

| Status plugin | State | Evidence |
|---|---|---|
| 0.7.1 | Loaded by the production gateway. Smoke test not re-run for this record. | Gateway log line `Enabled slack-delegation-status v0.7.1 scope_configured=True` at the 2026-10-07 09:58 UTC start. On the same day the seven deployed files matched commit `fc07f3f` by git blob hash. `docs/SLACK_SMOKE_TEST.md` was not run again, so this row does not claim the release is operational. |
| 0.6.3 | Superseded by 0.7.1. | Not loaded: the gateway log reports 0.7.1. |

Verified Hermes contracts:

- `pre_gateway_dispatch`, `pre_tool_call`, `post_tool_call`, `pre_llm_call`,
  `post_llm_call`, `transform_llm_output`, per-turn `on_session_end`, durable
  `on_session_finalize`, `subagent_start`, and `subagent_stop` plugin hooks.
- A background `delegate_task` result with `mode=background` and
  `delegation_id`.
- Slack session context containing exact `chat_id`, `thread_id`, and (when
  available) `scope_id`.
- The live Slack adapter's workspace-routed `_get_client`,
  `register_post_delivery_callback`, `set_status_text`, `send_typing`, and
  `stop_typing` contracts. The plugin's checked request and adapter helper both
  invoke `assistant.threads.setStatus(channel_id, thread_ts, status)`.
- Generation-owned post-delivery callbacks fire after the parent's dispatch
  reply, avoiding the gateway end-of-turn clear and Slack reply auto-clear.
- A live gateway event loop for thread-safe delayed reassert/refresh.
- `tools.async_delegation.list_async_delegations()` public status and
  `children_activity` fields, correlated only by exact `delegation_id`.
- `delegate-task-routing.delegation_phase_for_turn` and
  `delegation_lifecycle_for_turn`, which expose only generic phase/mode,
  authenticated owned delegation IDs, and dispatch state—never goals, labels,
  models, toolsets, arguments, or result content.

The plugin is rollout fail-closed: without configured exact `scope.chat_id` and
`scope.thread_id`, it performs no outbound status call. It does not touch other
threads. Restart recovery clears only persisted rows whose route exactly matches
the configured route; all other stale rows are discarded without outbound work.

Slack's generic adapter helper internally catches remote `setStatus` failures.
v0.6.3 therefore uses the same adapter-selected client for a checked request;
an API error triggers at most one generic fallback card and remains non-blocking.
An exact-route live diagnostic on 2026-09-09 returned `ok=true` for both set and
clear, proving API acceptance but not remote visual rendering.

Run the extension-suite gate before Hermes/plugin updates. A running gateway
needs a safe externally controlled restart to activate changed plugin code or
settings; do not claim live behavior from static tests alone.
