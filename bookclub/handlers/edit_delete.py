from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from bookclub.cefr import (
    format_language_levels,
    language_levels_display,
    parse_language_levels,
)
from bookclub.config import (
    DELETING_CHOOSE,
    EDITING_CHOOSE,
    EDITING_FIELD,
    entry_field_enabled,
)
from bookclub.db import (
    SourceParseError,
    db_delete_book,
    db_get_book,
    db_get_books_metadata,
    db_update_book_field,
    decode_sources,
    encode_sources,
    parse_source_links,
)
from bookclub.domain import can_modify, require_book
from bookclub.i18n import PM, get_lang, s, tr
from bookclub.original_languages import (
    display_original_language,
    stored_original_language,
)
from bookclub.types import BookLike
from bookclub.ui import (
    book_card,
    books_keyboard,
    cancel_button,
    cancel_keyboard,
    cefr_levels_keyboard,
    h,
    is_valid_url,
    original_language_keyboard,
    parse_optional_creation_year,
)

# ── /edit — sequential field-by-field editor ──────────────────────────────────
_EDIT_FIELD_ORDER = [
    "title",
    "author",
    "pages",
    "fiction",
    "review_link",
    "sources",
    "original_language",
    "creation_year",
    "language_levels",
    "description",
]
_EDIT_TO_ENTRY = {"review_link": "review"}
EDIT_FIELDS = [f for f in _EDIT_FIELD_ORDER if f != "language_levels"]


def get_edit_fields() -> list[str]:
    fields: list[str] = []
    for field in _EDIT_FIELD_ORDER:
        if entry_field_enabled(_EDIT_TO_ENTRY.get(field, field)):
            fields.append(field)
    return fields


def edit_field_key(field: str) -> str:
    return f"field_{field.replace('_link', '').replace('review', 'review')}"


def edit_current_value(book: BookLike, field: str, lang: str) -> str:
    """Return human-readable current value for a field."""
    if field == "fiction":
        return (
            s(lang, "fiction_label") if book["fiction"] else s(lang, "nonfiction_label")
        )
    if field == "review_link":
        return book["review_link"] or ("—" if lang == "en" else "—")
    if field == "sources":
        try:
            raw = book["sources"]
        except (KeyError, IndexError):
            raw = ""
        urls = decode_sources(raw)
        return " · ".join(urls) if urls else "—"
    if field == "description":
        return book["description"] or ("—" if lang == "en" else "—")
    if field == "original_language":
        raw = book["original_language"]
        if not raw:
            return "—"
        return display_original_language(str(raw), lang)
    if field == "creation_year":
        cy = book["creation_year"]
        return str(cy) if cy is not None else ("—" if lang == "en" else "—")
    if field == "language_levels":
        raw = (
            book["language_levels"]
            if "language_levels" in book.keys()  # noqa: SIM118
            else None
        )
        shown = language_levels_display(raw)
        return shown or ("—" if lang == "en" else "—")
    return str(book[field])


def edit_yn_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    s(lang, "edit_yes_btn"), callback_data="edit_yn:yes"
                ),
                InlineKeyboardButton(
                    s(lang, "edit_no_btn"), callback_data="edit_yn:no"
                ),
            ],
            [cancel_button(lang)],
        ]
    )


def edit_fiction_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    s(lang, "fiction_btn"), callback_data="edit_fiction:1"
                ),
                InlineKeyboardButton(
                    s(lang, "nonfiction_btn"), callback_data="edit_fiction:0"
                ),
            ],
            [cancel_button(lang)],
        ]
    )


async def _ask_edit_field(
    update_or_query: Any,
    ctx: ContextTypes.DEFAULT_TYPE,
    is_callback: bool = False,
) -> int:
    """Ask user about the next field to edit. Returns next state or END."""
    lang = get_lang(ctx)
    fields = ctx.user_data.get("edit_fields", [])
    if not fields:
        # All fields done — save and show result
        book_id = ctx.user_data.pop("edit_book_id")
        changes = ctx.user_data.pop("edit_changes", {})
        for field, value in changes.items():
            db_update_book_field(book_id, field, value)
        book = require_book(book_id)
        text = f"{s(lang, 'edit_done')}\n\n{book_card(book, lang)}"
        if is_callback:
            await update_or_query.edit_message_text(text, parse_mode=PM)
        else:
            await update_or_query.message.reply_text(text, parse_mode=PM)
        ctx.user_data.pop("edit_fields", None)
        return ConversationHandler.END

    ctx.user_data["edit_input"] = None
    field = fields[0]
    book = require_book(ctx.user_data["edit_book_id"])
    field_key = f"field_{field}" if field != "review_link" else "field_review"
    field_name = s(lang, field_key)
    current = edit_current_value(book, field, lang)
    text = tr(ctx, "edit_field_prompt", field=field_name, value=h(current))

    if is_callback:
        await update_or_query.edit_message_text(
            text, parse_mode=PM, reply_markup=edit_yn_keyboard(lang)
        )
    else:
        await update_or_query.message.reply_text(
            text, parse_mode=PM, reply_markup=edit_yn_keyboard(lang)
        )
    return EDITING_FIELD


async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    uname = update.effective_user.username
    all_books = db_get_books_metadata(discussed=False) + list(
        db_get_books_metadata(discussed=True)
    )
    books = [b for b in all_books if can_modify(user_id, b, uname)]
    if not books:
        await update.message.reply_text(tr(ctx, "no_own_books"), parse_mode=PM)
        return ConversationHandler.END
    await update.message.reply_text(
        tr(ctx, "choose_edit"),
        reply_markup=books_keyboard(books, "edit_pick", tr(ctx, "cancel_btn")),
    )
    return EDITING_CHOOSE


async def edit_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    parts = query.data.split(":")
    if len(parts) == 3 and parts[1] == "page":
        user = query.from_user
        all_books = db_get_books_metadata(discussed=False) + list(
            db_get_books_metadata(discussed=True)
        )
        books = [book for book in all_books if can_modify(user.id, book, user.username)]
        await query.edit_message_text(
            tr(ctx, "choose_edit"),
            reply_markup=books_keyboard(
                books,
                "edit_pick",
                tr(ctx, "cancel_btn"),
                page=int(parts[2]),
            ),
        )
        return EDITING_CHOOSE
    _, book_id = query.data.split(":", 1)
    if book_id == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END
    book_id = int(book_id)
    book = db_get_book(book_id)
    if book is None or not can_modify(
        query.from_user.id, book, query.from_user.username
    ):
        await query.edit_message_text(s(lang, "no_permission"), parse_mode=PM)
        return ConversationHandler.END
    ctx.user_data["edit_book_id"] = book_id
    ctx.user_data["edit_fields"] = get_edit_fields()
    ctx.user_data["edit_changes"] = {}
    return await _ask_edit_field(query, ctx, is_callback=True)


async def edit_yn_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User clicked Yes or No on whether to edit the current field."""
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    _, ans = query.data.split(":")
    field = ctx.user_data["edit_fields"][0]

    if ans == "no":
        ctx.user_data["edit_fields"].pop(0)
        return await _ask_edit_field(query, ctx, is_callback=True)

    # ans == "yes" — ask for new value
    if field == "fiction":
        await query.edit_message_text(
            tr(ctx, "edit_ask_new", field=s(lang, "field_fiction")),
            parse_mode=PM,
            reply_markup=edit_fiction_keyboard(lang),
        )
        return EDITING_FIELD  # handled by edit_fiction_cb

    if field == "original_language":
        await query.edit_message_text(
            tr(ctx, "ask_original_language"),
            parse_mode=PM,
            reply_markup=original_language_keyboard(
                lang, prefix="edit_orig_lang", show_skip=True
            ),
        )
        return EDITING_FIELD  # handled by edit_original_language_cb

    if field == "language_levels":
        book = require_book(ctx.user_data["edit_book_id"])
        raw = (
            book["language_levels"]
            if "language_levels" in book.keys()  # noqa: SIM118
            else None
        )
        selected = parse_language_levels(raw)
        ctx.user_data["edit_cefr_selected"] = selected
        await query.edit_message_text(
            tr(ctx, "ask_language_level", count=len(selected)),
            reply_markup=cefr_levels_keyboard(lang, selected, prefix="edit_cefr"),
            parse_mode=PM,
        )
        return EDITING_FIELD  # handled by edit_language_levels_cb

    if field == "review_link":
        ctx.user_data["edit_input"] = "clearable"
        await query.edit_message_text(
            tr(ctx, "ask_review"),
            parse_mode=PM,
            reply_markup=cancel_keyboard(lang),
        )
        return EDITING_FIELD
    if field == "sources":
        ctx.user_data["edit_input"] = "clearable"
        await query.edit_message_text(
            tr(ctx, "ask_sources"),
            parse_mode=PM,
            reply_markup=cancel_keyboard(lang),
        )
        return EDITING_FIELD

    field_name = s(lang, f"field_{field}")
    ctx.user_data["edit_input"] = "required"
    await query.edit_message_text(
        tr(ctx, "edit_ask_new", field=field_name),
        parse_mode=PM,
        reply_markup=cancel_keyboard(lang),
    )
    return EDITING_FIELD


async def edit_fiction_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked Fiction/Non-fiction via inline button."""
    query = update.callback_query
    await query.answer()
    _, value = query.data.split(":")
    ctx.user_data["edit_changes"]["fiction"] = int(value)
    ctx.user_data["edit_fields"].pop(0)
    return await _ask_edit_field(query, ctx, is_callback=True)


async def edit_original_language_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    _, action = query.data.split(":", 1)
    if action == "skip":
        await query.answer()
        ctx.user_data["edit_changes"]["original_language"] = None
        ctx.user_data.pop("edit_orig_lang_other", None)
        ctx.user_data["edit_fields"].pop(0)
        return await _ask_edit_field(query, ctx, is_callback=True)
    if action == "other":
        await query.answer()
        ctx.user_data["edit_orig_lang_other"] = True
        await query.edit_message_text(
            tr(ctx, "ask_original_language_other"),
            parse_mode=PM,
            reply_markup=cancel_keyboard(get_lang(ctx)),
        )
        return EDITING_FIELD
    stored = stored_original_language(action)
    if stored is None:
        await query.answer()
        return EDITING_FIELD
    await query.answer()
    ctx.user_data["edit_changes"]["original_language"] = stored
    ctx.user_data.pop("edit_orig_lang_other", None)
    ctx.user_data["edit_fields"].pop(0)
    return await _ask_edit_field(query, ctx, is_callback=True)


async def edit_language_levels_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    lang = get_lang(ctx)
    _, action, *rest = query.data.split(":")
    selected: set[str] = ctx.user_data.setdefault("edit_cefr_selected", set())
    if action == "toggle":
        level = rest[0]
        if level in selected:
            selected.discard(level)
        else:
            selected.add(level)
        await query.answer()
        await query.edit_message_text(
            tr(ctx, "ask_language_level", count=len(selected)),
            reply_markup=cefr_levels_keyboard(lang, selected, prefix="edit_cefr"),
            parse_mode=PM,
        )
        return EDITING_FIELD
    if action == "done":
        if not selected:
            await query.answer(tr(ctx, "language_level_none_selected"), show_alert=True)
            return EDITING_FIELD
        await query.answer()
        ctx.user_data["edit_changes"]["language_levels"] = format_language_levels(
            selected
        )
        ctx.user_data.pop("edit_cefr_selected", None)
        ctx.user_data["edit_fields"].pop(0)
        return await _ask_edit_field(query, ctx, is_callback=True)
    await query.answer()
    return EDITING_FIELD


async def edit_value_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a new value for the current field."""
    text = update.message.text.strip()
    field = ctx.user_data["edit_fields"][0]

    # Validate
    value: int | str | None
    if field == "original_language" and ctx.user_data.pop(
        "edit_orig_lang_other", False
    ):
        value = text or None
        ctx.user_data["edit_changes"][field] = value
        ctx.user_data["edit_fields"].pop(0)
        return await _ask_edit_field(update, ctx, is_callback=False)
    if field == "pages":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text(
                tr(ctx, "edit_invalid_pages"),
                parse_mode=PM,
                reply_markup=cancel_keyboard(get_lang(ctx)),
            )
            return EDITING_FIELD
        value = int(text)
    elif field == "review_link":
        if text == "/skip":
            value = ""
        elif not is_valid_url(text):
            await update.message.reply_text(
                tr(ctx, "edit_invalid_url"),
                parse_mode=PM,
                reply_markup=cancel_keyboard(get_lang(ctx)),
            )
            return EDITING_FIELD
        else:
            value = text
    elif field == "sources":
        try:
            value = encode_sources(parse_source_links(text))
        except SourceParseError:
            await update.message.reply_text(
                tr(ctx, "invalid_sources"),
                parse_mode=PM,
                reply_markup=cancel_keyboard(get_lang(ctx)),
            )
            return EDITING_FIELD
    elif field == "creation_year":
        try:
            value = parse_optional_creation_year(text)
        except ValueError:
            await update.message.reply_text(
                tr(ctx, "edit_invalid_creation_year"),
                parse_mode=PM,
                reply_markup=cancel_keyboard(get_lang(ctx)),
            )
            return EDITING_FIELD
        if value is None:
            await update.message.reply_text(
                tr(ctx, "edit_invalid_creation_year"),
                parse_mode=PM,
                reply_markup=cancel_keyboard(get_lang(ctx)),
            )
            return EDITING_FIELD
    else:
        value = text

    ctx.user_data["edit_changes"][field] = value
    ctx.user_data["edit_fields"].pop(0)
    ctx.user_data["edit_input"] = None
    return await _ask_edit_field(update, ctx, is_callback=False)


async def edit_skip_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Clear the review link or sources while editing. Other fields stay required."""
    if ctx.user_data.get("edit_input") != "clearable":
        if ctx.user_data.get("edit_input") == "required":
            await update.message.reply_text(
                tr(ctx, "edit_cannot_skip"),
                parse_mode=PM,
                reply_markup=cancel_keyboard(get_lang(ctx)),
            )
        return EDITING_FIELD
    field = ctx.user_data["edit_fields"][0]
    ctx.user_data["edit_changes"][field] = ""
    ctx.user_data["edit_fields"].pop(0)
    ctx.user_data["edit_input"] = None
    return await _ask_edit_field(update, ctx, is_callback=False)


# ── /delete ────────────────────────────────────────────────────────────────────
async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    all_books = db_get_books_metadata(discussed=False) + list(
        db_get_books_metadata(discussed=True)
    )
    books = [b for b in all_books if can_modify(user_id, b)]
    if not books:
        await update.message.reply_text(tr(ctx, "no_own_books"), parse_mode=PM)
        return ConversationHandler.END
    await update.message.reply_text(
        tr(ctx, "choose_delete"),
        reply_markup=books_keyboard(books, "del_pick", tr(ctx, "cancel_btn")),
    )
    return DELETING_CHOOSE


async def delete_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = get_lang(ctx)
    parts = query.data.split(":")
    if len(parts) == 3 and parts[1] == "page":
        all_books = db_get_books_metadata(discussed=False) + list(
            db_get_books_metadata(discussed=True)
        )
        books = [book for book in all_books if can_modify(query.from_user.id, book)]
        await query.edit_message_text(
            tr(ctx, "choose_delete"),
            reply_markup=books_keyboard(
                books,
                "del_pick",
                tr(ctx, "cancel_btn"),
                page=int(parts[2]),
            ),
        )
        return DELETING_CHOOSE
    _, book_id = query.data.split(":", 1)
    if book_id == "cancel":
        await query.edit_message_text(s(lang, "cancelled"))
        return ConversationHandler.END
    book_id = int(book_id)
    book = db_get_book(book_id)
    if book is None or not can_modify(
        query.from_user.id, book, query.from_user.username
    ):
        await query.edit_message_text(s(lang, "no_permission"), parse_mode=PM)
        return ConversationHandler.END
    title = book["title"]
    db_delete_book(book_id)
    await query.edit_message_text(tr(ctx, "deleted", title=h(title)), parse_mode=PM)
    return ConversationHandler.END
