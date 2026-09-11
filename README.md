# slack-delegation-status

A separate, fail-open presentation observer for `delegate-task-routing`. It
never overrides `delegate_task`, changes routing, or participates in final
result delivery.

## Primary UI: Slack Assistant status

For an accepted top-level background delegation, the plugin obtains the
workspace-routed client from the installed Slack adapter and makes a checked
`assistant.threads.setStatus` request. It never reads or stores a Slack token.
Unlike the adapter's generic `send_typing` helper, the checked request exposes
API failure to the plugin so the documented bounded fallback can actually run.
Slack automatically renders the app name before the supplied predicate. The
plugin therefore sends no `Alex` prefix and selects:

- one active worker: **`비동기 위임 작업 중…`**; multiple workers:
  **`{count}개 작업을 처리 중…`**; verifier: **`결과를 검증 중…`**.
- partial lanes: **`{done}/{total}개 완료 · {active}개 처리 중…`**;
  stalling: **`작업 응답 지연을 처리 중…`**; stable sampled web/file work:
  **`자료를 확인 중…`** / **`파일을 확인 중…`**; completion synthesis/writing:
  **`결과를 정리 중…`** / **`답변을 작성 중…`**.

In the Korean Slack client these appear as `Alex 앱이 …`; the native status
API cannot replace Slack's app-name/`앱이` prefix with `Alex가`.

The gateway explicitly clears typing when the parent turn ends, before it sends
the parent's short dispatch reply; Slack may also clear status when that reply
lands. v0.6.3 registers a generation-owned adapter post-delivery callback and
reasserts only after the dispatch reply has actually been delivered. This fixes
the v0.6.1/v0.6.2 race where a fixed two-second timer commonly fired before the
reply and was immediately erased. The plugin then samples public child activity
every 30 seconds and sends a 90-second heartbeat (below Slack's two-minute
expiry). A changed tool-family status requires two matching samples.
Child completion transitions to synthesis, the completion LLM turn transitions
to writing, and the turn-final output boundary removes the record so no final
reply can be followed by an accidental reassertion. It also clears on a new
human inbound message or the 30-minute maximum age.
Concurrent background delegations in the same exact route share one status and
only the final completion clears it.

## Scope, settings, and installation

The observer is inert until an exact Slack route is configured. Replace every
placeholder with the route approved for this deployment; `thread_id: "*"`
matches every real thread only inside the named conversation, not other DMs or
channels. Keep the scope as narrow as possible.

```yaml
plugins:
  entries:
    slack-delegation-status:
      settings:
        scope:
          team_id: "<optional Slack workspace ID>"
          chat_id: "<approved Slack conversation ID>"
          thread_id: "<thread timestamp or *>"
        status_text: "비동기 위임 작업 중…"
        multiple_status_text: "{count}개 작업을 처리 중…"
        verifier_status_text: "결과를 검증 중…"
        reassert_delay_seconds: 2
        sample_seconds: 30
        refresh_seconds: 90
        max_age_seconds: 1800
```

Install and enable `delegate-task-routing` first; this observer requires its
public phase/status contract and does not replace routing itself. Clone this
repository into Hermes' configured persistent plugin directory, review the
settings above, then run `./scripts/verify.sh` and `./scripts/deploy.sh --apply`
from that installed checkout. `deploy.sh` verifies and enables the installed
plugin; it deliberately does **not** copy an arbitrary checkout or restart the
gateway. Use the official Hermes configuration surface to activate settings. A
running gateway needs an externally controlled restart to load new code or
settings; do not restart while active work is running.

The implementation calls the installed Slack adapter's private `_get_client`
helper and the Slack native `assistant.threads.setStatus` method. Those are
compatibility-sensitive integration details, not a portable public API; run the
installed-adapter probe after every Hermes or adapter update.

## Fallback and privacy

The old thread-message surface is now **fallback only**: it is sent only when
the adapter status path cannot be scheduled or called. A Slack API failure stays fail-open and does not block, cancel, or alter a
delegation/final delivery. The fallback is at most one generic card per exact
route/lifecycle; Slack error text is never exposed to the user.

The status contains no goals, raw labels, arguments, paths, summaries, or child
errors. Persisted rows contain only delegation/session correlation, exact route,
timestamps, and generic lane state. On restart, rows are never guessed into a
thread: the plugin clears only a persisted row matching the configured exact
route, then discards recovery state.

## Lifecycle and logs

- `pre_gateway_dispatch`: clear scoped status on new inbound user message.
- `pre_tool_call` / `post_tool_call`: accept only successful background
  `delegate_task` work.
- `pre_llm_call`: mark authenticated completion turns as answer writing.
- `transform_llm_output`: release finalizing rows at the actual output boundary.
- `subagent_start` / `subagent_stop`: aggregate child lifecycle and transition
  the final child into result synthesis.

INFO logs report lifecycle state/counts and route identifiers, never goals or
arguments. Transport/persistence failures are non-blocking.

## Verify and activate

```bash
./scripts/verify.sh
hermes plugins doctor <installed-plugin-directory> --ci
```

Repository CI runs static compilation only; it cannot validate installed Hermes
or Slack-adapter private contracts. `./scripts/verify.sh` runs the integration
probe locally. Automated checks intentionally send no Slack messages. Follow
`docs/SLACK_SMOKE_TEST.md` only with explicit authorization for a harmless
manual delegation.

## Disable

```bash
hermes plugins disable slack-delegation-status
```

Disabling this observer leaves `delegate-task-routing` and existing delegation
records/results unchanged. ETA is intentionally absent until at least two
completed lanes provide measured durations; when shown it is explicitly an
estimate (`예상 약 …초`), never a fabricated deadline.
