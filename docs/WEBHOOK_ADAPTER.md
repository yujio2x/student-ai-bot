# One-Eco transport transition — 2026-09-30

The polling worker is a rollback transport, not the intended steady production topology. Core embeds a pinned source snapshot of bridge_handlers, bridge_client, payment_outbox and postgres_outbox under student_telegram. This repo owns adapter source; Core owns webhook ingress, durable job/effect journals and lifecycle. Core has no runtime dependency on this repo or its legacy bot/SQLite/AI engine. Refresh Core's snapshot explicitly after a reviewed adapter commit, with scripts/vendor_telegram_adapter.py.

Cloud worker fails before settings access when TELEGRAM_DELIVERY_MODE is not polling; its independent CLOUD_POLLING_ENABLED latch remains required. Under the existing PostgreSQL advisory lease it checks getWebhookInfo before run_polling, because run_polling otherwise deletes a webhook. It never deletes a registered webhook automatically. Core uses the same advisory lock. Stop Core webhook mode/delete webhook before any rollback polling attempt. Never run both transports.

Webhook runtime injects durable_delivery=true so the adapter delegates duplicate AI suppression to persisted update/effect fences. Polling keeps the existing in-memory suppression and stable Core request IDs. This flag is not a billing mode and does not change Stars/credits/products/entitlement rules.

Pending photo confirmation now retains a Telegram file_id, not raw image bytes. The adapter re-downloads on confirmation with the same 6 MiB bound. This lets Core persist short-lived quote context across restarts without retaining uploaded photos. Unit regression performs a JSON context round-trip.

The existing PostgreSQL payment outbox remains at-least-once delivery to Core's unique Telegram charge-id business boundary. Do not delete pending rows or replay successful payments into the legacy local ledger. Synthetic restart/outage/ambiguous-commit tests remain in test_outbox_persistence and Core cross-project CI.

Local checks: 109 unittest tests OK, 5 PostgreSQL cases skipped locally. PostgreSQL is delegated to existing CI, because local initdb was blocked by Windows application control. No production credentials are fetched to run tests. Source checkout at C:\student-ai-bot has pre-existing app/bot.py/assets/outputs changes and is not used for this commit; the isolated checkout starts from the latest origin/main.

Production audit/cutover/rollback truth: Core docs/HEROKU_ECO_INCIDENT_2026-09.md and docs/HEROKU_WEBHOOK_RUNBOOK.md. No automatic polling restart or real Stars purchase is authorized by this document.
