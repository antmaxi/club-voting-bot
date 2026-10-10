from __future__ import annotations

import os
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    InlineQueryHandler,
    MessageHandler,
    PicklePersistence,
    TypeHandler,
    filters,
)

from bookclub.config import (
    ADDING_AI_CHOOSE,
    ADDING_AUTHOR,
    ADDING_CONFIRM,
    ADDING_CREATION_YEAR,
    ADDING_DESCRIPTION,
    ADDING_DRAFT_CHOOSE,
    ADDING_FICTION,
    ADDING_LANGUAGE_LEVEL,
    ADDING_ORIGINAL_LANGUAGE,
    ADDING_ORIGINAL_LANGUAGE_OTHER,
    ADDING_PAGES,
    ADDING_REVIEW,
    ADDING_START,
    ADDING_TITLE,
    ADDING_TITLE_CONFIRM,
    ADMIN_EXPORT_CHOOSE,
    ADMIN_HIDE_CHOOSE,
    ADMIN_IMPORT_CONFIRM,
    ADMIN_IMPORT_WAIT,
    ADMIN_MARK_CHOOSE,
    ADMIN_MARK_DATE,
    ADMIN_MEETING_ADD_ID,
    ADMIN_MEETING_ATTENDEES,
    ADMIN_MEETING_BOOK,
    ADMIN_MEETINGS_VIEW,
    ADMIN_MENU,
    ADMIN_NOTIFY_CHAT_PICK,
    ADMIN_NOTIFY_PICK,
    ADMIN_UNHIDE_CHOOSE,
    BOT_TOKEN,
    CLUB_ENTITY,
    DELETING_CHOOSE,
    EDITING_CHOOSE,
    EDITING_FIELD,
)
from bookclub.db import init_db
from bookclub.handlers.add import (
    add_ai_cb,
    add_author,
    add_confirm_cb,
    add_creation_year,
    add_description,
    add_draft_cb,
    add_draft_del_cb,
    add_fiction_cb,
    add_language_level_cb,
    add_original_language_cb,
    add_original_language_other,
    add_original_language_skip,
    add_pages,
    add_review,
    add_review_pick_cb,
    add_start_cb,
    add_title,
    add_title_similar_cb,
    cmd_add,
)
from bookclub.handlers.add_flow import (
    add_edit_inline_query,
    add_go_back,
    add_go_edit,
    add_go_forward,
    add_go_save,
)
from bookclub.handlers.admin import (
    admin_export_pick_cb,
    admin_hide_pick_cb,
    admin_import_handler,
    admin_import_similar_cb,
    admin_mark_date_handler,
    admin_mark_edit_pick_cb,
    admin_mark_pick_cb,
    admin_meeting_add_id_handler,
    admin_meeting_att_cb,
    admin_meeting_book_cb,
    admin_meeting_view_cb,
    admin_menu_cb,
    admin_notify_chat_pick_cb,
    admin_notify_pick_cb,
    admin_unhide_pick_cb,
    cmd_admin_console,
)
from bookclub.handlers.commands import (
    book_page_cb,
    cmd_discussed,
    cmd_help,
    cmd_info,
    cmd_list,
    cmd_settings,
    cmd_start,
    cmd_top,
    list_choice_cb,
    score_calc_cb,
    settings_choice_cb,
)
from bookclub.handlers.edit_delete import (
    cmd_delete,
    cmd_edit,
    delete_pick_cb,
    edit_fiction_cb,
    edit_language_levels_cb,
    edit_original_language_cb,
    edit_pick_cb,
    edit_value_handler,
    edit_yn_cb,
)
from bookclub.handlers.misc import conv_cancel, vote_cast_cb
from bookclub.lifecycle import bot_notify_shutdown, bot_notify_startup
from bookclub.logging_setup import logger
from bookclub.membership import error_handler, membership_gate
from bookclub.ui import CONV_CANCEL

_ADD_NAV_HANDLERS = [
    CommandHandler("back", add_go_back),
    CallbackQueryHandler(add_go_back, pattern=r"^add_back$"),
    CommandHandler("forward", add_go_forward),
    CallbackQueryHandler(add_go_forward, pattern=r"^add_forward$"),
    CommandHandler("save", add_go_save),
    CallbackQueryHandler(add_go_save, pattern=r"^add_save$"),
    CallbackQueryHandler(add_go_edit, pattern=r"^add_edit$"),
]


_CONV_FALLBACKS: list[Any] = [
    CommandHandler("cancel", conv_cancel),
    CallbackQueryHandler(conv_cancel, pattern=rf"^{CONV_CANCEL}$"),
]


def add_flow_states() -> dict[Any, list[Any]]:
    """Conversation states for the sequential add-book wizard used by /add."""
    return {
        ADDING_START: [
            CallbackQueryHandler(add_start_cb, pattern=r"^add_start:"),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_DRAFT_CHOOSE: [
            CallbackQueryHandler(add_draft_cb, pattern=r"^add_draft:\d+$"),
            CallbackQueryHandler(add_draft_del_cb, pattern=r"^add_draft_del:\d+$"),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_TITLE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_title),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_TITLE_CONFIRM: [
            CallbackQueryHandler(add_title_similar_cb, pattern=r"^title_sim:"),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_AI_CHOOSE: [
            CallbackQueryHandler(add_ai_cb, pattern=r"^add_ai:"),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_AUTHOR: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_author),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_PAGES: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_pages),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_FICTION: [
            CallbackQueryHandler(add_fiction_cb, pattern=r"^fiction:"),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_REVIEW: [
            CallbackQueryHandler(add_review_pick_cb, pattern=r"^add_review_pick:\d+$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_review),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_ORIGINAL_LANGUAGE: [
            CommandHandler("skip", add_original_language_skip),
            CallbackQueryHandler(add_original_language_cb, pattern=r"^add_orig_lang:"),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_ORIGINAL_LANGUAGE_OTHER: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND, add_original_language_other
            ),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_CREATION_YEAR: [
            CommandHandler("skip", add_creation_year),
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_creation_year),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_LANGUAGE_LEVEL: [
            CallbackQueryHandler(add_language_level_cb, pattern=r"^add_cefr:"),
            *_ADD_NAV_HANDLERS,
        ],
        # /skip needs its own handler: a bare filters.TEXT here would also
        # swallow /cancel (state handlers are matched before fallbacks).
        ADDING_DESCRIPTION: [
            CommandHandler("skip", add_description),
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_description),
            *_ADD_NAV_HANDLERS,
        ],
        ADDING_CONFIRM: [
            CallbackQueryHandler(add_confirm_cb, pattern=r"^add_confirm$"),
            *_ADD_NAV_HANDLERS,
        ],
    }


def register_handlers(app: Application) -> None:
    """Attach every handler to the application.

    Split out of main() so tests can inspect the wiring — the state/fallback
    ordering here is subtle enough to be worth asserting on.
    """
    # Gate: silently block users not in the allowed chat (runs before all handlers)
    app.add_handler(TypeHandler(Update, membership_gate), group=-1)

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("add", cmd_add)],
            states=add_flow_states(),
            fallbacks=_CONV_FALLBACKS,
            per_message=False,
            # Without this, re-sending the entry command while the conversation is
            # still open matches nothing at all and the bot answers with silence —
            # an abandoned /adminconsole would stay stuck until the bot restarted.
            allow_reentry=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("adminconsole", cmd_admin_console)],
            states={
                ADMIN_MENU: [CallbackQueryHandler(admin_menu_cb, pattern=r"^admin:")],
                ADMIN_MARK_CHOOSE: [
                    CallbackQueryHandler(
                        admin_mark_pick_cb, pattern=r"^admin_mark_pick:"
                    ),
                    CallbackQueryHandler(
                        admin_mark_edit_pick_cb, pattern=r"^admin_mark_edit_pick:"
                    ),
                ],
                # /today needs its own handler — see the ADDING_DESCRIPTION note above.
                ADMIN_MARK_DATE: [
                    CommandHandler("today", admin_mark_date_handler),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, admin_mark_date_handler
                    ),
                ],
                ADMIN_HIDE_CHOOSE: [
                    CallbackQueryHandler(
                        admin_hide_pick_cb, pattern=r"^admin_hide_pick:"
                    )
                ],
                ADMIN_UNHIDE_CHOOSE: [
                    CallbackQueryHandler(
                        admin_unhide_pick_cb, pattern=r"^admin_unhide_pick:"
                    )
                ],
                ADMIN_NOTIFY_PICK: [
                    CallbackQueryHandler(
                        admin_notify_pick_cb, pattern=r"^admin_notify_pick:"
                    )
                ],
                ADMIN_NOTIFY_CHAT_PICK: [
                    CallbackQueryHandler(
                        admin_notify_chat_pick_cb, pattern=r"^admin_notify_chat_pick:"
                    )
                ],
                ADMIN_EXPORT_CHOOSE: [
                    CallbackQueryHandler(
                        admin_export_pick_cb, pattern=r"^admin_export_pick:"
                    )
                ],
                ADMIN_IMPORT_WAIT: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, admin_import_handler
                    )
                ],
                ADMIN_IMPORT_CONFIRM: [
                    CallbackQueryHandler(
                        admin_import_similar_cb, pattern=r"^title_sim:"
                    )
                ],
                ADMIN_MEETING_BOOK: [
                    CallbackQueryHandler(
                        admin_meeting_book_cb, pattern=r"^admin_meeting_book:"
                    )
                ],
                ADMIN_MEETING_ATTENDEES: [
                    CallbackQueryHandler(
                        admin_meeting_att_cb, pattern=r"^admin_meeting_att:"
                    )
                ],
                ADMIN_MEETING_ADD_ID: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, admin_meeting_add_id_handler
                    )
                ],
                ADMIN_MEETINGS_VIEW: [
                    CallbackQueryHandler(
                        admin_meeting_view_cb, pattern=r"^admin_meeting_view:"
                    )
                ],
            },
            fallbacks=_CONV_FALLBACKS,
            per_message=False,
            # Without this, re-sending the entry command while the conversation is
            # still open matches nothing at all and the bot answers with silence —
            # an abandoned /adminconsole would stay stuck until the bot restarted.
            allow_reentry=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("edit", cmd_edit)],
            states={
                EDITING_CHOOSE: [
                    CallbackQueryHandler(edit_pick_cb, pattern=r"^edit_pick:")
                ],
                EDITING_FIELD: [
                    CallbackQueryHandler(edit_yn_cb, pattern=r"^edit_yn:"),
                    CallbackQueryHandler(edit_fiction_cb, pattern=r"^edit_fiction:"),
                    CallbackQueryHandler(
                        edit_language_levels_cb, pattern=r"^edit_cefr:"
                    ),
                    CallbackQueryHandler(
                        edit_original_language_cb, pattern=r"^edit_orig_lang:"
                    ),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_handler),
                ],
            },
            fallbacks=_CONV_FALLBACKS,
            per_message=False,
            # Without this, re-sending the entry command while the conversation is
            # still open matches nothing at all and the bot answers with silence —
            # an abandoned /adminconsole would stay stuck until the bot restarted.
            allow_reentry=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("delete", cmd_delete)],
            states={
                DELETING_CHOOSE: [
                    CallbackQueryHandler(delete_pick_cb, pattern=r"^del_pick:")
                ],
            },
            fallbacks=_CONV_FALLBACKS,
            per_message=False,
            # Without this, re-sending the entry command while the conversation is
            # still open matches nothing at all and the bot answers with silence —
            # an abandoned /adminconsole would stay stuck until the bot restarted.
            allow_reentry=True,
        )
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("list_and_vote", cmd_list))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("discussed", cmd_discussed))

    app.add_handler(CallbackQueryHandler(list_choice_cb, pattern=r"^list:"))
    app.add_handler(CallbackQueryHandler(book_page_cb, pattern=r"^book_page:"))
    app.add_handler(CallbackQueryHandler(settings_choice_cb, pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(vote_cast_cb, pattern=r"^vote_cast:"))
    app.add_handler(CallbackQueryHandler(score_calc_cb, pattern=r"^score_calc_info$"))
    app.add_handler(InlineQueryHandler(add_edit_inline_query))

    # Catches anything the handlers above let escape, so a crash produces a
    # visible reply instead of silence.
    app.add_error_handler(error_handler)


def main() -> None:
    # Fail loudly here rather than letting Telegram reject the placeholder with
    # an opaque 401 several seconds into startup.
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "BOT_TOKEN is not set. Put it in your .env file (see README) "
            "or export it before starting the bot."
        )

    init_db()

    persistence_path = os.environ.get("PERSISTENCE_PATH", "bot_persistence")
    # Ensure persistence_path is a file, not a directory
    if os.path.isdir(persistence_path):
        logger.warning(
            f"Persistence path '{persistence_path}' is a directory. Removing it to allow file creation."
        )
        try:
            os.rmdir(persistence_path)
        except OSError:
            logger.error(
                f"Could not remove directory '{persistence_path}'. Please remove it manually."
            )

    persistence = PicklePersistence(filepath=persistence_path)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(bot_notify_startup)
        .post_stop(bot_notify_shutdown)
        .build()
    )
    # Verify JobQueue is available (requires: pip install "python-telegram-bot[job-queue]")
    if app.job_queue is None:
        logger.error(
            "JobQueue is not available! New book notifications will not work.\n"
            'Fix: pip install "python-telegram-bot[job-queue]"'
        )

    register_handlers(app)

    logger.info("Club entity: %s", CLUB_ENTITY)
    logger.info("Bot is running...")
    # Ignore update categories for which this bot has no handlers. This keeps
    # reactions and other unrelated chat traffic off the membership/DB hot path.
    app.run_polling(allowed_updates=["message", "callback_query", "inline_query"])
