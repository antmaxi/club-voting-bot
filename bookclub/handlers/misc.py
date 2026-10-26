from __future__ import annotations

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import bookclub.config as config
from bookclub.db import db_book_is_votable, db_cast_vote, db_upsert_club_user
from bookclub.domain import is_admin, require_book
from bookclub.i18n import PM, get_lang, s, tr, vote_label_text
from bookclub.logging_setup import logger
from bookclub.ui import book_card, score_keyboard


def _score_markup_with_navigation(
    query: object,
    book_id: int,
    lang: str,
    user_vote: int | None = None,
    *,
    for_admin: bool = False,
) -> InlineKeyboardMarkup:
    rows = [
        list(row)
        for row in score_keyboard(
            book_id, lang, user_vote, for_admin=for_admin
        ).inline_keyboard
    ]
    message = getattr(query, "message", None)
    markup = getattr(message, "reply_markup", None)
    if markup:
        for row in markup.inline_keyboard:
            if any(
                (button.callback_data or "").startswith("book_page:") for button in row
            ):
                rows.append(list(row))
    return InlineKeyboardMarkup(rows)


async def conv_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(tr(ctx, "cancelled"), parse_mode=PM)
    elif update.message:
        await update.message.reply_text(tr(ctx, "cancelled"), parse_mode=PM)
    ctx.user_data.pop("pending_import", None)
    ctx.user_data.clear()
    return ConversationHandler.END


# ── /vote removed — voting now done inline in /list_and_vote ─────────────────


async def vote_cast_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline voting callbacks — works for both private and group chats.

    Vote statistics are recalculated after each vote and the message is updated
    for all viewers in the group chat."""
    from telegram.error import BadRequest

    query = update.callback_query
    lang = get_lang(ctx)
    _, book_id, score = query.data.split(":")
    if book_id == "cancel":
        await query.answer()
        await query.edit_message_text(s(lang, "cancelled"))
        return
    book_id, score = int(book_id), int(score)
    if score not in (-1, 0, 1):
        logger.warning(f"vote_cast_cb: ignoring out-of-range score {score}")
        await query.answer()
        return

    user_id = query.from_user.id

    if not db_book_is_votable(book_id):
        await query.answer(tr(ctx, "voting_closed"), show_alert=True)
        return

    # The write returns the previous value, avoiding two extra vote lookups.
    old_vote = db_cast_vote(user_id, book_id, score)
    db_upsert_club_user(
        query.from_user.id,
        query.from_user.full_name or "",
        query.from_user.username,
    )

    book = require_book(book_id)
    uv = score

    chat = update.effective_chat

    # === SAME-VOTE RE-VOTE: nothing actually changed in statistics ===
    if old_vote is not None and old_vote == score:
        if chat is not None and chat.type != "private":
            # Group chat: acknowledge voter, skip edit entirely
            # (statistics would be identical anyway)
            vote_label = vote_label_text(config.CHAT_LANG, uv)
            await query.answer(
                tr(config.CHAT_LANG, "vote_registered", label=vote_label)
            )
            logger.info(
                f"[RE-VOTE] User {user_id} re-voted '{vote_label}' on book {book_id} "
                f"('{book['title']}') in group chat — no edit performed"
            )
            return
        else:
            # Private chat: same vote, message would be identical — skip edit
            vote_label = vote_label_text(lang, uv)
            await query.answer(tr(lang, "vote_registered", label=vote_label))
            logger.info(
                f"[RE-VOTE] User {user_id} re-voted '{vote_label}' on book {book_id} "
                f"('{book['title']}') in private chat — no edit performed"
            )
            return

    # === NORMAL VOTE PROCESSING (first vote or changed vote) ===
    if chat is not None and chat.type != "private":
        # Shared message: visible to whole club, show AGGREGATED statistics only
        vote_label = vote_label_text(config.CHAT_LANG, uv)
        await query.answer(tr(config.CHAT_LANG, "vote_registered", label=vote_label))

        # Build the message content with fresh statistics
        new_text = book_card(book, config.CHAT_LANG)
        new_markup = _score_markup_with_navigation(query, book_id, config.CHAT_LANG)

        # Attempt to update — on race condition, fetch fresh data and retry once
        max_retries = 2
        for attempt in range(max_retries):
            try:
                await query.edit_message_text(
                    new_text,
                    parse_mode=PM,
                    reply_markup=new_markup,
                )
                break  # Success — exit retry loop
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    if attempt < max_retries - 1:
                        # Another edit completed first — refetch fresh data and retry
                        logger.debug(
                            f"vote_cast_cb: race condition detected (attempt {attempt+1}/{max_retries}), "
                            f"refetching book {book_id}..."
                        )
                        book = require_book(book_id)  # Fresh aggregates
                        new_text = book_card(book, config.CHAT_LANG)
                        new_markup = _score_markup_with_navigation(
                            query, book_id, config.CHAT_LANG
                        )
                    else:
                        # Final attempt failed — log but don't propagate error
                        logger.info(
                            f"vote_cast_cb: message update skipped for book {book_id}, "
                            f"user {user_id} (concurrent edit with same final state)"
                        )
                else:
                    # Different error — propagate
                    raise
        return

    await query.answer()
    # Private chat: show individual vote inline (always unique per user)
    try:
        await query.edit_message_text(
            book_card(book, lang, user_vote=uv),
            parse_mode=PM,
            reply_markup=_score_markup_with_navigation(
                query, book_id, lang, uv, for_admin=is_admin(user_id)
            ),
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.info(
                f"vote_cast_cb: no-op edit for book {book_id}, user {user_id} "
                f"in private chat (likely concurrent edit)"
            )
        else:
            raise
