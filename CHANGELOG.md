# Changelog

All notable changes to this project are documented here.

## [0.7.1] - 2026-10-07

Code deployed to production earlier and recorded in this repository retroactively on 2026-10-07. Tagged on that date; the changes below were not released on that date.

### Changed

- Slack Assistant status now covers the whole request lifecycle, not only background delegation: request analysis, route decision, delegation preparation, worker, multiple workers, verifier, partial lanes, stalled, sampled web/file work, synthesis and writing. Status texts are configurable per state through `status_texts`; the legacy `status_text`, `multiple_status_text` and `verifier_status_text` keys remain supported.
- Both synchronous and detached background `delegate_task` calls are tracked. Completion ownership is authenticated through `delegate-task-routing.delegation_lifecycle_for_turn` using exact delegation IDs.
- New hook `on_session_finalize` (durable finalize) alongside per-turn `on_session_end`; an ordinary parent-turn boundary no longer clears detached background work.
- Exact Slack routes are isolated; unload cleanup is race-safe.
- `plugin.yaml`: new description, `provides_hooks` gains `on_session_finalize`, homepage moved to `joonseong11/hermes-plugin-slack-delegation-status`.
- Documentation: `COMPATIBILITY.md` adds the 0.7.1 row (Hermes 0.21.1, routing plugin 0.2.9); the smoke test no longer contains real workspace, conversation or thread IDs.

### Fixed

- `_queue_route` no longer imports `gateway.run`. Plugin registration runs while `gateway.run` is still importing, so the import could wait on that lock and deadlock startup; it now uses only an already-loaded module from `sys.modules`. This was edited on the server on 2026-09-28 and is first committed here.

### Added

- Regression test `test_queue_route_never_imports_gateway_during_plugin_registration`.
- GitHub Actions workflow `static-integrity` (py_compile and manifest identity check), `LICENSE`, `AGENTS.md`, `CHANGELOG.md` and `scripts/check-version.sh`.

### Verification

- Without a Hermes install, 19 of 20 tests in `tests/test_status.py` pass locally; `test_real_platform_enum_pre_auth_inbound_preserves_exact_route` needs the `gateway` package. `scripts/verify.sh` was not run (no Hermes source at `/opt/hermes`).

## [0.6.3] - 2026-10-07

Initial release of the plugin, recorded in commit `926a156`. Tagged retroactively on 2026-10-07.

### Added

- Thread-scoped dynamic Slack Assistant status for async `delegate-task-routing` delegation, via the installed Slack adapter's workspace-routed client and `assistant.threads.setStatus`, with a checked request and at most one generic fallback card on API failure.
- Rollout fail-closed scope: no outbound status call without a configured exact `scope.chat_id` and `scope.thread_id`.
- Verified against Hermes 0.20.6 (2026.8.27) and routing plugin 0.2.8 with 22 dynamic status, lifecycle, privacy and fallback tests (see `COMPATIBILITY.md`).
