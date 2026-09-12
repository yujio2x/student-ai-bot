import unittest

from app.ai_service import INSTRUCTIONS, PRODUCT_CAPABILITIES
from app.bot import (
    GITHUB_URL,
    format_about_message,
    feedback_keyboard,
    format_funnel_message,
    format_start_message,
    is_photo_followup,
    markdown_to_telegram_html,
    split_message,
)
from app.database import FunnelStats


class SplitMessageTest(unittest.TestCase):
    def test_chunks_fit_telegram_limit(self) -> None:
        chunks = split_message(("строка\n" * 1200).strip(), limit=500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))

    def test_model_markdown_becomes_telegram_html(self) -> None:
        source = (
            "**Исправленный код** — готово\n```python\nx = 0  # исправлено\n```\n"
            "- `x` начинается с нуля"
        )
        rendered = markdown_to_telegram_html(source)
        self.assertIn("<b>Исправленный код</b>", rendered)
        self.assertIn('<pre><code class="language-python">', rendered)
        self.assertIn("x = 0  # исправлено", rendered)
        self.assertIn("• <code>x</code>", rendered)
        self.assertNotIn("```", rendered)
        self.assertNotIn("—", rendered)
        self.assertNotIn("–", rendered)

    def test_start_message_explains_text_and_photo_tasks(self) -> None:
        rendered = format_start_message()
        self.assertIn("Пришли текст или фото реальной учебной задачи", rendered)
        self.assertIn("Первая задача бесплатна", rendered)
        self.assertIn("Пошаговый разбор", rendered)
        self.assertIn("Проверка результата", rendered)
        self.assertIn("Объяснение для защиты", rendered)
        self.assertIn("/faq", rendered)
        self.assertIn("/balance", rendered)
        self.assertIn("/buy", rendered)
        self.assertNotIn("24", rendered)
        self.assertNotIn("100 Stars", rendered)
        self.assertNotIn("/newtask", rendered)
        self.assertNotIn("—", rendered)
        self.assertNotIn("–", rendered)

    def test_about_message_contains_public_project_details(self) -> None:
        rendered = format_about_message(42)
        self.assertIn("Версия:", rendered)
        self.assertIn("Yujio", rendered)
        self.assertIn("Решено задач:</b> 42", rendered)
        self.assertIn(GITHUB_URL, rendered)

    def test_product_prompt_does_not_promise_unavailable_features(self) -> None:
        self.assertIn("не создаёт изображения, DOCX, PPTX или PDF", PRODUCT_CAPABILITIES)
        self.assertIn("Никогда не придумывай", PRODUCT_CAPABILITIES)

    def test_ai_prompts_forbid_long_dashes(self) -> None:
        prompts = INSTRUCTIONS + PRODUCT_CAPABILITIES
        self.assertIn("U+2014", prompts)
        self.assertIn("U+2013", prompts)
        self.assertNotIn("—", prompts)
        self.assertNotIn("–", prompts)

    def test_answer_format_starts_with_structured_given_data(self) -> None:
        self.assertIn("1. **Дано**", INSTRUCTIONS)
        self.assertIn("исходные значения, условия, ограничения", INSTRUCTIONS)
        self.assertNotIn("Что пошло не так", INSTRUCTIONS)

    def test_clear_photo_followups_are_detected(self) -> None:
        self.assertTrue(is_photo_followup("Теперь реши задачу 2 и задачу 3"))
        self.assertTrue(is_photo_followup("Разбери 2 и 3 задачи"))
        self.assertTrue(is_photo_followup("Объясни вторую подробнее"))
        self.assertTrue(is_photo_followup("Продолжи разбор"))
        self.assertTrue(is_photo_followup("Объясни для защиты"))
        self.assertTrue(is_photo_followup("все это тест"))
        self.assertTrue(is_photo_followup("с этим тестом помоги"))
        self.assertTrue(is_photo_followup("реши весь тест"))
        self.assertTrue(is_photo_followup("все"))
        self.assertFalse(is_photo_followup("Привет, сколько стоит бот?"))

    def test_bare_ordinal_requires_task_context(self) -> None:
        # Short references to the current photo session stay follow-ups.
        self.assertTrue(is_photo_followup("второй"))
        self.assertTrue(is_photo_followup("второй и третий"))
        self.assertTrue(is_photo_followup("вторая задача"))
        self.assertTrue(is_photo_followup("задача вторая"))
        self.assertTrue(is_photo_followup("реши первую и вторую"))

    def test_new_task_with_ordinal_word_is_not_hijacked_into_photo_flow(self) -> None:
        self.assertFalse(is_photo_followup("Объясни второй закон Ньютона"))
        self.assertFalse(is_photo_followup("Второй закон Ньютона сформулируй и покажи пример"))
        self.assertFalse(is_photo_followup("Первое начало термодинамики сформулируй"))
        self.assertFalse(is_photo_followup("пятница"))
        self.assertFalse(is_photo_followup("вторник"))

    def test_feedback_keyboard_contains_compact_callbacks(self) -> None:
        keyboard = feedback_keyboard(123)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(callbacks, [
            "feedback:123:positive", "feedback:123:negative", "defense:123"
        ])

    def test_funnel_message_shows_zeros_and_conversions(self) -> None:
        stats = FunnelStats(10, 5, 4, 2, 1, 1, 2, 125, 3, 1)
        rendered = format_funnel_message(stats, 7)
        self.assertIn("Уникальные старты: <b>10</b>", rendered)
        self.assertIn("50.0% от стартов", rendered)
        self.assertIn("Заработано: <b>125 Stars</b>", rendered)
        empty = format_funnel_message(FunnelStats(*(0 for _ in range(10))), 1)
        self.assertIn("0%", empty)


if __name__ == "__main__":
    unittest.main()
