"""Separate durable delivery journal. Never modifies the legacy ledger."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.bridge_client import BridgeError, StudentOSBridgeClient


class PaymentOutbox:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS payment_outbox (
                charge_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                created_at INTEGER NOT NULL, delivery_state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at INTEGER,
                last_error TEXT, delivered_at INTEGER)""")
            db.execute("CREATE INDEX IF NOT EXISTS outbox_pending ON payment_outbox(delivery_state, last_attempt_at)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _charge_identity(payload: dict) -> tuple:
        telegram = payload.get("telegram") if isinstance(payload, dict) else None
        if not isinstance(telegram, dict):
            raise ValueError("Invalid payment envelope")
        identity = (payload.get("product_id"), payload.get("stars_paid"), telegram.get("telegram_user_id"))
        if any(part is None for part in identity):
            raise ValueError("Invalid payment envelope")
        return identity

    def enqueue(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Invalid payment envelope")
        charge = payload.get("charge_id")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if not isinstance(charge, str) or not 1 <= len(charge) <= 180 or len(encoded) > 4096:
            raise ValueError("Invalid payment envelope")
        self._charge_identity(payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload FROM payment_outbox WHERE charge_id=?", (charge,)).fetchone()
            if old:
                try:
                    previous = json.loads(old["payload"])
                    previous_identity = self._charge_identity(previous)
                except ValueError:
                    raise ValueError("Conflicting payment charge") from None
                # Profile names may change between duplicate Telegram updates.
                if previous_identity != self._charge_identity(payload):
                    raise ValueError("Conflicting payment charge")
                return
            db.execute("INSERT INTO payment_outbox(charge_id,payload,created_at) VALUES (?,?,?)",
                       (charge, encoded, int(time.time())))

    def pending(self, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM payment_outbox WHERE delivery_state='pending' ORDER BY COALESCE(last_attempt_at,0),created_at LIMIT ?",
                (min(max(limit, 1), 100),))]

    def backlog(self) -> dict[str, int]:
        """Return aggregate delivery counts without exposing payment payloads."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT delivery_state, COUNT(*) AS count FROM payment_outbox GROUP BY delivery_state"
            )
            counts = {row["delivery_state"]: row["count"] for row in rows}
        return {"pending": counts.get("pending", 0), "delivered": counts.get("delivered", 0)}

    def get(self, charge_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM payment_outbox WHERE charge_id=?", (charge_id,)).fetchone()
        return dict(row) if row else None

    def deliver(self, client: StudentOSBridgeClient, row: dict) -> bool:
        current = self.get(row["charge_id"])
        if not current:
            return False
        if current["delivery_state"] == "delivered":
            return True
        row = current
        error = None
        try:
            payload = json.loads(row["payload"])
            response = client.record_payment(payload)
            if not isinstance(response, dict) or not isinstance(response.get("payment"), dict):
                raise BridgeError(502)
            receipt = response.get("payment", {})
            if (receipt.get("telegram_payment_charge_id") != payload["charge_id"]
                    or receipt.get("product_id") != payload["product_id"]
                    or receipt.get("stars_paid") != payload["stars_paid"]
                    or str(receipt.get("telegram_user_id")) != str(payload["telegram"]["telegram_user_id"])):
                raise BridgeError(502)
        except BridgeError as exc:
            error = f"core_http_{exc.status}"
        except (ValueError, TypeError, KeyError):
            error = "core_http_502"
        except (OSError, ConnectionError):
            error = "core_http_503"
        now = int(time.time())
        with self._connect() as db:
            # A late failed concurrent retry must never downgrade delivered.
            db.execute("""UPDATE payment_outbox SET attempts=attempts+1,
                last_attempt_at=?,last_error=?,delivery_state=?,delivered_at=?
                WHERE charge_id=? AND delivery_state='pending'""",
                (now, error, "pending" if error else "delivered", None if error else now, row["charge_id"]))
        return error is None

    def retry(self, client: StudentOSBridgeClient, limit: int = 20, *, backoff: bool = False) -> int:
        delivered = 0
        rows = self.pending(100 if backoff else limit)
        if backoff:
            now = int(time.time())
            rows = [row for row in rows if row["last_attempt_at"] is None or
                    now >= row["last_attempt_at"] + min(3600, 60 * (2 ** min(max(row["attempts"]-1,0),6)))]
            rows = rows[:min(max(limit, 1), 100)]
        for row in rows:
            if self.deliver(client, row):
                delivered += 1
            else:
                # Rotate attempted rows fairly, but stop a batch on network/server outage.
                latest = self.get(row["charge_id"])
                if latest and latest["last_error"] in {"core_http_429", "core_http_500", "core_http_502", "core_http_503", "core_http_504"}:
                    break
        return delivered
