# Reminder upsert canary

`core.reminder_upsert` is the narrow Apple Reminders action adapter for a
decision-to-action canary. It reconciles one reminder by an exact idempotency
marker, verifies the saved EventKit record, and emits a machine-readable undo
receipt. `bin/reminder-command-adapter` exposes that behavior through
Duffields' fixed `execute`, `readback`, `undo`, and `readback-undo` command
contract. It is not scheduled or deployed in this change.

## Safety boundary

- The only writable list is the unshared `Claude` list. `J&A Reminders`,
  `Store`, and every other list are rejected in both Python and Swift.
- The default CLI mode is a plan. It does not construct the EventKit backend or
  open the Reminders store.
- A live upsert requires an `approval_ref` in the request, `--apply`, the exact
  live-confirmation phrase, and a durable receipt path.
- Duffields bridge mutations are disabled unless the relay sets
  `REMINDER_COMMAND_ADAPTER_LIVE` to
  `I_UNDERSTAND_THIS_WRITES_APPLE_REMINDERS`.
- The Swift helper never requests TCC access. It fails unless the calling
  process already has Reminders Full Access, and every mutation also requires
  the private `REMINDER_UPSERT_LIVE=1` gate set by the Python adapter.
- Create is duplicate-guarded. Update and delete use compare-and-swap checks.
  Exact readback covers list, title, notes, due time, priority, and completion.
- The Duffields canary is create-only for an unbound operation. It holds the
  EventKit lock through verified receipt persistence. A pre-existing marker
  without operation state is uncertain and fails for reconciliation; the
  bridge never adopts or updates it.
- Completed reminders are reported but never reopened. Undo refuses to delete
  or overwrite a reminder that John edited after the automation.
- Existing manual Reminders workflows remain the fallback during the canary.

## Request and receipt interface

The action core writes one JSON request:

```json
{
  "schema": "reminder-upsert-request/v1",
  "idempotency_key": "duffields:decision-42",
  "list": "Claude",
  "title": "Confirm the canary action",
  "notes": "Created from a verified Duffields decision.",
  "due_at": "2026-07-22T09:30:00-04:00",
  "priority": "medium",
  "approval_ref": "approval:decision-42",
  "source_ref": "duffields:decision-42"
}
```

`due_at` must include a timezone. Priorities are `none`, `low`, `medium`, or
`high`. The adapter appends an exact marker line derived from
`idempotency_key`; callers cannot supply their own marker.

Plan without touching EventKit:

```sh
python3 -m core.reminder_upsert upsert \
  --request /absolute/path/request.json \
  --receipt /absolute/path/plan-receipt.json
```

A verified live receipt has status `created`, `updated`, `unchanged`, or
`completed`; `effect_verified` is true only after exact readback. Create and
update receipts contain a guarded undo payload. Plan receipts always have
`effect_verified: false`.

## Duffields command bridge

The executable accepts exactly one operation and a JSON object. Duffields sends
`{proposal, operation_id}` for `execute`, `{proposal, reference}` for the two
readbacks, and `{proposal, reference, operation_id}` for `undo`.

```sh
bin/reminder-command-adapter execute
bin/reminder-command-adapter readback
bin/reminder-command-adapter undo
bin/reminder-command-adapter readback-undo
```

For the TCC relay, use private files instead of putting proposal data in a shell
or argv:

```sh
bin/reminder-command-adapter execute \
  --request /absolute/private/request.json \
  --response /absolute/private/response.json \
  --state-root /absolute/private/state
```

The request must be a mode-0600 regular file owned by the current user. The
response is atomically written mode 0600. The state root is configurable with
`--state-root` or `REMINDER_COMMAND_ADAPTER_STATE_ROOT`; its directories are
mode 0700. Each stable `operation_id` is bound to one proposal and one reminder
request. The bridge persists the verified adapter receipt under that operation
digest before releasing the EventKit lock and refuses a changed replay.

`execute` returns a `reminder-upsert-operation:<sha256>` reference. `readback`
rereads EventKit independently and compares it with the persisted execute
receipt. `undo` consumes the receipt's compare-and-swap guard and persists its
own receipt; `readback-undo` independently confirms absence or exact
restoration. Neither readback trusts the mutation return value alone.

## TCC carrier contract

A raw LaunchAgent must not invoke EventKit or this executable directly. The
production caller must create a private, one-shot tmux session targeting the
existing `claude`/`main` Terminal-backed server, following
`/Users/johncornelius/bin/operator-ops-data-collect.sh`. Run the synchronous
bridge with direct argv and private request/response paths inside that session,
wait for its exit status and atomic response, then destroy only that private
session. This preserves Terminal's existing TCC carrier without scraping or
disturbing an interactive pane.

The relay is responsible for a bounded timeout, one request per invocation,
absolute input/output paths, restrictive file permissions, and returning the
adapter's JSON receipt unchanged. A production rollout should first exercise a
single ephemeral item in `Claude`, prove create/replay/update/undo parity, and
retain the manual path until that receipt set passes review.

## Focused verification

These checks are read-only with respect to Reminders:

```sh
python3 -m pytest -q tests/test_reminder_upsert.py
python3 -m pytest -q tests/test_reminder_command_adapter.py
xcrun swiftc -typecheck core/reminder_eventkit.swift
```
