from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram import Bot, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import InlineKeyboardButtonLimit
from telegram.error import RetryAfter
from telegram.ext import ContextTypes, ConversationHandler

import bookclub.config as config
from bookclub.cefr import CEFR_LEVELS, language_levels_display
from bookclub.config import (
    ADMIN_MEETING_ATTENDEES,
    ADMIN_NOTIFY_CHAT_PICK,
    ADMIN_NOTIFY_PICK,
    MEETING_ATTENDEES_PAGE_SIZE,
    NOTIFY_BOOKS_PAGE_SIZE,
    entry_field_enabled,
)
from bookclub.db import (
    club_user_has_shown_name,
    db_meeting_user_suggestions,
    db_upsert_club_user,
    format_club_user_display,
)
from bookclub.i18n import PM, T, get_lang, s, tr, vote_label_text
from bookclub.logging_setup import logger
from bookclub.original_languages import (
    ORIGINAL_LANGUAGE_CODES,
    display_original_language,
)
from bookclub.types import BookLike

CONV_CANCEL = "conv_cancel"


def cancel_button(lang: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(s(lang, "cancel_btn"), callback_data=CONV_CANCEL)


def save_button(lang: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(s(lang, "add_save_btn"), callback_data="add_save")


def add_wizard_footer(
    lang: str, *, show_save: bool = False
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if show_save:
        rows.append([save_button(lang)])
    rows.append([cancel_button(lang)])
    return rows


def cancel_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[cancel_button(lang)]])


def format_user(book: BookLike) -> str:
    """Return @username if available, otherwise fall back to display name."""
    username = book["added_by_username"]
    if username:
        return f"@{h(username)}"
    return h(book["added_by_name"] or "unknown")


def h(text: str) -> str:
    # `"` must be escaped too: h() is used inside href="..." attributes, where a
    # raw quote breaks out of the attribute and makes Telegram reject the whole
    # message — which would take down /list_and_vote for everyone, not just the author.
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def similar_title_warning_matches_text(
    matches: Sequence[tuple[int, str, float]], *, limit: int = 5
) -> str:
    lines: list[str] = []
    for _book_id, title, ratio in matches[:limit]:
        pct = int(round(ratio * 100))
        lines.append(f"• {h(title)} ({pct}%)")
    return "\n".join(lines)


def similar_title_confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    s(lang, "similar_title_confirm_btn"),
                    callback_data="title_sim:yes",
                ),
                InlineKeyboardButton(
                    s(lang, "similar_title_cancel_btn"),
                    callback_data="title_sim:no",
                ),
            ],
            add_nav_buttons(lang, show_back=True, show_forward=False),
            *add_wizard_footer(lang, show_save=True),
        ]
    )


def add_ai_choice_keyboard(
    lang: str, *, show_save: bool = True
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    s(lang, "add_ai_yes_btn"), callback_data="add_ai:yes"
                ),
                InlineKeyboardButton(
                    s(lang, "add_ai_no_btn"), callback_data="add_ai:no"
                ),
            ],
            add_nav_buttons(lang, show_back=True, show_forward=False),
            *add_wizard_footer(lang, show_save=show_save),
        ]
    )


def add_confirm_keyboard(lang: str, *, show_save: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    s(lang, "add_confirm_btn"), callback_data="add_confirm"
                )
            ],
            add_nav_buttons(lang, show_back=True, show_forward=False),
            *add_wizard_footer(lang, show_save=show_save),
        ]
    )


def add_start_keyboard(
    lang: str, *, llm: bool, has_drafts: bool
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if llm:
        rows.append(
            [
                InlineKeyboardButton(
                    s(lang, "add_ai_yes_btn"), callback_data="add_start:ai"
                ),
                InlineKeyboardButton(
                    s(lang, "add_ai_no_btn"), callback_data="add_start:manual"
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    s(lang, "add_start_new_btn"), callback_data="add_start:manual"
                )
            ]
        )
    if has_drafts:
        rows.append(
            [
                InlineKeyboardButton(
                    s(lang, "add_continue_btn"), callback_data="add_start:drafts"
                )
            ]
        )
    rows.append([cancel_button(lang)])
    return InlineKeyboardMarkup(rows)


def add_drafts_keyboard(
    lang: str, drafts: Sequence[tuple[int, str]]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for draft_id, title in drafts:
        label = title.strip() or s(lang, "add_draft_untitled")
        if len(label) > 40:
            label = label[:37] + "…"
        rows.append(
            [
                InlineKeyboardButton(label, callback_data=f"add_draft:{draft_id}"),
                InlineKeyboardButton("🗑", callback_data=f"add_draft_del:{draft_id}"),
            ]
        )
    nav = add_nav_buttons(lang, show_back=True, show_forward=False)
    if nav:
        rows.append(nav)
    rows.append([cancel_button(lang)])
    return InlineKeyboardMarkup(rows)


def add_edit_button(
    lang: str, value: str, *, use_inline: bool = False
) -> InlineKeyboardButton:
    """Put ``value`` in the compose field (inline) or copy it (clipboard)."""
    if use_inline and 1 <= len(value) <= InlineKeyboardButtonLimit.MAX_COPY_TEXT:
        return InlineKeyboardButton(
            s(lang, "add_edit_btn"),
            switch_inline_query_current_chat=value,
        )
    if 1 <= len(value) <= InlineKeyboardButtonLimit.MAX_COPY_TEXT:
        return InlineKeyboardButton(
            s(lang, "add_edit_btn"),
            copy_text=CopyTextButton(value),
        )
    return InlineKeyboardButton(s(lang, "add_edit_btn"), callback_data="add_edit")


def add_nav_buttons(
    lang: str,
    *,
    show_back: bool = True,
    show_forward: bool = False,
    edit_value: str | None = None,
    use_inline: bool = False,
) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    if show_back:
        buttons.append(
            InlineKeyboardButton(s(lang, "add_back_btn"), callback_data="add_back")
        )
    if edit_value:
        buttons.append(add_edit_button(lang, edit_value, use_inline=use_inline))
    if show_forward:
        buttons.append(
            InlineKeyboardButton(
                s(lang, "add_forward_btn"), callback_data="add_forward"
            )
        )
    return buttons


def add_nav_keyboard(
    lang: str,
    *,
    show_back: bool = True,
    show_forward: bool = False,
    edit_value: str | None = None,
    use_inline: bool = False,
    show_save: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row = add_nav_buttons(
        lang,
        show_back=show_back,
        show_forward=show_forward,
        edit_value=edit_value,
        use_inline=use_inline,
    )
    if row:
        rows.append(row)
    rows.extend(add_wizard_footer(lang, show_save=show_save))
    return InlineKeyboardMarkup(rows)


def add_back_keyboard(lang: str) -> InlineKeyboardMarkup:
    return add_nav_keyboard(lang, show_back=True, show_forward=False)


def usable_review_choices(choices: object) -> list[dict[str, Any]]:
    if not isinstance(choices, list):
        return []
    usable: list[dict[str, Any]] = []
    for item in choices:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url.strip():
                usable.append(item)
    return usable


def review_choice_label(choice: dict[str, Any]) -> str:
    """Button text: rating count, edition detail, then author."""
    bits: list[str] = []
    count = choice.get("review_count")
    if isinstance(count, int) and not isinstance(count, bool):
        bits.append(str(count))
    detail = str(choice.get("detail") or "").strip()
    author = str(choice.get("author") or "").strip()
    if detail:
        bits.append(detail)
    if author and author.casefold() not in detail.casefold():
        bits.append(author)
    if not bits:
        title = str(choice.get("title") or "").strip()
        bits.append(title or str(choice.get("url") or ""))
    text = " · ".join(bits)
    if len(text) > 60:
        return text[:59] + "…"
    return text


def review_choice_text(lang: str, choices: list[dict[str, Any]]) -> str:
    lines = [s(lang, "review_choice_prompt"), ""]
    for index, choice in enumerate(choices, 1):
        url = h(str(choice.get("url") or ""))
        label = h(review_choice_label(choice))
        lines.append(f'{index}. <a href="{url}">{label}</a>')
    lines.append("")
    lines.append(s(lang, "ask_review"))
    lines.append(s(lang, "add_back_hint"))
    return "\n".join(lines)


def review_choice_keyboard(
    lang: str, choices: list[dict[str, Any]], *, show_save: bool = False
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, choice in enumerate(choices):
        rows.append(
            [
                InlineKeyboardButton(
                    review_choice_label(choice),
                    callback_data=f"add_review_pick:{index}",
                )
            ]
        )
    nav = add_nav_buttons(lang, show_back=True, show_forward=False)
    if nav:
        rows.append(nav)
    rows.extend(add_wizard_footer(lang, show_save=show_save))
    return InlineKeyboardMarkup(rows)


def fmt_dt_utc(dt: datetime) -> str:
    """Format a datetime in the configured display timezone (default UTC+2).

    Naive datetimes are treated as UTC instants. Aware datetimes are converted
    to the display zone. The label uses UTC±HH:MM for the display offset.
    """
    tz = config.display_timezone()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(tz)
    off = local.strftime("%z")
    return local.strftime("%Y-%m-%d %H:%M:%S") + f" UTC{off[:3]}:{off[3:5]}"


SCORE_EMOJI = {1: "✅", 0: "😐", -1: "❌", None: "—"}


def score_display(book: BookLike, lang: str = "en") -> str:
    """Show vote tally: ✅12  😐3  ❌2  (N votes)"""
    yes = book["votes_yes"]
    meh = book["votes_meh"]
    no = book["votes_no"]
    total = book["vote_count"]
    if total == 0:
        votes_label = T[lang]["votes_label"]
        if not callable(votes_label):
            raise TypeError("votes_label must be callable")
        return str(votes_label(0))
    votes_label = T[lang]["votes_label"]
    if not callable(votes_label):
        raise TypeError("votes_label must be callable")
    return f"✅ {yes}  😐 {meh}  ❌ {no}  {votes_label(total)}"


def book_card(book: BookLike, lang: str = "en", user_vote: int | None = None) -> str:
    fiction_label = (
        s(lang, "fiction_label") if book["fiction"] else s(lang, "nonfiction_label")
    )
    lines = [f"{s(lang, 'card_icon')} <b>{h(book['title'])}</b>"]
    if entry_field_enabled("author"):
        lines.append(f"{s(lang, 'subtitle_icon')} {h(book['author'])}")
    details: list[str] = []
    if entry_field_enabled("fiction"):
        details.append(f"📂 {h(fiction_label)}")
    if entry_field_enabled("pages"):
        details.append(f"📄 {h(str(book['pages']))} {h(s(lang, 'pages_label'))}")
    if details:
        lines.append("  •  ".join(details))
    extra: list[str] = []
    # sqlite3.Row: `key in row` checks values, not column names.
    has_language_levels = "language_levels" in book.keys()  # noqa: SIM118
    levels_raw = book["language_levels"] if has_language_levels else None
    levels_text = language_levels_display(levels_raw)
    if entry_field_enabled("language_levels") and levels_text:
        extra.append(
            f"🎓 {h(s(lang, 'language_levels_label'))}: {h(levels_text)}",
        )
    creation_year = book["creation_year"]
    if entry_field_enabled("creation_year") and creation_year is not None:
        extra.append(
            f"📅 {h(s(lang, 'creation_year_label'))}: {h(str(creation_year))}",
        )
    orig_lang = book["original_language"] if book["original_language"] else None
    if entry_field_enabled("original_language") and orig_lang:
        extra.append(
            f"🌐 {h(s(lang, 'original_language_label'))}: {h(display_original_language(str(orig_lang), lang))}",
        )
    lines.extend(extra)
    lines.append(score_display(book, lang))
    if user_vote is not None:
        vote_label = vote_label_text(lang, user_vote)
        lines[-1] += f"  <i>({h(s(lang, 'your_vote'))}: {h(vote_label)})</i>"
    if entry_field_enabled("review") and book["review_link"]:
        lines.append(
            f'🔗 <a href="{h(book["review_link"])}">{h(s(lang, "review_label"))}</a>'
        )
    if entry_field_enabled("description") and book["description"]:
        lines += ["", f"<i>{h(book['description'])}</i>"]
    meta = (
        f"<i>{h(s(lang, 'added_by'))}: {h(format_user(book))}"
        f"  •  {h(s(lang, 'added_on'))}: {h(book['added_at'])}"
    )
    if book["discussed"] and book["discussed_at"]:
        meta += f"  •  ✅ {h(s(lang, 'discussed_on'))}: {h(book['discussed_at'])}"
    meta += "</i>"
    lines += ["", meta]
    return "\n".join(lines)


def book_compact_line(index: int, book: BookLike) -> str:
    year = book["creation_year"]
    year_suffix = (
        f" ({year})"
        if entry_field_enabled("creation_year") and year is not None
        else ""
    )
    author_part = f" — {h(book['author'])}" if entry_field_enabled("author") else ""
    score_fmt = f"{book['avg_score']:g}"
    return (
        f"{index}. <b>{score_fmt}</b> <b>{h(book['title'])}</b>"
        f"{author_part}{year_suffix}"
    )


def compact_book_list_lines(
    books: Sequence[BookLike], ctx_or_lang: ContextTypes.DEFAULT_TYPE | str
) -> list[str]:
    """Header + compact lines, matching /list_and_vote compact format."""
    header = tr(ctx_or_lang, "list_compact_title", count=len(books))
    return [header] + [book_compact_line(i, book) for i, book in enumerate(books, 1)]


TELEGRAM_MESSAGE_MAX = 4000


async def send_chunked_html_messages(
    bot: Bot,
    chat_id: int,
    lines: Sequence[str],
    *,
    joiner: str = "\n",
) -> None:
    """Send lines in as few Telegram messages as possible (HTML parse mode)."""
    if not lines:
        return
    chunk = ""
    for line in lines:
        candidate = joiner.join(filter(None, [chunk, line])) if chunk else line
        if len(candidate) > TELEGRAM_MESSAGE_MAX:
            if chunk:
                await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=PM)
            chunk = line
            if len(chunk) > TELEGRAM_MESSAGE_MAX:
                await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=PM)
                chunk = ""
        else:
            chunk = candidate
    if chunk:
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=PM)


async def send_compact_book_list(
    bot: Bot,
    chat_id: int,
    books: Sequence[BookLike],
    ctx_or_lang: ContextTypes.DEFAULT_TYPE | str,
) -> None:
    """Send the compact ranking list to chat_id (no-op if books is empty)."""
    if not books:
        return
    await send_chunked_html_messages(
        bot, chat_id, compact_book_list_lines(books, ctx_or_lang), joiner="\n"
    )


def _parse_list_callback(data: str) -> tuple[str, str | None]:
    """Return (filter: all|unvoted, format: compact|full|None)."""
    parts = data.split(":")
    if parts[0] != "list" or len(parts) not in (2, 3):
        raise ValueError(f"unexpected list callback: {data!r}")
    filter_choice = parts[1]
    format_choice = parts[2] if len(parts) == 3 else None
    if filter_choice not in ("all", "unvoted"):
        raise ValueError(f"unexpected list filter: {filter_choice!r}")
    if format_choice is not None and format_choice not in ("compact", "full"):
        raise ValueError(f"unexpected list format: {format_choice!r}")
    return filter_choice, format_choice


async def _show_list_format_prompt(
    query: Any, ctx: ContextTypes.DEFAULT_TYPE, filter_choice: str
) -> None:
    ctx.user_data["pending_list_choice"] = filter_choice
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr(ctx, "list_compact_btn"),
                    callback_data=f"list:{filter_choice}:compact",
                ),
                InlineKeyboardButton(
                    tr(ctx, "list_full_btn"),
                    callback_data=f"list:{filter_choice}:full",
                ),
            ]
        ]
    )
    await query.edit_message_text(
        tr(ctx, "list_format_prompt"), reply_markup=keyboard, parse_mode=PM
    )


def books_keyboard(
    books: Sequence[BookLike], prefix: str, cancel_label: str, *, page: int = 0
) -> InlineKeyboardMarkup:
    page_size = config.PICKER_PAGE_SIZE
    page = max(0, page)
    start = page * page_size
    buttons = []
    for b in books[start : start + page_size]:
        label = f"{b['title']} — {b['author']}"
        if len(label) > 48:
            label = label[:45] + "…"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"{prefix}:{b['id']}")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("◀️", callback_data=f"{prefix}:page:{page - 1}")
        )
    if start + page_size < len(books):
        nav.append(
            InlineKeyboardButton("▶️", callback_data=f"{prefix}:page:{page + 1}")
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton(cancel_label, callback_data=f"{prefix}:cancel")]
    )
    return InlineKeyboardMarkup(buttons)


def _notify_book_ids(ctx: ContextTypes.DEFAULT_TYPE) -> set[int]:
    raw = ctx.user_data.get("notify_book_ids")
    if raw is None:
        raw = set()
        ctx.user_data["notify_book_ids"] = raw
    return raw


def notify_books_keyboard(
    lang: str,
    books: Sequence[BookLike],
    selected: set[int],
    page: int,
    prefix: str,
    *,
    done_label_key: str,
) -> InlineKeyboardMarkup:
    total = len(books)
    page_size = NOTIFY_BOOKS_PAGE_SIZE
    start = page * page_size
    chunk = books[start : start + page_size]
    buttons: list[list[InlineKeyboardButton]] = []
    for b in chunk:
        bid = int(b["id"])
        mark = "✅" if bid in selected else "⬜"
        label = f"{b['title']} — {b['author']}"
        if b["discussed"]:
            label = f"📌 {label}"
        if len(label) > 42:
            label = label[:39] + "…"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{mark} {label}",
                    callback_data=f"{prefix}:toggle:{bid}:{page}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if start > 0:
        nav.append(
            InlineKeyboardButton("◀️", callback_data=f"{prefix}:page:{page - 1}")
        )
    if start + page_size < total:
        nav.append(
            InlineKeyboardButton("▶️", callback_data=f"{prefix}:page:{page + 1}")
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                s(lang, done_label_key),
                callback_data=f"{prefix}:done",
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton(s(lang, "cancel_btn"), callback_data=f"{prefix}:cancel")]
    )
    return InlineKeyboardMarkup(buttons)


async def show_notify_books_picker(
    update_or_query: Any,
    ctx: ContextTypes.DEFAULT_TYPE,
    books: Sequence[BookLike],
    *,
    page: int = 0,
    is_callback: bool = False,
    prefix: str,
    prompt_key: str,
    done_label_key: str,
) -> int:
    lang = get_lang(ctx)
    selected = _notify_book_ids(ctx)
    text = tr(ctx, prompt_key, count=len(selected))
    markup = notify_books_keyboard(
        lang, books, selected, page, prefix, done_label_key=done_label_key
    )
    if is_callback:
        await update_or_query.edit_message_text(
            text, reply_markup=markup, parse_mode=PM
        )
    else:
        await update_or_query.message.reply_text(
            text, reply_markup=markup, parse_mode=PM
        )
    ctx.user_data["notify_books_page"] = page
    if prefix == "admin_notify_chat_pick":
        return ADMIN_NOTIFY_CHAT_PICK
    return ADMIN_NOTIFY_PICK


def meetings_keyboard(
    meetings: Sequence[BookLike], prefix: str, cancel_label: str, *, page: int = 0
) -> InlineKeyboardMarkup:
    page_size = config.PICKER_PAGE_SIZE
    page = max(0, page)
    start = page * page_size
    buttons = []
    for m in meetings[start : start + page_size]:
        label = f"{m['meeting_date']} — {m['title']}"
        if m["attendee_count"]:
            label += f" ({m['attendee_count']})"
        if len(label) > 48:
            label = label[:45] + "…"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"{prefix}:{m['id']}")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("◀️", callback_data=f"{prefix}:page:{page - 1}")
        )
    if start + page_size < len(meetings):
        nav.append(
            InlineKeyboardButton("▶️", callback_data=f"{prefix}:page:{page + 1}")
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton(cancel_label, callback_data=f"{prefix}:cancel")]
    )
    return InlineKeyboardMarkup(buttons)


def meeting_view_keyboard(lang: str, meeting_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    s(lang, "meeting_view_edit_btn"),
                    callback_data=f"admin_meeting_view:edit:{meeting_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    s(lang, "meeting_view_delete_btn"),
                    callback_data=f"admin_meeting_view:delete:{meeting_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    s(lang, "meeting_view_back_btn"),
                    callback_data="admin_meeting_view:list",
                )
            ],
            [
                InlineKeyboardButton(
                    s(lang, "cancel_btn"), callback_data="admin_meeting_view:cancel"
                )
            ],
        ]
    )


def meeting_delete_confirm_keyboard(lang: str, meeting_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    s(lang, "meeting_delete_yes_btn"),
                    callback_data=f"admin_meeting_view:delete_yes:{meeting_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    s(lang, "meeting_view_back_btn"),
                    callback_data=f"admin_meeting_view:{meeting_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    s(lang, "cancel_btn"), callback_data="admin_meeting_view:cancel"
                )
            ],
        ]
    )


def _meeting_attendee_ids(ctx: ContextTypes.DEFAULT_TYPE) -> set[int]:
    raw = ctx.user_data.get("meeting_attendee_ids")
    if raw is None:
        raw = set()
        ctx.user_data["meeting_attendee_ids"] = raw
    return raw


def _telegram_shown_profile(obj: Any) -> tuple[str, str | None]:
    """Extract (full_name, username) from a Telegram User or private Chat.

    Non-string attributes (e.g. AsyncMock in tests) are treated as missing so
    we never persist dummy names.
    """
    obj_type = getattr(obj, "type", None)
    if isinstance(obj_type, str) and obj_type not in ("private",):
        return "", None
    if getattr(obj, "is_bot", False) is True:
        return "", None
    name = getattr(obj, "full_name", None)
    if not isinstance(name, str):
        first = getattr(obj, "first_name", None)
        last = getattr(obj, "last_name", None)
        first_s = first if isinstance(first, str) else ""
        last_s = last if isinstance(last, str) else ""
        name = f"{first_s} {last_s}".strip()
    username = getattr(obj, "username", None)
    uname = username.strip() if isinstance(username, str) and username.strip() else None
    return (name or "").strip(), uname


async def fetch_telegram_user_profile(bot: Bot, user_id: int) -> tuple[str, str | None]:
    """Look up a user's shown name via the group, then via a private getChat."""
    if user_id <= 0:
        return "", None
    if config.ALLOWED_CHAT_ID:
        try:
            member = await bot.get_chat_member(config.ALLOWED_CHAT_ID, user_id)
            name, uname = _telegram_shown_profile(getattr(member, "user", member))
            if name or uname:
                return name, uname
        except Exception as e:
            logger.warning(
                "Could not fetch chat member %s for attendance name: %s", user_id, e
            )
    try:
        chat = await bot.get_chat(user_id)
    except Exception as e:
        logger.warning("Could not fetch chat %s for attendance name: %s", user_id, e)
        return "", None
    return _telegram_shown_profile(chat)


async def _refresh_chat_admin_suggestions(bot: Bot) -> None:
    """Best-effort: record chat admins as known users for attendee suggestions."""
    if not config.ALLOWED_CHAT_ID:
        return
    try:
        admins = await bot.get_chat_administrators(config.ALLOWED_CHAT_ID)
    except Exception as e:
        logger.warning(
            "Could not fetch chat administrators for meeting suggestions: %s", e
        )
        return
    for member in admins:
        user = member.user
        if user.is_bot:
            continue
        db_upsert_club_user(user.id, user.full_name or "", user.username)


async def refresh_missing_club_user_names(bot: Bot, rows: Sequence[BookLike]) -> bool:
    """Fill Telegram shown names for club users that would otherwise display as IDs.

    Returns True if any row was updated (caller should re-query).
    """
    updated = False
    for row in rows:
        uid = int(row["user_id"])
        if club_user_has_shown_name(row["full_name"], row["username"]):
            continue
        name, uname = await fetch_telegram_user_profile(bot, uid)
        if not name and not uname:
            continue
        db_upsert_club_user(uid, name, uname)
        updated = True
    return updated


def meeting_attendees_keyboard(
    lang: str,
    suggestions: Sequence[BookLike],
    selected: set[int],
    page: int,
) -> InlineKeyboardMarkup:
    total = len(suggestions)
    page_size = MEETING_ATTENDEES_PAGE_SIZE
    start = page * page_size
    chunk = suggestions[start : start + page_size]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in chunk:
        uid = int(row["user_id"])
        mark = "✅" if uid in selected else "⬜"
        label = format_club_user_display(uid, row["full_name"], row["username"])
        if row["voted"]:
            label = f"🗳 {label}"
        if len(label) > 42:
            label = label[:39] + "…"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{mark} {label}",
                    callback_data=f"admin_meeting_att:toggle:{uid}:{page}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if start > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️", callback_data=f"admin_meeting_att:page:{page - 1}"
            )
        )
    if start + page_size < total:
        nav.append(
            InlineKeyboardButton(
                "▶️", callback_data=f"admin_meeting_att:page:{page + 1}"
            )
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                s(lang, "meeting_attendee_add_id_btn"),
                callback_data="admin_meeting_att:addid",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                s(lang, "meeting_attendee_done_btn"),
                callback_data="admin_meeting_att:done",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                s(lang, "cancel_btn"), callback_data="admin_meeting_att:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(buttons)


async def _show_meeting_attendee_picker(
    update_or_query: Any,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 0,
    is_callback: bool = False,
) -> int:
    lang = get_lang(ctx)
    book_id = ctx.user_data.get("meeting_book_id")
    if book_id is None:
        text = s(lang, "cancelled")
        if is_callback:
            await update_or_query.edit_message_text(text, parse_mode=PM)
        else:
            await update_or_query.message.reply_text(text, parse_mode=PM)
        return ConversationHandler.END
    await _refresh_chat_admin_suggestions(ctx.bot)
    suggestions = db_meeting_user_suggestions(book_id)
    if await refresh_missing_club_user_names(ctx.bot, suggestions):
        suggestions = db_meeting_user_suggestions(book_id)
    selected = _meeting_attendee_ids(ctx)
    meeting_date = ctx.user_data.get("meeting_date", "")
    text = tr(
        ctx,
        "meeting_attendees_prompt",
        count=len(selected),
        date=h(str(meeting_date)),
    )
    markup = meeting_attendees_keyboard(lang, suggestions, selected, page)
    if is_callback:
        await update_or_query.edit_message_text(
            text, reply_markup=markup, parse_mode=PM
        )
    else:
        await update_or_query.message.reply_text(
            text, reply_markup=markup, parse_mode=PM
        )
    ctx.user_data["meeting_attendee_page"] = page
    return ADMIN_MEETING_ATTENDEES


def fiction_keyboard(
    lang: str,
    *,
    show_add_back: bool = False,
    show_add_forward: bool = False,
    show_save: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(s(lang, "fiction_btn"), callback_data="fiction:1"),
            InlineKeyboardButton(s(lang, "nonfiction_btn"), callback_data="fiction:0"),
        ]
    ]
    nav = add_nav_buttons(lang, show_back=show_add_back, show_forward=show_add_forward)
    if nav:
        rows.append(nav)
    rows.extend(add_wizard_footer(lang, show_save=show_save))
    return InlineKeyboardMarkup(rows)


def original_language_keyboard(
    lang: str,
    *,
    prefix: str,
    show_add_back: bool = False,
    show_add_forward: bool = False,
    show_skip: bool = True,
    show_save: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code in ORIGINAL_LANGUAGE_CODES:
        row.append(
            InlineKeyboardButton(
                s(lang, f"orig_lang_{code}"),
                callback_data=f"{prefix}:{code}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    action_row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            s(lang, "orig_lang_other_btn"),
            callback_data=f"{prefix}:other",
        )
    ]
    if show_skip:
        action_row.append(
            InlineKeyboardButton(
                s(lang, "orig_lang_skip_btn"),
                callback_data=f"{prefix}:skip",
            )
        )
    rows.append(action_row)
    nav = add_nav_buttons(lang, show_back=show_add_back, show_forward=show_add_forward)
    if nav:
        rows.append(nav)
    rows.extend(add_wizard_footer(lang, show_save=show_save))
    return InlineKeyboardMarkup(rows)


def cefr_levels_keyboard(
    lang: str,
    selected: set[str],
    *,
    prefix: str,
    done_label_key: str = "language_level_done_btn",
    show_add_back: bool = False,
    show_add_forward: bool = False,
    show_save: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for level in CEFR_LEVELS:
        mark = "✅" if level in selected else "⬜"
        row.append(
            InlineKeyboardButton(
                f"{mark} {level}",
                callback_data=f"{prefix}:toggle:{level}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = add_nav_buttons(lang, show_back=show_add_back, show_forward=show_add_forward)
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                s(lang, done_label_key),
                callback_data=f"{prefix}:done",
            )
        ]
    )
    rows.extend(add_wizard_footer(lang, show_save=show_save))
    return InlineKeyboardMarkup(rows)


def books_top_n(books: Sequence[BookLike], n: int = 5) -> list[BookLike]:
    """First n books by rank, including all ties at the nth position."""
    top: list[BookLike] = []
    for i, book in enumerate(books):
        if i < n:
            top.append(book)
        elif n > 0:
            nth = books[n - 1]
            if (
                book["avg_score"] == nth["avg_score"]
                and book["vote_count"] == nth["vote_count"]
            ):
                top.append(book)
            else:
                break
        else:
            break
    return top


async def post_book_voting_to_group_chat(
    bot: Bot, book: BookLike, *, intro_key: str
) -> bool:
    """Post a book card with inline vote buttons to the configured group chat."""
    if not config.ALLOWED_CHAT_ID:
        return False
    chat_lang = config.CHAT_LANG
    text = tr(chat_lang, intro_key) + book_card(book, chat_lang)
    error: Exception | None = None
    for attempt in range(2):
        try:
            await bot.send_message(
                chat_id=config.ALLOWED_CHAT_ID,
                text=text,
                parse_mode=PM,
                reply_markup=score_keyboard(book["id"], chat_lang),
            )
            return True
        except RetryAfter as exc:
            if attempt:
                error = exc
                break
            retry_after = getattr(exc, "_retry_after", None)
            if retry_after is None:
                retry_after = exc.retry_after
            delay = (
                retry_after.total_seconds()
                if isinstance(retry_after, timedelta)
                else float(retry_after)
            )
            await asyncio.sleep(delay)
        except Exception as exc:
            error = exc
            break
    logger.warning(
        "post_book_voting_to_group_chat: failed to post book %s to chat %s: %s",
        book["id"],
        config.ALLOWED_CHAT_ID,
        error,
    )
    return False


def score_keyboard(
    book_id: int, lang: str, current: int | None = None
) -> InlineKeyboardMarkup:
    """Compact 3-button vote row to attach directly to book cards."""
    options = [
        (1, s(lang, "want_btn")),
        (0, s(lang, "meh_btn")),
        (-1, s(lang, "no_btn")),
    ]
    row = [
        InlineKeyboardButton(
            label + (" ✓" if current == score else ""),
            callback_data=f"vote_cast:{book_id}:{score}",
        )
        for score, label in options
    ]
    return InlineKeyboardMarkup([row])


def is_valid_url(text: str) -> bool:
    if not (text.startswith("http://") or text.startswith("https://")):
        return False
    # Reject characters that cannot legally appear unencoded in a URL. They are
    # the ones that would otherwise corrupt the surrounding HTML anchor.
    if any(c in text for c in '"<>') or any(c.isspace() for c in text):
        return False
    # Require something after the scheme.
    return bool(text.split("://", 1)[1])


_CREATION_YEAR_MIN = 1000
_CREATION_YEAR_MAX = 2100


def parse_optional_creation_year(text: str) -> int | None:
    """Return year, or None for /skip. Raises ValueError if invalid."""
    stripped = text.strip()
    if stripped == "/skip":
        return None
    if len(stripped) != 4 or not stripped.isdigit():
        raise ValueError("invalid year")
    year = int(stripped)
    if year < _CREATION_YEAR_MIN or year > _CREATION_YEAR_MAX:
        raise ValueError("year out of range")
    return year


def parse_date(text: str) -> str | None:
    """Return date string if valid YYYY-MM-DD, DD.MM.YYYY, or DD/MM/YYYY, else None."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
