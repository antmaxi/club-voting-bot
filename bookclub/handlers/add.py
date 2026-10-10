from __future__ import annotations

import asyncio

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from bookclub.cefr import format_language_levels
from bookclub.config import (
    ADDING_AI_CHOOSE,
    ADDING_AUTHOR,
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
    ENTRY_FIELDS,
    entry_field_enabled,
    llm_configured,
)
from bookclub.db import (
    db_add_book,
    db_cast_vote,
    db_delete_add_draft,
    db_get_add_draft,
    db_get_book,
    db_list_add_drafts,
    find_similar_book_titles,
)
from bookclub.handlers.add_flow import (
    add_next_state,
    apply_add_draft,
    build_add_prompt_text,
    continue_add,
    markup_for_add,
    persist_add_draft_if_saved,
    resume_add_state,
    send_add_prompt,
    typed_add_text,
)
from bookclub.i18n import PM, get_lang, tr
from bookclub.llm import (
    apply_suggestions_to_book,
    llm_error_kind_i18n_key,
    split_llm_error,
    suggest_book_fields,
    suggest_fields_after_review,
    suggest_review_choices,
    ui_llm_error,
)
from bookclub.logging_setup import logger
from bookclub.notifications import schedule_new_book_notifications
from bookclub.original_languages import stored_original_language
from bookclub.review_page import catalog_choice_dict
from bookclub.ui import (
    add_ai_choice_keyboard,
    add_drafts_keyboard,
    add_start_keyboard,
    book_card,
    h,
    is_valid_url,
    parse_optional_creation_year,
    similar_title_confirm_keyboard,
    similar_title_warning_matches_text,
    usable_review_choices,
)


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_add_state(ctx)
    ctx.user_data["new_book"] = {}
    user = update.effective_user
    drafts = db_list_add_drafts(user.id) if user else []
    if llm_configured() or drafts:
        return await ask_add_start(update, ctx, edit=False)
    return await send_add_prompt(update, ctx, ADDING_TITLE)


def _clear_add_state(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("new_book", None)
    ctx.user_data.pop("add_state", None)
    ctx.user_data.pop("llm_add", None)
    ctx.user_data.pop("admin_add", None)
    ctx.user_data.pop("llm_suggestions_applied", None)
    ctx.user_data.pop("llm_filled_keys", None)
    ctx.user_data.pop("llm_extracted_review", None)
    ctx.user_data.pop("review_choices", None)
    ctx.user_data.pop("add_draft_id", None)
    ctx.user_data.pop("add_from_start", None)


async def ask_add_start(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, edit: bool = False
) -> int:
    ctx.user_data["add_state"] = ADDING_START
    ctx.user_data["add_from_start"] = True
    user = update.effective_user
    drafts = db_list_add_drafts(user.id) if user else []
    lang = get_lang(ctx)
    llm = llm_configured()
    text = tr(ctx, "add_start_ask")
    markup = add_start_keyboard(lang, llm=llm, has_drafts=bool(drafts))
    query = update.callback_query
    if edit and query:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=PM)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=PM)
    elif query and query.message:
        await query.message.reply_text(  # type: ignore[attr-defined]
            text, reply_markup=markup, parse_mode=PM
        )
    return ADDING_START


async def add_start_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, action = query.data.split(":", 1)
    ctx.user_data.setdefault("new_book", {})
    ctx.user_data["add_from_start"] = True
    if action == "drafts":
        return await ask_add_drafts(update, ctx, edit=True)
    ctx.user_data["new_book"] = {}
    ctx.user_data.pop("add_draft_id", None)
    ctx.user_data.pop("llm_suggestions_applied", None)
    ctx.user_data.pop("llm_filled_keys", None)
    ctx.user_data.pop("llm_extracted_review", None)
    ctx.user_data.pop("review_choices", None)
    ctx.user_data["llm_add"] = action == "ai"
    return await send_add_prompt(update, ctx, ADDING_TITLE, edit=True)


async def ask_add_drafts(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
    alert_empty: bool = True,
) -> int:
    ctx.user_data["add_state"] = ADDING_DRAFT_CHOOSE
    ctx.user_data["add_from_start"] = True
    user = update.effective_user
    drafts = db_list_add_drafts(user.id) if user else []
    lang = get_lang(ctx)
    query = update.callback_query
    if not drafts:
        if alert_empty and query:
            await query.answer(tr(ctx, "add_drafts_empty"), show_alert=True)
        return await ask_add_start(update, ctx, edit=edit or bool(query))
    text = tr(ctx, "add_drafts_ask")
    markup = add_drafts_keyboard(lang, drafts)
    if edit and query:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=PM)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=PM)
    elif query and query.message:
        await query.message.reply_text(  # type: ignore[attr-defined]
            text, reply_markup=markup, parse_mode=PM
        )
    return ADDING_DRAFT_CHOOSE


async def add_draft_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _, raw_id = query.data.split(":", 1)
    try:
        draft_id = int(raw_id)
    except ValueError:
        await query.answer()
        return await ask_add_drafts(update, ctx, edit=True)
    user = update.effective_user
    if user is None:
        await query.answer()
        return ConversationHandler.END
    payload = db_get_add_draft(draft_id, user.id)
    if payload is None:
        await query.answer(tr(ctx, "add_draft_missing"), show_alert=True)
        return await ask_add_drafts(update, ctx, edit=True)
    await query.answer()
    apply_add_draft(ctx, payload, draft_id)
    state = resume_add_state(ctx.user_data.get("add_state"))
    if state == ADDING_AI_CHOOSE:
        return await ask_add_ai(update, ctx, edit=True)
    return await send_add_prompt(update, ctx, state, edit=True)


async def add_draft_del_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _, raw_id = query.data.split(":", 1)
    try:
        draft_id = int(raw_id)
    except ValueError:
        await query.answer()
        return await ask_add_drafts(update, ctx, edit=True)
    user = update.effective_user
    if user is None:
        await query.answer()
        return ConversationHandler.END
    db_delete_add_draft(draft_id, user.id)
    await query.answer(tr(ctx, "add_draft_deleted"))
    return await ask_add_drafts(update, ctx, edit=True, alert_empty=False)


async def add_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.message.text.strip()
    nb = ctx.user_data.setdefault("new_book", {})
    nb["title"] = title
    nb.pop("review_link", None)
    ctx.user_data.pop("llm_suggestions_applied", None)
    ctx.user_data.pop("llm_filled_keys", None)
    ctx.user_data.pop("llm_extracted_review", None)
    ctx.user_data.pop("review_choices", None)
    lang = get_lang(ctx)
    similar = find_similar_book_titles(title)
    if similar:
        ctx.user_data["add_state"] = ADDING_TITLE_CONFIRM
        persist_add_draft_if_saved(update, ctx)
        await update.message.reply_text(
            tr(
                ctx,
                "similar_title_warning",
                title=h(title),
                matches=similar_title_warning_matches_text(similar),
            ),
            reply_markup=similar_title_confirm_keyboard(lang),
            parse_mode=PM,
        )
        return ADDING_TITLE_CONFIRM
    return await advance_after_title(update, ctx, ADDING_TITLE)


async def add_title_similar_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, action = query.data.split(":", 1)
    if action == "no":
        _clear_add_state(ctx)
        await query.edit_message_text(tr(ctx, "cancelled"), parse_mode=PM)
        return ConversationHandler.END
    return await advance_after_title(update, ctx, ADDING_TITLE_CONFIRM, edit=True)


async def _send_add_status(
    update: Update, text: str, *, edit: bool = False, parse_mode: str | None = PM
) -> None:
    query = update.callback_query
    try:
        if edit and query:
            await query.edit_message_text(text, parse_mode=parse_mode)
            return
        if update.message:
            await update.message.reply_text(text, parse_mode=parse_mode)
            return
        if query and query.message:
            await query.message.reply_text(text, parse_mode=parse_mode)  # type: ignore[attr-defined]
    except BadRequest:
        logger.warning(
            "add status send failed with parse_mode=%s; retrying plain", parse_mode
        )
        if edit and query:
            await query.edit_message_text(text)
            return
        if update.message:
            await update.message.reply_text(text)
            return
        if query and query.message:
            await query.message.reply_text(text)  # type: ignore[attr-defined]


async def apply_llm_suggestions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.user_data.get("llm_add"):
        return
    if ctx.user_data.get("llm_suggestions_applied"):
        return
    if add_next_state(ADDING_TITLE) is None:
        ctx.user_data["llm_suggestions_applied"] = True
        return
    user = update.effective_user
    if user is None:
        return
    title = str(ctx.user_data.get("new_book", {}).get("title") or "")
    if entry_field_enabled("review"):
        status_key = "add_ai_suggesting_review"
    else:
        status_key = "add_ai_suggesting"
    await _send_add_status(
        update,
        tr(ctx, status_key, title=h(title)),
        edit=bool(update.callback_query),
    )
    lang = get_lang(ctx)
    if entry_field_enabled("review"):
        ctx.user_data.pop("review_choices", None)
        choices, error = await asyncio.to_thread(
            suggest_review_choices, title, lang=lang
        )
        ctx.user_data["llm_suggestions_applied"] = True
        if not await _report_llm_error(update, ctx, error):
            return
        nb = ctx.user_data.setdefault("new_book", {})
        filled: set[str] = set()
        if len(choices) == 1:
            filled = apply_suggestions_to_book(nb, {"review_link": choices[0].url})
        elif len(choices) > 1:
            nb.pop("review_link", None)
            ctx.user_data["review_choices"] = [
                catalog_choice_dict(hit) for hit in choices
            ]
        ctx.user_data["llm_filled_keys"] = filled
        if filled:
            await _send_add_status(update, tr(ctx, "add_ai_suggested"))
        return
    suggestions, error = await asyncio.to_thread(suggest_book_fields, title, lang=lang)
    ctx.user_data["llm_suggestions_applied"] = True
    if not await _report_llm_error(update, ctx, error):
        return
    nb = ctx.user_data.setdefault("new_book", {})
    ctx.user_data["llm_filled_keys"] = apply_suggestions_to_book(nb, suggestions)
    if ctx.user_data["llm_filled_keys"]:
        await _send_add_status(update, tr(ctx, "add_ai_suggested"))


async def apply_llm_from_review_page(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not ctx.user_data.get("llm_add"):
        return
    nb = ctx.user_data.setdefault("new_book", {})
    url = str(nb.get("review_link") or "").strip()
    if not url:
        return
    if ctx.user_data.get("llm_extracted_review") == url:
        return
    remaining = frozenset(
        name for name in ENTRY_FIELDS if name != "review" and entry_field_enabled(name)
    )
    if not remaining:
        ctx.user_data["llm_extracted_review"] = url
        return
    title = str(nb.get("title") or "")
    await _send_add_status(
        update,
        tr(ctx, "add_ai_reading_review", title=h(title)),
        edit=bool(update.callback_query),
    )
    lang = get_lang(ctx)
    suggestions, error = await asyncio.to_thread(
        suggest_fields_after_review,
        title,
        url,
        lang=lang,
        enabled_fields=remaining,
    )
    if not await _report_llm_error(update, ctx, error):
        ctx.user_data["llm_extracted_review"] = url
        return
    already = ctx.user_data.get("llm_filled_keys")
    filled_now = already if isinstance(already, set) else set()
    preserve = {key for key in nb if key not in filled_now and key != "title"}
    newly = apply_suggestions_to_book(nb, suggestions, preserve=preserve)
    ctx.user_data["llm_filled_keys"] = (filled_now | newly) - preserve
    ctx.user_data["llm_extracted_review"] = url
    if newly:
        await _send_add_status(update, tr(ctx, "add_ai_suggested"))


async def _report_llm_error(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, error: str | None
) -> bool:
    """Show a provider error and return False; True means keep going."""
    if not error:
        return True
    if error == "not_configured":
        await _send_add_status(update, tr(ctx, "add_ai_no_llm"))
        return False
    kind, detail = split_llm_error(error)
    kind_label = tr(ctx, llm_error_kind_i18n_key(kind))
    detail_text = ui_llm_error(detail) or error
    # Plain text: provider errors often contain <>&{} that break Telegram HTML
    # and would drop this message, leaving only the generic warning.
    await _send_add_status(
        update,
        tr(ctx, "add_ai_suggest_failed", kind=kind_label, error=detail_text),
        parse_mode=None,
    )
    return False


async def ask_add_ai(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, edit: bool = False
) -> int:
    ctx.user_data["add_state"] = ADDING_AI_CHOOSE
    persist_add_draft_if_saved(update, ctx)
    lang = get_lang(ctx)
    text = tr(ctx, "add_ai_ask")
    markup = add_ai_choice_keyboard(lang)
    query = update.callback_query
    if edit and query:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=PM)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=PM)
    elif query and query.message:
        await query.message.reply_text(  # type: ignore[attr-defined]
            text, reply_markup=markup, parse_mode=PM
        )
    return ADDING_AI_CHOOSE


async def add_ai_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, action = query.data.split(":", 1)
    ctx.user_data["llm_add"] = action == "yes"
    if ctx.user_data["llm_add"]:
        await apply_llm_suggestions(update, ctx)
        return await continue_add(update, ctx, ADDING_TITLE, edit=False)
    return await continue_add(update, ctx, ADDING_TITLE, edit=True)


async def advance_after_title(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    current: int,
    *,
    edit: bool = False,
) -> int:
    if add_next_state(ADDING_TITLE) is None:
        return await continue_add(update, ctx, current, edit=edit)
    if ctx.user_data.get("llm_add") is True:
        await apply_llm_suggestions(update, ctx)
        return await continue_add(update, ctx, ADDING_TITLE, edit=False)
    if ctx.user_data.get("llm_add") is False:
        return await continue_add(update, ctx, current, edit=edit)
    if llm_configured():
        return await ask_add_ai(update, ctx, edit=edit)
    return await continue_add(update, ctx, current, edit=edit)


async def add_author(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["new_book"]["author"] = typed_add_text(update, ctx)
    return await continue_add(update, ctx, ADDING_AUTHOR)


async def add_pages(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = typed_add_text(update, ctx)
    nb = ctx.user_data["new_book"]
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text(
            f"{tr(ctx, 'invalid_pages')}\n\n{build_add_prompt_text(ctx, ADDING_PAGES, nb)}",
            reply_markup=markup_for_add(ctx, ADDING_PAGES, nb),
            parse_mode=PM,
        )
        ctx.user_data["add_state"] = ADDING_PAGES
        return ADDING_PAGES
    ctx.user_data["new_book"]["pages"] = int(text)
    return await continue_add(update, ctx, ADDING_PAGES)


async def add_fiction_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, value = query.data.split(":")
    ctx.user_data["new_book"]["fiction"] = value == "1"
    return await continue_add(update, ctx, ADDING_FICTION, edit=True)


async def add_review(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = typed_add_text(update, ctx)
    nb = ctx.user_data["new_book"]
    if not is_valid_url(text):
        await update.message.reply_text(
            f"{tr(ctx, 'invalid_review')}\n\n{build_add_prompt_text(ctx, ADDING_REVIEW, nb)}",
            reply_markup=markup_for_add(ctx, ADDING_REVIEW, nb),
            parse_mode=PM,
        )
        ctx.user_data["add_state"] = ADDING_REVIEW
        return ADDING_REVIEW
    ctx.user_data["new_book"]["review_link"] = text
    ctx.user_data.pop("review_choices", None)
    return await continue_add(update, ctx, ADDING_REVIEW)


async def add_review_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, raw = query.data.split(":", 1)
    choices = usable_review_choices(ctx.user_data.get("review_choices"))
    try:
        index = int(raw)
    except ValueError:
        return ADDING_REVIEW
    if not isinstance(choices, list) or index < 0 or index >= len(choices):
        return ADDING_REVIEW
    chosen = choices[index]
    url = chosen.get("url") if isinstance(chosen, dict) else None
    if not isinstance(url, str) or not is_valid_url(url):
        return ADDING_REVIEW
    ctx.user_data.setdefault("new_book", {})["review_link"] = url
    ctx.user_data.pop("review_choices", None)
    return await continue_add(update, ctx, ADDING_REVIEW, edit=True)


async def add_original_language_skip(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    ctx.user_data["new_book"]["original_language"] = ""
    return await continue_add(update, ctx, ADDING_ORIGINAL_LANGUAGE)


async def add_original_language_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    _, action = query.data.split(":", 1)
    if action == "skip":
        ctx.user_data["new_book"]["original_language"] = ""
        return await continue_add(update, ctx, ADDING_ORIGINAL_LANGUAGE, edit=True)
    if action == "other":
        ctx.user_data["add_state"] = ADDING_ORIGINAL_LANGUAGE_OTHER
        await query.edit_message_text(
            build_add_prompt_text(
                ctx, ADDING_ORIGINAL_LANGUAGE_OTHER, ctx.user_data["new_book"]
            ),
            reply_markup=markup_for_add(
                ctx,
                ADDING_ORIGINAL_LANGUAGE_OTHER,
                ctx.user_data["new_book"],
            ),
            parse_mode=PM,
        )
        return ADDING_ORIGINAL_LANGUAGE_OTHER
    stored = stored_original_language(action)
    if stored is None:
        return ADDING_ORIGINAL_LANGUAGE
    ctx.user_data["new_book"]["original_language"] = stored
    return await continue_add(update, ctx, ADDING_ORIGINAL_LANGUAGE, edit=True)


async def add_original_language_other(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> int:
    text = typed_add_text(update, ctx)
    if not text:
        await update.message.reply_text(
            build_add_prompt_text(
                ctx, ADDING_ORIGINAL_LANGUAGE_OTHER, ctx.user_data["new_book"]
            ),
            reply_markup=markup_for_add(
                ctx, ADDING_ORIGINAL_LANGUAGE_OTHER, ctx.user_data["new_book"]
            ),
            parse_mode=PM,
        )
        ctx.user_data["add_state"] = ADDING_ORIGINAL_LANGUAGE_OTHER
        return ADDING_ORIGINAL_LANGUAGE_OTHER
    ctx.user_data["new_book"]["original_language"] = text
    return await continue_add(update, ctx, ADDING_ORIGINAL_LANGUAGE_OTHER)


async def add_creation_year(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = typed_add_text(update, ctx)
    nb = ctx.user_data["new_book"]
    try:
        year = parse_optional_creation_year(text)
    except ValueError:
        await update.message.reply_text(
            f"{tr(ctx, 'invalid_creation_year')}\n\n"
            f"{build_add_prompt_text(ctx, ADDING_CREATION_YEAR, nb)}",
            reply_markup=markup_for_add(ctx, ADDING_CREATION_YEAR, nb),
            parse_mode=PM,
        )
        ctx.user_data["add_state"] = ADDING_CREATION_YEAR
        return ADDING_CREATION_YEAR
    ctx.user_data["new_book"]["creation_year"] = year
    return await continue_add(update, ctx, ADDING_CREATION_YEAR)


async def add_language_level_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _, action, *rest = query.data.split(":")
    selected: set[str] = ctx.user_data["new_book"].setdefault("language_levels", set())
    if action == "toggle":
        level = rest[0]
        if level in selected:
            selected.discard(level)
        else:
            selected.add(level)
        await query.answer()
        await query.edit_message_text(
            build_add_prompt_text(
                ctx, ADDING_LANGUAGE_LEVEL, ctx.user_data["new_book"]
            ),
            reply_markup=markup_for_add(
                ctx, ADDING_LANGUAGE_LEVEL, ctx.user_data["new_book"]
            ),
            parse_mode=PM,
        )
        ctx.user_data["add_state"] = ADDING_LANGUAGE_LEVEL
        return ADDING_LANGUAGE_LEVEL
    if action == "done":
        if not selected:
            await query.answer(tr(ctx, "language_level_none_selected"), show_alert=True)
            return ADDING_LANGUAGE_LEVEL
        await query.answer()
        return await continue_add(update, ctx, ADDING_LANGUAGE_LEVEL, edit=True)
    await query.answer()
    return ADDING_LANGUAGE_LEVEL


async def complete_new_book(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    description: str | None = None,
) -> int:
    lang = get_lang(ctx)
    if ctx.user_data is None or "new_book" not in ctx.user_data:
        logger.warning(
            f"User {update.effective_user.id} tried to add an entry but 'new_book' is missing."
        )
        if update.message:
            await update.message.reply_text(tr(ctx, "cancelled"), parse_mode=PM)
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                tr(ctx, "cancelled"), parse_mode=PM
            )
        return ConversationHandler.END

    nb = ctx.user_data["new_book"]
    desc = description if description is not None else (nb.get("description") or "")
    user = update.effective_user
    levels_set = nb.get("language_levels")
    language_levels = (
        format_language_levels(levels_set) if isinstance(levels_set, set) else None
    )
    book_id = db_add_book(
        nb["title"],
        nb.get("author") or "",
        nb.get("pages") or 0,
        nb.get("fiction", True),
        nb.get("review_link") or "",
        desc,
        user.id,
        user.full_name,
        user.username,
        original_language=nb.get("original_language") or None,
        creation_year=nb.get("creation_year"),
        language_levels=language_levels,
    )
    if book_id is None:
        raise RuntimeError("db_add_book did not return a book id")
    # Adding a book is a yes vote from the person who proposed it.
    db_cast_vote(user.id, book_id, 1)
    draft_id = ctx.user_data.get("add_draft_id")
    if draft_id:
        db_delete_add_draft(int(draft_id), user.id)
    book = db_get_book(book_id)
    if book is None:
        raise RuntimeError(f"book {book_id} missing immediately after insert")

    confirm_text = f"{tr(ctx, 'book_added')}\n\n{book_card(book, lang)}{tr(ctx, 'new_book_delay_note')}"

    query = update.callback_query
    if update.message:
        await update.message.reply_text(confirm_text, parse_mode=PM)
    elif query and query.message:
        await query.edit_message_text(confirm_text, parse_mode=PM)

    _clear_add_state(ctx)

    schedule_new_book_notifications(ctx.job_queue, book_id, user.id)

    return ConversationHandler.END


async def add_confirm_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    return await complete_new_book(update, ctx)


async def add_description(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if ctx.user_data is None or "new_book" not in ctx.user_data:
        return await complete_new_book(update, ctx)
    text = typed_add_text(update, ctx)
    desc = "" if text == "/skip" else text
    ctx.user_data["new_book"]["description"] = desc
    return await continue_add(update, ctx, ADDING_DESCRIPTION)
