# Air Reminder owner dependency

Installed owner: `core/reminder_upsert.py` and `core/reminder_eventkit.swift`.
The supported writer is the receipt-bound `~/claude-config/bin/life-ops` synthetic canary relay. No live Calendar or Reminder writes are approved by this installation.

The standalone upsert CLI is plan-only; `--apply` fails closed. Adapter upsert requires saved `expected` state or explicit `create_only`. Saved expected state is checked under the adapter lock and passed to native compare-before-write. Missing bound targets cannot be recreated. Unbound collisions cannot be adopted. Create replay after update is rejected without mutation. Cleanup rejects changed or unknown modification state. Native comparison does not provide an atomic transaction with external human edits after comparison.

The native helper requires an existing access grant and never requests one. Only the uniquely named AI list with Air UUID `320F68D8-7CDC-4862-AC7E-CA2F2887A51E`, source `B099804C-4B2B-4B88-8C90-986143FA2E5E`, and content writability is accepted. Public EventKit metadata here does not expose sharing status: the name does not prove an unshared list. Air native UI independently showed AI with Share List and no Shared annotation on September 30, 2026, while shared lists showed Manage Shared List. Before live dispatch, recheck identity and sharing read-only and obtain explicit Air approval. Shared Family approval from Mini provides no Air authorization or parity evidence.

Mutating helper failures are outcome-unknown. Preserve saved state and reconcile read-only before retry or cleanup; do not blindly compensate. Original create followed by update then replay of original create is rejected, preserving the update.

Verification uses fixture backends only and Swift typecheck. Live acceptance requires explicit approval for a bounded synthetic AI-list create, readback, replay, update, readback, original-create replay refusal, and guarded cleanup with final absence verification. No new grants, credentials, process restarts, or inference canaries are needed or authorized.
