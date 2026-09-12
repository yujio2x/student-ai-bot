import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from uuid import uuid4

from app.bridge_client import BridgeError
from app.payment_outbox import PaymentOutbox
from app.postgres_outbox import PostgresPaymentOutbox


def payload():
    return {"charge_id": "synthetic-atomic-charge", "product_id": "task_help_1_v1",
            "stars_paid": 25, "telegram": {"telegram_user_id": 123, "display_name": "Әлия 😀"}}


def receipt():
    return {"payment": {"telegram_payment_charge_id": payload()["charge_id"],
        "product_id": "task_help_1_v1", "stars_paid": 25, "telegram_user_id": "123"}}


class OutboxContract:
    def test_persisted_backoff_survives_reopen(self):
        box = self.open()
        core = Mock()
        core.record_payment.side_effect = BridgeError(503)
        with patch('app.payment_outbox.time.time', return_value=1000):
            box.enqueue(payload())
            self.assertEqual(box.retry(core, backoff=True), 0)
        with patch('app.payment_outbox.time.time', return_value=1059):
            self.assertEqual(self.open().retry(core, backoff=True), 0)
            self.assertEqual(core.record_payment.call_count, 1)
        core.record_payment.side_effect = None
        core.record_payment.return_value = receipt()
        with patch('app.payment_outbox.time.time', return_value=1060):
            self.assertEqual(self.open().retry(core, backoff=True), 1)
        self.assertEqual(self.open().pending(), [])

    def test_faults_malformed_and_wrong_receipts_remain_pending(self):
        box = self.open()
        box.enqueue(payload())
        core = Mock()
        for fault in (BridgeError(500), BridgeError(504), TimeoutError(), ConnectionResetError()):
            core.record_payment.side_effect = fault
            self.assertEqual(box.retry(core), 0)
        core.record_payment.side_effect = None
        bad = [None, [], {}, {"payment": None}, {"payment": []}]
        for field in ("telegram_payment_charge_id", "product_id", "stars_paid", "telegram_user_id"):
            value = receipt()
            value["payment"][field] = "wrong"
            bad.append(value)
        for value in bad:
            core.record_payment.return_value = value
            self.assertEqual(box.retry(core), 0)
            self.assertEqual(box.get(payload()["charge_id"])["delivery_state"], "pending")
        core.record_payment.return_value = receipt()
        self.assertEqual(box.retry(core), 1)
        stale = {"charge_id": payload()["charge_id"], "payload": "not trusted"}
        core.reset_mock()
        self.assertTrue(box.deliver(core, stale))
        core.record_payment.assert_not_called()

    def test_duplicate_enqueue_and_concurrent_retry(self):
        box = self.open()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: box.enqueue(payload()), range(2)))
        self.assertEqual(len(box.pending()), 1)
        core = Mock()
        core.record_payment.return_value = receipt()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: box.retry(core), range(2)))
        self.assertEqual(box.pending(), [])
        box.enqueue(payload())
        self.assertEqual(box.pending(), [])
        with self.assertRaises(ValueError):
            box.enqueue({**payload(), "stars_paid": 100})

    def test_corrupt_or_foreign_stored_payload_fails_closed_as_value_error(self):
        box = self.open()
        box.enqueue(payload())
        with box._connect() as db:
            db.execute("UPDATE payment_outbox SET payload=? WHERE charge_id=?",
                       ('{"legacy": "shape"}', payload()["charge_id"]))
        with self.assertRaises(ValueError):
            box.enqueue(payload())
        with self.assertRaises(ValueError):
            box.enqueue({"charge_id": "missing-product", "stars_paid": 25,
                         "telegram": {"telegram_user_id": 1}})
        with self.assertRaises(ValueError):
            box.enqueue("not-a-dict")

    def test_process_crash_after_enqueue_before_delivery(self):
        env = os.environ.copy()
        env.update(self.child_environment())
        # os._exit simulates abrupt termination, not an orderly connection cleanup.
        script = """import json,os
from pathlib import Path
from app.payment_outbox import PaymentOutbox
from app.postgres_outbox import PostgresPaymentOutbox
box = (PostgresPaymentOutbox(os.environ['BOT_OUTBOX_TEST_URL'], schema=os.environ['OUTBOX_SCHEMA'])
       if os.environ.get('OUTBOX_SCHEMA') else PaymentOutbox(Path(os.environ['OUTBOX_PATH'])))
box.enqueue(json.loads(os.environ['OUTBOX_TEST_PAYLOAD']))
os._exit(23)
"""
        env["OUTBOX_TEST_PAYLOAD"] = json.dumps(payload())
        completed = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, timeout=60)
        self.assertEqual(completed.returncode, 23, "Synthetic child crash/setup failed; output suppressed")
        reopened = self.open()
        self.assertEqual(len(reopened.pending()), 1)
        core = Mock()
        core.record_payment.side_effect = BridgeError(503)
        self.assertEqual(reopened.retry(core), 0)
        reopened = self.open()
        core.record_payment.side_effect = None
        core.record_payment.return_value = receipt()
        self.assertEqual(reopened.retry(core), 1)
        self.assertEqual(self.open().pending(), [])


class SQLiteOutboxTest(OutboxContract, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "outbox.db"
        self.addCleanup(self.temp.cleanup)

    def open(self):
        return PaymentOutbox(self.path)

    def child_environment(self):
        return {"OUTBOX_PATH": str(self.path), "OUTBOX_SCHEMA": ""}

    def test_backlog_returns_counts_only(self):
        box = self.open()
        item = payload()
        item["charge_id"] = "count-pending"
        box.enqueue(item)
        self.assertEqual(box.backlog(), {"pending": 1, "delivered": 0})


@unittest.skipUnless(os.getenv("BOT_OUTBOX_TEST_URL"), "PostgreSQL test URL not configured")
class PostgreSQLOutboxTest(OutboxContract, unittest.TestCase):
    def setUp(self):
        self.schema = "test_" + uuid4().hex
        self.url = os.environ["BOT_OUTBOX_TEST_URL"]
        self.addCleanup(self.cleanup)

    def open(self):
        return PostgresPaymentOutbox(self.url, schema=self.schema)

    def child_environment(self):
        return {"OUTBOX_SCHEMA": self.schema}

    def cleanup(self):
        import psycopg
        from psycopg import sql
        assert self.schema.startswith("test_") and len(self.schema) == 37
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))
