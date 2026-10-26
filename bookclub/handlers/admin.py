from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import bookclub.config as config
from bookclub.config import (
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
)
from bookclub.db import (
    VOTES_USE_ATTENDANCE_KEY,
    book_to_export_payload,
    db_delete_meeting,
    db_get_admin_setting,
    db_get_book,
    db_get_books,
    db_get_books_metadata,
    db_get_meeting,
    db_get_meeting_attendee_rows,
    db_get_meeting_for_book,
    db_get_users_missing_votes,
    db_get_users_with_setting,
    db_import_book,
    db_list_meetings,
    db_mark_discussed,
    db_save_meeting,
    db_set_admin_setting,
    db_set_discussed_at,
    db_set_hidden,
    db_upsert_club_user,
    find_similar_book_titles,
    format_club_user_display,
    parse_book_import,
)
from bookclub.domain import is_admin, require_book
from bookclub.i18n import PM, get_lang, s, tr
from bookclub.notifications import (
    enqueue_vote_reminder_job,
    schedule_new_book_notifications,
)
from bookclub.types import BookLike
from bookclub.ui import (
    _meeting_attendee_ids,
    _show_meeting_attendee_picker,
    books_keyboard,
    books_top_n,
    cancel_button,
    cancel_keyboard,
    fetch_telegram_user_profile,
    fmt_dt_utc,
    h,
    meeting_delete_confirm_keyboard,
    meeting_view_keyboard,
    meetings_keyboard,
    parse_date,
    refresh_missing_club_user_names,
    send_compact_book_list,
    show_notify_books_picker,
    similar_title_confirm_keyboard,
    similar_title_warning_matches_text,
)


async def _deny_non_admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Answer and reject a callback from a non-admin. Returns True if denied.

    Conversation state already keeps non-admins out, but these buttons are
    visible to everyone when /adminconsole is run in a group, so the handlers
    verify the caller themselves rather than relying on routing alone.
    """
    if is_admin(update.effective_user.id):
        return False
    await update.callback_query.answer(tr(ctx, "admin_only"), show_alert=True)
    return True


def _clear_meeting_ctx(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("meeting_book_id", None)
    ctx.user_data.pop("meeting_date", None)
    ctx.user_data.pop("meeting_attendee_ids", None)
    ctx.user_data.pop("meeting_id", None)
    ctx.user_data.pop("meeting_attendee_page", None)


def _meeting_detail_text(ctx: ContextTypes.DEFAULT_TYPE, meeting: BookLike) -> str:
    rows = db_get_meeting_attendee_rows(int(meeting["id"]))
    lines = []
    for row in rows:
        name = format_club_user_display(
            int(row["user_id"]), row["full_name"], row["username"]
        )
        lines.append(tr(ctx, "meeting_attendee_line", name=h(name)))
    body = "\n".join(lines) if lines else tr(ctx, "meeting_view_empty")
    return (
        tr(
            ctx,
            "meeting_view_title",
            title=h(meeting["title"]),
            date=h(meeting["meeting_date"]),
            count=len(rows),
        )
        + body
    )


async def _show_meetings_picker(query: Any, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    meetings = db_list_meetings()
    if not meetings:
        await query.edit_message_text(tr(ctx, "no_meetings"), parse_mode=PM)
        return ConversationHandler.END
    await query.edit_message_text(
        tr(ctx, "choose_meeting_view"),
        reply_markup=meetings_keyboard(
            meetings, "admin_meeting_view", tr(ctx, "cancel_btn")
        ),
        parse_mode=PM,
    )
    return ADMIN_MEETINGS_VIEW


async def _show_meeting_detail(
    query: Any, ctx: ContextTypes.DEFAULT_TYPE, meeting: BookLike
) -> int:
    rows = db_get_meeting_attendee_rows(int(meeting["id"]))
    if await refresh_missing_club_user_names(ctx.bot, rows):
        meeting = db_get_meeting(int(meeting["id"])) or meeting
    await query.edit_message_text(
        _meeting_detail_text(ctx, meeting),
        parse_mode=PM,
        reply_markup=meeting_view_keyboard(get_lang(ctx), int(meeting["id"])),
    )
    return ADMIN_MEETINGS_VIEW


async def _start_meeting_attendee_edit(
    query: Any, ctx: ContextTypes.DEFAULT_TYPE, book: BookLike
) -> int:
    book_id = int(book["id"])
    existing = db_get_meeting_for_book(book_id)
    discussed_at = (book["discussed_at"] or "").strip()
    if existing:
        ctx.user_data["meeting_id"] = int(existing["id"])
        ctx.user_data["meeting_attendee_ids"] = {
            int(r["user_id"]) for r in db_get_meeting_attendee_rows(int(existing["id"]))
        }
        ctx.user_data["meeting_date"] = discussed_at or existing["meeting_date"]
    else:
        ctx.user_data.pop("meeting_id", None)
        ctx.user_data["meeting_attendee_ids"] = set()
        if not discussed_at:
            await query.edit_message_text(
                tr(ctx, "meeting_no_discussed_date"), parse_mode=PM
            )
            return ConversationHandler.END
        ctx.user_data["meeting_date"] = discussed_at
    ctx.user_data["meeting_book_id"] = book_id
    return await _show_meeting_attendee_picker(query, ctx, page=0, is_callback=True)


async def _render_book_picker_page(
    query: Any,
    ctx: ContextTypes.DEFAULT_TYPE,
    books: Sequence[BookLike],
    *,
    prefix: str,
    prompt_key: str,
) -> bool:
    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] != "page":
        return False
    await query.edit_message_text(
        tr(ctx, prompt_key),
        reply_markup=books_keyboard(
            books, prefix, tr(ctx, "cancel_btn"), page=int(parts[2])
        ),
        parse_mode=PM,
    )
    return True


async def _render_meeting_picker_page(
    query: Any, ctx: ContextTypes.DEFAULT_TYPE, meetings: Sequence[BookLike]
) -> bool:
    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] != "page":
        return False
    await query.edit_message_text(
        tr(ctx, "choose_meeting_view"),
        reply_markup=meetings_keyboard(
            meetings,
            "admin_meeting_view",
            tr(ctx, "cancel_btn"),
            page=int(parts[2]),
        ),
        parse_mode=PM,
    )
    return True


async def cmd_admin_console(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(tr(ctx, "admin_only"), parse_mode=PM)
        return ConversationHandler.END

    last_act = ctx.bot_data.get("last_non_admin_activity")
    last_act_str = fmt_dt_utc(last_act) if last_act else tr(ctx, "never")

    text = (
        tr(ctx, "admin_console_title")
        + f"\n\n{tr(ctx, 'last_activity_label')}: <code>{last_act_str}</code>"
    )

    post_chat = db_get_admin_setting("post_new_books_to_chat", 0)
    chat_state = "✅" if post_chat else "❌"
    votes_attendance = db_get_admin_setting(VOTES_USE_ATTENDANCE_KEY, 0)
    votes_state = tr(
        ctx,
        "admin_votes_mode_attendance" if votes_attendance else "admin_votes_mode_all",
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_mark_btn"), callback_data="admin:mark"
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_hide_btn"), callback_data="admin:hide"
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_unhide_btn"), callback_data="admin:unhide"
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_notify_btn"), callback_data="admin:notify"
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_notify_one_btn"), callback_data="admin:notify_pick"
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_notify_chat_btn"), callback_data="admin:notify_chat"
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_notify_chat_one_btn"),
                    callback_data="admin:notify_chat_pick",
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_toggle_chat_btn", state=chat_state),
                    callback_data="admin:toggle_chat",
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_toggle_votes_btn", state=votes_state),
                    callback_data="admin:toggle_votes",
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_meeting_create_btn"),
                    callback_data="admin:meeting_create",
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_meetings_view_btn"),
                    callback_data="admin:meetings_view",
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_export_btn"), callback_data="admin:export"
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "admin_import_btn"), callback_data="admin:import"
                )
            ],
            [cancel_button(get_lang(ctx))],
        ]
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode=PM
        )
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=PM)
    return ADMIN_MENU


async def admin_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")[1]

    if data == "mark":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr(ctx, "admin_mark_new_btn"), callback_data="admin:mark_new"
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr(ctx, "admin_mark_edit_date_btn"),
                        callback_data="admin:mark_edit",
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr(ctx, "cancel_btn"), callback_data="admin:mark_back"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            tr(ctx, "admin_mark_menu"), reply_markup=keyboard, parse_mode=PM
        )
        return ADMIN_MENU
    elif data == "mark_back":
        return await cmd_admin_console(update, ctx)
    elif data == "mark_new":
        books = db_get_books_metadata(discussed=False, include_hidden=True)
        if not books:
            await query.edit_message_text(tr(ctx, "no_unmark"), parse_mode=PM)
            return ConversationHandler.END
        await query.edit_message_text(
            tr(ctx, "choose_mark"),
            reply_markup=books_keyboard(
                books, "admin_mark_pick", tr(ctx, "cancel_btn")
            ),
        )
        return ADMIN_MARK_CHOOSE
    elif data == "mark_edit":
        books = db_get_books_metadata(discussed=True, include_hidden=True)
        if not books:
            await query.edit_message_text(
                tr(ctx, "no_discussed_to_edit_date"), parse_mode=PM
            )
            return ConversationHandler.END
        await query.edit_message_text(
            tr(ctx, "choose_edit_discuss_date"),
            reply_markup=books_keyboard(
                books, "admin_mark_edit_pick", tr(ctx, "cancel_btn")
            ),
        )
        return ADMIN_MARK_CHOOSE
    elif data == "hide":
        books = [
            b
            for b in db_get_books_metadata(discussed=False, include_hidden=True)
            if not b["hidden"]
        ]
        if not books:
            await query.edit_message_text(tr(ctx, "no_undiscussed"), parse_mode=PM)
            return ConversationHandler.END

        await query.edit_message_text(
            tr(ctx, "choose_hide"),
            reply_markup=books_keyboard(
                books, "admin_hide_pick", tr(ctx, "cancel_btn")
            ),
        )
        return ADMIN_HIDE_CHOOSE
    elif data == "unhide":
        books = _admin_hidden_books()
        if not books:
            await query.edit_message_text(tr(ctx, "no_hidden"), parse_mode=PM)
            return ConversationHandler.END
        await query.edit_message_text(
            tr(ctx, "choose_unhide"),
            reply_markup=books_keyboard(
                books, "admin_unhide_pick", tr(ctx, "cancel_btn")
            ),
        )
        return ADMIN_UNHIDE_CHOOSE
    elif data == "notify":
        return await admin_notify_top_cb(update, ctx)
    elif data == "notify_pick":
        books = _admin_notify_books()
        if not books:
            await query.edit_message_text(tr(ctx, "no_books"), parse_mode=PM)
            return ConversationHandler.END
        ctx.user_data["notify_book_ids"] = set()
        return await show_notify_books_picker(
            query,
            ctx,
            books,
            page=0,
            is_callback=True,
            prefix="admin_notify_pick",
            prompt_key="choose_notify_books",
            done_label_key="notify_books_send_btn",
        )
    elif data == "notify_chat":
        return await admin_notify_chat_top_cb(update, ctx)
    elif data == "notify_chat_pick":
        books = _admin_notify_books()
        if not books:
            await query.edit_message_text(tr(ctx, "no_books"), parse_mode=PM)
            return ConversationHandler.END
        ctx.user_data["notify_book_ids"] = set()
        return await show_notify_books_picker(
            query,
            ctx,
            books,
            page=0,
            is_callback=True,
            prefix="admin_notify_chat_pick",
            prompt_key="choose_notify_chat_books",
            done_label_key="notify_books_post_chat_btn",
        )
    elif data == "toggle_chat":
        current = db_get_admin_setting("post_new_books_to_chat", 0)
        db_set_admin_setting("post_new_books_to_chat", 1 - current)
        return await cmd_admin_console(update, ctx)
    elif data == "toggle_votes":
        current = db_get_admin_setting(VOTES_USE_ATTENDANCE_KEY, 0)
        db_set_admin_setting(VOTES_USE_ATTENDANCE_KEY, 1 - current)
        state = await cmd_admin_console(update, ctx)
        await send_compact_book_list(
            ctx.bot,
            update.effective_chat.id,
            db_get_books(discussed=False),
            ctx,
        )
        return state
    elif data == "export":
        all_books = db_get_books_metadata(discussed=False, include_hidden=True) + list(
            db_get_books_metadata(discussed=True, include_hidden=True)
        )
        if not all_books:
            await query.edit_message_text(tr(ctx, "no_books"), parse_mode=PM)
            return ConversationHandler.END
        await query.edit_message_text(
            tr(ctx, "choose_export"),
            reply_markup=books_keyboard(
                all_books, "admin_export_pick", tr(ctx, "cancel_btn")
            ),
        )
        return ADMIN_EXPORT_CHOOSE
    elif data == "import":
        await query.edit_message_text(
            tr(ctx, "import_prompt"),
            parse_mode=PM,
            reply_markup=cancel_keyboard(get_lang(ctx)),
        )
        return ADMIN_IMPORT_WAIT
    elif data == "meeting_create":
        books = db_get_books_metadata(discussed=True, include_hidden=True)
        if not books:
            await query.edit_message_text(
                tr(ctx, "no_discussed_for_meeting"), parse_mode=PM
            )
            return ConversationHandler.END
        await query.edit_message_text(
            tr(ctx, "choose_meeting_book"),
            reply_markup=books_keyboard(
                books, "admin_meeting_book", tr(ctx, "cancel_btn")
            ),
            parse_mode=PM,
        )
        return ADMIN_MEETING_BOOK
    elif data == "meetings_view":
        return await _show_meetings_picker(query, ctx)
    return ConversationHandler.END


async def admin_notify_top_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query

    books = db_get_books(discussed=False)
    if not books:
        await query.edit_message_text(tr(ctx, "no_undiscussed"), parse_mode=PM)
        return ConversationHandler.END

    # Top 5 selection (same logic as /top)
    top_books = books_top_n(books)

    user_ids = db_get_users_with_setting("notify_new_books", 1)
    book_ids = [int(book["id"]) for book in top_books]
    eligible_users = db_get_users_missing_votes(user_ids, book_ids)
    scheduled = bool(eligible_users) and enqueue_vote_reminder_job(
        ctx.job_queue,
        book_ids,
        user_ids=eligible_users,
    )

    if eligible_users and scheduled:
        await query.edit_message_text(
            tr(ctx, "admin_notify_confirm", count=len(eligible_users)), parse_mode=PM
        )
    else:
        await query.edit_message_text(tr(ctx, "admin_notify_no_users"), parse_mode=PM)

    return ConversationHandler.END


async def admin_notify_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return await _admin_notify_books_pick_cb(update, ctx, to_chat=False)


async def admin_notify_chat_pick_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    return await _admin_notify_books_pick_cb(update, ctx, to_chat=True)


async def _admin_notify_books_pick_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, to_chat: bool
) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    prefix = "admin_notify_chat_pick" if to_chat else "admin_notify_pick"
    books = _admin_notify_books()
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    def _clear_notify_pick_state() -> None:
        ctx.user_data.pop("notify_book_ids", None)
        ctx.user_data.pop("notify_books_page", None)

    if action == "cancel":
        _clear_notify_pick_state()
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END

    if action == "page" and len(parts) > 2:
        page = int(parts[2])
        return await show_notify_books_picker(
            query,
            ctx,
            books,
            page=page,
            is_callback=True,
            prefix=prefix,
            prompt_key=(
                "choose_notify_chat_books" if to_chat else "choose_notify_books"
            ),
            done_label_key=(
                "notify_books_post_chat_btn" if to_chat else "notify_books_send_btn"
            ),
        )

    if action == "toggle" and len(parts) > 2:
        book_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        selected = ctx.user_data.get("notify_book_ids")
        if selected is None:
            selected = set()
            ctx.user_data["notify_book_ids"] = selected
        if book_id in selected:
            selected.remove(book_id)
        else:
            selected.add(book_id)
        return await show_notify_books_picker(
            query,
            ctx,
            books,
            page=page,
            is_callback=True,
            prefix=prefix,
            prompt_key=(
                "choose_notify_chat_books" if to_chat else "choose_notify_books"
            ),
            done_label_key=(
                "notify_books_post_chat_btn" if to_chat else "notify_books_send_btn"
            ),
        )

    if action == "done":
        selected_ids = list(ctx.user_data.pop("notify_book_ids", set()))
        ctx.user_data.pop("notify_books_page", None)
        if not selected_ids:
            await query.edit_message_text(
                tr(ctx, "notify_no_books_selected"), parse_mode=PM
            )
            return ConversationHandler.END

        if to_chat:
            if not config.ALLOWED_CHAT_ID:
                await query.edit_message_text(
                    tr(ctx, "admin_notify_chat_no_chat"), parse_mode=PM
                )
                return ConversationHandler.END
            scheduled = enqueue_vote_reminder_job(
                ctx.job_queue, selected_ids, to_chat=True
            )
            if scheduled:
                await query.edit_message_text(
                    tr(ctx, "admin_notify_chat_confirm", count=len(selected_ids)),
                    parse_mode=PM,
                )
            else:
                await query.edit_message_text(
                    tr(ctx, "admin_notify_chat_failed"), parse_mode=PM
                )
            return ConversationHandler.END

        user_ids = db_get_users_with_setting("notify_new_books", 1)
        eligible_users = db_get_users_missing_votes(user_ids, selected_ids)
        scheduled = bool(eligible_users) and enqueue_vote_reminder_job(
            ctx.job_queue,
            selected_ids,
            user_ids=eligible_users,
        )
        if eligible_users and scheduled:
            await query.edit_message_text(
                tr(ctx, "admin_notify_confirm", count=len(eligible_users)),
                parse_mode=PM,
            )
        else:
            await query.edit_message_text(
                tr(ctx, "admin_notify_no_users"), parse_mode=PM
            )
        return ConversationHandler.END

    return ADMIN_NOTIFY_PICK if not to_chat else ADMIN_NOTIFY_CHAT_PICK


async def admin_notify_chat_top_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query

    if not config.ALLOWED_CHAT_ID:
        await query.edit_message_text(
            tr(ctx, "admin_notify_chat_no_chat"), parse_mode=PM
        )
        return ConversationHandler.END

    books = db_get_books(discussed=False)
    if not books:
        await query.edit_message_text(tr(ctx, "no_undiscussed"), parse_mode=PM)
        return ConversationHandler.END

    top_books = books_top_n(books)
    scheduled = enqueue_vote_reminder_job(
        ctx.job_queue,
        [int(book["id"]) for book in top_books],
        to_chat=True,
    )
    if scheduled:
        await query.edit_message_text(
            tr(ctx, "admin_notify_chat_confirm", count=len(top_books)), parse_mode=PM
        )
    else:
        await query.edit_message_text(
            tr(ctx, "admin_notify_chat_failed"), parse_mode=PM
        )

    return ConversationHandler.END


async def admin_mark_from_card_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the discussion-date prompt from a vote card. Admins only."""
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    lang = get_lang(ctx)
    try:
        book_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return ConversationHandler.END
    book = db_get_book(book_id)
    if book is None or book["discussed"]:
        await query.answer(tr(ctx, "voting_closed"), show_alert=True)
        return ConversationHandler.END
    if query.message is None:
        await query.answer()
        return ConversationHandler.END
    await query.answer()
    ctx.user_data.pop("mark_edit_date", None)
    ctx.user_data["mark_book_id"] = book_id
    await query.message.reply_text(
        tr(ctx, "ask_discuss_date"),
        parse_mode=PM,
        reply_markup=cancel_keyboard(lang),
    )
    return ADMIN_MARK_DATE


async def admin_mark_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    if await _render_book_picker_page(
        query,
        ctx,
        db_get_books_metadata(discussed=False, include_hidden=True),
        prefix="admin_mark_pick",
        prompt_key="choose_mark",
    ):
        return ADMIN_MARK_CHOOSE
    _, book_id = query.data.split(":", 1)
    if book_id == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END
    ctx.user_data.pop("mark_edit_date", None)
    ctx.user_data["mark_book_id"] = int(book_id)
    await query.edit_message_text(
        tr(ctx, "ask_discuss_date"),
        parse_mode=PM,
        reply_markup=cancel_keyboard(lang),
    )
    return ADMIN_MARK_DATE


async def admin_mark_edit_pick_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    if await _render_book_picker_page(
        query,
        ctx,
        db_get_books_metadata(discussed=True, include_hidden=True),
        prefix="admin_mark_edit_pick",
        prompt_key="choose_edit_discuss_date",
    ):
        return ADMIN_MARK_CHOOSE
    _, book_id = query.data.split(":", 1)
    if book_id == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END
    book = require_book(int(book_id))
    current = book["discussed_at"] or "—"
    ctx.user_data["mark_edit_date"] = True
    ctx.user_data["mark_book_id"] = int(book_id)
    prompt = (
        tr(ctx, "ask_discuss_date")
        + "\n\n"
        + tr(ctx, "current_discussed_date", date=h(current))
    )
    await query.edit_message_text(
        prompt, parse_mode=PM, reply_markup=cancel_keyboard(lang)
    )
    return ADMIN_MARK_DATE


async def admin_mark_date_handler(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(tr(ctx, "admin_only"), parse_mode=PM)
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "/today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        parsed = parse_date(text)
        if parsed is None:
            await update.message.reply_text(
                tr(ctx, "invalid_date"),
                parse_mode=PM,
                reply_markup=cancel_keyboard(get_lang(ctx)),
            )
            return ADMIN_MARK_DATE
        date_str = parsed
    book_id = ctx.user_data.pop("mark_book_id", None)
    edit_date = ctx.user_data.pop("mark_edit_date", False)
    if book_id is None:
        # State was lost (e.g. bot restarted mid-conversation).
        await update.message.reply_text(tr(ctx, "cancelled"), parse_mode=PM)
        return ConversationHandler.END
    book = require_book(book_id)
    if edit_date:
        if not db_set_discussed_at(book_id, date_str):
            await update.message.reply_text(tr(ctx, "cancelled"), parse_mode=PM)
            return ConversationHandler.END
        await update.message.reply_text(
            tr(
                ctx,
                "discussed_date_updated",
                title=h(book["title"]),
                date=h(date_str),
            ),
            parse_mode=PM,
        )
    else:
        db_mark_discussed(book_id, date_str)
        await update.message.reply_text(
            tr(
                ctx,
                "marked_discussed",
                title=h(book["title"]),
                date=h(date_str),
            ),
            parse_mode=PM,
        )
    return ConversationHandler.END


async def admin_meeting_book_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    if await _render_book_picker_page(
        query,
        ctx,
        db_get_books_metadata(discussed=True, include_hidden=True),
        prefix="admin_meeting_book",
        prompt_key="choose_meeting_book",
    ):
        return ADMIN_MEETING_BOOK
    _, book_id = query.data.split(":", 1)
    if book_id == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END
    book_id = int(book_id)
    book = db_get_book(book_id)
    if not book or not book["discussed"]:
        await query.edit_message_text(
            tr(ctx, "no_discussed_for_meeting"), parse_mode=PM
        )
        return ConversationHandler.END
    return await _start_meeting_attendee_edit(query, ctx, book)


async def admin_meeting_att_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        _clear_meeting_ctx(ctx)
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END

    if action == "addid":
        await query.edit_message_text(
            tr(ctx, "meeting_attendee_add_id_prompt"),
            parse_mode=PM,
            reply_markup=cancel_keyboard(lang),
        )
        return ADMIN_MEETING_ADD_ID

    if action == "page" and len(parts) > 2:
        page = int(parts[2])
        return await _show_meeting_attendee_picker(
            query, ctx, page=page, is_callback=True
        )

    if action == "toggle" and len(parts) > 2:
        uid = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        selected = _meeting_attendee_ids(ctx)
        if uid in selected:
            selected.remove(uid)
        else:
            selected.add(uid)
        return await _show_meeting_attendee_picker(
            query, ctx, page=page, is_callback=True
        )

    if action == "done":
        book_id = ctx.user_data.pop("meeting_book_id", None)
        date_str = ctx.user_data.pop("meeting_date", None)
        attendee_ids = list(ctx.user_data.pop("meeting_attendee_ids", set()))
        ctx.user_data.pop("meeting_id", None)
        ctx.user_data.pop("meeting_attendee_page", None)
        if book_id is None or date_str is None:
            await query.edit_message_text(s(lang, "cancelled"))
            return ConversationHandler.END
        db_save_meeting(
            book_id,
            date_str,
            query.from_user.id,
            attendee_ids,
        )
        book = require_book(book_id)
        await query.edit_message_text(
            tr(
                ctx,
                "meeting_saved",
                title=h(book["title"]),
                date=h(date_str),
                count=len(attendee_ids),
            ),
            parse_mode=PM,
        )
        return ConversationHandler.END

    return ADMIN_MEETING_ATTENDEES


async def admin_meeting_add_id_handler(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(tr(ctx, "admin_only"), parse_mode=PM)
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    try:
        user_id = int(text)
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            tr(ctx, "meeting_attendee_invalid_id"),
            parse_mode=PM,
            reply_markup=cancel_keyboard(get_lang(ctx)),
        )
        return ADMIN_MEETING_ADD_ID

    full_name, username = await fetch_telegram_user_profile(ctx.bot, user_id)
    db_upsert_club_user(user_id, full_name, username)
    _meeting_attendee_ids(ctx).add(user_id)
    display = format_club_user_display(user_id, full_name, username)
    name_html = h(display) if display != str(user_id) else f"<code>{user_id}</code>"
    await update.message.reply_text(
        tr(ctx, "meeting_attendee_added_id", name=name_html), parse_mode=PM
    )
    page = int(ctx.user_data.get("meeting_attendee_page", 0))
    return await _show_meeting_attendee_picker(
        update, ctx, page=page, is_callback=False
    )


async def admin_meeting_view_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END
    if action == "page":
        if await _render_meeting_picker_page(query, ctx, db_list_meetings()):
            return ADMIN_MEETINGS_VIEW
        return ADMIN_MEETINGS_VIEW
    if action == "list":
        return await _show_meetings_picker(query, ctx)
    if action in ("edit", "delete", "delete_yes") and len(parts) > 2:
        try:
            meeting_id = int(parts[2])
        except ValueError:
            await query.edit_message_text("Error: meeting not found.")
            return ConversationHandler.END
        meeting = db_get_meeting(meeting_id)
        if not meeting:
            await query.edit_message_text("Error: meeting not found.")
            return ConversationHandler.END
        if action == "edit":
            book = db_get_book(int(meeting["book_id"]))
            if not book:
                await query.edit_message_text("Error: meeting not found.")
                return ConversationHandler.END
            return await _start_meeting_attendee_edit(query, ctx, book)
        if action == "delete":
            await query.edit_message_text(
                tr(
                    ctx,
                    "meeting_delete_confirm",
                    title=h(meeting["title"]),
                    date=h(meeting["meeting_date"]),
                ),
                parse_mode=PM,
                reply_markup=meeting_delete_confirm_keyboard(lang, meeting_id),
            )
            return ADMIN_MEETINGS_VIEW
        db_delete_meeting(meeting_id)
        remaining = db_list_meetings()
        if not remaining:
            await query.edit_message_text(tr(ctx, "meeting_deleted"), parse_mode=PM)
            return ConversationHandler.END
        await query.edit_message_text(
            tr(ctx, "meeting_deleted") + "\n\n" + tr(ctx, "choose_meeting_view"),
            reply_markup=meetings_keyboard(
                remaining, "admin_meeting_view", tr(ctx, "cancel_btn")
            ),
            parse_mode=PM,
        )
        return ADMIN_MEETINGS_VIEW

    try:
        meeting_id = int(action)
    except ValueError:
        await query.edit_message_text("Error: meeting not found.")
        return ConversationHandler.END
    meeting = db_get_meeting(meeting_id)
    if not meeting:
        await query.edit_message_text("Error: meeting not found.")
        return ConversationHandler.END
    return await _show_meeting_detail(query, ctx, meeting)


async def admin_hide_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    visible_books = [
        book
        for book in db_get_books_metadata(discussed=False, include_hidden=True)
        if not book["hidden"]
    ]
    if await _render_book_picker_page(
        query,
        ctx,
        visible_books,
        prefix="admin_hide_pick",
        prompt_key="choose_hide",
    ):
        return ADMIN_HIDE_CHOOSE
    _, book_id = query.data.split(":", 1)
    if book_id == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END

    book_id = int(book_id)
    db_set_hidden(book_id, True)
    book = db_get_book(book_id)

    await query.edit_message_text(
        tr(ctx, "book_hidden", title=h(book["title"])),
        parse_mode=PM,
    )
    return ConversationHandler.END


def _admin_hidden_books() -> list[sqlite3.Row]:
    hidden: list[sqlite3.Row] = []
    for discussed in (False, True):
        hidden.extend(
            b
            for b in db_get_books_metadata(discussed=discussed, include_hidden=True)
            if b["hidden"]
        )
    return hidden


def _admin_all_books() -> list[sqlite3.Row]:
    return list(db_get_books_metadata(discussed=False, include_hidden=True)) + list(
        db_get_books_metadata(discussed=True, include_hidden=True)
    )


def _admin_notify_books() -> list[sqlite3.Row]:
    """Every entry for reminder pickers, highest current rating first.

    Covers the configured club entity (books, films, and the rest): they share
    one table, and the score is the same live total the voting list uses.
    """
    books = list(db_get_books(discussed=False, include_hidden=True)) + list(
        db_get_books(discussed=True, include_hidden=True)
    )
    books.sort(
        key=lambda book: (
            float(book["avg_score"]),
            int(book["vote_count"]),
            str(book["added_at"]),
            int(book["id"]),
        ),
        reverse=True,
    )
    return books


async def admin_unhide_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    if await _render_book_picker_page(
        query,
        ctx,
        _admin_hidden_books(),
        prefix="admin_unhide_pick",
        prompt_key="choose_unhide",
    ):
        return ADMIN_UNHIDE_CHOOSE
    _, book_id = query.data.split(":", 1)
    if book_id == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END

    book_id = int(book_id)
    db_set_hidden(book_id, False)
    book = db_get_book(book_id)

    await query.edit_message_text(
        tr(ctx, "book_unhidden", title=h(book["title"])),
        parse_mode=PM,
    )
    return ConversationHandler.END


async def admin_export_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    if await _render_book_picker_page(
        query,
        ctx,
        _admin_all_books(),
        prefix="admin_export_pick",
        prompt_key="choose_export",
    ):
        return ADMIN_EXPORT_CHOOSE
    _, book_id = query.data.split(":", 1)
    if book_id == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END

    book = db_get_book(int(book_id))
    if not book:
        await query.edit_message_text("Error: book not found.")
        return ConversationHandler.END

    payload = h(book_to_export_payload(book))
    await query.edit_message_text(
        tr(ctx, "export_done", payload=payload),
        parse_mode=PM,
    )
    return ConversationHandler.END


async def _finish_admin_import(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    book_data: Mapping[str, Any],
    source_entity: str | None,
    *,
    reply: Callable[..., Any],
) -> int:
    book_id = db_import_book(book_data)
    msg = tr(ctx, "import_done", title=h(book_data["title"]), book_id=book_id)
    if source_entity and source_entity != config.CLUB_ENTITY:
        msg += tr(
            ctx,
            "import_entity_mismatch",
            exported=h(source_entity),
            local=h(config.CLUB_ENTITY),
        )
    await reply(msg)
    schedule_new_book_notifications(ctx.job_queue, book_id, update.effective_user.id)
    return ConversationHandler.END


async def admin_import_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(tr(ctx, "admin_only"), parse_mode=PM)
        return ConversationHandler.END
    text = update.message.text or ""
    try:
        book_data, source_entity = parse_book_import(text)
    except ValueError as e:
        await update.message.reply_text(
            tr(ctx, "import_invalid", error=h(str(e))),
            parse_mode=PM,
            reply_markup=cancel_keyboard(get_lang(ctx)),
        )
        return ADMIN_IMPORT_WAIT

    similar = find_similar_book_titles(book_data["title"])
    if similar:
        ctx.user_data["pending_import"] = {
            "book_data": dict(book_data),
            "source_entity": source_entity,
        }
        lang = get_lang(ctx)
        await update.message.reply_text(
            tr(
                ctx,
                "similar_title_warning",
                title=h(book_data["title"]),
                matches=similar_title_warning_matches_text(similar),
            ),
            reply_markup=similar_title_confirm_keyboard(lang),
            parse_mode=PM,
        )
        return ADMIN_IMPORT_CONFIRM

    return await _finish_admin_import(
        update,
        ctx,
        book_data,
        source_entity,
        reply=lambda msg: update.message.reply_text(msg, parse_mode=PM),
    )


async def admin_import_similar_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _deny_non_admin_cb(update, ctx):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    _, action = query.data.split(":", 1)
    pending = ctx.user_data.pop("pending_import", None)
    if action == "no" or not pending:
        await query.edit_message_text(tr(ctx, "cancelled"), parse_mode=PM)
        return ConversationHandler.END
    return await _finish_admin_import(
        update,
        ctx,
        pending["book_data"],
        pending.get("source_entity"),
        reply=lambda msg: query.edit_message_text(msg, parse_mode=PM),
    )
