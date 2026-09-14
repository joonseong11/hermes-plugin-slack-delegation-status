# Slack Assistant-status smoke checklist

Automated verification sends **no** Slack messages. With explicit authorization
for one harmless background delegation, perform this only on the configured
exact Slack route; do not put real workspace, conversation, or thread IDs in
this document or in a public issue.

1. Confirm no agent/delegation/process is active before restarting. Restart via
   the official external gateway lifecycle, then verify a fresh Slack connection
   and an INFO load log for `slack-delegation-status v0.7.1`.
2. In the configured route, start one harmless background delegation. Confirm
   the accepted parent reply is the normal one-line dispatch response. Within a
   few seconds, confirm Slack's Assistant status advances through generic
   processing/delegation phases and no editable status card appears.
3. Let it run beyond the configured refresh interval. Confirm the same current
   Assistant phase remains visible without repeated Slack messages.
4. For concurrent delegations in that same configured route, confirm one status
   remains after the first finishes and clears only after the last owned child
   and any required verifier reach a terminal state.
5. Confirm an ordinary parent-turn boundary does not clear detached background
   work. Confirm final output transitions through synthesis/writing and then
   clears only the completed turn's owned status.
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
