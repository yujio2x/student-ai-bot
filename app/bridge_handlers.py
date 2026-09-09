"""Default-off Telegram presentation adapter; no independent credits or AI engine."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import ApplicationHandlerStop, TypeHandler

from app.bridge_client import BridgeError, StudentOSBridgeClient
from app.payment_outbox import PaymentOutbox

logger = logging.getLogger(__name__)
UNAVAILABLE = "Student AI временно недоступен. Попробуй позже. Баланс: /balance"


def identity(user) -> dict:
    return {"telegram_user_id": user.id, "username": (user.username or "")[:80],
            "display_name": (getattr(user, "full_name", "") or "")[:160]}


def balance_text(entitlement: dict) -> str:
    if entitlement.get("unlimited"):
        return "Безлимитный доступ: активен. Попытки не списываются."
    trial = "доступен" if entitlement.get("free_trial_available") else "использован"
    return f"Бесплатный разбор: {trial}. Оплаченных разборов: {entitlement['balance']}."


def telegram_math(text: str) -> str:
    """Convert the small LaTeX subset models sometimes emit into safe plain text."""
    value = str(text or "").replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", value)
        value = re.sub(r"\\sqrt\{([^{}]+)\}", r"√(\1)", value)
    replacements = {r"\\pi": "π", r"\\cdot": "·", r"\\times": "×", r"\\pm": "±",
                    r"\\leq": "≤", r"\\geq": "≥", r"\\neq": "≠", r"\\infty": "∞"}
    for source, target in replacements.items():
        value = value.replace(source, target)
    superscripts = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
    value = re.sub(r"\^\{?([0-9+\-]+)\}?", lambda match: match.group(1).translate(superscripts), value)
    return value.replace("{", "").replace("}", "").replace("\\", "").strip()


def render_result(result: dict, *, defense: bool = False) -> str:
    if defense:
        fields = (("Как защитить", "defense_points"),)
        if not result.get("defense_points"):
            fields = (("Как защитить", "how_to_defend"),)
    else:
        fields = (("Решение", "solution"), ("Ответ", "answer"),
                  ("Проверка", "optional_check"))
        if not result.get("solution"):
            fields = (("Решение", "explanation"), ("Проверка", "checks"))
    sections = []
    for title, key in fields:
        value = result.get(key, "")
        if isinstance(value, list):
            value = "\n".join(f"• {telegram_math(item)}" for item in value)
        else:
            value = telegram_math(value)
        if value:
            sections.append(f"{title}\n{value}")
    return "\n\n".join(sections)[:60000] or "Раздел отсутствует в ответе."


async def send_plain(message, text: str, reply_markup=None):
    # 1800 Unicode code points fit Telegram's UTF-16 limit even for emoji.
    for offset in range(0, len(text), 1800):
        await message.reply_text(text[offset:offset + 1800],
                                 reply_markup=reply_markup if offset + 1800 >= len(text) else None)


async def show_products(message, client):
    products = (await asyncio.to_thread(client.get_products))["products"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{p['title']} — {p['stars']} ⭐", callback_data=f"corebuy:{p['id']}")]
        for p in products[:10]
    ])
    await message.reply_text("Выбери разборы для общего баланса Student OS:", reply_markup=keyboard)


async def send_result(message, context, result, defense_key):
    entries = context.user_data.setdefault("core_defense", {})
    while len(entries) >= 5:
        entries.pop(next(iter(entries)))
    entries[defense_key] = (time.time() + 3600, render_result(result, defense=True))
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Как защитить", callback_data=f"coredef:{defense_key}")],
        [InlineKeyboardButton("👍", callback_data=f"corefb:{defense_key}:positive"),
         InlineKeyboardButton("👎", callback_data=f"corefb:{defense_key}:negative")]])
    await send_plain(message, render_result(result), keyboard)


def claim_ai_delivery(context, key: str) -> bool:
    """Suppress duplicate Telegram updates and uncertain resend attempts in one worker."""
    deliveries = context.application.bot_data.setdefault("core_ai_deliveries", {})
    now = time.time()
    for old_key, expires in list(deliveries.items()):
        if expires <= now:
            deliveries.pop(old_key, None)
    if key in deliveries:
        return False
    deliveries[key] = now + 86400
    return True


async def retry_loop(application):
    data = application.bot_data
    while True:
        try:
            await asyncio.to_thread(data["payment_outbox"].retry, data["bridge"], 20, backoff=True)
        except Exception:
            # Do not log payment payload or raw transport exceptions.
            logger.error("Payment outbox retry failed; durable records retained")
            from app.observability import report
            report("outbox_retry_failed")
        await asyncio.sleep(60)


async def dispatch(update: Update, context):
    """Runs before legacy handlers and stops only bridge-owned updates."""
    data = context.application.bot_data
    client = data.get("bridge")
    if client is None or update.effective_user is None:
        return
    message = update.effective_message
    user = identity(update.effective_user)
    query = update.callback_query
    checkout = update.pre_checkout_query
    payment = getattr(message, "successful_payment", None)
    text = (getattr(message, "text", None) or "").strip()
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    try:
        if checkout:
            try:
                products = (await asyncio.to_thread(client.get_products))["products"]
                valid = checkout.currency == "XTR" and any(
                    p["id"] == checkout.invoice_payload and p["stars"] == checkout.total_amount for p in products)
            except (BridgeError, KeyError, TypeError):
                valid = False
            await checkout.answer(ok=valid, error_message=None if valid else "Покупка временно недоступна. Открой /buy позже.")
        elif payment:
            if payment.currency != "XTR":
                logger.error("Unexpected non-Stars payment; manual support required")
                await message.reply_text("Платёж требует проверки поддержки: /paysupport")
            else:
                payload = {"telegram": user, "charge_id": payment.telegram_payment_charge_id,
                           "product_id": payment.invoice_payload, "stars_paid": payment.total_amount}
                outbox = data["payment_outbox"]
                # FIRST durable commit, even if Core/catalog is currently unavailable.
                await asyncio.to_thread(outbox.enqueue, payload)
                row = await asyncio.to_thread(outbox.get, payload["charge_id"])
                if row["delivery_state"] == "pending":
                    await asyncio.to_thread(outbox.deliver, client, row)
                await message.reply_text("Платёж сохранён. Баланс: /balance. Если Core недоступен, начисление будет повторено автоматически.")
        elif query:
            action = query.data or ""
            if action.startswith(("corebuy:", "buy:")):
                await query.answer()
                product_id = action.split(":", 1)[1]
                product_id = {"1": "task_help_1_v1", "5": "task_help_5_v1"}.get(product_id, product_id)
                products = (await asyncio.to_thread(client.get_products))["products"]
                product = next((p for p in products if p["id"] == product_id), None)
                if product is None:
                    await show_products(message, client)
                else:
                    await context.bot.send_invoice(chat_id=update.effective_chat.id,
                        title=product["title"], description="Разборы на общем балансе Student OS и Telegram.",
                        payload=product["id"], currency="XTR", prices=[LabeledPrice(product["title"], product["stars"])])
            elif action.startswith("coredef:"):
                await query.answer()
                entry = context.user_data.get("core_defense", {}).get(action[8:])
                if entry and entry[0] > time.time():
                    await send_plain(message, entry[1])
                else:
                    await message.reply_text("Контекст защиты истёк. Он доступен час после ответа.")
            elif action.startswith("corefb:"):
                await query.answer()
                _, key, rating = action.split(":")
                entry = context.user_data.get("core_defense", {}).get(key)
                if entry and entry[0] > time.time() and rating in {"positive", "negative"}:
                    await asyncio.to_thread(client.feedback, user, rating, f"telegram-feedback:{user['telegram_user_id']}:{key}")
                    await message.reply_text("Спасибо, отзыв сохранён в Student OS.")
                else:
                    await message.reply_text("Кнопка отзыва истекла.")
            elif action.startswith("corephoto:"):
                await query.answer()
                pending = context.user_data.pop("core_pending_photo", None)
                if not pending or pending["expires"] <= time.time() or pending["quote_id"] != action[10:]:
                    await message.reply_text("Подтверждение истекло. Пришли фото снова.")
                else:
                    session = await asyncio.to_thread(client.confirm_photo, user, pending["data"], "image/jpeg", pending["quote_id"])
                    context.user_data["core_photo_session"] = session
                    prefix = f"coreselect:{session['session_id']}:"
                    buttons = [[InlineKeyboardButton("Все задачи", callback_data=prefix + "all")]]
                    buttons.extend([InlineKeyboardButton(f"Задача {i+1}", callback_data=prefix + str(i))] for i in range(len(session["tasks"])))
                    await send_plain(message, "Распознано:\n\n" + "\n\n".join(session["tasks"])[:24000], InlineKeyboardMarkup(buttons))
            elif action.startswith("coreselect:"):
                await query.answer()
                session = context.user_data.get("core_photo_session")
                if not session:
                    session = (await asyncio.to_thread(client.latest_photo, user)).get("session")
                    if session:
                        context.user_data["core_photo_session"] = session
                parts = action.split(":")
                if not session or session["expires_at"] <= time.time() or len(parts) != 3 or parts[1] != session["session_id"]:
                    await message.reply_text("Фото-сессия истекла. Пришли фото снова.")
                else:
                    choice = parts[2]
                    selection = list(range(len(session["tasks"]))) if choice == "all" else [int(choice)]
                    result = await asyncio.to_thread(client.answer_photo, user, session["session_id"], selection, f"telegram-photo:{user['telegram_user_id']}:{query.id}")
                    await send_result(message, context, result, "photo" + str(message.message_id))
            elif action.startswith(("photo:", "defense:", "admin:", "feedback:")):
                await query.answer()
                await message.reply_text("Эта старая кнопка недоступна в общем режиме. Управление балансом — в Student OS.")
            else:
                return
        elif command == "/balance":
            result = await asyncio.to_thread(client.get_entitlement, user)
            await message.reply_text(balance_text(result["entitlement"]))
        elif command == "/buy" or (command == "/start" and text.split()[1:] == ["buy"]):
            await show_products(message, client)
        elif command == "/start":
            await asyncio.to_thread(client.resolve_user, user)
            await message.reply_text("Student AI связан с общим аккаунтом Student OS. Пришли учебную задачу текстом.\nОдин общий пробный разбор; /balance — остаток, /buy — покупка.")
        elif command in {"/admin", "/referral", "/partner", "/refstats", "/funnel", "/funnelcsv"}:
            await message.reply_text("В общем режиме управление и статистика находятся в Student OS. Старый баланс не используется.")
        elif command == "/faq":
            await message.reply_text("Текст до 6000 символов. Один общий пробный разбор с Web; далее попытки с общего баланса. Фото до 6 МБ: trial либо 5 credits после подтверждения; задачи с того же фото доступны 24 часа без повторной оплаты. Цены: /buy. Проверяй ответы AI.")
        elif command == "/newtask":
            context.user_data.pop("core_pending_photo", None)
            context.user_data.pop("core_photo_session", None)
            await message.reply_text("Фото-контекст сброшен. Пришли новое задание.")
        elif getattr(message, "photo", None):
            photo = message.photo[-1]
            if not photo.file_size or photo.file_size > 6 * 1024 * 1024:
                await message.reply_text("Пришли фото до 6 МБ. Попытка не списана.")
            else:
                file = await context.bot.get_file(photo.file_id)
                raw = bytes(await file.download_as_bytearray())
                if len(raw) > 6 * 1024 * 1024:
                    await message.reply_text("Пришли фото до 6 МБ. Попытка не списана.")
                else:
                    quote = await asyncio.to_thread(client.quote_photo, user, raw, "image/jpeg")
                    pending = {**quote, "data": raw, "expires": time.time() + 300}
                    context.user_data["core_pending_photo"] = pending
                    quote_id = quote["quote_id"]
                    def expire():
                        current = context.user_data.get("core_pending_photo")
                        if current and current["quote_id"] == quote_id:
                            context.user_data.pop("core_pending_photo", None)
                    asyncio.get_running_loop().call_later(300, expire)
                    cost = "одна общая бесплатная попытка" if quote["uses_trial"] else "5 оплаченных попыток" if quote["credits"] else "без списания — безлимит"
                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Подтвердить распознавание", callback_data=f"corephoto:{quote_id}")]]) if quote["can_confirm"] else None
                    await message.reply_text(f"Фото-разбор: {cost}. Условия сохранятся на 24 часа. Другие задачи с этого фото — без повторного списания. До 20 запросов в час. Покупка: /buy", reply_markup=keyboard)
        elif text and not command:
            if not 3 <= len(text) <= 6000:
                await message.reply_text("Пришли условие длиной от 3 до 6000 символов.")
            else:
                key = f"telegram:{user['telegram_user_id']}:{update.effective_chat.id}:{message.message_id}"
                if not claim_ai_delivery(context, key):
                    raise ApplicationHandlerStop
                result = (await asyncio.to_thread(client.submit_text_task, user, text, key))["result"]
                await send_result(message, context, result, str(message.message_id))
        else:
            return
    except BridgeError as exc:
        if message and exc.status != 409:
            await message.reply_text({402: "Попытки закончились. Купить: /buy"}.get(exc.status, UNAVAILABLE))
    except (KeyError, TypeError, ValueError):
        logger.error("Invalid bridge contract or conflicting payment; review required")
        if message:
            await message.reply_text("Не удалось подтвердить операцию. Обратись в /paysupport.")
    raise ApplicationHandlerStop


def install(application, settings):
    if not settings.student_os_bridge_enabled:
        return
    application.bot_data["bridge"] = StudentOSBridgeClient(settings.student_os_api_url, settings.student_os_bridge_secret)
    if settings.outbox_database_url:
        from app.postgres_outbox import PostgresPaymentOutbox
        outbox = PostgresPaymentOutbox(settings.outbox_database_url)
    else:
        outbox = PaymentOutbox(settings.database_path.with_name("core_payment_outbox.db"))
    application.bot_data["payment_outbox"] = outbox
    application.add_handler(TypeHandler(Update, dispatch), group=-1)
