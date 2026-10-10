from __future__ import annotations

from datetime import datetime
from typing import Any

from telegram import (
    ForceReply,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes

from bookclub.cefr import format_language_levels, language_levels_display
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
    entry_field_enabled,
)
from bookclub.db import db_update_add_draft
from bookclub.i18n import PM, get_lang, s, tr
from bookclub.original_languages import display_original_language
from bookclub.ui import (
    add_confirm_keyboard,
    add_nav_keyboard,
    book_card,
    cancel_keyboard,
    cefr_levels_keyboard,
    fiction_keyboard,
    h,
    original_language_keyboard,
    review_choice_keyboard,
    review_choice_text,
    usable_review_choices,
)

_TEXT_EDIT_STATES = frozenset(
    {
        ADDING_TITLE,
        ADDING_AUTHOR,
        ADDING_PAGES,
        ADDING_REVIEW,
        ADDING_ORIGINAL_LANGUAGE_OTHER,
        ADDING_CREATION_YEAR,
        ADDING_DESCRIPTION,
    }
)
_PRIVATE_INLINE_CHATS = frozenset({None, "sender", "private"})

_ADD_STEP_ORDER = (
    ADDING_TITLE,
    ADDING_REVIEW,
    ADDING_AUTHOR,
    ADDING_PAGES,
    ADDING_FICTION,
    ADDING_ORIGINAL_LANGUAGE,
    ADDING_CREATION_YEAR,
    ADDING_LANGUAGE_LEVEL,
    ADDING_DESCRIPTION,
)
_STATE_ENTRY_FIELD = {
    ADDING_TITLE: "title",
    ADDING_TITLE_CONFIRM: "title",
    ADDING_AUTHOR: "author",
    ADDING_PAGES: "pages",
    ADDING_FICTION: "fiction",
    ADDING_REVIEW: "review",
    ADDING_ORIGINAL_LANGUAGE: "original_language",
    ADDING_ORIGINAL_LANGUAGE_OTHER: "original_language",
    ADDING_CREATION_YEAR: "creation_year",
    ADDING_LANGUAGE_LEVEL: "language_levels",
    ADDING_DESCRIPTION: "description",
}


def _canonical_add_state(current: int) -> int:
    if current == ADDING_TITLE_CONFIRM:
        return ADDING_TITLE
    if current == ADDING_ORIGINAL_LANGUAGE_OTHER:
        return ADDING_ORIGINAL_LANGUAGE
    return current


def enabled_add_states() -> list[int]:
    return [st for st in _ADD_STEP_ORDER if entry_field_enabled(_STATE_ENTRY_FIELD[st])]


def add_previous_state(current: int) -> int | None:
    if current == ADDING_TITLE_CONFIRM:
        return ADDING_TITLE
    if current == ADDING_AI_CHOOSE:
        return ADDING_TITLE
    if current == ADDING_DRAFT_CHOOSE:
        return ADDING_START
    if current == ADDING_ORIGINAL_LANGUAGE_OTHER:
        return ADDING_ORIGINAL_LANGUAGE
    if current == ADDING_CONFIRM:
        enabled = enabled_add_states()
        return enabled[-1] if enabled else ADDING_TITLE
    enabled = enabled_add_states()
    try:
        idx = enabled.index(current)
    except ValueError:
        return None
    if idx <= 0:
        return None
    return enabled[idx - 1]


def add_next_state(current: int) -> int | None:
    enabled = enabled_add_states()
    key = _canonical_add_state(current)
    try:
        idx = enabled.index(key)
    except ValueError:
        try:
            pos = _ADD_STEP_ORDER.index(key)
        except ValueError:
            return enabled[0] if enabled else None
        for st in _ADD_STEP_ORDER[pos + 1 :]:
            if st in enabled:
                return st
        return None
    if idx + 1 >= len(enabled):
        return None
    return enabled[idx + 1]


async def continue_add(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    current: int,
    *,
    edit: bool = False,
) -> int:
    note_user_edit(ctx, current)
    if current == ADDING_REVIEW:
        from bookclub.handlers.add import apply_llm_from_review_page

        await apply_llm_from_review_page(update, ctx)
    nxt = add_next_state(current)
    if nxt is None:
        return await show_add_confirm(update, ctx, edit=edit)
    if nxt == ADDING_LANGUAGE_LEVEL:
        ctx.user_data.setdefault("new_book", {}).setdefault("language_levels", set())
    return await send_add_prompt(update, ctx, nxt, edit=edit)


def draft_book_like(nb: dict, user: Any) -> dict[str, Any]:
    """Build a book-card mapping from the in-progress /add draft."""
    levels_set = nb.get("language_levels")
    if isinstance(levels_set, set):
        language_levels = format_language_levels(levels_set)
    elif isinstance(levels_set, str):
        language_levels = levels_set
    else:
        language_levels = None
    uid = 0
    name = "unknown"
    username = None
    if user is not None:
        uid = int(getattr(user, "id", 0) or 0)
        raw_name = getattr(user, "full_name", None)
        name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
        raw_user = getattr(user, "username", None)
        username = raw_user if isinstance(raw_user, str) and raw_user else None
    return {
        "title": nb.get("title") or "",
        "author": nb.get("author") or "",
        "pages": nb.get("pages") or 0,
        "fiction": 1 if nb.get("fiction", True) else 0,
        "review_link": nb.get("review_link") or "",
        "description": nb.get("description") or "",
        "hidden": 0,
        "discussed": 0,
        "discussed_at": None,
        "added_by": uid,
        "added_by_name": name,
        "added_by_username": username,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "original_language": nb.get("original_language") or None,
        "creation_year": nb.get("creation_year"),
        "language_levels": language_levels,
        "votes_yes": 0,
        "votes_meh": 0,
        "votes_no": 0,
        "vote_count": 0,
        "avg_score": 0,
    }


async def show_add_confirm(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
) -> int:
    nb = ctx.user_data.setdefault("new_book", {})
    ctx.user_data["add_state"] = ADDING_CONFIRM
    persist_add_draft_if_saved(update, ctx)
    lang = get_lang(ctx)
    preview = book_card(draft_book_like(nb, update.effective_user), lang)
    text = f"{tr(ctx, 'add_confirm_prompt')}\n\n{preview}"
    markup = add_confirm_keyboard(lang, show_save=bool(nb.get("title")))
    query = update.callback_query
    if edit and query:
        await query.edit_message_text(text, parse_mode=PM, reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode=PM, reply_markup=markup)
    elif query and query.message:
        await query.message.reply_text(  # type: ignore[attr-defined]
            text, parse_mode=PM, reply_markup=markup
        )
    return ADDING_CONFIRM


def _prompt_key_for_state(state: int) -> str | None:
    return {
        ADDING_TITLE: "ask_title",
        ADDING_AUTHOR: "ask_author",
        ADDING_PAGES: "ask_pages",
        ADDING_FICTION: "ask_fiction",
        ADDING_REVIEW: "ask_review",
        ADDING_ORIGINAL_LANGUAGE: "ask_original_language",
        ADDING_ORIGINAL_LANGUAGE_OTHER: "ask_original_language_other",
        ADDING_CREATION_YEAR: "ask_creation_year",
        ADDING_LANGUAGE_LEVEL: "ask_language_level",
        ADDING_DESCRIPTION: "ask_desc",
    }.get(state)


def _current_value_display(nb: dict, state: int, lang: str) -> str | None:
    dash = "—"
    if state == ADDING_TITLE:
        v = nb.get("title")
        return str(v) if v else None
    if state == ADDING_AUTHOR:
        v = nb.get("author")
        return str(v) if v else None
    if state == ADDING_PAGES:
        v = nb.get("pages")
        return str(v) if v is not None else None
    if state == ADDING_FICTION:
        if "fiction" not in nb:
            return None
        return (
            s(lang, "fiction_label") if nb["fiction"] else s(lang, "nonfiction_label")
        )
    if state == ADDING_REVIEW:
        v = nb.get("review_link")
        return str(v) if v else None
    if state in (ADDING_ORIGINAL_LANGUAGE, ADDING_ORIGINAL_LANGUAGE_OTHER):
        if "original_language" not in nb:
            return None
        v = nb.get("original_language")
        if not v:
            return dash
        return display_original_language(str(v), lang)
    if state == ADDING_CREATION_YEAR:
        if "creation_year" not in nb:
            return None
        v = nb.get("creation_year")
        return str(v) if v is not None else dash
    if state == ADDING_LANGUAGE_LEVEL:
        levels = nb.get("language_levels")
        if isinstance(levels, set):
            text = language_levels_display(format_language_levels(levels))
            return text if text else None
        if isinstance(levels, str):
            return language_levels_display(levels)
        return None
    if state == ADDING_DESCRIPTION:
        v = nb.get("description")
        return str(v) if v else None
    return None


def _nb_key_for_state(state: int) -> str | None:
    if state == ADDING_REVIEW:
        return "review_link"
    field = _STATE_ENTRY_FIELD.get(state)
    return field


def _is_llm_suggestion(ctx: ContextTypes.DEFAULT_TYPE, state: int) -> bool:
    filled = ctx.user_data.get("llm_filled_keys")
    key = _nb_key_for_state(state)
    return isinstance(filled, set) and key in filled


def note_user_edit(ctx: ContextTypes.DEFAULT_TYPE, state: int) -> None:
    filled = ctx.user_data.get("llm_filled_keys")
    key = _nb_key_for_state(state)
    if isinstance(filled, set) and key:
        filled.discard(key)


def serialize_add_draft(ctx: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    nb = dict(ctx.user_data.get("new_book") or {})
    levels = nb.get("language_levels")
    if isinstance(levels, set):
        nb["language_levels"] = sorted(levels)
    filled = ctx.user_data.get("llm_filled_keys")
    return {
        "new_book": nb,
        "add_state": ctx.user_data.get("add_state"),
        "llm_add": ctx.user_data.get("llm_add"),
        "admin_add": ctx.user_data.get("admin_add"),
        "llm_suggestions_applied": ctx.user_data.get("llm_suggestions_applied"),
        "llm_filled_keys": sorted(filled) if isinstance(filled, set) else [],
        "llm_extracted_review": ctx.user_data.get("llm_extracted_review"),
        "add_from_start": bool(ctx.user_data.get("add_from_start")),
        "review_choices": usable_review_choices(ctx.user_data.get("review_choices")),
    }


def apply_add_draft(
    ctx: ContextTypes.DEFAULT_TYPE, payload: dict[str, Any], draft_id: int
) -> None:
    nb = dict(payload.get("new_book") or {})
    levels = nb.get("language_levels")
    if isinstance(levels, list):
        nb["language_levels"] = set(levels)
    ctx.user_data["new_book"] = nb
    ctx.user_data["add_state"] = payload.get("add_state")
    ctx.user_data["llm_add"] = payload.get("llm_add")
    ctx.user_data["admin_add"] = payload.get("admin_add")
    ctx.user_data["llm_suggestions_applied"] = payload.get("llm_suggestions_applied")
    filled = payload.get("llm_filled_keys") or []
    ctx.user_data["llm_filled_keys"] = (
        set(filled) if isinstance(filled, list) else set()
    )
    ctx.user_data["add_draft_id"] = draft_id
    ctx.user_data["add_from_start"] = True
    extracted = payload.get("llm_extracted_review")
    if extracted:
        ctx.user_data["llm_extracted_review"] = extracted
    choices = usable_review_choices(payload.get("review_choices"))
    if choices:
        ctx.user_data["review_choices"] = choices
    else:
        ctx.user_data.pop("review_choices", None)


def persist_add_draft_if_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    draft_id = ctx.user_data.get("add_draft_id")
    if not draft_id:
        return
    user = update.effective_user
    if user is None:
        return
    nb = ctx.user_data.get("new_book") or {}
    title = str(nb.get("title") or "")
    if not title:
        return
    db_update_add_draft(int(draft_id), user.id, title, serialize_add_draft(ctx))


def resume_add_state(saved: object) -> int:
    if not isinstance(saved, int):
        return ADDING_TITLE
    special = {
        ADDING_TITLE_CONFIRM,
        ADDING_AI_CHOOSE,
        ADDING_ORIGINAL_LANGUAGE_OTHER,
        ADDING_START,
        ADDING_DRAFT_CHOOSE,
        ADDING_CONFIRM,
    }
    if saved in special:
        if saved in (ADDING_START, ADDING_DRAFT_CHOOSE, ADDING_TITLE_CONFIRM):
            return ADDING_TITLE
        return saved
    enabled = enabled_add_states()
    if saved in enabled:
        return saved
    key = _canonical_add_state(saved)
    nxt = add_next_state(key)
    return nxt if nxt is not None else (enabled[0] if enabled else ADDING_TITLE)


def raw_add_value(nb: dict, state: int) -> str | None:
    if state == ADDING_TITLE:
        v = nb.get("title")
        return str(v) if v else None
    if state == ADDING_AUTHOR:
        v = nb.get("author")
        return str(v) if v else None
    if state == ADDING_PAGES:
        v = nb.get("pages")
        return str(v) if v is not None else None
    if state == ADDING_REVIEW:
        v = nb.get("review_link")
        return str(v) if v else None
    if state == ADDING_ORIGINAL_LANGUAGE_OTHER:
        v = nb.get("original_language")
        return str(v) if v else None
    if state == ADDING_CREATION_YEAR:
        v = nb.get("creation_year")
        return str(v) if v is not None else None
    if state == ADDING_DESCRIPTION:
        v = nb.get("description")
        return str(v) if v else None
    return None


def bot_supports_inline(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    return getattr(getattr(ctx, "bot", None), "supports_inline_queries", False) is True


def add_edit_value(state: int, nb: dict) -> str | None:
    """Current text for Edit: own saved answer or AI suggestion."""
    if state not in _TEXT_EDIT_STATES:
        return None
    return raw_add_value(nb, state) or None


def typed_add_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """User-typed add-wizard text, without a leftover ``@bot`` inline prefix."""
    text = (update.message.text or "").strip() if update.message else ""
    username = getattr(getattr(ctx, "bot", None), "username", None)
    if isinstance(username, str) and username:
        prefix = f"@{username}"
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip()
    return text


def pending_review_choices(ctx: ContextTypes.DEFAULT_TYPE, nb: dict) -> list[dict]:
    """Same-title catalog pages still waiting for the user to pick one."""
    if nb.get("review_link"):
        return []
    return usable_review_choices(ctx.user_data.get("review_choices"))


def markup_for_add(
    ctx: ContextTypes.DEFAULT_TYPE, state: int, nb: dict | None = None
) -> InlineKeyboardMarkup:
    if nb is None:
        nb = ctx.user_data.setdefault("new_book", {})
    return add_prompt_markup(
        get_lang(ctx),
        state,
        nb,
        edit_value=add_edit_value(state, nb),
        use_inline=bot_supports_inline(ctx),
        show_save=bool(nb.get("title")),
        show_title_back=bool(ctx.user_data.get("add_from_start")),
        review_choices=(
            pending_review_choices(ctx, nb) if state == ADDING_REVIEW else []
        ),
    )


def add_field_is_set(nb: dict, state: int) -> bool:
    return _current_value_display(nb, state, "en") is not None


def build_add_prompt_text(ctx: ContextTypes.DEFAULT_TYPE, state: int, nb: dict) -> str:
    if state == ADDING_REVIEW:
        choices = pending_review_choices(ctx, nb)
        if choices:
            return review_choice_text(get_lang(ctx), choices)
    key = _prompt_key_for_state(state)
    if key is None:
        return ""
    lang = get_lang(ctx)
    if state == ADDING_LANGUAGE_LEVEL:
        levels = nb.get("language_levels")
        count = len(levels) if isinstance(levels, set) else 0
        body = tr(ctx, key, count=count)
    else:
        body = tr(ctx, key)
    current = _current_value_display(nb, state, lang)
    parts = [body]
    suggested = current is not None and _is_llm_suggestion(ctx, state)
    if current is not None:
        value_key = "add_suggested_value" if suggested else "add_current_value"
        parts.append(tr(ctx, value_key, value=h(current)))
        if suggested:
            parts.append(tr(ctx, "add_suggested_hint"))
        elif state == ADDING_TITLE:
            parts.append(tr(ctx, "add_forward_hint"))
        else:
            parts.append(tr(ctx, "add_nav_hint"))
    elif state != ADDING_TITLE:
        parts.append(tr(ctx, "add_back_hint"))
    return "\n".join(parts)


def add_prompt_markup(
    lang: str,
    state: int,
    nb: dict,
    *,
    edit_value: str | None = None,
    use_inline: bool = False,
    show_save: bool = False,
    show_title_back: bool = False,
    review_choices: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    can_forward = add_field_is_set(nb, state)
    show_back = state != ADDING_TITLE
    if state == ADDING_TITLE:
        return add_nav_keyboard(
            lang,
            show_back=show_title_back,
            show_forward=can_forward,
            edit_value=edit_value,
            use_inline=use_inline,
            show_save=show_save,
        )
    if state == ADDING_FICTION:
        return fiction_keyboard(
            lang,
            show_add_back=True,
            show_add_forward=can_forward,
            show_save=show_save,
        )
    if state == ADDING_ORIGINAL_LANGUAGE:
        return original_language_keyboard(
            lang,
            prefix="add_orig_lang",
            show_add_back=True,
            show_add_forward=can_forward,
            show_save=show_save,
        )
    if state == ADDING_LANGUAGE_LEVEL:
        levels = nb.get("language_levels")
        selected = levels if isinstance(levels, set) else set()
        return cefr_levels_keyboard(
            lang,
            selected,
            prefix="add_cefr",
            show_add_back=True,
            show_add_forward=can_forward,
            show_save=show_save,
        )
    if state == ADDING_REVIEW and review_choices:
        return review_choice_keyboard(lang, review_choices, show_save=show_save)
    if state in (
        ADDING_AUTHOR,
        ADDING_PAGES,
        ADDING_REVIEW,
        ADDING_ORIGINAL_LANGUAGE_OTHER,
        ADDING_CREATION_YEAR,
        ADDING_DESCRIPTION,
    ):
        return add_nav_keyboard(
            lang,
            show_back=show_back,
            show_forward=can_forward,
            edit_value=edit_value,
            use_inline=use_inline,
            show_save=show_save,
        )
    return cancel_keyboard(lang)


async def send_add_prompt(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    state: int,
    *,
    edit: bool = False,
) -> int:
    nb = ctx.user_data.setdefault("new_book", {})
    ctx.user_data["add_state"] = state
    persist_add_draft_if_saved(update, ctx)
    if state == ADDING_CONFIRM:
        return await show_add_confirm(update, ctx, edit=edit)
    text = build_add_prompt_text(ctx, state, nb)
    markup = markup_for_add(ctx, state, nb)

    query = update.callback_query
    if edit and query:
        await query.edit_message_text(text, parse_mode=PM, reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode=PM, reply_markup=markup)
    elif query and query.message:
        await query.message.reply_text(  # type: ignore[attr-defined]
            text, parse_mode=PM, reply_markup=markup
        )
    return state


async def _nav_alert(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, key: str, current: int
) -> int:
    query = update.callback_query
    msg = tr(ctx, key)
    if query:
        await query.answer(msg, show_alert=True)
    elif update.message:
        await update.message.reply_text(msg, parse_mode=PM)
    return current


async def add_go_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    current = ctx.user_data.get("add_state")
    if current is None:
        return ADDING_TITLE
    query = update.callback_query
    if current == ADDING_START:
        return await _nav_alert(update, ctx, "add_back_at_start", current)
    if current == ADDING_DRAFT_CHOOSE or (
        current == ADDING_TITLE and ctx.user_data.get("add_from_start")
    ):
        if query:
            await query.answer()
        from bookclub.handlers.add import ask_add_start

        return await ask_add_start(update, ctx, edit=bool(query))
    prev = add_previous_state(current)
    if prev is None:
        return await _nav_alert(update, ctx, "add_back_at_start", current)
    if query:
        await query.answer()
    return await send_add_prompt(update, ctx, prev, edit=bool(query))


async def add_go_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    current = ctx.user_data.get("add_state")
    if current is None:
        return ADDING_TITLE
    query = update.callback_query
    if current == ADDING_CONFIRM:
        if query:
            await query.answer()
        from bookclub.handlers.add import complete_new_book

        return await complete_new_book(update, ctx)
    if current == ADDING_AI_CHOOSE:
        if query:
            await query.answer()
        ctx.user_data["llm_add"] = False
        return await continue_add(update, ctx, ADDING_TITLE, edit=bool(query))
    if current in (ADDING_START, ADDING_DRAFT_CHOOSE):
        return await _nav_alert(update, ctx, "add_forward_need_value", current)
    nb = ctx.user_data.setdefault("new_book", {})
    query = update.callback_query
    if not add_field_is_set(nb, current):
        return await _nav_alert(update, ctx, "add_forward_need_value", current)
    if current == ADDING_REVIEW:
        from bookclub.handlers.add import apply_llm_from_review_page

        await apply_llm_from_review_page(update, ctx)
    nxt = add_next_state(current)
    if nxt is None:
        if current == ADDING_DESCRIPTION and not add_field_is_set(nb, current):
            return await _nav_alert(update, ctx, "add_forward_at_end", current)
        if query:
            await query.answer()
        return await show_add_confirm(update, ctx, edit=bool(query))
    if query:
        await query.answer()
    return await send_add_prompt(update, ctx, nxt, edit=bool(query))


async def add_go_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    from bookclub.db import ADD_DRAFTS_MAX, db_count_add_drafts, db_insert_add_draft

    current = ctx.user_data.get("add_state")
    if not isinstance(current, int):
        return ADDING_TITLE
    nb = ctx.user_data.get("new_book") or {}
    title = str(nb.get("title") or "").strip()
    if not title:
        return await _nav_alert(update, ctx, "add_save_need_title", current)
    user = update.effective_user
    if user is None:
        return current
    payload = serialize_add_draft(ctx)
    draft_id = ctx.user_data.get("add_draft_id")
    query = update.callback_query
    if draft_id:
        db_update_add_draft(int(draft_id), user.id, title, payload)
    else:
        if db_count_add_drafts(user.id) >= ADD_DRAFTS_MAX:
            return await _nav_alert(update, ctx, "add_save_too_many", current)
        ctx.user_data["add_draft_id"] = db_insert_add_draft(user.id, title, payload)
    msg = tr(ctx, "add_saved")
    if query:
        await query.answer(msg, show_alert=True)
    elif update.message:
        await update.message.reply_text(msg, parse_mode=PM)
    return current


async def add_go_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    current = ctx.user_data.get("add_state")
    if not isinstance(current, int):
        return ADDING_TITLE
    nb = ctx.user_data.setdefault("new_book", {})
    value = add_edit_value(current, nb)
    if not value:
        return await _nav_alert(update, ctx, "add_edit_need_value", current)
    query = update.callback_query
    if query:
        await query.answer()
    lang = get_lang(ctx)
    placeholder = s(lang, "add_edit_placeholder")
    if len(placeholder) > ForceReply.MAX_INPUT_FIELD_PLACEHOLDER:
        placeholder = placeholder[: ForceReply.MAX_INPUT_FIELD_PLACEHOLDER]
    markup = ForceReply(input_field_placeholder=placeholder)
    text = tr(ctx, "add_edit_prompt", value=h(value))
    if query and query.message:
        await query.message.reply_text(  # type: ignore[attr-defined]
            text, parse_mode=PM, reply_markup=markup
        )
    elif update.message:
        await update.message.reply_text(text, parse_mode=PM, reply_markup=markup)
    return current


async def add_edit_inline_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    if query is None:
        return
    chat_type = getattr(query, "chat_type", None)
    if chat_type is not None:
        chat_type = str(chat_type)
    state = ctx.user_data.get("add_state")
    nb = ctx.user_data.get("new_book") or {}
    in_private = chat_type in _PRIVATE_INLINE_CHATS
    if (
        not in_private
        or not isinstance(state, int)
        or state not in _TEXT_EDIT_STATES
        or not isinstance(nb, dict)
    ):
        await query.answer([], cache_time=0, is_personal=True)
        return
    typed = (query.query or "").strip()
    text = typed or raw_add_value(nb, state) or ""
    if not text:
        await query.answer([], cache_time=0, is_personal=True)
        return
    title = text.replace("\n", " ")
    if len(title) > 64:
        title = title[:61] + "..."
    result = InlineQueryResultArticle(
        id="add-edit",
        title=title,
        description=s(get_lang(ctx), "add_edit_inline_hint"),
        input_message_content=InputTextMessageContent(text),
    )
    await query.answer([result], cache_time=0, is_personal=True)
