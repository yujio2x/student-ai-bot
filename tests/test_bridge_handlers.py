import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram.ext import ApplicationHandlerStop
from app.bridge_client import BridgeError
from app.bridge_handlers import dispatch, install, render_result, telegram_math
from app.payment_outbox import PaymentOutbox


class HandlersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = Mock()
        self.outbox = PaymentOutbox(Path(self.temp.name) / "test.db")
        self.message = SimpleNamespace(text="Реши x + 1 = 2", message_id=12,
                                       photo=None, successful_payment=None, reply_text=AsyncMock())
        self.update = SimpleNamespace(effective_message=self.message,
            effective_user=SimpleNamespace(id=123, username="student", full_name="Әлия"),
            effective_chat=SimpleNamespace(id=123), callback_query=None, pre_checkout_query=None)
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={"bridge": self.client,
            "payment_outbox": self.outbox}), user_data={}, bot=SimpleNamespace(send_invoice=AsyncMock()))

    def tearDown(self):
        self.temp.cleanup()

    async def run_dispatch(self):
        with self.assertRaises(ApplicationHandlerStop):
            await dispatch(self.update, self.context)

    async def test_text_and_defense_share_one_result(self):
        self.client.submit_text_task.return_value = {"result": {"analysis": "Понимание",
            "explanation": "x = 1", "how_to_defend": "Подстановка", "defense_questions": ["Почему?"], "pitfalls": ["Ошибка"]}}
        await self.run_dispatch()
        self.assertEqual(self.client.submit_text_task.call_args.args[2], "telegram:123:123:12")
        self.update.callback_query = SimpleNamespace(data="coredef:12", answer=AsyncMock())
        await self.run_dispatch()
        self.assertEqual(self.client.submit_text_task.call_count, 1)
        self.assertIn("Подстановка", self.message.reply_text.call_args.args[0])
        self.update.callback_query.data = "corefb:12:positive"
        await self.run_dispatch()
        self.client.feedback.assert_called_once()

    async def test_outage_payment_is_durable_before_call(self):
        self.message.successful_payment = SimpleNamespace(currency="XTR", telegram_payment_charge_id="paid",
            invoice_payload="task_help_1_v1", total_amount=25)
        def unavailable(payload):
            self.assertEqual(len(self.outbox.pending()), 1)
            raise BridgeError()
        self.client.record_payment.side_effect = unavailable
        await self.run_dispatch()
        self.assertEqual(len(self.outbox.pending()), 1)
        await self.run_dispatch()
        self.assertEqual(len(self.outbox.pending()), 1)

    async def test_checkout_fails_closed_when_core_down(self):
        self.update.pre_checkout_query = SimpleNamespace(currency="XTR", invoice_payload="task_help_1_v1",
            total_amount=25, answer=AsyncMock())
        self.client.get_products.side_effect = BridgeError()
        await self.run_dispatch()
        self.assertFalse(self.update.pre_checkout_query.answer.call_args.kwargs["ok"])

    async def test_catalog_balance_and_start_buy(self):
        self.client.get_products.return_value = {"products": [{"id": "task_help_1_v1", "title": "1 разбор", "stars": 25}]}
        self.message.text = "/start buy"
        await self.run_dispatch()
        self.client.get_products.assert_called_once()
        self.message.text = "/balance"
        self.client.get_entitlement.return_value = {"entitlement": {"balance": 5, "free_trial_available": False}}
        await self.run_dispatch()
        self.assertIn("5", self.message.reply_text.call_args.args[0])

    async def test_off_is_noop_and_photo_never_uses_old_engine(self):
        app = Mock()
        install(app, SimpleNamespace(student_os_bridge_enabled=False))
        app.add_handler.assert_not_called()
        self.message.photo = [SimpleNamespace(file_size=7*1024*1024)]
        await self.run_dispatch()
        self.client.submit_text_task.assert_not_called()
        self.context.application.bot_data.clear()
        await dispatch(self.update, self.context)

    def test_formatter_preserves_unicode_and_is_bounded(self):
        result = render_result({"explanation": "Ә <script> 😀" * 10000})
        self.assertLessEqual(len(result), 60000)
        self.assertIn("Ә", result)

    def test_compact_formatter_uses_russian_sections_and_unicode_math(self):
        result = render_result({
            "solution": r"r=\\sqrt{21+7}=2\\sqrt{7}",
            "answer": r"z=2\\sqrt{7}(\\cos(\\pi/6)+i\\sin(\\pi/6))",
            "optional_check": None,
        })
        self.assertIn("Решение", result)
        self.assertIn("Ответ", result)
        self.assertIn("√", result)
        self.assertIn("π/6", result)
        for raw in (r"\\(", r"\\)", r"\\frac", r"\\sqrt"):
            self.assertNotIn(raw, result)

    def test_plain_math_handles_fraction_superscripts_and_markup_characters(self):
        value = telegram_math(r"\\frac{a_b}{2} + x^{2} < y & z")
        self.assertEqual(value, "(a_b)/(2) + x² < y & z")

    async def test_duplicate_update_has_one_core_call_and_one_visible_result(self):
        self.client.submit_text_task.return_value = {"result": {
            "solution": "x = 1", "answer": "1", "defense_points": ["Подстановка"]}}
        await self.run_dispatch()
        await self.run_dispatch()
        self.client.submit_text_task.assert_called_once()
        self.assertEqual(self.message.reply_text.await_count, 1)

    async def test_core_conflict_does_not_emit_a_second_visible_result(self):
        self.client.submit_text_task.side_effect = BridgeError(409)
        await self.run_dispatch()
        self.assertEqual(self.message.reply_text.await_count, 0)

    async def test_uncertain_send_timeout_is_not_retried_as_a_second_message(self):
        self.client.submit_text_task.return_value = {"result": {
            "solution": "r=2", "answer": "2(cos(π/2)+i sin(π/2))",
            "defense_points": ["Модуль равен 2."]}}
        self.message.reply_text.side_effect = TimeoutError("synthetic send timeout")
        with self.assertRaises(TimeoutError):
            await dispatch(self.update, self.context)
        self.message.reply_text.side_effect = None
        await self.run_dispatch()
        self.client.submit_text_task.assert_called_once()
        self.assertEqual(self.message.reply_text.await_count, 1)

    async def test_photo_quote_confirm_selection_share_core(self):
        self.message.photo = [SimpleNamespace(file_size=100, file_id="fixture")]
        self.context.bot.get_file = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b"synthetic-jpeg")))
        self.client.quote_photo.return_value = {"quote_id": "synthetic-quote-id", "uses_trial": True, "credits": 0, "can_confirm": True}
        await self.run_dispatch()
        self.client.confirm_photo.assert_not_called()
        self.message.photo = None
        self.update.callback_query = SimpleNamespace(data="corephoto:synthetic-quote-id", answer=AsyncMock(), id="callback-1")
        self.client.confirm_photo.return_value = {"session_id": "synthetic-quote-id", "tasks": ["Задача 1", "Задача 2"], "expires_at": 9999999999}
        await self.run_dispatch()
        self.assertNotIn("core_pending_photo", self.context.user_data)
        self.update.callback_query.data = "coreselect:synthetic-quote-id:all"
        self.client.answer_photo.return_value = {"analysis": "Понимание", "how_to_defend": "Защита"}
        await self.run_dispatch()
        self.assertEqual(self.client.answer_photo.call_args.args[2], [0, 1])
        self.update.callback_query.data = "coreselect:other-session:all"
        await self.run_dispatch()
        self.assertEqual(self.client.answer_photo.call_count, 1)
