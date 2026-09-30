"""Cloud-only entry point. No legacy database, no polling without cutover latch."""
import asyncio
import os
from contextlib import contextmanager, suppress
from urllib.parse import urlsplit

import psycopg

from telegram import BotCommand, Update
from telegram.ext import Application

from app.config import load_settings
from app.bridge_handlers import install, retry_loop

POLLING_LOCK = 734923403


@contextmanager
def polling_lease(database_url):
    """Fail closed before Telegram polling if another cloud worker owns the lease."""
    connection = None
    acquired = False
    try:
        connection = psycopg.connect(database_url, connect_timeout=10, autocommit=True,
            sslmode="disable" if urlsplit(database_url).hostname in
            {"localhost", "127.0.0.1", "::1"} else "require")
        row = connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (POLLING_LOCK,)).fetchone()
        acquired = bool(row and row[0])
        if not acquired:
            from app.observability import report
            report("polling_lease_failed")
            raise RuntimeError("Another cloud polling worker already owns the lease")
        yield
    except psycopg.Error:
        from app.observability import report
        report("polling_lease_failed")
        raise RuntimeError("Cloud polling lease unavailable") from None
    finally:
        if connection is not None:
            if acquired:
                with suppress(psycopg.Error):
                    connection.execute("SELECT pg_advisory_unlock(%s)", (POLLING_LOCK,))
            connection.close()


def validate(settings):
    if not settings.student_os_bridge_enabled:
        raise RuntimeError("Cloud worker requires Core bridge")
    if not settings.outbox_database_url.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("Cloud worker requires PostgreSQL outbox")
    if not settings.student_os_api_url.startswith("https://"):
        raise RuntimeError("Cloud worker requires HTTPS Core")
    if len(settings.student_os_bridge_secret) < 32:
        raise RuntimeError("Cloud worker requires strong bridge secret")


async def startup(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Как пользоваться"), BotCommand("balance", "Общий баланс"),
        BotCommand("buy", "Купить разборы"), BotCommand("newtask", "Новая задача"),
        BotCommand("paysupport", "Поддержка оплаты")])
    application.bot_data["retry_task"] = asyncio.create_task(retry_loop(application))


async def shutdown(application):
    task = application.bot_data.get("retry_task")
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def build(settings):
    validate(settings)
    application = Application.builder().token(settings.telegram_bot_token).post_init(startup).post_shutdown(shutdown).build()
    application.bot_data["settings"] = settings
    install(application, settings)
    return application


def main():
    # Independent of formation=0: accidental scaling must still not start polling.
    if os.getenv("TELEGRAM_DELIVERY_MODE", "polling").lower() != "polling":
        raise RuntimeError("Cloud polling forbidden in webhook delivery mode")
    if os.getenv("CLOUD_POLLING_ENABLED", "false").lower() != "true":
        raise RuntimeError("Cloud polling disabled until explicit live cutover")
    from app.observability import initialize
    initialize("bot")
    settings = load_settings()
    with polling_lease(settings.outbox_database_url):
        print("CLOUD_POLLING_LEASE_ACQUIRED", flush=True)
        application = build(settings)
        # run_polling otherwise deletes an existing webhook automatically.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(require_no_webhook(application))
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        finally:
            if not loop.is_closed():
                loop.close()


async def require_no_webhook(application):
    async with application.bot:
        info = await application.bot.get_webhook_info()
        if info.url:
            raise RuntimeError("Webhook exists; refusing to delete it or start polling")


if __name__ == "__main__":
    main()
