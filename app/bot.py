from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import re
import socket
from contextlib import suppress

from telegram import (
    BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
    LabeledPrice, Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
    PreCheckoutQueryHandler, filters,
)

from app.ai_service import AIService
from app.config import Settings, load_settings
from app.bridge_handlers import install as install_bridge, retry_loop as bridge_retry_loop
from app.database import AdminUser, DailyFunnelStats, Database, FunnelStats


logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs complete Telegram request URLs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
SINGLE_PAYLOAD = "task_help_1_v1"
PACK_PAYLOAD = "task_help_5_v1"
BOT_VERSION = "1.7.0"
FOUNDER_NAME = "Yujio (yujio-dev)"
GITHUB_URL = "https://github.com/yujio-dev/student-ai-bot"
PHOTO_PRICE_STARS = 100
PHOTO_CREDITS = 5
PHOTO_SESSION_HOURS = 24
SINGLE_INSTANCE_PORT = 38473
ADMIN_PAGE_SIZE = 5


def acquire_single_instance_lock() -> socket.socket:
    """Keep one local bot process so test runs cannot steal Telegram polling."""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        lock.listen(1)
    except OSError as exc:
        lock.close()
        raise RuntimeError(
            "Student AI Bot is already running. Use the background task instead of "
            "starting a second copy."
        ) from exc
    return lock


def markdown_to_telegram_html(text: str) -> str:
    """Convert the small Markdown subset requested from the model to Telegram HTML."""
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    result: list[str] = []
    code_lines: list[str] = []
    in_code = False
    language = ""

    def flush_code() -> None:
        nonlocal code_lines
        code = html.escape("\n".join(code_lines))
        language_class = f' class="language-{html.escape(language)}"' if language else ""
        result.append(f"<pre><code{language_class}>{code}</code></pre>")
        code_lines = []

    for raw_line in text.strip().splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
                language = ""
            else:
                in_code = True
                language = stripped[3:].strip().lower()
            continue
        if in_code:
            code_lines.append(raw_line)
            continue

        line = html.escape(raw_line)
        line = re.sub(r"^#{1,4}\s+(.+)$", r"<b>\1</b>", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", line)
        line = re.sub(r"^\s*-\s+", "• ", line)
        result.append(line)

    if in_code:
        flush_code()
    return "\n".join(result)


def split_message(text: str, limit: int = 4000) -> list[str]:
    parts: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


_ORDINAL_STEMS = r"(?:перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)"
_TASK_NOUNS = r"(?:задач|пункт|номер|вопрос|пример|вариант|задани|тест)"


def is_photo_followup(text: str) -> bool:
    """Recognize clear references to tasks from the current photo session."""
    normalized = text.casefold().strip()
    references = (
        r"\bзадач[а-я]*\s*(?:№\s*)?\d+",
        r"\b\d+(?:\s*(?:,|и)\s*\d+)*\s+задач[а-я]*\b",
        r"\bпункт[а-я]*\s*(?:№\s*)?\d+",
        r"\bномер[а-я]*\s*\d+",
        r"\b(?:реши|решить|разбери|разобрать|объясни|сделай)\s+(?:задач[а-я]*\s*)?\d+",
        # A bare ordinal anywhere in the text would hijack new paid tasks like
        # "второй закон Ньютона" into the free photo flow, so require context:
        # a task noun, a task verb at the end, or a standalone short reply.
        rf"\b{_TASK_NOUNS}[а-я]*\s+(?:№\s*)?{_ORDINAL_STEMS}[а-я]*\b",
        rf"\b{_ORDINAL_STEMS}[а-я]*\s+{_TASK_NOUNS}[а-я]*\b",
        rf"\b(?:реши|решить|разбери|разобрать|объясни|объяснить|проверь|перепроверь|сделай|покажи)"
        rf"\s+(?:мне\s+)?(?:ещё\s+)?(?:задач[а-я]*\s+)?(?:№\s*)?{_ORDINAL_STEMS}[а-я]*"
        rf"(?:\s+и\s+{_ORDINAL_STEMS}[а-я]*)?"
        rf"(?:\s+(?:подробнее|попроще|ещё|снова|пожалуйста|полностью|сначала))*[.!,?\s]*$",
        rf"^(?:ну\s+)?(?!вторник\b|пятница\b){_ORDINAL_STEMS}[а-я]*(?:\s+и\s+\S+)?[.!,?\s]*$",
        r"\b(?:продолж|подробн|попроще|перепроверь|проверь ещё|объясни ещё)[а-я]*\b",
        r"\b(?:для|к|на)\s+защит[а-я]*\b",
        r"\b(?:все|всё|весь|всю)\s+(?:это|этот|эту|задани[ея]|задач[иу]|тест)\b",
        r"\b(?:реши|решить|разбери|разобрать|объясни|сделай|помоги)\s+"
        r"(?:со?\s+)?(?:все(?:ми)?|всё|весь|этим|этот|тест)[а-я]*\b",
        r"\b(?:с|со)\s+(?:этим|всем)\s+(?:тестом|заданием|задачами)\b",
        r"^(?:все|всё|весь тест|все задания|все задачи)[.!?\s]*$",
    )
    return any(re.search(pattern, normalized) for pattern in references)


def format_about_message(solved_tasks: int) -> str:
    return (
        "<b>О Student AI Bot</b>\n\n"
        "Помогаю разбирать учебные задачи по шагам, проверять результат и готовить "
        "понятное объяснение для защиты.\n\n"
        f"<b>Версия:</b> {BOT_VERSION}\n"
        f"<b>Основатель:</b> {FOUNDER_NAME}\n"
        f"<b>Решено задач:</b> {solved_tasks}\n"
        f'<b>Открытый код:</b> <a href="{GITHUB_URL}">GitHub</a>'
    )


def format_start_message() -> str:
    return (
        "Пришли текст или фото реальной учебной задачи. Я разберу решение по шагам, "
        "проверю результат и помогу подготовить объяснение для защиты.\n\n"
        "Первая задача бесплатна.\n\n"
        "• Пошаговый разбор\n"
        "• Проверка результата\n"
        "• Объяснение для защиты\n\n"
        "Подробнее: /faq  •  Баланс: /balance  •  Купить: /buy"
    )


def conversion_percent(current: int, previous: int) -> str:
    return "0%" if previous <= 0 else f"{current / previous * 100:.1f}%"


def format_funnel_message(stats: FunnelStats, days: int) -> str:
    return (
        f"<b>Воронка за последние {days} дн.</b>\n\n"
        f"Уникальные старты: <b>{stats.starts}</b>\n"
        f"Отправили задачу: <b>{stats.task_submitters}</b> "
        f"({conversion_percent(stats.task_submitters, stats.starts)} от стартов)\n"
        f"Получили ответ: <b>{stats.answer_users}</b> "
        f"({conversion_percent(stats.answer_users, stats.task_submitters)} от задач)\n"
        f"Открыли покупку: <b>{stats.buy_users}</b> "
        f"({conversion_percent(stats.buy_users, stats.answer_users)} от ответов)\n"
        f"Запросили инвойс: <b>{stats.invoice_users}</b> "
        f"({conversion_percent(stats.invoice_users, stats.buy_users)} от покупок)\n"
        f"Уникальные покупатели: <b>{stats.buyers}</b> "
        f"({conversion_percent(stats.buyers, stats.invoice_users)} от инвойсов)\n\n"
        f"Платежей: <b>{stats.payments}</b>\n"
        f"Заработано: <b>{stats.stars} Stars</b>\n"
        f"Отзывы: 👍 {stats.feedback_positive}  👎 {stats.feedback_negative}"
    )


def build_funnel_csv(rows: list[DailyFunnelStats]) -> bytes:
    """Build a privacy-safe UTF-8 CSV for spreadsheet import."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "date_utc", "starts", "task_submitters", "answer_users", "buy_users",
        "invoice_users", "buyers", "payments", "stars", "feedback_positive",
        "feedback_negative", "task_from_start_pct", "answer_from_task_pct",
        "buy_from_answer_pct", "invoice_from_buy_pct", "buyer_from_invoice_pct",
    ])

    def percent(current: int, previous: int) -> float:
        return round(current / previous * 100, 1) if previous else 0.0

    for row in rows:
        writer.writerow([
            row.date_utc, row.starts, row.task_submitters, row.answer_users, row.buy_users,
            row.invoice_users, row.buyers, row.payments, row.stars,
            row.feedback_positive, row.feedback_negative,
            percent(row.task_submitters, row.starts),
            percent(row.answer_users, row.task_submitters),
            percent(row.buy_users, row.answer_users),
            percent(row.invoice_users, row.buy_users),
            percent(row.buyers, row.invoice_users),
        ])
    return output.getvalue().encode("utf-8-sig")


def feedback_keyboard(request_id: int, feedback_enabled: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if feedback_enabled:
        rows.append([
            InlineKeyboardButton("👍 Помогло", callback_data=f"feedback:{request_id}:positive"),
            InlineKeyboardButton("👎 Не помогло", callback_data=f"feedback:{request_id}:negative"),
        ])
    rows.append([InlineKeyboardButton(
        "🎓 Объяснение для защиты", callback_data=f"defense:{request_id}"
    )])
    return InlineKeyboardMarkup(rows)


def photo_paid_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"Подтвердить - {PHOTO_CREDITS} попыток", callback_data="photo:confirm"
        )],
        [InlineKeyboardButton("Отмена", callback_data="photo:cancel")],
    ])


def remember_answer_context(
    context: ContextTypes.DEFAULT_TYPE, request_id: int, task_context: str, answer_text: str,
) -> None:
    contexts = context.user_data.setdefault("answer_contexts", {})
    contexts[str(request_id)] = {
        "task": task_context[:12000],
        "answer": answer_text[:24000],
        "defense": None,
    }
    while len(contexts) > 5:
        contexts.pop(next(iter(contexts)))


async def send_completed_answer(
    message, context: ContextTypes.DEFAULT_TYPE, telegram_id: int, request_id: int,
    answer_text: str, task_context: str, show_trial_cta: bool = False,
) -> None:
    remember_answer_context(context, request_id, task_context, answer_text)
    parts = split_message(answer_text, limit=3400)
    for index, part in enumerate(parts):
        await message.reply_text(
            markdown_to_telegram_html(part),
            parse_mode="HTML",
            reply_markup=feedback_keyboard(request_id) if index == len(parts) - 1 else None,
        )
    _, db, _ = services(context)
    db.log_event(telegram_id, "answer_completed")
    if show_trial_cta:
        settings, _, _ = services(context)
        try:
            await message.reply_text(
                "Если понадобится разобрать следующую задачу: "
                f"1 разбор - {settings.single_price_stars} Stars, пакет из "
                f"{settings.pack_credits} - {settings.pack_price_stars} Stars.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Купить следующий разбор", callback_data="buy:open")
                ]]),
            )
        except Exception:
            logger.exception("Could not send post-trial purchase CTA to user %s", telegram_id)


def services(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, Database, AIService]:
    data = context.application.bot_data
    return data["settings"], data["db"], data["ai"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db, _ = services(context)
    user = update.effective_user
    is_new_user = db.ensure_user(user.id, user.username)
    db.log_event(user.id, "start")
    if is_new_user and context.args and context.args[0].startswith("ref_"):
        if db.attach_referral(user.id, context.args[0][4:].upper()):
            db.log_event(user.id, "referral_attached", context.args[0][4:].upper())
    await update.message.reply_text(format_start_message())


async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = services(context)
    await update.message.reply_text(
        "Частые вопросы\n\n"
        "Что умеет бот?\n"
        "Разбирает текстовые учебные задачи по разным предметам, объясняет ход решения, "
        "помогает проверить результат и подготовиться к защите.\n\n"
        "Что расходует попытку?\n"
        "Первая распознанная учебная задача бесплатна независимо от того, отправлена она "
        "текстом или фотографией. Это одна общая попытка. Приветствия и вопросы о самом "
        "боте попытку не списывают.\n\n"
        "Сколько стоит?\n"
        f"Первый разбор бесплатный. Один дополнительный разбор стоит "
        f"{settings.single_price_stars} Stars. Выгодный пакет: {settings.pack_credits} "
        f"разборов за {settings.pack_price_stars} Stars вместо "
        f"{settings.single_price_stars * settings.pack_credits}. Купить: /buy\n\n"
        "Какие ограничения?\n"
        "Текст - до 6000 символов. Можно отправить одну фотографию задачи; перед обработкой "
        "бот попросит подтверждение. Первая общая попытка делает фоторазбор бесплатным. После "
        f"её использования фоторазбор стоит {PHOTO_PRICE_STARS} Stars или списывает "
        f"{PHOTO_CREDITS} оплаченных попыток. PDF и другие файлы пока не "
        f"принимаются. Распознанные условия фото хранятся {PHOTO_SESSION_HOURS} часа; дальнейшие "
        "просьбы решить другие номера с этого фото не требуют повторной оплаты. Новый контекст: "
        "/newtask. Ответ AI стоит перепроверять.\n\n"
        "Где посмотреть остаток?\n/balance\n\n"
        "Где узнать больше о проекте?\n/about"
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db, _ = services(context)
    await update.message.reply_text(
        format_about_message(db.solved_tasks_count()),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db, _ = services(context)
    if db.has_unlimited_access(update.effective_user.id):
        await update.message.reply_text("Безлимитный доступ: активен. Попытки не списываются.")
        return
    trial_available, credits = db.balance(update.effective_user.id)
    trial = "доступен" if trial_available else "использован"
    await update.message.reply_text(f"Бесплатный разбор: {trial}. Оплаченных разборов: {credits}.")


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    db.log_event(update.effective_user.id, "buy_opened")
    regular_pack_price = settings.single_price_stars * settings.pack_credits
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"1 разбор - {settings.single_price_stars} ⭐", callback_data="buy:1"
        )],
        [InlineKeyboardButton(
            f"{settings.pack_credits} разборов - {settings.pack_price_stars} ⭐ (вместо {regular_pack_price})",
            callback_data="buy:5",
        )],
    ])
    await update.effective_message.reply_text(
        "Выбери вариант",
        reply_markup=keyboard,
    )


async def send_product_invoice(
    update: Update, context: ContextTypes.DEFAULT_TYPE, product: str
) -> None:
    settings, db, _ = services(context)
    if product == "1":
        credits, stars, payload = 1, settings.single_price_stars, SINGLE_PAYLOAD
        title = "1 разбор учебной задачи"
        description = "Пошаговое решение, объяснение и проверка результата."
    else:
        credits, stars, payload = settings.pack_credits, settings.pack_price_stars, PACK_PAYLOAD
        title = f"{credits} разборов - выгодный пакет"
        description = (
            f"По одному: {settings.single_price_stars * credits} Stars. "
            f"Цена пакета: {stars} Stars."
        )
    db.log_event(update.effective_user.id, "invoice_requested", product)
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(f"{credits} разборов", stars)],
        start_parameter=f"task-help-{credits}",
    )


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    product = query.data.split(":", 1)[1]
    if product == "open":
        await buy(update, context)
    else:
        await send_product_invoice(update, context, product)


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = services(context)
    query = update.pre_checkout_query
    expected = {
        SINGLE_PAYLOAD: settings.single_price_stars,
        PACK_PAYLOAD: settings.pack_price_stars,
    }
    valid = (query.currency == "XTR"
             and expected.get(query.invoice_payload) == query.total_amount)
    await query.answer(ok=valid, error_message=None if valid else "Цена изменилась. Открой /buy ещё раз.")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    payment = update.message.successful_payment
    products = {
        SINGLE_PAYLOAD: (1, settings.single_price_stars),
        PACK_PAYLOAD: (settings.pack_credits, settings.pack_price_stars),
    }
    product = products.get(payment.invoice_payload)
    if (not product or payment.currency != "XTR"
            or payment.total_amount != product[1]):
        logger.error("Unexpected payment payload for user %s", update.effective_user.id)
        return
    credits = product[0]
    result = db.add_payment(
        update.effective_user.id, payment.telegram_payment_charge_id,
        payment.total_amount, credits, settings.referral_reward_credits,
    )
    if result.added:
        db.log_event(update.effective_user.id, "payment_completed", payment.invoice_payload)
        await update.message.reply_text(
            f"Оплата получена. Добавлено разборов: {credits}. Пришли учебную задачу."
        )
        if result.rewarded_referrer_id:
            try:
                await context.bot.send_message(
                    result.rewarded_referrer_id,
                    f"Твой приглашённый друг сделал первую покупку. Начислено бесплатных разборов: "
                    f"{settings.referral_reward_credits}.",
                )
            except Exception:
                logger.exception("Could not notify referrer %s", result.rewarded_referrer_id)


async def funnel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    if update.effective_user.id != settings.owner_telegram_id:
        await update.message.reply_text("Команда доступна только владельцу бота.")
        return
    if len(context.args) > 1 or (context.args and context.args[0] not in {"1", "7", "30"}):
        await update.message.reply_text("Формат: /funnel 1, /funnel 7 или /funnel 30")
        return
    days = int(context.args[0]) if context.args else 7
    await update.message.reply_text(
        format_funnel_message(db.funnel_stats(days), days), parse_mode="HTML"
    )


async def funnel_csv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    if update.effective_user.id != settings.owner_telegram_id:
        await update.message.reply_text("Команда доступна только владельцу бота.")
        return
    if len(context.args) > 1 or (context.args and context.args[0] not in {"1", "7", "30"}):
        await update.message.reply_text("Формат: /funnelcsv 1, /funnelcsv 7 или /funnelcsv 30")
        return
    days = int(context.args[0]) if context.args else 30
    filename = f"taskmentor-funnel-{days}d.csv"
    document = InputFile(io.BytesIO(build_funnel_csv(db.daily_funnel_stats(days))), filename)
    await update.message.reply_document(
        document=document,
        caption=(
            f"Воронка по дням за {days} дн. (UTC). Без Telegram ID, имён и текстов задач. "
            "Импортируй файл в лист «Факт CSV» в Excel или Google Sheets."
        ),
    )


async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    user = update.effective_user
    code = db.personal_referral_code(user.id, user.username)
    link = f"https://t.me/{context.bot.username}?start=ref_{code}"
    stats = db.referral_stats(user.id)[0]
    await update.message.reply_text(
        "<b>Твоя реферальная ссылка</b>\n"
        f"<code>{html.escape(link)}</code>\n\n"
        f"За первую покупку приглашённого ты получишь "
        f"<b>{settings.referral_reward_credits} бесплатный разбор</b>.\n"
        f"Переходов с запуском бота: {stats.joins}\n"
        f"Покупателей: {stats.buyers}",
        parse_mode="HTML",
    )


def normalize_partner_code(label: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9_]", "_", label.upper()).strip("_")
    return f"P_{cleaned[:24]}" if cleaned else ""


async def partner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    if update.effective_user.id != settings.owner_telegram_id:
        await update.message.reply_text("Команда доступна только владельцу бота.")
        return
    if not context.args:
        await update.message.reply_text("Формат: /partner имя_партнёра")
        return
    label = " ".join(context.args).strip()
    code = normalize_partner_code(label)
    if not code or not db.create_cash_referral(code, label):
        await update.message.reply_text("Не удалось создать код. Выбери другое имя.")
        return
    link = f"https://t.me/{context.bot.username}?start=ref_{code}"
    await update.message.reply_text(
        f"Денежная партнёрская ссылка для <b>{html.escape(label)}</b>:\n"
        f"<code>{html.escape(link)}</code>\n\n"
        "Статистика появится в /refstats. Выплата рассчитывается вручную.",
        parse_mode="HTML",
    )


async def refstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    if update.effective_user.id == settings.owner_telegram_id:
        stats = db.referral_stats()
    else:
        db.personal_referral_code(update.effective_user.id, update.effective_user.username)
        stats = db.referral_stats(update.effective_user.id)
    if not stats:
        await update.message.reply_text("Реферальных ссылок пока нет.")
        return
    lines = ["<b>Статистика рефералов</b>"]
    for item in stats:
        kind = "деньги" if item.kind == "cash" else "бесплатные разборы"
        lines.append(
            f"\n<b>{html.escape(item.label)}</b> - {kind}\n"
            f"Код: <code>{item.code}</code>\n"
            f"Запустили бота: {item.joins}\n"
            f"Покупателей: {item.buyers}\n"
            f"Платежей: {item.payments}\n"
            f"Выручка: {item.stars} Stars"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Статистика", callback_data="admin:home"),
            InlineKeyboardButton("👥 Пользователи", callback_data="admin:allusers:0"),
        ],
        [
            InlineKeyboardButton("💳 Платежи", callback_data="admin:payments:0"),
            InlineKeyboardButton("🤝 Партнёры", callback_data="admin:partners:0"),
        ],
        [InlineKeyboardButton("🧾 Журнал действий", callback_data="admin:audit:0")],
    ])


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:home")
    ]])


def admin_overview_text(db: Database) -> str:
    overview = db.admin_overview()
    today = db.funnel_stats(1)
    week = db.funnel_stats(7)
    return (
        "<b>Админ-панель TaskMentor</b>\n\n"
        "<b>Сегодня / 7 дней</b>\n"
        f"Запуски: <b>{today.starts}</b> / {week.starts}\n"
        f"Получили ответ: <b>{today.answer_users}</b> / {week.answer_users}\n"
        f"Покупатели: <b>{today.buyers}</b> / {week.buyers}\n"
        f"Stars: <b>{today.stars}</b> / {week.stars}\n\n"
        "<b>За всё время</b>\n"
        f"Пользователи: <b>{overview.total_users}</b>\n"
        f"Использовали пробный разбор: <b>{overview.trial_users}</b>\n"
        f"Платящие: <b>{overview.paying_users}</b>\n"
        f"Безлимит: <b>{overview.unlimited_users}</b>\n"
        f"Успешные ответы: <b>{overview.completed_requests}</b>\n"
        f"Ошибки: <b>{overview.failed_requests}</b>\n"
        f"Платежи: <b>{overview.payments}</b> на <b>{overview.stars} Stars</b>\n"
        f"Расчётные расходы AI: <b>${overview.estimated_cost_usd:.4f}</b>\n"
        f"Токены: {overview.input_tokens:,} вход / {overview.output_tokens:,} выход"
    )


def admin_user_text(user: AdminUser) -> str:
    username = f"@{html.escape(user.username)}" if user.username else "без username"
    trial = "доступна" if user.trial_available else "использована"
    unlimited = "включён" if user.unlimited else "выключен"
    return (
        f"<b>Пользователь {username}</b>\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Регистрация: {html.escape(user.created_at)} UTC\n\n"
        f"Бесплатная попытка: <b>{trial}</b>\n"
        f"Кредиты: <b>{user.credits}</b>\n"
        f"Безлимит: <b>{unlimited}</b>\n\n"
        f"Успешные запросы: {user.completed_requests}\n"
        f"Ошибки: {user.failed_requests}\n"
        f"Платежи: {user.payments} на {user.stars} Stars\n"
        f"Расходы AI: ${user.estimated_cost_usd:.4f}"
    )


def admin_user_keyboard(user: AdminUser) -> InlineKeyboardMarkup:
    unlimited_label = "🚫 Убрать безлимит" if user.unlimited else "♾ Выдать безлимит"
    unlimited_value = 0 if user.unlimited else 1
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("+1", callback_data=f"admin:credits:{user.telegram_id}:p1"),
            InlineKeyboardButton("+5", callback_data=f"admin:credits:{user.telegram_id}:p5"),
            InlineKeyboardButton("−1", callback_data=f"admin:credits:{user.telegram_id}:m1"),
        ],
        [InlineKeyboardButton(
            unlimited_label,
            callback_data=f"admin:unlimited:{user.telegram_id}:{unlimited_value}",
        )],
        [InlineKeyboardButton(
            "🎁 Восстановить бесплатную попытку",
            callback_data=f"admin:trial:{user.telegram_id}",
        )],
        [InlineKeyboardButton("⬅️ К пользователям", callback_data="admin:users:0")],
    ])


def pagination_row(prefix: str, page: int, total: int) -> list[InlineKeyboardButton]:
    pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️", callback_data=f"admin:{prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="admin:noop"))
    if page + 1 < pages:
        row.append(InlineKeyboardButton("➡️", callback_data=f"admin:{prefix}:{page + 1}"))
    return row


def admin_users_view(db: Database, query: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    users, total = db.admin_users(query, ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE)
    title = f"Результаты поиска: <b>{html.escape(query)}</b>" if query else "<b>Пользователи</b>"
    lines = [title, f"Найдено: {total}"]
    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        name = f"@{user.username}" if user.username else str(user.telegram_id)
        flags = " ♾" if user.unlimited else ""
        lines.append(f"\n{name} · {user.credits} кр. · {user.stars} Stars{flags}")
        rows.append([InlineKeyboardButton(
            f"{name} · {user.credits} кр.", callback_data=f"admin:user:{user.telegram_id}"
        )])
    if not users:
        lines.append("\nСовпадений нет.")
    rows.append(pagination_row("users", page, total))
    rows.append([InlineKeyboardButton("🔎 Найти пользователя", callback_data="admin:search")])
    if query:
        rows.append([InlineKeyboardButton("Показать всех", callback_data="admin:allusers:0")])
    rows.append([InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    if update.effective_user.id != settings.owner_telegram_id:
        await update.message.reply_text("Команда недоступна.")
        return
    context.user_data.pop("admin_search_pending", None)
    await update.message.reply_text(
        admin_overview_text(db), parse_mode="HTML", reply_markup=admin_menu_keyboard()
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings, db, _ = services(context)
    if update.effective_user.id != settings.owner_telegram_id:
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    if action == "noop":
        return
    if action == "home":
        context.user_data.pop("admin_search_pending", None)
        await query.edit_message_text(
            admin_overview_text(db), parse_mode="HTML", reply_markup=admin_menu_keyboard()
        )
        return
    if action == "search":
        context.user_data["admin_search_pending"] = True
        await query.edit_message_text(
            "<b>Поиск пользователя</b>\n\nОтправьте @username или цифровой Telegram ID.",
            parse_mode="HTML", reply_markup=admin_back_keyboard(),
        )
        return
    if action in {"users", "allusers"}:
        if action == "allusers":
            context.user_data.pop("admin_search_query", None)
        page = max(0, int(parts[2]))
        text, keyboard = admin_users_view(
            db, context.user_data.get("admin_search_query", ""), page
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return
    if action == "user":
        user = db.admin_user(int(parts[2]))
        if not user:
            await query.edit_message_text("Пользователь не найден.", reply_markup=admin_back_keyboard())
            return
        await query.edit_message_text(
            admin_user_text(user), parse_mode="HTML", reply_markup=admin_user_keyboard(user)
        )
        return
    if action == "credits":
        telegram_id = int(parts[2])
        delta = {"p1": 1, "p5": 5, "m1": -1}[parts[3]]
        balance = db.admin_adjust_credits(settings.owner_telegram_id, telegram_id, delta)
        if balance is None:
            user = db.admin_user(telegram_id)
            message = "Недостаточно кредитов для списания." if user else "Пользователь не найден."
            await query.edit_message_text(
                message, reply_markup=admin_user_keyboard(user) if user else admin_back_keyboard()
            )
            return
        user = db.admin_user(telegram_id)
        await query.edit_message_text(
            admin_user_text(user), parse_mode="HTML", reply_markup=admin_user_keyboard(user)
        )
        return
    if action == "unlimited":
        telegram_id, enabled = int(parts[2]), bool(int(parts[3]))
        if not db.admin_set_unlimited(settings.owner_telegram_id, telegram_id, enabled):
            await query.edit_message_text("Пользователь не найден.", reply_markup=admin_back_keyboard())
            return
        user = db.admin_user(telegram_id)
        await query.edit_message_text(
            admin_user_text(user), parse_mode="HTML", reply_markup=admin_user_keyboard(user)
        )
        return
    if action == "trial":
        telegram_id = int(parts[2])
        if not db.admin_reset_trial(settings.owner_telegram_id, telegram_id):
            await query.edit_message_text("Пользователь не найден.", reply_markup=admin_back_keyboard())
            return
        user = db.admin_user(telegram_id)
        await query.edit_message_text(
            admin_user_text(user), parse_mode="HTML", reply_markup=admin_user_keyboard(user)
        )
        return
    if action == "payments":
        page = max(0, int(parts[2]))
        payments, total = db.admin_payments(ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE)
        lines = ["<b>Платежи</b>", f"Всего: {total}"]
        for payment in payments:
            name = f"@{html.escape(payment.username)}" if payment.username else str(payment.telegram_id)
            lines.append(
                f"\n<b>{payment.stars} Stars</b> · {payment.credits} кр.\n"
                f"{name} · <code>{payment.telegram_id}</code>\n{payment.created_at} UTC"
            )
        keyboard = InlineKeyboardMarkup([
            pagination_row("payments", page, total),
            [InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:home")],
        ])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
        return
    if action == "partners":
        page = max(0, int(parts[2]))
        all_stats = db.referral_stats()
        items = all_stats[page * ADMIN_PAGE_SIZE:(page + 1) * ADMIN_PAGE_SIZE]
        lines = ["<b>Партнёры и рефералы</b>", f"Всего ссылок: {len(all_stats)}"]
        for item in items:
            kind = "партнёр" if item.kind == "cash" else "реферал"
            lines.append(
                f"\n<b>{html.escape(item.label)}</b> · {kind}\n"
                f"<code>{item.code}</code> · запусков {item.joins} · покупателей {item.buyers}\n"
                f"{item.payments} платежей · {item.stars} Stars"
            )
        lines.append("\nСоздать денежную ссылку: /partner имя")
        keyboard = InlineKeyboardMarkup([
            pagination_row("partners", page, len(all_stats)),
            [InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:home")],
        ])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
        return
    if action == "audit":
        page = max(0, int(parts[2]))
        actions, total = db.admin_actions(ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE)
        labels = {
            "credits_adjusted": "Изменены кредиты",
            "unlimited_changed": "Изменён безлимит",
            "trial_reset": "Восстановлена попытка",
        }
        lines = ["<b>Журнал действий</b>", f"Всего записей: {total}"]
        for item in actions:
            lines.append(
                f"\n<b>{labels.get(item.action, html.escape(item.action))}</b>\n"
                f"Пользователь: <code>{item.target_telegram_id}</code>\n"
                f"{html.escape(item.details) or 'без дополнительных данных'}\n{item.created_at} UTC"
            )
        keyboard = InlineKeyboardMarkup([
            pagination_row("audit", page, total),
            [InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:home")],
        ])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


async def admin_or_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db, _ = services(context)
    if (update.effective_user.id == settings.owner_telegram_id
            and context.user_data.pop("admin_search_pending", False)):
        query = (update.message.text or "").strip()[:64]
        context.user_data["admin_search_query"] = query
        text, keyboard = admin_users_view(db, query, 0)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        return
    await handle_question(update, context)


async def grant_reactivation_bonuses(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    db: Database = application.bot_data["db"]
    candidates = db.reactivation_candidates(settings.reactivation_days)
    for telegram_id in candidates:
        if not db.grant_reactivation_bonus(telegram_id, settings.reactivation_credits):
            continue
        try:
            await application.bot.send_message(
                telegram_id,
                "Давно не виделись 👋\n\n"
                f"Мы начислили тебе {settings.reactivation_credits} бесплатных разбора, "
                "чтобы ты мог попробовать бота ещё раз. Просто пришли следующую учебную задачу.",
            )
        except Exception:
            # The credits remain available if the user later returns or unblocks the bot.
            logger.exception("Could not send reactivation message to user %s", telegram_id)


async def reactivation_loop(application: Application) -> None:
    while True:
        try:
            await grant_reactivation_bonuses(application)
        except Exception:
            logger.exception("Reactivation scan failed")
        await asyncio.sleep(60 * 60)


async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот помогает разбирать учебные задачи. Ответы AI могут содержать "
        "ошибки - проверяй код перед сдачей. Оплата даёт указанное число разборов. "
        "При технической ошибке кредит возвращается. Возврат Stars по спорной покупке "
        "рассматривается через /paysupport. Не отправляй персональные данные и секреты."
    )


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = services(context)
    text = (f"По вопросам оплаты и работы бота: @{settings.support_username}"
            if settings.support_username else
            "Контакт поддержки пока не настроен. Владелец должен заполнить SUPPORT_USERNAME в .env.")
    await update.message.reply_text(text)


async def new_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db, _ = services(context)
    context.user_data.pop("pending_photo", None)
    removed = db.clear_photo_session(update.effective_user.id)
    await update.message.reply_text(
        "Контекст предыдущей фотографии удалён. Пришли новую задачу текстом или фотографией."
        if removed else
        "Активной фотографии в памяти нет. Пришли новую задачу текстом или фотографией."
    )


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db, ai = services(context)
    text = (update.message.text or "").strip()
    if not text:
        return
    if len(text) > 6000:
        await update.message.reply_text("Сейчас лимит - 6000 символов. Оставь условие и проблемный код.")
        return
    user = update.effective_user
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    session = db.photo_session(user.id, PHOTO_SESSION_HOURS)
    if session and is_photo_followup(text):
        try:
            answer = await asyncio.to_thread(
                ai.answer_photo_session, session.recognized_tasks, text, session.last_request
            )
            db.touch_photo_session(user.id, text)
            request_id = db.log_request(
                user.id, "photo_followup", answer.input_tokens, answer.output_tokens,
                answer.estimated_cost_usd, "completed",
            )
            await send_completed_answer(
                update.message, context, user.id, request_id, answer.text,
                f"{session.recognized_tasks}\n\nЗапрос: {text}",
                show_trial_cta=session.access_source == "trial" and not session.last_request,
            )
        except Exception:
            logger.exception("Photo follow-up failed for user %s", user.id)
            db.log_request(user.id, "photo_followup", 0, 0, 0, "failed")
            db.log_event(user.id, "answer_failed", "photo_followup")
            await update.message.reply_text(
                "Не удалось продолжить разбор. Фото осталось в памяти - попробуй ещё раз."
            )
        return

    try:
        route = await asyncio.to_thread(ai.route, text)
    except Exception:
        logger.exception("Routing request failed for user %s", user.id)
        db.log_event(user.id, "answer_failed", "routing")
        await update.message.reply_text("Не удалось распознать сообщение. Попробуй ещё раз позже.")
        return

    if not route.is_task:
        db.log_request(user.id, "free_chat", route.input_tokens, route.output_tokens,
                       route.estimated_cost_usd, "completed")
        await update.message.reply_text(markdown_to_telegram_html(route.reply), parse_mode="HTML")
        return

    db.log_event(user.id, "text_task_submitted")
    access = db.claim_access(user.id, user.username)
    if not access.allowed:
        db.log_request(user.id, "routing", route.input_tokens, route.output_tokens,
                       route.estimated_cost_usd, "unpaid")
        await update.message.reply_text("Бесплатный разбор уже использован. Пакет доступен по команде /buy.")
        await buy(update, context)
        return
    try:
        answer = await asyncio.to_thread(ai.answer, text)
        request_id = db.log_request(
            user.id, access.source,
            route.input_tokens + answer.input_tokens,
            route.output_tokens + answer.output_tokens,
            route.estimated_cost_usd + answer.estimated_cost_usd,
            "completed",
        )
        await send_completed_answer(
            update.message, context, user.id, request_id, answer.text, text,
            show_trial_cta=access.source == "trial",
        )
    except Exception:
        logger.exception("AI request failed for user %s", user.id)
        db.restore_access(user.id, access.source, access.credits_charged)
        db.log_request(user.id, access.source, 0, 0, 0, "failed")
        db.log_event(user.id, "answer_failed", access.source)
        await update.message.reply_text(
            "Не удалось получить ответ. Бесплатный разбор или кредит возвращён - попробуй позже."
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db, _ = services(context)
    user = update.effective_user
    photo = update.message.photo[-1]
    if photo.file_size and photo.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("Фото слишком большое. Пришли изображение до 10 МБ.")
        return
    db.log_event(user.id, "photo_submitted")
    trial_available, _ = db.balance(user.id)
    unlimited = db.has_unlimited_access(user.id)
    context.user_data["pending_photo"] = {
        "file_id": photo.file_id,
        "caption": (update.message.caption or "").strip()[:2000],
        "trial_offer": trial_available or unlimited,
    }
    if trial_available or unlimited:
        label = "Подтвердить" if unlimited else "Подтвердить бесплатный разбор"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data="photo:confirm")],
            [InlineKeyboardButton("Отмена", callback_data="photo:cancel")],
        ])
        offer_text = (
            "Для безлимитного доступа фоторазбор не списывает попытки."
            if unlimited else
            "Этот первый фоторазбор бесплатный. После подтверждения будет использована "
            "твоя единая бесплатная попытка."
        )
        await update.message.reply_text(
            f"{offer_text}\n\n"
            f"Распознанные условия сохранятся на {PHOTO_SESSION_HOURS} часа, поэтому другие "
            "задачи с этого же фото можно будет разобрать без повторного списания.",
            reply_markup=keyboard,
        )
    else:
        db.log_event(user.id, "photo_price_shown", "paid")
        await update.message.reply_text(
            "Фоторазбор стоит 100 Telegram Stars или списывает "
            f"{PHOTO_CREDITS} оплаченных попыток.\n\n"
            f"Распознанные условия сохранятся на {PHOTO_SESSION_HOURS} часа. Другие задачи "
            "с этого же фото можно будет разобрать без повторной оплаты.",
            reply_markup=photo_paid_keyboard(),
        )


async def photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "photo:cancel":
        pending = context.user_data.pop("pending_photo", None)
        if pending:
            _, db, _ = services(context)
            db.log_event(update.effective_user.id, "photo_cancelled")
        await query.edit_message_text("Фоторазбор отменён. Попытки не списаны.")
        return

    pending = context.user_data.pop("pending_photo", None)
    if not pending:
        await query.edit_message_text("Фото больше не доступно. Пришли его ещё раз.")
        return

    _, db, ai = services(context)
    user = update.effective_user
    offer_source = "trial" if pending.get("trial_offer") else "paid"
    db.log_event(user.id, "photo_confirmed", offer_source)
    access = (
        db.claim_trial_access(user.id, user.username)
        if pending.get("trial_offer") else
        db.claim_paid_credits(user.id, user.username, PHOTO_CREDITS, "photo_paid")
    )
    if not access.allowed:
        if pending.get("trial_offer"):
            pending["trial_offer"] = False
            context.user_data["pending_photo"] = pending
            db.log_event(user.id, "photo_price_shown", "trial_already_used")
            await query.edit_message_text(
                "Бесплатная попытка уже использована. Фоторазбор стоит 100 Stars или "
                f"{PHOTO_CREDITS} оплаченных попыток.",
                reply_markup=photo_paid_keyboard(),
            )
            return
        _, credits = db.balance(user.id)
        await query.edit_message_text(
            f"Для фоторазбора нужно {PHOTO_CREDITS} оплаченных попыток, сейчас доступно: "
            f"{credits}. Пакет из {PHOTO_CREDITS} попыток стоит {PHOTO_PRICE_STARS} Stars. "
            "Купить: /buy"
        )
        return

    await query.edit_message_text(
        f"Принято. Списано попыток: {access.credits_charged}. Распознаю задачи..."
        if access.credits_charged else
        ("Принято. Использована бесплатная попытка. Распознаю задачи..."
         if access.source == "trial" else
         "Принято. Для безлимитного доступа попытки не списываются. Распознаю задачи...")
    )
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    previous_session = db.photo_session(user.id, PHOTO_SESSION_HOURS)
    try:
        telegram_file = await context.bot.get_file(pending["file_id"])
        image_bytes = bytes(await telegram_file.download_as_bytearray())
        extraction = await asyncio.to_thread(ai.extract_image_tasks, image_bytes)
        db.save_photo_session(user.id, extraction.text, access.source)
        caption = pending["caption"].strip()
        if caption:
            answer = await asyncio.to_thread(ai.answer_photo_session, extraction.text, caption)
            db.touch_photo_session(user.id, caption)
            input_tokens = extraction.input_tokens + answer.input_tokens
            output_tokens = extraction.output_tokens + answer.output_tokens
            estimated_cost = extraction.estimated_cost_usd + answer.estimated_cost_usd
            response_text = answer.text
            log_source = access.source
        else:
            input_tokens = extraction.input_tokens
            output_tokens = extraction.output_tokens
            estimated_cost = extraction.estimated_cost_usd
            response_text = (
                f"{extraction.text}\n\nФото сохранено на {PHOTO_SESSION_HOURS} часа. "
                "Напиши, например: реши задачу 1. После ответа можно отдельно попросить "
                "решить задачи 2 и 3 - повторно платить за фото не нужно."
            )
            log_source = "photo_setup"
        request_id = db.log_request(
            user.id, log_source, input_tokens, output_tokens, estimated_cost, "completed",
        )
        if caption:
            await send_completed_answer(
                update.effective_message, context, user.id, request_id, response_text,
                f"{extraction.text}\n\nЗапрос: {caption}",
                show_trial_cta=access.source == "trial",
            )
        else:
            for part in split_message(response_text, limit=3400):
                await update.effective_message.reply_text(
                    markdown_to_telegram_html(part), parse_mode="HTML"
                )
            db.log_event(user.id, "answer_completed", "photo_setup")
    except Exception:
        logger.exception("Photo AI request failed for user %s", user.id)
        if previous_session:
            db.save_photo_session(
                user.id, previous_session.recognized_tasks, previous_session.access_source
            )
            db.touch_photo_session(user.id, previous_session.last_request)
        else:
            db.clear_photo_session(user.id)
        db.restore_access(user.id, access.source, access.credits_charged)
        db.log_request(user.id, access.source, 0, 0, 0, "failed")
        db.log_event(user.id, "answer_failed", access.source)
        await update.effective_message.reply_text(
            ("Не удалось обработать фото. Бесплатная попытка восстановлена - пришли "
             "фотографию ещё раз."
             if access.source == "trial" else
             ("Не удалось обработать фото. Попытки не списывались - пришли фотографию "
              "ещё раз."
              if access.source == "unlimited" else
             f"Не удалось обработать фото. Все {access.credits_charged or PHOTO_CREDITS} "
             "попыток возвращены - пришли фотографию ещё раз."))
        )


async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, request_id_text, value = query.data.split(":", 2)
    request_id = int(request_id_text)
    _, db, _ = services(context)
    added = db.record_feedback(
        update.effective_user.id, request_id, positive=value == "positive"
    )
    if not added:
        await query.answer("Отзыв уже учтён")
        return
    await query.answer("Спасибо за отзыв!")
    with suppress(Exception):
        await query.edit_message_reply_markup(
            reply_markup=feedback_keyboard(request_id, feedback_enabled=False)
        )
    if value == "positive":
        await update.effective_message.reply_text(
            "Спасибо! Если появится следующая задача - /buy. Пригласить друга: /referral"
        )
    else:
        await update.effective_message.reply_text(
            "Спасибо, это важно. Если ответ содержит ошибку, напиши в /paysupport."
        )


async def defense_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":", 1)[1])
    answer_context = context.user_data.get("answer_contexts", {}).get(str(request_id))
    if not answer_context:
        await update.effective_message.reply_text(
            "Контекст этого ответа уже недоступен после перезапуска, кнопка ничего не "
            "списала. Для активной фото-сессии напиши «объясни для защиты» - это не требует "
            "новой попытки. Обычную текстовую задачу придётся прислать заново."
        )
        return
    if answer_context.get("defense"):
        for part in split_message(answer_context["defense"], limit=3400):
            await update.effective_message.reply_text(
                markdown_to_telegram_html(part), parse_mode="HTML"
            )
        return

    _, db, ai = services(context)
    user = update.effective_user
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        answer = await asyncio.to_thread(
            ai.defense_explanation, answer_context["task"], answer_context["answer"]
        )
        answer_context["defense"] = answer.text
        db.log_request(
            user.id, "defense_followup", answer.input_tokens, answer.output_tokens,
            answer.estimated_cost_usd, "completed",
        )
        for part in split_message(answer.text, limit=3400):
            await update.effective_message.reply_text(
                markdown_to_telegram_html(part), parse_mode="HTML"
            )
    except Exception:
        logger.exception("Defense explanation failed for user %s", user.id)
        db.log_request(user.id, "defense_followup", 0, 0, 0, "failed")
        await update.effective_message.reply_text(
            "Не удалось подготовить объяснение. Попробуй нажать кнопку ещё раз позже. "
            "Попытка не списана."
        )


async def post_init(application: Application) -> None:
    public_commands = [
        BotCommand("start", "Как пользоваться"), BotCommand("balance", "Остаток разборов"),
        BotCommand("newtask", "Сбросить контекст фото"),
        BotCommand("faq", "Частые вопросы"), BotCommand("buy", "Купить пакет"),
        BotCommand("referral", "Пригласить друга"),
        BotCommand("about", "О боте и открытом коде"),
        BotCommand("terms", "Условия"),
        BotCommand("paysupport", "Поддержка по оплате"),
    ]
    await application.bot.set_my_commands(public_commands)
    settings: Settings = application.bot_data["settings"]
    if settings.owner_telegram_id:
        await application.bot.set_my_commands(
            [
                BotCommand("admin", "Панель владельца"),
                BotCommand("funnel", "Воронка запуска"),
                BotCommand("funnelcsv", "Выгрузить воронку в CSV"),
                BotCommand("partner", "Создать партнёрскую ссылку"),
                *public_commands,
            ],
            scope=BotCommandScopeChat(chat_id=settings.owner_telegram_id),
        )
    application.bot_data["reactivation_task"] = asyncio.create_task(
        bridge_retry_loop(application) if application.bot_data.get("bridge") else reactivation_loop(application),
        name="maintenance-loop"
    )


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("reactivation_task")
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def main() -> None:
    instance_lock = acquire_single_instance_lock()
    settings = load_settings()
    db = Database(settings.database_path)
    ai = None if settings.student_os_bridge_enabled else AIService(
        settings.openai_api_key, settings.openai_model, settings.max_output_tokens,
        settings.input_usd_per_million, settings.output_usd_per_million)
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data.update(settings=settings, db=db, ai=ai)
    install_bridge(application, settings)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("newtask", new_task))
    application.add_handler(CommandHandler("faq", faq))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("referral", referral))
    application.add_handler(CommandHandler("partner", partner))
    application.add_handler(CommandHandler("refstats", refstats))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("funnel", funnel))
    application.add_handler(CommandHandler("funnelcsv", funnel_csv))
    application.add_handler(CommandHandler("terms", terms))
    application.add_handler(CommandHandler(["support", "paysupport"], support))
    application.add_handler(PreCheckoutQueryHandler(precheckout))
    application.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:(open|1|5)$"))
    application.add_handler(
        CallbackQueryHandler(photo_callback, pattern=r"^photo:(confirm|cancel)$")
    )
    application.add_handler(
        CallbackQueryHandler(
            feedback_callback, pattern=r"^feedback:\d+:(positive|negative)$"
        )
    )
    application.add_handler(CallbackQueryHandler(defense_callback, pattern=r"^defense:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_or_question))
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        instance_lock.close()


if __name__ == "__main__":
    main()
