# Slack Assistant-status smoke checklist

Automated verification sends **no** Slack messages. With explicit authorization
for one harmless background delegation, perform this only in rollout DM
`D0B91EGBA56`, thread `1788759304.795359`:

1. Confirm no agent/delegation/process is active before restarting. Restart via
   the official external gateway lifecycle, then verify a fresh Slack connection
   and `slack-delegation-status v0.3.0` INFO load log.
2. Confirm the accepted parent reply is the normal one-line dispatch response.
   Within a few seconds after it posts, confirm Slack's Assistant status bar
   says `is working on delegated tasks…` and no editable status card appeared.
3. Let it run beyond 90 seconds. Confirm the same Assistant status remains
   visible (refresh is below Slack's two-minute expiry) without repeated Slack
   messages.
4. For concurrent async delegations in this same thread, confirm one status
   remains after the first finishes and clears only after the last child reaches
   complete/error/stopped.
5. Send a normal inbound user message in that exact thread. Confirm the status
   clears immediately. Do **not** test a different thread during rollout.
6. Confirm the status clears at completion and, in a controlled long-run test,
   by 30 minutes maximum age.
7. If the adapter status call cannot be scheduled/called, confirm at most one
   legacy editable thread card is used as fallback. Status failures must never
   block the final delegation delivery.
8. Inspect INFO lifecycle logs only for route/state/count diagnostics; ensure no
   goals, tool args, child summaries, or raw errors were exposed.

Rollback: `hermes plugins disable slack-delegation-status`; keep
`delegate-task-routing` enabled. Do not send unsolicited test messages.
