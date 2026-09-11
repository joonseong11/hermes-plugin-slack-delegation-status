# Slack Assistant-status smoke checklist

Automated verification sends **no** Slack messages. With explicit authorization
for one harmless background delegation, perform this only on the configured
exact Slack route; do not put real workspace, conversation, or thread IDs in
this document or in a public issue.

1. Confirm no agent/delegation/process is active before restarting. Restart via
   the official external gateway lifecycle, then verify a fresh Slack connection
   and an INFO load log for `slack-delegation-status v0.6.3`.
2. In the configured route, start one harmless background delegation. Confirm
   the accepted parent reply is the normal one-line dispatch response. Within a
   few seconds after it posts, confirm Slack's Assistant status bar shows the
   configured generic status and no editable status card appeared.
3. Let it run beyond the configured refresh interval. Confirm the same Assistant
   status remains visible without repeated Slack messages.
4. For concurrent async delegations in that same configured route, confirm one
   status remains after the first finishes and clears only after the last child
   reaches complete/error/stopped.
5. Send a normal inbound user message in the exact configured route. Confirm the
   status clears immediately. Do not test any other route.
6. Confirm the status clears at completion and, in a controlled long-run test,
   by the configured maximum age.
7. If the private adapter status call cannot be scheduled/called, confirm at
   most one generic fallback card is used. Status failures must never block the
   final delegation delivery.
8. Inspect INFO lifecycle logs only for generic route/state/count diagnostics;
   ensure no goals, tool args, child summaries, raw errors, or route identifiers
   were exposed.

Rollback: `hermes plugins disable slack-delegation-status`; keep
`delegate-task-routing` enabled. Do not send unsolicited test messages.
