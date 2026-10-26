from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import UTC, datetime

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

import bookclub.config as config
from bookclub.db import (
    db_book_is_votable,
    db_get_book,
    db_get_books,
    db_get_user_setting,
    db_get_user_vote,
    db_set_user_setting,
    db_votes_use_attendance,
)
from bookclub.domain import is_admin
from bookclub.i18n import (
    COMMAND_SPECS,
    LANG_NATIVE_NAME,
    PM,
    format_ui,
    get_lang,
    next_ui_lang,
    s,
    tr,
)
from bookclub.logging_setup import logger
from bookclub.types import BookLike
from bookclub.ui import (
    _parse_list_callback,
    _show_list_format_prompt,
    book_card,
    books_top_n,
    fmt_dt_utc,
    h,
    linked_book_title,
    resolve_bot_username,
    score_display,
    score_keyboard,
    send_compact_book_list,
    vote_book_id_from_start_args,
)


def _build_commands() -> dict[str, list[BotCommand]]:
    return {
        lang: [BotCommand(name, format_ui(lang, desc)) for name, desc in specs]
        for lang, specs in COMMAND_SPECS.items()
    }


COMMANDS = _build_commands()

_ADMIN_MENU_COMMAND = "adminconsole"
_BOOK_PAGE_SESSION_MAX = 5


def _new_book_page_session(
    ctx: ContextTypes.DEFAULT_TYPE, scope: str, books: Sequence[BookLike]
) -> str:
    sessions = ctx.user_data.setdefault("_book_page_sessions", {})
    token = secrets.token_hex(4)
    sessions[token] = {
        "scope": scope,
        "book_ids": [int(book["id"]) for book in books],
    }
    while len(sessions) > _BOOK_PAGE_SESSION_MAX:
        sessions.pop(next(iter(sessions)))
    return token


def _book_page_markup(
    session: str,
    index: int,
    total: int,
    *,
    book_id: int | None = None,
    lang: str = "ru",
    user_vote: int | None = None,
    for_admin: bool = False,
) -> InlineKeyboardMarkup:
    rows = (
        [
            list(row)
            for row in score_keyboard(
                book_id, lang, user_vote, for_admin=for_admin
            ).inline_keyboard
        ]
        if book_id is not None
        else []
    )
    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(
            InlineKeyboardButton("◀️", callback_data=f"book_page:{session}:{index - 1}")
        )
    if index + 1 < total:
        nav.append(
            InlineKeyboardButton("▶️", callback_data=f"book_page:{session}:{index + 1}")
        )
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def book_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render one card at a time so large catalogs do not block all updates."""
    query = update.callback_query
    await query.answer()
    _, token, raw_index = query.data.split(":", 2)
    sessions = ctx.user_data.get("_book_page_sessions", {})
    session = sessions.get(token) if isinstance(sessions, dict) else None
    if not isinstance(session, dict):
        await query.edit_message_text(tr(ctx, "no_undiscussed"), parse_mode=PM)
        return
    scope = str(session.get("scope"))
    book_ids = session.get("book_ids")
    if not isinstance(book_ids, list) or not book_ids:
        await query.edit_message_text(
            tr(ctx, "no_discussed" if scope == "discussed" else "no_undiscussed"),
            parse_mode=PM,
        )
        return
    index = max(0, min(int(raw_index), len(book_ids) - 1))
    book = db_get_book(int(book_ids[index]))
    if book is None:
        book_ids.pop(index)
        if not book_ids:
            await query.edit_message_text(tr(ctx, "no_undiscussed"), parse_mode=PM)
            return
        index = min(index, len(book_ids) - 1)
        book = db_get_book(int(book_ids[index]))
    if book is None:
        await query.edit_message_text(tr(ctx, "no_undiscussed"), parse_mode=PM)
        return
    lang = get_lang(ctx)
    show_votes = scope != "discussed"
    user_vote = (
        None
        if not show_votes
        else db_get_user_vote(query.from_user.id, int(book["id"]))
    )
    header = (
        tr(ctx, "discussed_title")
        if scope == "discussed"
        else tr(ctx, "list_compact_title", count=len(book_ids))
    )
    await query.edit_message_text(
        f"{header}\n\n{book_card(book, lang, user_vote=user_vote)}",
        parse_mode=PM,
        reply_markup=_book_page_markup(
            token,
            index,
            len(book_ids),
            book_id=None if not show_votes else int(book["id"]),
            lang=lang,
            user_vote=user_vote,
            for_admin=show_votes and is_admin(query.from_user.id),
        ),
    )


def _settings_keyboard(ctx: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    next_label = LANG_NATIVE_NAME[next_ui_lang(get_lang(ctx))]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr(ctx, "settings_notify_btn"),
                    callback_data="settings:toggle_notify",
                )
            ],
            [
                InlineKeyboardButton(
                    tr(ctx, "settings_lang_btn", next_lang_label=next_label),
                    callback_data="settings:toggle_lang",
                )
            ],
        ]
    )


def _settings_text(ctx: ContextTypes.DEFAULT_TYPE, notify: int) -> str:
    val_str = tr(ctx, "settings_notify_on" if notify == 1 else "settings_notify_off")
    return (
        f"{tr(ctx, 'settings_title')}\n\n{tr(ctx, 'settings_notify_label')} {val_str}"
    )


def commands_for_user(lang: str, user_id: int) -> list[BotCommand]:
    """Telegram command menu for a user; admins see /adminconsole, others do not."""
    cmds = COMMANDS[lang]
    if user_id in config.ADMIN_IDS:
        return cmds
    return [c for c in cmds if c.command != _ADMIN_MENU_COMMAND]


async def refresh_admin_command_menus(bot: Bot) -> None:
    """Push admin command menus to each admin's private chat (if the bot can)."""
    from telegram import BotCommandScopeChat

    for admin_id in config.ADMIN_IDS:
        for lang in ("en", "ru"):
            scope = BotCommandScopeChat(chat_id=admin_id)
            try:
                await bot.delete_my_commands(scope=scope)
                await bot.set_my_commands(
                    commands_for_user(lang, admin_id), scope=scope
                )
            except Exception as e:
                logger.warning(
                    "Could not set admin commands for user %s (%s): %s",
                    admin_id,
                    lang,
                    e,
                )


async def set_user_commands(bot: Bot, update: Update, lang: str) -> None:
    """Set the command menu for a specific user in their chosen language.
    Uses BotCommandScopeChatMember for groups, BotCommandScopeChat for private."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    chat_id = chat.id
    user_id = user.id
    menu = commands_for_user(lang, user_id)
    try:
        if chat.type == "private":
            scope: BotCommandScopeChat | BotCommandScopeChatMember = (
                BotCommandScopeChat(chat_id=chat_id)
            )
            await bot.delete_my_commands(scope=scope)
            await bot.set_my_commands(menu, scope=scope)
        else:
            scope = BotCommandScopeChatMember(chat_id=chat_id, user_id=user_id)
            await bot.delete_my_commands(scope=scope)
            await bot.set_my_commands(menu, scope=scope)
    except Exception as e:
        logger.warning(f"Could not set commands for user {user_id}: {e}")


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    notify = db_get_user_setting(user_id, "notify_new_books")

    # -1 means not set, we'll treat it as Off (0) for the UI if they just run /settings
    # but the logic for /list_and_vote will still trigger the opt-in if it's -1.
    await update.message.reply_text(
        _settings_text(ctx, notify),
        reply_markup=_settings_keyboard(ctx),
        parse_mode=PM,
    )


async def settings_choice_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data.split(":")

    if data[1] == "toggle_notify":
        await query.answer()
        current = db_get_user_setting(user_id, "notify_new_books")
        new_val = 1 if current <= 0 else 0
        db_set_user_setting(user_id, "notify_new_books", new_val)
        await query.edit_message_text(
            _settings_text(ctx, new_val),
            reply_markup=_settings_keyboard(ctx),
            parse_mode=PM,
        )
    elif data[1] == "toggle_lang":
        new_lang = next_ui_lang(get_lang(ctx))
        ctx.user_data["lang"] = new_lang
        await set_user_commands(ctx.bot, update, new_lang)
        await query.answer(tr(ctx, "lang_set"))

        notify = db_get_user_setting(user_id, "notify_new_books")
        await query.edit_message_text(
            _settings_text(ctx, notify),
            reply_markup=_settings_keyboard(ctx),
            parse_mode=PM,
        )
    elif data[1] == "optin":
        val = int(data[2])
        db_set_user_setting(user_id, "notify_new_books", val)
        await query.answer(tr(ctx, "notify_optin_success"))
        # After choosing, we continue with the list if possible?
        # Actually, the opt-in was triggered by /list_and_vote.
        # Let's just say "Settings saved" and let them run /list_and_vote again or just finish.
        # But the prompt said "ask... first time one runs list command".
        # Better to show the list after they choose.
        await list_choice_cb(update, ctx)


async def _reply_private_vote_card(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, book_id: int
) -> None:
    """Send one votable entry's card into the chat that opened the deep link."""
    if update.message is None:
        return
    lang = get_lang(ctx)
    if not db_book_is_votable(book_id):
        await update.message.reply_text(tr(ctx, "voting_closed"), parse_mode=PM)
        return
    book = db_get_book(book_id)
    if book is None:
        await update.message.reply_text(tr(ctx, "voting_closed"), parse_mode=PM)
        return
    user_vote = db_get_user_vote(update.effective_user.id, book_id)
    await update.message.reply_text(
        book_card(book, lang, user_vote=user_vote),
        parse_mode=PM,
        reply_markup=score_keyboard(
            book_id,
            lang,
            user_vote,
            for_admin=is_admin(update.effective_user.id),
        ),
        disable_web_page_preview=True,
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await set_user_commands(ctx.bot, update, get_lang(ctx))
    book_id = vote_book_id_from_start_args(getattr(ctx, "args", None))
    if book_id is not None and update.message is not None:
        await _reply_private_vote_card(update, ctx, book_id)
        return
    text = tr(ctx, "welcome")
    if is_admin(update.effective_user.id):
        text += tr(ctx, "welcome_admin_suffix")
    await update.message.reply_text(text, parse_mode=PM)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    import os
    import subprocess

    last_commit = None
    # 1. Try git log — commit time as a Unix timestamp, so it goes through the
    #    same formatter as everything else (server-local time + UTC offset)
    #    instead of git's own zone-dependent rendering.
    try:
        if os.path.exists(".git"):
            ct = (
                subprocess.check_output(
                    ["git", "log", "-1", "--format=%ct"], stderr=subprocess.DEVNULL
                )
                .decode("utf-8")
                .strip()
            )
            last_commit = fmt_dt_utc(datetime.fromtimestamp(int(ct), tz=UTC))
    except Exception as e:
        logger.warning(f"Could not get last commit via git: {e}")

    # 2. Fallback to file mtime
    if not last_commit:
        try:
            mtime = os.path.getmtime(__file__)
            last_commit = fmt_dt_utc(datetime.fromtimestamp(mtime, tz=UTC))
        except Exception as e:
            logger.warning(f"Could not get file mtime: {e}")
            last_commit = "unknown"

    text = tr(
        ctx,
        "info_msg",
        bot_name=s(get_lang(ctx), "bot_name"),
        last_commit=last_commit,
        github_repo=config.GITHUB_REPO,
    )
    await update.message.reply_text(text, parse_mode=PM, disable_web_page_preview=True)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(tr(ctx, "list_all_btn"), callback_data="list:all"),
                InlineKeyboardButton(
                    tr(ctx, "list_unvoted_btn"), callback_data="list:unvoted"
                ),
            ]
        ]
    )
    await update.message.reply_text(
        tr(ctx, "list_prompt"), reply_markup=keyboard, parse_mode=PM
    )


async def list_choice_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    # We might be called from settings_choice_cb, so query might be None-ish or already answered
    if query.data.startswith("settings:optin:"):
        # We need to recover the original list choice if we want to be seamless.
        # But for simplicity, let's just show 'all' if they just opted in,
        # or we could have stored it in user_data.
        filter_choice = ctx.user_data.get("pending_list_choice", "all")
        format_choice = None
        user_id = query.from_user.id
        # We don't call query.answer() here because it was already answered in settings_choice_cb
    else:
        await query.answer()
        user_id = query.from_user.id
        assert query.data is not None
        filter_choice, format_choice = _parse_list_callback(query.data)

    # Check for notification opt-in
    if db_get_user_setting(user_id, "notify_new_books") == -1:
        ctx.user_data["pending_list_choice"] = filter_choice
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr(ctx, "notify_optin_yes"), callback_data="settings:optin:1"
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr(ctx, "notify_optin_no"), callback_data="settings:optin:0"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            tr(ctx, "notify_optin_prompt"), reply_markup=keyboard, parse_mode=PM
        )
        return

    if format_choice is None:
        await _show_list_format_prompt(query, ctx, filter_choice)
        return

    lang = get_lang(ctx)
    choice = filter_choice

    user_id_unvoted = user_id if choice == "unvoted" else None
    books = db_get_books(discussed=False, user_id_unvoted=user_id_unvoted)

    if not books:
        if choice == "unvoted":
            # Check if there are ANY books at all
            all_undiscussed = db_get_books(discussed=False)
            if not all_undiscussed:
                text = tr(ctx, "no_undiscussed")
            else:
                text = "✅ " + tr(ctx, "all_voted")
        else:
            text = tr(ctx, "no_undiscussed")

        try:
            await query.edit_message_text(text, parse_mode=PM)
        except Exception as e:
            if "Message to edit not found" in str(e):
                await ctx.bot.send_message(
                    chat_id=update.effective_chat.id, text=text, parse_mode=PM
                )
            else:
                raise
        return

    chat_id = update.effective_chat.id

    if format_choice == "compact":
        try:
            await query.delete_message()
        except Exception as e:
            if "Message to delete not found" not in str(e):
                raise
        await send_compact_book_list(ctx.bot, chat_id, books, ctx)
        return

    first = books[0]
    session = _new_book_page_session(ctx, choice, books)
    uv = db_get_user_vote(user_id, int(first["id"]))
    await query.edit_message_text(
        f"{tr(ctx, 'list_compact_title', count=len(books))}\n\n"
        f"{book_card(first, lang, user_vote=uv)}",
        parse_mode=PM,
        reply_markup=_book_page_markup(
            session,
            0,
            len(books),
            book_id=int(first["id"]),
            lang=lang,
            user_vote=uv,
            for_admin=is_admin(user_id),
        ),
    )


async def cmd_discussed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(ctx)
    books = db_get_books(discussed=True)
    if not books:
        await update.message.reply_text(tr(ctx, "no_discussed"), parse_mode=PM)
        return
    first = books[0]
    session = _new_book_page_session(ctx, "discussed", books)
    await update.message.reply_text(
        f"{tr(ctx, 'discussed_title')}\n\n{book_card(first, lang)}",
        parse_mode=PM,
        reply_markup=_book_page_markup(session, 0, len(books)),
    )


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(ctx)
    books = db_get_books(discussed=False)
    if not books:
        await update.message.reply_text(tr(ctx, "no_undiscussed"), parse_mode=PM)
        return

    # Show top 5, but if there's a tie for the 5th place, show all tied books.
    # Sorting is already done in db_get_books by (avg_score DESC, vote_count DESC, added_at DESC)
    top_books = books_top_n(books)
    bot_username = await resolve_bot_username(ctx.bot)

    lines = [tr(ctx, "top_title")]
    for i, book in enumerate(top_books, 1):
        score_val = book["avg_score"]
        score_fmt = f"{score_val:g}"
        author_part = (
            f" — {h(book['author'])}" if config.entry_field_enabled("author") else ""
        )
        meta: list[str] = []
        if config.entry_field_enabled("fiction"):
            fiction_label = (
                s(lang, "fiction_label")
                if book["fiction"]
                else s(lang, "nonfiction_label")
            )
            meta.append(h(fiction_label))
        if config.entry_field_enabled("pages"):
            meta.append(f"{h(str(book['pages']))} {h(s(lang, 'pages_label'))}")
        meta.append(f"<b>{h(s(lang, 'score_label'))}: {score_fmt}</b>")
        title = linked_book_title(str(book["title"]), int(book["id"]), bot_username)
        lines.append(
            f"{i}. {title}{author_part}\n"
            f"   {'  •  '.join(meta)}\n"
            f"   {score_display(book, lang)}"
        )

    async def _send_top_text(text: str) -> None:
        # Title links point at t.me; skip the link preview so the ranking stays compact.
        await update.message.reply_text(
            text, parse_mode=PM, disable_web_page_preview=True
        )

    # Send as one message; if it exceeds Telegram's limit split into chunks
    MAX = 4000
    message = "\n\n".join(lines)
    if len(message) <= MAX:
        await _send_top_text(message)
    else:
        chunk = ""
        for line in lines:
            candidate = (chunk + "\n\n" + line).lstrip("\n")
            if len(candidate) > MAX:
                await _send_top_text(chunk)
                chunk = line
            else:
                chunk = candidate
        if chunk:
            await _send_top_text(chunk)

    # Add "How a score is calculated" button
    reply_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr(ctx, "score_calc_btn"), callback_data="score_calc_info"
                )
            ]
        ]
    )
    await update.message.reply_text(
        "---",  # Visual separator or just a small text
        reply_markup=reply_markup,
        parse_mode=PM,
    )


async def score_calc_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    key = (
        "score_calc_info_attendance" if db_votes_use_attendance() else "score_calc_info"
    )
    await query.answer(text=tr(ctx, key), show_alert=True)
