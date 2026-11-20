from __future__ import annotations

from telegram import BotCommandScopeDefault
from telegram.ext import Application

import bookclub.config as config
from bookclub.handlers.commands import commands_for_user, refresh_admin_command_menus
from bookclub.i18n import PM, T
from bookclub.logging_setup import _drain_alert_queue, logger
from bookclub.notifications import recover_pending_new_book_notifications


async def bot_notify_startup(app: Application) -> None:
    """Notify first admin that bot has started, and set default command menu."""
    from bookclub.membership import initialize_activity

    initialize_activity()
    try:
        await app.bot.delete_my_commands(scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(
            commands_for_user("ru", 0), scope=BotCommandScopeDefault()
        )
        await refresh_admin_command_menus(app.bot)
    except Exception as e:
        logger.warning(f"Could not set default commands: {e}")
    if config.ALLOWED_CHAT_ID:
        try:
            chat = await app.bot.get_chat(config.ALLOWED_CHAT_ID)
            logger.info(
                "Membership gate: ALLOWED_CHAT_ID=%s is %r",
                chat.id,
                chat.title or chat.full_name,
            )
        except Exception as e:
            logger.error(
                "ALLOWED_CHAT_ID=%s is not reachable (%s). Add this bot to the "
                "group/supergroup or update the ID. Membership checks will allow "
                "everyone until the chat is reachable again.",
                config.ALLOWED_CHAT_ID,
                e,
            )
    if not config.ADMIN_IDS:
        return
    if config.ERROR_ALERTS:
        app.create_task(_drain_alert_queue(app))
    recover_pending_new_book_notifications(app.job_queue)
    admin_id = config.ADMIN_IDS[0]
    try:
        await app.bot.send_message(
            chat_id=admin_id, text=T["en"]["bot_started"], parse_mode=PM
        )
    except Exception as e:
        logger.error(f"Failed to send startup notification: {e}")


async def bot_notify_shutdown(app: Application) -> None:
    """Notify first admin that bot is shutting down."""
    if not config.ADMIN_IDS:
        return
    admin_id = config.ADMIN_IDS[0]
    try:
        await app.bot.send_message(
            chat_id=admin_id, text=T["en"]["bot_stopped"], parse_mode=PM
        )
    except Exception as e:
        logger.error(f"Failed to send shutdown notification: {e}")
