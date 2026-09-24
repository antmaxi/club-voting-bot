from __future__ import annotations

from typing import Any

from telegram.ext import ContextTypes

from bookclub.config import CLUB_ENTITY, notify_delay_minutes
from bookclub.types import TranslationValue

SUPPORTED_LANGS: tuple[str, ...] = ("en", "ru", "de")
LANG_NATIVE_NAME: dict[str, str] = {
    "en": "English",
    "ru": "Русский",
    "de": "Deutsch",
}


def next_ui_lang(current: str) -> str:
    if current not in SUPPORTED_LANGS:
        return SUPPORTED_LANGS[0]
    idx = SUPPORTED_LANGS.index(current)
    return SUPPORTED_LANGS[(idx + 1) % len(SUPPORTED_LANGS)]


# Grammatical forms and a few fixed phrases for the club entity (book vs film).
# Templates in T use these keys; film-specific vocabulary (pages vs runtime, …)
# stays in ENTITY_STRING_OVERLAYS.
ENTITY_LEX: dict[str, dict[str, dict[str, str]]] = {
    "book": {
        "en": {
            "sg": "book",
            "pl": "books",
            "acc": "book",
            "gen_sg": "book",
            "gen_pl": "books",
            "Sg": "Book",
            "Pl": "Books",
            "club_name": "Book Club Bot",
            "bot_name": "Book Club Bot",
            "card_icon": "📖",
            "subtitle_icon": "✍️",
            "list_icon": "📚",
            "author": "author",
            "Author": "Author",
            "verb": "read",
            "verb_gerund": "reading",
            "added": "Book added!",
            "added_new": "New book added!",
            "updated": "Book updated!",
            "this_prep": "this book",
        },
        "ru": {
            "sg": "книга",
            "pl": "книги",
            "acc": "книгу",
            "gen_sg": "книги",
            "gen_pl": "книг",
            "prep_pl": "книгах",
            "Sg": "Книга",
            "Pl": "Книги",
            "club_name": "Книжный клуб",
            "bot_name": "Бот книжного клуба",
            "card_icon": "📖",
            "subtitle_icon": "✍️",
            "list_icon": "📚",
            "author": "автор",
            "Author": "Автор",
            "verb": "читать",
            "verb_gerund": "чтения",
            "added": "Книга добавлена!",
            "added_new": "Добавлена новая книга!",
            "updated": "Книга обновлена!",
            "this_prep": "этой книге",
            "deleted_pp": "удалена",
            "marked_pp": "отмечена",
            "hidden_pp": "скрыта",
            "visible_pp": "видна",
            "discussed_acc": "обсуждённую книгу",
            "undiscussed_acc": "необсуждённую книгу",
            "hidden_acc": "скрытую книгу",
            "one_acc": "одну книгу",
            "none_discussed": "ни одна книга не была обсуждена",
        },
        "de": {
            "sg": "Buch",
            "pl": "Bücher",
            "acc": "ein Buch",
            "gen_sg": "Buches",
            "gen_pl": "Bücher",
            "prep_pl": "Büchern",
            "Sg": "Buch",
            "Pl": "Bücher",
            "club_name": "Bücherclub-Bot",
            "bot_name": "Bücherclub-Bot",
            "card_icon": "📖",
            "subtitle_icon": "✍️",
            "list_icon": "📚",
            "author": "Autor",
            "Author": "Autor",
            "verb": "lesen",
            "verb_gerund": "Lesen",
            "added": "Buch hinzugefügt!",
            "added_new": "Neues Buch hinzugefügt!",
            "updated": "Buch aktualisiert!",
            "this_prep": "dieses Buch",
            "discussed_acc": "ein diskutiertes Buch",
            "undiscussed_acc": "ein noch nicht diskutiertes Buch",
            "hidden_acc": "ein verborgenes Buch",
            "one_acc": "ein Buch",
            "none_discussed": "es wurde noch kein Buch diskutiert",
        },
    },
    "film": {
        "en": {
            "sg": "film",
            "pl": "films",
            "acc": "film",
            "gen_sg": "film",
            "gen_pl": "films",
            "Sg": "Film",
            "Pl": "Films",
            "club_name": "Film Club Bot",
            "bot_name": "Film Club Bot",
            "card_icon": "🎬",
            "subtitle_icon": "🎬",
            "list_icon": "🎬",
            "author": "director",
            "Author": "Director",
            "verb": "watch",
            "verb_gerund": "watching",
            "added": "Film added!",
            "added_new": "New film added!",
            "updated": "Film updated!",
            "this_prep": "this film",
        },
        "ru": {
            "sg": "фильм",
            "pl": "фильмы",
            "acc": "фильм",
            "gen_sg": "фильма",
            "gen_pl": "фильмов",
            "prep_pl": "фильмах",
            "Sg": "Фильм",
            "Pl": "Фильмы",
            "club_name": "Киноклуб",
            "bot_name": "Киноклуб-бот",
            "card_icon": "🎬",
            "subtitle_icon": "🎬",
            "list_icon": "🎬",
            "author": "режиссёр",
            "Author": "Режиссёр",
            "verb": "смотреть",
            "verb_gerund": "просмотра",
            "added": "Фильм добавлен!",
            "added_new": "Добавлен новый фильм!",
            "updated": "Фильм обновлён!",
            "this_prep": "этом фильме",
            "deleted_pp": "удалён",
            "marked_pp": "отмечен",
            "hidden_pp": "скрыт",
            "visible_pp": "виден",
            "discussed_acc": "обсуждённый фильм",
            "undiscussed_acc": "необсуждённый фильм",
            "hidden_acc": "скрытый фильм",
            "one_acc": "один фильм",
            "none_discussed": "ни один фильм не был обсуждён",
        },
        "de": {
            "sg": "Film",
            "pl": "Filme",
            "acc": "einen Film",
            "gen_sg": "Films",
            "gen_pl": "Filme",
            "prep_pl": "Filmen",
            "Sg": "Film",
            "Pl": "Filme",
            "club_name": "Filmclub-Bot",
            "bot_name": "Filmclub-Bot",
            "card_icon": "🎬",
            "subtitle_icon": "🎬",
            "list_icon": "🎬",
            "author": "Regisseur",
            "Author": "Regisseur",
            "verb": "sehen",
            "verb_gerund": "Ansehen",
            "added": "Film hinzugefügt!",
            "added_new": "Neuer Film hinzugefügt!",
            "updated": "Film aktualisiert!",
            "this_prep": "diesen Film",
            "discussed_acc": "einen diskutierten Film",
            "undiscussed_acc": "einen noch nicht diskutierten Film",
            "hidden_acc": "einen verborgenen Film",
            "one_acc": "einen Film",
            "none_discussed": "es wurde noch kein Film diskutiert",
        },
    },
}


class _FormatDefaults(dict[str, Any]):
    """Leave unknown {placeholders} intact for a later .format() call."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _ru_num_word(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    r = n_abs % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def format_defaults(lang: str) -> dict[str, Any]:
    minutes = notify_delay_minutes()
    lex = ENTITY_LEX[CLUB_ENTITY][lang]
    if lang == "ru":
        min_word = _ru_num_word(minutes, "минута", "минуты", "минут")
        minutes_phrase = f"{minutes} {min_word}"
        minutes_adj = minutes_phrase
        min_short = "мин"
    elif lang == "de":
        min_word = "Minute" if minutes == 1 else "Minuten"
        minutes_phrase = f"{minutes} {min_word}"
        minutes_adj = minutes_phrase
        min_short = "Min"
    else:
        min_word = "minute" if minutes == 1 else "minutes"
        minutes_phrase = f"{minutes} {min_word}"
        minutes_adj = f"{minutes}-minute"
        min_short = "min"
    return {
        **lex,
        "minutes": minutes,
        "minutes_phrase": minutes_phrase,
        "minutes_adj": minutes_adj,
        "min_short": min_short,
    }


def format_ui(lang: str, text: str, **kwargs: Any) -> str:
    return text.format_map(_FormatDefaults({**format_defaults(lang), **kwargs}))


T: dict[str, dict[str, TranslationValue]] = {
    "en": {
        "welcome": (
            "{card_icon} <b>Welcome to the {club_name}!</b>\n\n"
            "➕ /add — Add a {sg}\n"
            "📋 /list_and_vote — See all {pl}\n"
            "🏆 /top — Top rated {pl}\n"
            "⚙️ /settings — Settings\n"
            "ℹ️ /info — About the bot\n"
            "✏️ /edit — Edit a {sg} entry\n"
            "🗑 /delete — Delete a {sg}\n"
            "✅ /discussed — {Pl} already discussed\n"
            "❓ /help — Show this message"
        ),
        "welcome_admin_suffix": "\n🛠 /adminconsole — Admin console",
        "lang_set": "🇬🇧 Language set to English.",
        "ask_title": "{card_icon} What is the <b>title</b> of the {sg}?",
        "add_ai_ask": (
            "✨ Fill in the other fields with <b>AI help</b>, or enter them yourself?"
        ),
        "add_ai_yes_btn": "✨ Use AI",
        "add_ai_no_btn": "✍️ I'll fill it in",
        "add_start_ask": "How do you want to add a {sg}?",
        "add_start_new_btn": "➕ New",
        "add_continue_btn": "📋 Continue a saved draft",
        "add_drafts_ask": "📋 Saved drafts — pick one to continue:",
        "add_draft_untitled": "(no title)",
        "add_save_btn": "💾 Save",
        "add_saved": "Progress saved. Use /add later to continue this draft.",
        "add_save_need_title": "Enter a title before saving.",
        "add_save_too_many": ("You already have 20 saved drafts. Delete one first."),
        "add_drafts_empty": "No saved drafts.",
        "add_draft_missing": "That draft is gone.",
        "add_draft_deleted": "Draft deleted.",
        "add_back_btn": "⬅️ Back",
        "add_forward_btn": "➡️ Forward",
        "add_edit_btn": "✏️ Edit",
        "add_edit_placeholder": "Edit the current value",
        "add_edit_prompt": ("✏️ Edit the value and send it:\n\n<pre>{value}</pre>"),
        "add_edit_need_value": "There's no value to edit on this step.",
        "add_edit_inline_hint": "Tap to send the edited value",
        "add_back_at_start": "You're already at the first step.",
        "add_forward_at_end": "This is the last step — send a description or /skip.",
        "add_forward_need_value": "This step has no saved answer yet. Fill it in first.",
        "add_back_hint": "<i>Tap Back or send /back to change a previous answer.</i>",
        "add_forward_hint": (
            "<i>Tap Forward or send /forward to keep this answer, or send a new "
            "value to replace it. Edit puts it in the message field.</i>"
        ),
        "add_nav_hint": (
            "<i>Tap Forward to keep this answer, or send a new value to replace "
            "it. Edit puts it in the message field.</i>"
        ),
        "add_current_value": "Current: <i>{value}</i>",
        "add_suggested_value": "Suggested: <i>{value}</i>",
        "add_suggested_hint": (
            "<i>Tap Forward to keep this suggestion, or send a new value to "
            "replace it. Edit puts it in the message field.</i>"
        ),
        "add_confirm_prompt": (
            "📋 <b>Review the entry</b> before adding it. Go back to change a "
            "field, or add it now:"
        ),
        "add_confirm_btn": "✅ Add",
        "ask_author": "{subtitle_icon} Who is the <b>{author}</b>?",
        "ask_pages": "📄 How many <b>pages</b> does it have? (enter a number)",
        "invalid_pages": "⚠️ Please enter a valid number of pages (e.g. 320):",
        "ask_fiction": "📂 Is it <b>Fiction</b> or <b>Non-fiction</b>?",
        "fiction_btn": "📖 Fiction",
        "nonfiction_btn": "📰 Non-fiction",
        "ask_review": "🔗 Paste the <b>link to a review</b> (must start with http:// or https://):",
        "invalid_review": "⚠️ That doesn't look like a valid URL. Please paste a link starting with http:// or https://:",
        "ask_original_language": "🌐 <b>Original language</b> — pick one:",
        "ask_original_language_other": "🌐 <b>Original language</b> — type the language name:",
        "orig_lang_ru": "🇷🇺 Russian",
        "orig_lang_de": "🇩🇪 German",
        "orig_lang_en": "🇬🇧 English",
        "orig_lang_it": "🇮🇹 Italian",
        "orig_lang_fr": "🇫🇷 French",
        "orig_lang_es": "🇪🇸 Spanish",
        "orig_lang_zh": "🇨🇳 Chinese",
        "orig_lang_ja": "🇯🇵 Japanese",
        "orig_lang_other_btn": "✏️ Other",
        "orig_lang_skip_btn": "⏭ Skip",
        "ask_creation_year": "📅 <b>Year of creation</b> (publication year; 4 digits, or /skip if unknown):",
        "ask_language_level": (
            "🎓 Estimated <b>language level</b> to {verb} the {sg} (CEFR A1–C2). "
            "Tap to toggle. Selected: <b>{count}</b>"
        ),
        "language_level_done_btn": "✅ Done",
        "language_level_none_selected": "Select at least one level (A1–C2).",
        "invalid_creation_year": "⚠️ Please enter a valid 4-digit year (e.g. 1984), or /skip:",
        "ask_desc": "📝 Add a <b>description</b> (or /skip to leave empty):",
        "book_added": "✅ {added}",
        "similar_title_warning": (
            "⚠️ A {sg} with a <b>similar title</b> is already in the list:\n{matches}\n\n"
            "Continue adding <b>{title}</b>?"
        ),
        "similar_title_confirm_btn": "✅ Yes, continue",
        "similar_title_cancel_btn": "❌ No, cancel",
        "no_books": "📭 No {pl} yet. Use /add to add one!",
        "no_undiscussed": "📭 No undiscussed {pl} — use /discussed to see past {pl}.",
        "no_votes": "No votes yet. Use /list_and_vote to see {pl} and vote inline!",
        "no_books_edit": "📭 No {pl} to edit yet.",
        "no_books_delete": "📭 No {pl} to delete yet.",
        "cancelled": "❌ Cancelled.",
        "unexpected_error": "⚠️ Something went wrong on my side. Please try again — use /cancel first if you were in the middle of something.",
        "choose_vote": "📊 Choose a {sg} to vote on:",
        "choose_edit": "✏️ Choose a {sg} to edit:",
        "choose_delete": "🗑 Choose a {sg} to delete:",
        "your_vote": "Your current vote",
        "none_vote": "—",
        "rate_book": "📊 Vote on <b>{title}</b>",
        "desc_updated": "✅ Description updated!",
        "top_title": "🏆 <b>Top {Pl}</b>\nSorted by total score.\n",
        "added_by": "Added by",
        "added_on": "Added on",
        "pages_label": "Pages",
        "original_language_label": "Original language",
        "creation_year_label": "Year",
        "language_levels_label": "Language level",
        "review_label": "Review",
        "cancel_btn": "❌ Cancel",
        "edit_field_prompt": "✏️ <b>{field}</b>\nCurrent value: <i>{value}</i>\n\nModify this field?",
        "edit_yes_btn": "✏️ Yes, change it",
        "edit_no_btn": "⏭ Skip",
        "edit_ask_new": "Send the new value for <b>{field}</b>:",
        "edit_done": "✅ {updated}",
        "edit_invalid_pages": "⚠️ Must be a positive number. Send again:",
        "edit_invalid_url": "⚠️ Must start with http:// or https://. Send again:",
        "edit_invalid_creation_year": "⚠️ Must be a 4-digit year (e.g. 1984). Send again:",
        "field_title": "Title",
        "field_author": "{Author}",
        "field_pages": "Pages",
        "field_fiction": "Fiction / Non-fiction",
        "field_review": "Review link",
        "field_description": "Description",
        "field_original_language": "Original language",
        "field_creation_year": "Year of creation",
        "field_language_levels": "Language level (CEFR)",
        "deleted": "🗑 <b>{title}</b> has been deleted.",
        "fiction_label": "Fiction",
        "nonfiction_label": "Non-fiction",
        "votes_label": lambda n, **_: f"({n} vote{'s' if n != 1 else ''})",
        "want_label": "✅ want to {verb}",
        "meh_label": "😐 don't care",
        "no_label": "❌ don't want to {verb}",
        "vote_registered": "Your vote: {label}",
        "voting_closed": "Voting for this entry is closed.",
        "want_btn": "✅ Want",
        "meh_btn": "😐 Don't care",
        "no_btn": "❌ Don't want",
        "voted_msg": "✅ Vote saved for <b>{title}</b>",
        "score_label": "Score",
        "no_permission": "⛔ You can only edit or delete {pl} you added.",
        "no_own_books": "📭 You have no {pl} to edit or delete.",
        "admin_only": "⛔ This command is for admins only.",
        "admin_console_title": "🛠 <b>Admin Console</b>",
        "admin_mark_btn": "📌 Mark discussed",
        "admin_hide_btn": "👻 Hide {sg}",
        "admin_notify_btn": "🔔 Send voting reminder (top)",
        "admin_notify_one_btn": "🔔 Send reminder (pick {pl})",
        "admin_notify_chat_btn": "💬 Post voting reminder to chat (top)",
        "admin_notify_chat_one_btn": "💬 Post reminder to chat (pick {pl})",
        "choose_notify_chat": "💬 Choose a {sg} to post a voting reminder in the group chat:",
        "vote_reminder_chat": "🔔 <b>Voting reminder!</b>\n\n",
        "admin_notify_chat_confirm": (
            lambda count, sg, pl, **_: (
                f"💬 Voting reminder posted to the group chat ({count} {sg if count == 1 else pl})."
            )
        ),
        "admin_notify_chat_no_chat": "ℹ️ Group chat is not configured (ALLOWED_CHAT_ID).",
        "admin_notify_chat_failed": "⚠️ Failed to post to the group chat.",
        "admin_toggle_chat_btn": "💬 Post to chat: {state}",
        "admin_toggle_votes_btn": "📊 Vote counting: {state}",
        "admin_votes_mode_all": "all votes",
        "admin_votes_mode_attendance": "attendance (net ≥ 1)",
        "admin_unhide_btn": "👁 Show {sg}",
        "choose_hide": "👻 Choose a {sg} to hide from the list:",
        "choose_unhide": "👁 Choose a hidden {sg} to show again in the list:",
        "no_hidden": "📭 No hidden {pl}.",
        "choose_notify": "🔔 Choose a {sg} to send a reminder for:",
        "choose_notify_books": "🔔 Tap {pl} to include in the reminder. Selected: <b>{count}</b>",
        "choose_notify_chat_books": "💬 Tap {pl} to post in the group chat. Selected: <b>{count}</b>",
        "notify_books_send_btn": "✅ Send reminders",
        "notify_books_post_chat_btn": "✅ Post to chat",
        "notify_no_books_selected": "⚠️ Select at least one {sg}.",
        "book_hidden": "✅ <b>{title}</b> is now hidden.",
        "book_unhidden": "✅ <b>{title}</b> is now visible.",
        "choose_mark": "📌 Choose a {sg} to mark as discussed:",
        "admin_mark_menu": "📌 <b>Mark as discussed</b>\nWhat would you like to do?",
        "admin_mark_new_btn": "➕ Mark undiscussed as discussed",
        "admin_mark_edit_date_btn": "📅 Edit discussion date",
        "choose_edit_discuss_date": "📅 Choose a discussed {sg} to change its discussion date:",
        "no_discussed_to_edit_date": "📭 No discussed {pl} to edit.",
        "current_discussed_date": "Current date: <b>{date}</b>",
        "discussed_date_updated": "✅ Discussion date for <b>{title}</b> updated to {date}.",
        "no_unmark": "📭 No undiscussed {pl} to mark.",
        "ask_discuss_date": "📅 Enter the <b>discussion date</b> (YYYY-MM-DD), or /today to use today:",
        "invalid_date": "⚠️ Invalid date. Use YYYY-MM-DD format (e.g. 2026-03-17):",
        "marked_discussed": "✅ <b>{title}</b> marked as discussed on {date}.",
        "discussed_title": "✅ <b>Discussed {Pl}</b>\n\n",
        "no_discussed": "📭 No {pl} have been discussed yet.",
        "discussed_on": "Discussed on",
        "list_prompt": "📋 <b>List of {Pl}</b>\nShow all {pl} or only those you haven't voted for yet?",
        "list_all_btn": "{list_icon} All {pl}",
        "list_unvoted_btn": "🗳 Unvoted only",
        "list_format_prompt": "📋 How would you like to view the list?",
        "list_compact_btn": "📄 Compact list",
        "list_full_btn": "📖 Full cards (vote inline)",
        "list_compact_title": "📋 <b>{Pl}</b> ({count})\n",
        "score_calc_btn": "📊 How a score is calculated",
        "score_calc_info": "✅ Want: +1 point\n😐 Don't care: +0.5 points\n❌ Don't want: -1 point\nTotal score = sum of all votes (not average).Sorted by this score, then by date added.",
        "score_calc_info_attendance": "✅ +1  😐 +0.5  ❌ −1 (sum, not average).\nAttendance: visit +1, miss −1, surplus never below 0. Votes count only if surplus ≥ 1; coming back restores voting.",
        "settings_title": "⚙️ <b>Settings</b>",
        "settings_notify_label": "Notifications for new {pl}:",
        "settings_notify_on": "🔔 Enabled ({minutes} {min_short} delay)",
        "settings_notify_off": "🔕 Disabled",
        "settings_notify_btn": "Toggle Notifications",
        "settings_lang_btn": "🌐 {next_lang_label}",
        "notify_optin_prompt": "Would you like to receive notifications (with a {minutes_adj} delay) when others add new {pl}?",
        "notify_optin_yes": "🔔 Yes, notify me",
        "notify_optin_no": "🔕 No, thanks",
        "notify_optin_success": "✅ Settings saved!",
        "new_book_notification": "🆕 <b>{added_new}</b>\n(Note: you receive this {minutes_phrase} after it was added)\n\n",
        "new_book_delay_note": "\n\n<i>(Notifications for {this_prep} will be sent to others in {minutes_phrase})</i>",
        "not_member": "⛔ This bot is only for members of the <b>{chat}</b> chat. Please join first.",
        "bot_started": "🚀 <b>Bot is up!</b>",
        "bot_stopped": "🛑 <b>Bot is down.</b>",
        "admin_notify_confirm": "🔔 Voting reminder sent to {count} users.",
        "admin_notify_no_users": "ℹ️ No users to notify (everyone has voted or notifications disabled).",
        "vote_reminder_msg": "👋 <b>Friendly reminder!</b>\nYou haven't voted for some of our top {pl} yet. Take a look and cast your vote:\n\n",
        "last_activity_label": "Last non-admin activity",
        "never": "never",
        "admin_export_btn": "📤 Export {sg} (JSON)",
        "admin_import_btn": "📥 Import {sg} (JSON)",
        "add_ai_suggesting": "⏳ Looking up details for <b>{title}</b>…",
        "add_ai_suggesting_review": ("⏳ Looking up a review page for <b>{title}</b>…"),
        "add_ai_reading_review": (
            "⏳ Reading the review page to fill in the other fields…"
        ),
        "add_ai_suggested": (
            "✅ Suggestions ready. Review each field — tap Forward to keep it, "
            "or Edit to change it."
        ),
        "add_ai_suggest_failed": (
            "⚠️ Could not fetch suggestions. Fill in the fields yourself.\n\n"
            "{kind}\n{error}"
        ),
        "llm_err_auth": "API key / auth",
        "llm_err_rate_limit": "rate limit",
        "llm_err_timeout": "timeout",
        "llm_err_network": "network",
        "llm_err_bad_model": "unknown model",
        "llm_err_bad_request": "bad request",
        "llm_err_not_found": "not found",
        "llm_err_server": "provider server error",
        "llm_err_empty_reply": "empty model reply",
        "llm_err_unusable_json": "unusable JSON",
        "llm_err_provider_non_json": "non-JSON response",
        "llm_err_http": "HTTP error",
        "llm_err_request": "request failed",
        "add_ai_no_llm": (
            "⚠️ No LLM API key configured (LLM_API_KEY / XAI_API_KEY / "
            "OPENAI_API_KEY, or LLM_PROVIDER=cursor with CURSOR_API_KEY). "
            "Fill in the fields yourself."
        ),
        "choose_export": "📤 Choose a {sg} to export as JSON:",
        "export_done": "📤 Copy the JSON below and send it to another bot instance (Import in /adminconsole):\n\n<pre>{payload}</pre>",
        "import_prompt": "📥 Paste the {sg} <b>JSON</b> from an export (one message). Votes are not included.\n\nSend /cancel to abort.",
        "import_done": "✅ Imported <b>{title}</b> (new id: {book_id}).",
        "import_invalid": "⚠️ Invalid import data. Expected JSON from 📤 Export {sg}. Error: {error}",
        "import_entity_mismatch": "\n\n<i>Note: export was for “{exported}”, this bot uses “{local}”.</i>",
        "admin_meeting_create_btn": "📅 Record meeting attendance",
        "admin_meetings_view_btn": "👥 View meeting attendance",
        "choose_meeting_book": "📅 Choose the <b>discussed</b> {sg} to record or edit attendance:",
        "no_discussed_for_meeting": "📭 No discussed entries yet — mark one as discussed first.",
        "meeting_no_discussed_date": "⚠️ This entry has no discussion date — mark it as discussed with a date first.",
        "meeting_attendees_prompt": (
            "👥 <b>Who attended?</b> (discussion date: <b>{date}</b>)\n"
            "Tap names to toggle. Suggestions include voters and people who used the bot.\n"
            "Selected: <b>{count}</b>"
        ),
        "meeting_attendee_done_btn": "✅ Save meeting",
        "meeting_attendee_add_id_btn": "➕ Add by Telegram ID",
        "meeting_attendee_add_id_prompt": "Send the attendee's <b>numeric Telegram user ID</b> (or /cancel):",
        "meeting_attendee_invalid_id": "⚠️ Send a positive numeric Telegram user ID.",
        "meeting_attendee_added_id": "✅ Added {name}.",
        "meeting_saved": "✅ Meeting saved for <b>{title}</b> on {date} — <b>{count}</b> attendee(s).",
        "no_meetings": "📭 No meetings recorded yet.",
        "choose_meeting_view": "👥 Choose a meeting to see who attended (edit or delete):",
        "meeting_view_title": "👥 <b>{title}</b>\n📅 Meeting: {date}\n\n<b>Attendees ({count}):</b>\n",
        "meeting_view_empty": "<i>No attendees recorded.</i>",
        "meeting_attendee_line": "• {name}",
        "meeting_view_edit_btn": "✏️ Edit attendance",
        "meeting_view_delete_btn": "🗑 Delete meeting",
        "meeting_view_back_btn": "◀️ Back to list",
        "meeting_delete_confirm": (
            "🗑 Delete the meeting for <b>{title}</b> on {date}?\n"
            "Attendance for this meeting will be removed."
        ),
        "meeting_delete_yes_btn": "✅ Delete meeting",
        "meeting_deleted": "✅ Meeting deleted.",
        "bot_name": "{bot_name}",
        "card_icon": "{card_icon}",
        "subtitle_icon": "{subtitle_icon}",
        "all_voted": "You've voted on all {pl}!",
        "info_msg": (
            "🤖 <b>{bot_name}</b>\n\n"
            "📅 <b>Last update:</b> {last_commit}\n"
            "🔗 <b>Source code:</b> {github_repo}\n\n"
            "💬 Feel free to contact @antmaxi for suggestions on what to improve "
            "or if you run into issues with the bot."
        ),
    },
    "ru": {
        "welcome": (
            "{card_icon} <b>Добро пожаловать в {club_name}!</b>\n\n"
            "➕ /add — Добавить {acc}\n"
            "📋 /list_and_vote — Список {gen_pl}\n"
            "🏆 /top — Топ {gen_pl}\n"
            "⚙️ /settings — Настройки\n"
            "ℹ️ /info — О боте\n"
            "✏️ /edit — Редактировать запись\n"
            "🗑 /delete — Удалить {acc}\n"
            "✅ /discussed — Обсуждённые {pl}\n"
            "❓ /help — Показать это сообщение"
        ),
        "welcome_admin_suffix": "\n🛠 /adminconsole — Админ-панель",
        "lang_set": "🇷🇺 Язык установлен: Русский.",
        "ask_title": "{card_icon} Как называется {sg} (<b>название</b>)?",
        "add_ai_ask": ("✨ Заполнить остальные поля с <b>помощью ИИ</b> или вручную?"),
        "add_ai_yes_btn": "✨ С ИИ",
        "add_ai_no_btn": "✍️ Заполню сам",
        "add_start_ask": "Как добавить {acc}?",
        "add_start_new_btn": "➕ Новая",
        "add_continue_btn": "📋 Продолжить сохранённый черновик",
        "add_drafts_ask": "📋 Сохранённые черновики — выберите, чтобы продолжить:",
        "add_draft_untitled": "(без названия)",
        "add_save_btn": "💾 Сохранить",
        "add_saved": "Прогресс сохранён. Позже /add — чтобы продолжить этот черновик.",
        "add_save_need_title": "Сначала введите название.",
        "add_save_too_many": (
            "Уже есть 20 сохранённых черновиков. Сначала удалите один."
        ),
        "add_drafts_empty": "Нет сохранённых черновиков.",
        "add_draft_missing": "Этот черновик уже удалён.",
        "add_draft_deleted": "Черновик удалён.",
        "add_back_btn": "⬅️ Назад",
        "add_forward_btn": "➡️ Вперёд",
        "add_edit_btn": "✏️ Изменить",
        "add_edit_placeholder": "Измените значение",
        "add_edit_prompt": ("✏️ Измените значение и отправьте:\n\n<pre>{value}</pre>"),
        "add_edit_need_value": "На этом шаге нет значения для правки.",
        "add_edit_inline_hint": "Нажмите, чтобы отправить изменённое значение",
        "add_back_at_start": "Вы уже на первом шаге.",
        "add_forward_at_end": "Это последний шаг — отправьте описание или /skip.",
        "add_forward_need_value": "На этом шаге ещё нет сохранённого ответа. Сначала заполните его.",
        "add_back_hint": "<i>Нажмите «Назад» или отправьте /back, чтобы изменить предыдущий ответ.</i>",
        "add_forward_hint": (
            "<i>Нажмите «Вперёд» или отправьте /forward, чтобы оставить этот ответ, "
            "или отправьте новое значение, чтобы заменить его. «Изменить» вставит "
            "его в поле ввода.</i>"
        ),
        "add_nav_hint": (
            "<i>Нажмите «Вперёд», чтобы оставить этот ответ, или отправьте новое "
            "значение, чтобы заменить его. «Изменить» вставит его в поле ввода.</i>"
        ),
        "add_current_value": "Сейчас: <i>{value}</i>",
        "add_suggested_value": "Предложение: <i>{value}</i>",
        "add_suggested_hint": (
            "<i>Нажмите «Вперёд», чтобы оставить это предложение, или отправьте "
            "новое значение, чтобы заменить его. «Изменить» вставит его в поле "
            "ввода.</i>"
        ),
        "add_confirm_prompt": (
            "📋 <b>Проверьте запись</b> перед добавлением. Вернитесь назад, "
            "чтобы изменить поле, или добавьте её:"
        ),
        "add_confirm_btn": "✅ Добавить",
        "ask_author": "{subtitle_icon} Кто <b>{author}</b>?",
        "ask_pages": "📄 Сколько <b>страниц</b> в книге? (введите число)",
        "invalid_pages": "⚠️ Введите корректное число страниц (например, 320):",
        "ask_fiction": "📂 Это <b>художественная</b> или <b>нехудожественная</b> литература?",
        "fiction_btn": "📖 Худ. литература",
        "nonfiction_btn": "📰 Нехуд. литература",
        "ask_review": "🔗 Вставьте <b>ссылку на рецензию</b> (должна начинаться с http:// или https://):",
        "invalid_review": "⚠️ Это не похоже на корректный URL. Вставьте ссылку, начинающуюся с http:// или https://:",
        "ask_original_language": "🌐 <b>Язык оригинала</b> — выберите:",
        "ask_original_language_other": "🌐 <b>Язык оригинала</b> — введите название:",
        "orig_lang_ru": "🇷🇺 Русский",
        "orig_lang_de": "🇩🇪 Немецкий",
        "orig_lang_en": "🇬🇧 Английский",
        "orig_lang_it": "🇮🇹 Итальянский",
        "orig_lang_fr": "🇫🇷 Французский",
        "orig_lang_es": "🇪🇸 Испанский",
        "orig_lang_zh": "🇨🇳 Китайский",
        "orig_lang_ja": "🇯🇵 Японский",
        "orig_lang_other_btn": "✏️ Другой",
        "orig_lang_skip_btn": "⏭ Пропустить",
        "ask_creation_year": "📅 <b>Год создания</b> (год издания; 4 цифры, или /skip, если не знаете):",
        "ask_language_level": (
            "🎓 <b>Уровень языка</b> для комфортного {verb_gerund} (CEFR A1–C2). "
            "Нажимайте, чтобы отметить. Выбрано: <b>{count}</b>"
        ),
        "language_level_done_btn": "✅ Готово",
        "language_level_none_selected": "Выберите хотя бы один уровень (A1–C2).",
        "invalid_creation_year": "⚠️ Введите корректный год из 4 цифр (например, 1984) или /skip:",
        "ask_desc": "📝 Добавьте <b>описание</b> (или /skip, чтобы пропустить):",
        "book_added": "✅ {added}",
        "similar_title_warning": (
            "⚠️ В списке уже есть {sg} с <b>похожим названием</b>:\n{matches}\n\n"
            "Всё равно добавить <b>{title}</b>?"
        ),
        "similar_title_confirm_btn": "✅ Да, продолжить",
        "similar_title_cancel_btn": "❌ Нет, отмена",
        "no_books": "📭 Пока нет {gen_pl}. Используйте /add, чтобы добавить!",
        "no_undiscussed": "📭 Необсуждённых {gen_pl} нет — используйте /discussed для просмотра архива.",
        "no_votes": "Голосов пока нет. Используйте /list_and_vote для голосования!",
        "no_books_edit": "📭 Нет {gen_pl} для редактирования.",
        "no_books_delete": "📭 Нет {gen_pl} для удаления.",
        "cancelled": "❌ Отменено.",
        "unexpected_error": "⚠️ Что-то пошло не так с моей стороны. Попробуйте ещё раз — если вы были в середине команды, сначала используйте /cancel.",
        "choose_vote": "📊 Выберите {acc} для голосования:",
        "choose_edit": "✏️ Выберите {acc} для редактирования:",
        "choose_delete": "🗑 Выберите {acc} для удаления:",
        "your_vote": "Ваш текущий голос",
        "none_vote": "—",
        "rate_book": "📊 Голосование: <b>{title}</b>",
        "desc_updated": "✅ Описание обновлено!",
        "top_title": "🏆 <b>Топ {gen_pl}</b>\nСортировка по общему баллу.\n",
        "added_by": "Добавил",
        "added_on": "Добавлено",
        "pages_label": "Страниц",
        "original_language_label": "Язык оригинала",
        "creation_year_label": "Год",
        "language_levels_label": "Уровень языка",
        "review_label": "Рецензия",
        "cancel_btn": "❌ Отмена",
        "edit_field_prompt": "✏️ <b>{field}</b>\nТекущее значение: <i>{value}</i>\n\nИзменить это поле?",
        "edit_yes_btn": "✏️ Да, изменить",
        "edit_no_btn": "⏭ Пропустить",
        "edit_ask_new": "Отправьте новое значение для <b>{field}</b>:",
        "edit_done": "✅ {updated}",
        "edit_invalid_pages": "⚠️ Должно быть положительным числом. Отправьте снова:",
        "edit_invalid_url": "⚠️ Должна начинаться с http:// или https://. Отправьте снова:",
        "edit_invalid_creation_year": "⚠️ Должен быть год из 4 цифр (например, 1984). Отправьте снова:",
        "field_title": "Название",
        "field_author": "{Author}",
        "field_pages": "Страниц",
        "field_fiction": "Fiction / Non-fiction",
        "field_review": "Ссылка на рецензию",
        "field_description": "Описание",
        "field_original_language": "Язык оригинала",
        "field_creation_year": "Год создания",
        "field_language_levels": "Уровень языка (CEFR)",
        "deleted": "🗑 <b>{title}</b> {deleted_pp}.",
        "fiction_label": "Fiction",
        "nonfiction_label": "Non-fiction",
        "votes_label": lambda n, **_: (
            f"({n} оценка)"
            if n == 1
            else f"({n} оценки)" if 2 <= n <= 4 else f"({n} оценок)"
        ),
        "want_label": "✅ хочу {verb}",
        "meh_label": "😐 всё равно",
        "no_label": "❌ не хочу {verb}",
        "vote_registered": "Ваш голос: {label}",
        "voting_closed": "Голосование за эту запись закрыто.",
        "want_btn": "✅ Хочу",
        "meh_btn": "😐 Всё равно",
        "no_btn": "❌ Не хочу",
        "voted_msg": "✅ Голос сохранён для <b>{title}</b>",
        "score_label": "Балл",
        "no_permission": "⛔ Вы можете редактировать или удалять только добавленные вами {pl}.",
        "no_own_books": "📭 У вас нет {gen_pl} для редактирования или удаления.",
        "admin_only": "⛔ Эта команда доступна только администраторам.",
        "admin_console_title": "🛠 <b>Админ-панель</b>",
        "admin_mark_btn": "📌 Отметить обсуждённой",
        "admin_hide_btn": "👻 Скрыть {acc}",
        "admin_notify_btn": "🔔 Напомнить о голосовании (топ)",
        "admin_notify_one_btn": "🔔 Напомнить (выбрать {pl})",
        "admin_notify_chat_btn": "💬 Напомнить в чате (топ)",
        "admin_notify_chat_one_btn": "💬 Напомнить в чате (выбрать {pl})",
        "choose_notify_chat": "💬 Выберите {acc} для напоминания о голосовании в общем чате:",
        "vote_reminder_chat": "🔔 <b>Напоминание о голосовании!</b>\n\n",
        "admin_notify_chat_confirm": "💬 Напоминание о голосовании отправлено в общий чат ({count} {gen_pl}).",
        "admin_notify_chat_no_chat": "ℹ️ Общий чат не настроен (ALLOWED_CHAT_ID).",
        "admin_notify_chat_failed": "⚠️ Не удалось отправить сообщение в общий чат.",
        "admin_toggle_chat_btn": "💬 Писать в чат: {state}",
        "admin_toggle_votes_btn": "📊 Подсчёт голосов: {state}",
        "admin_votes_mode_all": "все",
        "admin_votes_mode_attendance": "посещаемость (нетто ≥ 1)",
        "admin_unhide_btn": "👁 Показать {acc}",
        "choose_hide": "👻 Выберите {acc}, чтобы скрыть из списка:",
        "choose_unhide": "👁 Выберите {hidden_acc}, чтобы снова показать в списке:",
        "no_hidden": "📭 Нет скрытых {gen_pl}.",
        "choose_notify": "🔔 Выберите {acc} для напоминания:",
        "choose_notify_books": "🔔 Нажимайте на {pl} для напоминания. Выбрано: <b>{count}</b>",
        "choose_notify_chat_books": "💬 Нажимайте на {pl} для публикации в чате. Выбрано: <b>{count}</b>",
        "notify_books_send_btn": "✅ Отправить напоминания",
        "notify_books_post_chat_btn": "✅ Опубликовать в чате",
        "notify_no_books_selected": "⚠️ Выберите хотя бы {one_acc}.",
        "book_hidden": "✅ <b>{title}</b> {hidden_pp}.",
        "book_unhidden": "✅ <b>{title}</b> снова {visible_pp}.",
        "choose_mark": "📌 Выберите {discussed_acc} для отметки:",
        "admin_mark_menu": "📌 <b>Отметить обсуждение</b>\nЧто сделать?",
        "admin_mark_new_btn": "➕ Отметить {undiscussed_acc}",
        "admin_mark_edit_date_btn": "📅 Изменить дату обсуждения",
        "choose_edit_discuss_date": "📅 Выберите {discussed_acc} для изменения даты:",
        "no_discussed_to_edit_date": "📭 Нет обсуждённых {gen_pl} для редактирования даты.",
        "current_discussed_date": "Текущая дата: <b>{date}</b>",
        "discussed_date_updated": "✅ Дата обсуждения для <b>{title}</b> изменена на {date}.",
        "no_unmark": "📭 Нет необсуждённых {gen_pl} для отметки.",
        "ask_discuss_date": "📅 Введите <b>дату обсуждения</b> (ГГГГ-ММ-ДД) или /today для сегодняшней даты:",
        "invalid_date": "⚠️ Неверный формат даты. Используйте ГГГГ-ММ-ДД (например, 2026-03-17):",
        "marked_discussed": "✅ <b>{title}</b> {marked_pp} ({date}).",
        "discussed_title": "✅ <b>Обсуждённые {pl}</b>\n\n",
        "no_discussed": "📭 Пока {none_discussed}.",
        "discussed_on": "Обсуждено",
        "list_prompt": "📋 <b>Список {gen_pl}</b>\nПоказать все {pl} или только те, за которые вы ещё не голосовали?",
        "list_all_btn": "{list_icon} Все {pl}",
        "list_unvoted_btn": "🗳 Только без моего голоса",
        "list_format_prompt": "📋 Как показать список?",
        "list_compact_btn": "📄 Краткий список",
        "list_full_btn": "📖 Полные карточки (голосование)",
        "list_compact_title": "📋 <b>{Pl}</b> ({count})\n",
        "score_calc_btn": "📊 Как рассчитывается балл",
        "score_calc_info": "✅ Хочу: +1 балл\n😐 Всё равно: +0.5 баллов\n❌ Не хочу: -1 балл\n\nСортировка по суммарному баллу, затем по дате добавления.",
        "score_calc_info_attendance": "✅ +1  😐 +0.5  ❌ −1 (сумма, не среднее).\nПосещаемость: визит +1, пропуск −1, не ниже 0. Голос учитывается при запасе ≥ 1; возвращение восстанавливает голосование.",
        "settings_title": "⚙️ <b>Настройки</b>",
        "settings_notify_label": "Уведомления о новых {prep_pl}:",
        "settings_notify_on": "🔔 Включены (задержка {minutes} {min_short})",
        "settings_notify_off": "🔕 Выключены",
        "settings_notify_btn": "Переключить уведомления",
        "settings_lang_btn": "🌐 {next_lang_label}",
        "notify_optin_prompt": "Хотите получать уведомления (с задержкой {minutes_phrase}), когда другие добавляют новые {pl}?",
        "notify_optin_yes": "🔔 Да, уведомлять",
        "notify_optin_no": "🔕 Нет, спасибо",
        "notify_optin_success": "✅ Настройки сохранены!",
        "new_book_notification": "🆕 <b>{added_new}</b>\n(Примечание: вы получили это через {minutes_phrase} после добавления)\n\n",
        "new_book_delay_note": "\n\n<i>(Уведомления об {this_prep} будут разосланы остальным через {minutes_phrase})</i>",
        "not_member": "⛔ Этот бот только для участников чата <b>{chat}</b>. Пожалуйста, сначала вступите в него.",
        "bot_started": "🚀 <b>Бот запущен!</b>",
        "bot_stopped": "🛑 <b>Бот остановлен.</b>",
        "admin_notify_confirm": "🔔 Напоминание о голосовании отправлено {count} пользователям.",
        "admin_notify_no_users": "ℹ️ Нет пользователей для уведомления (все проголосовали или уведомления отключены).",
        "vote_reminder_msg": "👋 <b>Напоминание!</b>\nВы еще не проголосовали за некоторые популярные {pl}. Посмотрите и оставьте свой голос:\n\n",
        "last_activity_label": "Последняя активность (не админ)",
        "never": "никогда",
        "admin_export_btn": "📤 Экспорт {gen_sg} (JSON)",
        "admin_import_btn": "📥 Импорт {gen_sg} (JSON)",
        "add_ai_suggesting": "⏳ Ищу данные для <b>{title}</b>…",
        "add_ai_suggesting_review": "⏳ Ищу страницу рецензии для <b>{title}</b>…",
        "add_ai_reading_review": (
            "⏳ Читаю страницу рецензии, чтобы заполнить остальные поля…"
        ),
        "add_ai_suggested": (
            "✅ Подсказки готовы. Проверьте каждое поле — «Вперёд», чтобы оставить, "
            "или «Изменить», чтобы поправить."
        ),
        "add_ai_suggest_failed": (
            "⚠️ Не удалось получить подсказки. Заполните поля вручную.\n\n"
            "{kind}\n{error}"
        ),
        "llm_err_auth": "ключ API / авторизация",
        "llm_err_rate_limit": "лимит запросов",
        "llm_err_timeout": "таймаут",
        "llm_err_network": "сеть",
        "llm_err_bad_model": "неизвестная модель",
        "llm_err_bad_request": "некорректный запрос",
        "llm_err_not_found": "не найдено",
        "llm_err_server": "ошибка сервера провайдера",
        "llm_err_empty_reply": "пустой ответ модели",
        "llm_err_unusable_json": "непригодный JSON",
        "llm_err_provider_non_json": "ответ не JSON",
        "llm_err_http": "ошибка HTTP",
        "llm_err_request": "сбой запроса",
        "add_ai_no_llm": (
            "⚠️ Не задан ключ LLM (LLM_API_KEY / XAI_API_KEY / OPENAI_API_KEY "
            "или LLM_PROVIDER=cursor и CURSOR_API_KEY). "
            "Заполните поля вручную."
        ),
        "choose_export": "📤 Выберите {acc} для экспорта в JSON:",
        "export_done": "📤 Скопируйте JSON и отправьте на другой инстанс бота (Импорт в /adminconsole):\n\n<pre>{payload}</pre>",
        "import_prompt": "📥 Вставьте <b>JSON</b> {gen_sg} из экспорта (одним сообщением). Голоса не переносятся.\n\n/cancel — отмена.",
        "import_done": "✅ Импортировано: <b>{title}</b> (новый id: {book_id}).",
        "import_invalid": "⚠️ Неверные данные. Нужен JSON из 📤 Экспорт {gen_sg}. Ошибка: {error}",
        "import_entity_mismatch": "\n\n<i>Экспорт для «{exported}», этот бот — «{local}».</i>",
        "admin_meeting_create_btn": "📅 Записать посещение встречи",
        "admin_meetings_view_btn": "👥 Кто был на встречах",
        "choose_meeting_book": "📅 Выберите <b>обсуждённую</b> запись, чтобы записать или изменить посещаемость:",
        "no_discussed_for_meeting": "📭 Пока нет обсуждённых записей — сначала отметьте обсуждение.",
        "meeting_no_discussed_date": "⚠️ У записи нет даты обсуждения — сначала отметьте обсуждение с датой.",
        "meeting_attendees_prompt": (
            "👥 <b>Кто присутствовал?</b> (дата обсуждения: <b>{date}</b>)\n"
            "Нажимайте на имена, чтобы отметить. В списке — голосовавшие и пользовавшиеся ботом.\n"
            "Выбрано: <b>{count}</b>"
        ),
        "meeting_attendee_done_btn": "✅ Сохранить встречу",
        "meeting_attendee_add_id_btn": "➕ Добавить по Telegram ID",
        "meeting_attendee_add_id_prompt": "Отправьте <b>числовой Telegram user ID</b> участника (или /cancel):",
        "meeting_attendee_invalid_id": "⚠️ Нужен положительный числовой Telegram user ID.",
        "meeting_attendee_added_id": "✅ Добавлен(а) {name}.",
        "meeting_saved": "✅ Встреча сохранена: <b>{title}</b>, {date} — <b>{count}</b> участник(ов).",
        "no_meetings": "📭 Встречи ещё не записывались.",
        "choose_meeting_view": "👥 Выберите встречу, чтобы увидеть участников (изменить или удалить):",
        "meeting_view_title": "👥 <b>{title}</b>\n📅 Встреча: {date}\n\n<b>Участники ({count}):</b>\n",
        "meeting_view_empty": "<i>Участники не указаны.</i>",
        "meeting_attendee_line": "• {name}",
        "meeting_view_edit_btn": "✏️ Изменить посещаемость",
        "meeting_view_delete_btn": "🗑 Удалить встречу",
        "meeting_view_back_btn": "◀️ К списку",
        "meeting_delete_confirm": (
            "🗑 Удалить встречу для <b>{title}</b> ({date})?\n"
            "Запись посещаемости этой встречи будет удалена."
        ),
        "meeting_delete_yes_btn": "✅ Удалить встречу",
        "meeting_deleted": "✅ Встреча удалена.",
        "bot_name": "{bot_name}",
        "card_icon": "{card_icon}",
        "subtitle_icon": "{subtitle_icon}",
        "all_voted": "Вы проголосовали за все {pl}!",
        "info_msg": (
            "🤖 <b>{bot_name}</b>\n\n"
            "📅 <b>Последнее обновление:</b> {last_commit}\n"
            "🔗 <b>Исходный код:</b> {github_repo}\n\n"
            "💬 Пишите @antmaxi с предложениями по улучшению бота или если что-то "
            "не работает."
        ),
    },
    "de": {
        "welcome": (
            "{card_icon} <b>Willkommen beim {club_name}!</b>\n\n"
            "➕ /add — {acc} hinzufügen\n"
            "📋 /list_and_vote — Alle {pl} anzeigen\n"
            "🏆 /top — Bestbewertete {pl}\n"
            "⚙️ /settings — Einstellungen\n"
            "ℹ️ /info — Über den Bot\n"
            "✏️ /edit — Eintrag bearbeiten\n"
            "🗑 /delete — {acc} löschen\n"
            "✅ /discussed — Bereits diskutierte {pl}\n"
            "❓ /help — Diese Nachricht anzeigen"
        ),
        "welcome_admin_suffix": "\n🛠 /adminconsole — Admin-Konsole",
        "lang_set": "🇩🇪 Sprache auf Deutsch gestellt.",
        "ask_title": "{card_icon} Wie lautet der <b>Titel</b> des {gen_sg}?",
        "add_ai_ask": (
            "✨ Die übrigen Felder mit <b>KI-Hilfe</b> ausfüllen oder selbst eingeben?"
        ),
        "add_ai_yes_btn": "✨ KI nutzen",
        "add_ai_no_btn": "✍️ Selbst ausfüllen",
        "add_start_ask": "Wie möchtest du {acc} hinzufügen?",
        "add_start_new_btn": "➕ Neu",
        "add_continue_btn": "📋 Gespeicherten Entwurf fortsetzen",
        "add_drafts_ask": "📋 Gespeicherte Entwürfe — wähle einen zum Fortsetzen:",
        "add_draft_untitled": "(kein Titel)",
        "add_save_btn": "💾 Speichern",
        "add_saved": "Fortschritt gespeichert. Mit /add kannst du diesen Entwurf später fortsetzen.",
        "add_save_need_title": "Bitte zuerst einen Titel eingeben.",
        "add_save_too_many": (
            "Du hast bereits 20 gespeicherte Entwürfe. Bitte zuerst einen löschen."
        ),
        "add_drafts_empty": "Keine gespeicherten Entwürfe.",
        "add_draft_missing": "Dieser Entwurf ist weg.",
        "add_draft_deleted": "Entwurf gelöscht.",
        "add_back_btn": "⬅️ Zurück",
        "add_forward_btn": "➡️ Weiter",
        "add_edit_btn": "✏️ Bearbeiten",
        "add_edit_placeholder": "Aktuellen Wert bearbeiten",
        "add_edit_prompt": (
            "✏️ Bearbeite den Wert und sende ihn:\n\n<pre>{value}</pre>"
        ),
        "add_edit_need_value": "Auf diesem Schritt gibt es keinen Wert zum Bearbeiten.",
        "add_edit_inline_hint": "Tippen, um den geänderten Wert zu senden",
        "add_back_at_start": "Du bist bereits beim ersten Schritt.",
        "add_forward_at_end": "Das ist der letzte Schritt — sende eine Beschreibung oder /skip.",
        "add_forward_need_value": "Dieser Schritt hat noch keine gespeicherte Antwort. Bitte zuerst ausfüllen.",
        "add_back_hint": "<i>Tippe auf Zurück oder sende /back, um eine vorherige Antwort zu ändern.</i>",
        "add_forward_hint": (
            "<i>Tippe auf Weiter oder sende /forward, um diese Antwort zu behalten, "
            "oder sende einen neuen Wert, um sie zu ersetzen. Bearbeiten legt ihn "
            "ins Eingabefeld.</i>"
        ),
        "add_nav_hint": (
            "<i>Tippe auf Weiter, um diese Antwort zu behalten, oder sende einen "
            "neuen Wert, um sie zu ersetzen. Bearbeiten legt ihn ins "
            "Eingabefeld.</i>"
        ),
        "add_current_value": "Aktuell: <i>{value}</i>",
        "add_suggested_value": "Vorschlag: <i>{value}</i>",
        "add_suggested_hint": (
            "<i>Tippe auf Weiter, um diesen Vorschlag zu behalten, oder sende "
            "einen neuen Wert, um ihn zu ersetzen. Bearbeiten legt ihn ins "
            "Eingabefeld.</i>"
        ),
        "add_confirm_prompt": (
            "📋 <b>Eintrag prüfen</b>, bevor er hinzugefügt wird. Zurück, um ein "
            "Feld zu ändern, oder jetzt hinzufügen:"
        ),
        "add_confirm_btn": "✅ Hinzufügen",
        "ask_author": "{subtitle_icon} Wer ist der <b>{author}</b>?",
        "ask_pages": "📄 Wie viele <b>Seiten</b> hat es? (Zahl eingeben)",
        "invalid_pages": "⚠️ Bitte eine gültige Seitenzahl eingeben (z. B. 320):",
        "ask_fiction": "📂 Ist es <b>Belletristik</b> oder ein <b>Sachbuch</b>?",
        "fiction_btn": "📖 Belletristik",
        "nonfiction_btn": "📰 Sachbuch",
        "ask_review": "🔗 Füge den <b>Link zur Rezension</b> ein (muss mit http:// oder https:// beginnen):",
        "invalid_review": "⚠️ Das sieht nicht nach einer gültigen URL aus. Bitte einen Link einfügen, der mit http:// oder https:// beginnt:",
        "ask_original_language": "🌐 <b>Originalsprache</b> — eine wählen:",
        "ask_original_language_other": "🌐 <b>Originalsprache</b> — Name eingeben:",
        "orig_lang_ru": "🇷🇺 Russisch",
        "orig_lang_de": "🇩🇪 Deutsch",
        "orig_lang_en": "🇬🇧 Englisch",
        "orig_lang_it": "🇮🇹 Italienisch",
        "orig_lang_fr": "🇫🇷 Französisch",
        "orig_lang_es": "🇪🇸 Spanisch",
        "orig_lang_zh": "🇨🇳 Chinesisch",
        "orig_lang_ja": "🇯🇵 Japanisch",
        "orig_lang_other_btn": "✏️ Andere",
        "orig_lang_skip_btn": "⏭ Überspringen",
        "ask_creation_year": "📅 <b>Entstehungsjahr</b> (Erscheinungsjahr; 4 Ziffern, oder /skip wenn unbekannt):",
        "ask_language_level": (
            "🎓 Geschätztes <b>Sprachniveau</b> zum {verb_gerund} des {gen_sg} (GER A1–C2). "
            "Tippen zum Umschalten. Ausgewählt: <b>{count}</b>"
        ),
        "language_level_done_btn": "✅ Fertig",
        "language_level_none_selected": "Bitte mindestens ein Niveau wählen (A1–C2).",
        "invalid_creation_year": "⚠️ Bitte ein gültiges 4-stelliges Jahr eingeben (z. B. 1984) oder /skip:",
        "ask_desc": "📝 Füge eine <b>Beschreibung</b> hinzu (oder /skip, um leer zu lassen):",
        "book_added": "✅ {added}",
        "similar_title_warning": (
            "⚠️ Ein {sg} mit <b>ähnlichem Titel</b> ist bereits in der Liste:\n{matches}\n\n"
            "<b>{title}</b> trotzdem hinzufügen?"
        ),
        "similar_title_confirm_btn": "✅ Ja, weiter",
        "similar_title_cancel_btn": "❌ Nein, abbrechen",
        "no_books": "📭 Noch keine {pl}. Mit /add eines hinzufügen!",
        "no_undiscussed": "📭 Keine undiskutierten {pl} — mit /discussed vergangene {pl} anzeigen.",
        "no_votes": "Noch keine Stimmen. Mit /list_and_vote {pl} anzeigen und abstimmen!",
        "no_books_edit": "📭 Noch keine {pl} zum Bearbeiten.",
        "no_books_delete": "📭 Noch keine {pl} zum Löschen.",
        "cancelled": "❌ Abgebrochen.",
        "unexpected_error": "⚠️ Auf meiner Seite ist etwas schiefgelaufen. Bitte erneut versuchen — zuerst /cancel, falls du mitten in einer Aktion warst.",
        "choose_vote": "📊 Wähle {acc} zum Abstimmen:",
        "choose_edit": "✏️ Wähle {acc} zum Bearbeiten:",
        "choose_delete": "🗑 Wähle {acc} zum Löschen:",
        "your_vote": "Deine aktuelle Stimme",
        "none_vote": "—",
        "rate_book": "📊 Abstimmung: <b>{title}</b>",
        "desc_updated": "✅ Beschreibung aktualisiert!",
        "top_title": "🏆 <b>Top-{Pl}</b>\nSortiert nach Gesamtpunktzahl.\n",
        "added_by": "Hinzugefügt von",
        "added_on": "Hinzugefügt am",
        "pages_label": "Seiten",
        "original_language_label": "Originalsprache",
        "creation_year_label": "Jahr",
        "language_levels_label": "Sprachniveau",
        "review_label": "Rezension",
        "cancel_btn": "❌ Abbrechen",
        "edit_field_prompt": "✏️ <b>{field}</b>\nAktueller Wert: <i>{value}</i>\n\nDieses Feld ändern?",
        "edit_yes_btn": "✏️ Ja, ändern",
        "edit_no_btn": "⏭ Überspringen",
        "edit_ask_new": "Sende den neuen Wert für <b>{field}</b>:",
        "edit_done": "✅ {updated}",
        "edit_invalid_pages": "⚠️ Muss eine positive Zahl sein. Nochmal senden:",
        "edit_invalid_url": "⚠️ Muss mit http:// oder https:// beginnen. Nochmal senden:",
        "edit_invalid_creation_year": "⚠️ Muss ein 4-stelliges Jahr sein (z. B. 1984). Nochmal senden:",
        "field_title": "Titel",
        "field_author": "{Author}",
        "field_pages": "Seiten",
        "field_fiction": "Belletristik / Sachbuch",
        "field_review": "Rezensionslink",
        "field_description": "Beschreibung",
        "field_original_language": "Originalsprache",
        "field_creation_year": "Entstehungsjahr",
        "field_language_levels": "Sprachniveau (GER)",
        "deleted": "🗑 <b>{title}</b> wurde gelöscht.",
        "fiction_label": "Belletristik",
        "nonfiction_label": "Sachbuch",
        "votes_label": lambda n, **_: f"({n} Stimme)" if n == 1 else f"({n} Stimmen)",
        "want_label": "✅ möchte {verb}",
        "meh_label": "😐 egal",
        "no_label": "❌ möchte nicht {verb}",
        "vote_registered": "Deine Stimme: {label}",
        "voting_closed": "Die Abstimmung für diesen Eintrag ist geschlossen.",
        "want_btn": "✅ Will",
        "meh_btn": "😐 Egal",
        "no_btn": "❌ Will nicht",
        "voted_msg": "✅ Stimme gespeichert für <b>{title}</b>",
        "score_label": "Punktzahl",
        "no_permission": "⛔ Du kannst nur {pl} bearbeiten oder löschen, die du hinzugefügt hast.",
        "no_own_books": "📭 Du hast keine {pl} zum Bearbeiten oder Löschen.",
        "admin_only": "⛔ Dieser Befehl ist nur für Admins.",
        "admin_console_title": "🛠 <b>Admin-Konsole</b>",
        "admin_mark_btn": "📌 Als diskutiert markieren",
        "admin_hide_btn": "👻 {acc} verbergen",
        "admin_notify_btn": "🔔 Abstimmungserinnerung senden (Top)",
        "admin_notify_one_btn": "🔔 Erinnerung senden ({pl} wählen)",
        "admin_notify_chat_btn": "💬 Erinnerung in den Chat (Top)",
        "admin_notify_chat_one_btn": "💬 Erinnerung in den Chat ({pl} wählen)",
        "choose_notify_chat": "💬 Wähle {acc} für eine Abstimmungserinnerung im Gruppenchat:",
        "vote_reminder_chat": "🔔 <b>Abstimmungserinnerung!</b>\n\n",
        "admin_notify_chat_confirm": (
            lambda count, sg, pl, **_: (
                f"💬 Abstimmungserinnerung im Gruppenchat veröffentlicht ({count} {sg if count == 1 else pl})."
            )
        ),
        "admin_notify_chat_no_chat": "ℹ️ Gruppenchat ist nicht konfiguriert (ALLOWED_CHAT_ID).",
        "admin_notify_chat_failed": "⚠️ Beitrag im Gruppenchat fehlgeschlagen.",
        "admin_toggle_chat_btn": "💬 In den Chat posten: {state}",
        "admin_toggle_votes_btn": "📊 Stimmenzählung: {state}",
        "admin_votes_mode_all": "alle Stimmen",
        "admin_votes_mode_attendance": "Anwesenheit (Saldo ≥ 1)",
        "admin_unhide_btn": "👁 {acc} anzeigen",
        "choose_hide": "👻 Wähle {acc} zum Verbergen aus der Liste:",
        "choose_unhide": "👁 Wähle {hidden_acc} zum erneuten Anzeigen in der Liste:",
        "no_hidden": "📭 Keine verborgenen {pl}.",
        "choose_notify": "🔔 Wähle {acc} für eine Erinnerung:",
        "choose_notify_books": "🔔 Tippe auf {pl} für die Erinnerung. Ausgewählt: <b>{count}</b>",
        "choose_notify_chat_books": "💬 Tippe auf {pl} für den Gruppenchat. Ausgewählt: <b>{count}</b>",
        "notify_books_send_btn": "✅ Erinnerungen senden",
        "notify_books_post_chat_btn": "✅ In den Chat posten",
        "notify_no_books_selected": "⚠️ Wähle mindestens {one_acc}.",
        "book_hidden": "✅ <b>{title}</b> ist jetzt verborgen.",
        "book_unhidden": "✅ <b>{title}</b> ist wieder sichtbar.",
        "choose_mark": "📌 Wähle {acc} zum Markieren als diskutiert:",
        "admin_mark_menu": "📌 <b>Als diskutiert markieren</b>\nWas möchtest du tun?",
        "admin_mark_new_btn": "➕ {undiscussed_acc} markieren",
        "admin_mark_edit_date_btn": "📅 Diskussionsdatum ändern",
        "choose_edit_discuss_date": "📅 Wähle {discussed_acc}, um das Datum zu ändern:",
        "no_discussed_to_edit_date": "📭 Keine diskutierten {pl} zum Ändern des Datums.",
        "current_discussed_date": "Aktuelles Datum: <b>{date}</b>",
        "discussed_date_updated": "✅ Diskussionsdatum für <b>{title}</b> auf {date} geändert.",
        "no_unmark": "📭 Keine undiskutierten {pl} zum Markieren.",
        "ask_discuss_date": "📅 Gib das <b>Diskussionsdatum</b> ein (JJJJ-MM-TT) oder /today für heute:",
        "invalid_date": "⚠️ Ungültiges Datum. Format JJJJ-MM-TT verwenden (z. B. 2026-03-17):",
        "marked_discussed": "✅ <b>{title}</b> als diskutiert markiert ({date}).",
        "discussed_title": "✅ <b>Diskutierte {pl}</b>\n\n",
        "no_discussed": "📭 Noch {none_discussed}.",
        "discussed_on": "Diskutiert am",
        "list_prompt": "📋 <b>Liste der {pl}</b>\nAlle {pl} anzeigen oder nur die, für die du noch nicht abgestimmt hast?",
        "list_all_btn": "{list_icon} Alle {pl}",
        "list_unvoted_btn": "🗳 Nur ohne meine Stimme",
        "list_format_prompt": "📋 Wie soll die Liste angezeigt werden?",
        "list_compact_btn": "📄 Kompakte Liste",
        "list_full_btn": "📖 Volle Karten (Abstimmung)",
        "list_compact_title": "📋 <b>{Pl}</b> ({count})\n",
        "score_calc_btn": "📊 So wird die Punktzahl berechnet",
        "score_calc_info": "✅ Will: +1 Punkt\n😐 Egal: +0,5 Punkte\n❌ Will nicht: −1 Punkt\nGesamtpunktzahl = Summe aller Stimmen (kein Durchschnitt). Sortiert nach dieser Punktzahl, dann nach Hinzufügedatum.",
        "score_calc_info_attendance": "✅ +1  😐 +0,5  ❌ −1 (Summe, kein Durchschnitt).\nAnwesenheit: Besuch +1, Fehlen −1, Saldo nie unter 0. Stimmen zählen nur bei Saldo ≥ 1; Rückkehr stellt das Abstimmen wieder her.",
        "settings_title": "⚙️ <b>Einstellungen</b>",
        "settings_notify_label": "Benachrichtigungen für neue {pl}:",
        "settings_notify_on": "🔔 Aktiviert ({minutes} {min_short} Verzögerung)",
        "settings_notify_off": "🔕 Deaktiviert",
        "settings_notify_btn": "Benachrichtigungen umschalten",
        "settings_lang_btn": "🌐 {next_lang_label}",
        "notify_optin_prompt": "Möchtest du Benachrichtigungen erhalten (mit {minutes_phrase} Verzögerung), wenn andere neue {pl} hinzufügen?",
        "notify_optin_yes": "🔔 Ja, benachrichtigen",
        "notify_optin_no": "🔕 Nein, danke",
        "notify_optin_success": "✅ Einstellungen gespeichert!",
        "new_book_notification": "🆕 <b>{added_new}</b>\n(Hinweis: du erhältst dies {minutes_phrase} nach dem Hinzufügen)\n\n",
        "new_book_delay_note": "\n\n<i>(Benachrichtigungen für {this_prep} werden in {minutes_phrase} an die anderen gesendet)</i>",
        "not_member": "⛔ Dieser Bot ist nur für Mitglieder des Chats <b>{chat}</b>. Bitte zuerst beitreten.",
        "bot_started": "🚀 <b>Bot ist online!</b>",
        "bot_stopped": "🛑 <b>Bot ist offline.</b>",
        "admin_notify_confirm": "🔔 Abstimmungserinnerung an {count} Nutzer gesendet.",
        "admin_notify_no_users": "ℹ️ Keine Nutzer zum Benachrichtigen (alle haben abgestimmt oder Benachrichtigungen sind aus).",
        "vote_reminder_msg": "👋 <b>Erinnerung!</b>\nDu hast noch nicht für einige unserer Top-{pl} abgestimmt. Schau vorbei und stimme ab:\n\n",
        "last_activity_label": "Letzte Aktivität (nicht Admin)",
        "never": "nie",
        "admin_export_btn": "📤 {sg} exportieren (JSON)",
        "admin_import_btn": "📥 {sg} importieren (JSON)",
        "add_ai_suggesting": "⏳ Suche Angaben zu <b>{title}</b>…",
        "add_ai_suggesting_review": (
            "⏳ Suche eine Rezensionsseite zu <b>{title}</b>…"
        ),
        "add_ai_reading_review": (
            "⏳ Lese die Rezensionsseite, um die übrigen Felder zu füllen…"
        ),
        "add_ai_suggested": (
            "✅ Vorschläge bereit. Prüfe jedes Feld — Weiter zum Behalten "
            "oder Bearbeiten zum Ändern."
        ),
        "add_ai_suggest_failed": (
            "⚠️ Vorschläge konnten nicht geladen werden. "
            "Bitte die Felder selbst ausfüllen.\n\n"
            "{kind}\n{error}"
        ),
        "llm_err_auth": "API-Schlüssel / Auth",
        "llm_err_rate_limit": "Rate-Limit",
        "llm_err_timeout": "Zeitüberschreitung",
        "llm_err_network": "Netzwerk",
        "llm_err_bad_model": "unbekanntes Modell",
        "llm_err_bad_request": "ungültige Anfrage",
        "llm_err_not_found": "nicht gefunden",
        "llm_err_server": "Anbieter-Serverfehler",
        "llm_err_empty_reply": "leere Modellantwort",
        "llm_err_unusable_json": "unbrauchbares JSON",
        "llm_err_provider_non_json": "Antwort kein JSON",
        "llm_err_http": "HTTP-Fehler",
        "llm_err_request": "Anfrage fehlgeschlagen",
        "add_ai_no_llm": (
            "⚠️ Kein LLM-API-Schlüssel gesetzt (LLM_API_KEY / XAI_API_KEY / "
            "OPENAI_API_KEY, oder LLM_PROVIDER=cursor mit CURSOR_API_KEY). "
            "Bitte die Felder selbst ausfüllen."
        ),
        "choose_export": "📤 Wähle {acc} zum Export als JSON:",
        "export_done": "📤 JSON kopieren und an eine andere Bot-Instanz senden (Import in /adminconsole):\n\n<pre>{payload}</pre>",
        "import_prompt": "📥 Füge das {sg}-<b>JSON</b> aus einem Export ein (eine Nachricht). Stimmen sind nicht enthalten.\n\n/cancel zum Abbrechen.",
        "import_done": "✅ Importiert: <b>{title}</b> (neue id: {book_id}).",
        "import_invalid": "⚠️ Ungültige Daten. Erwartet wird JSON aus 📤 {sg} exportieren. Fehler: {error}",
        "import_entity_mismatch": "\n\n<i>Export war für „{exported}“, dieser Bot nutzt „{local}“.</i>",
        "admin_meeting_create_btn": "📅 Anwesenheit bei einem Treffen erfassen",
        "admin_meetings_view_btn": "👥 Treffen-Anwesenheit anzeigen",
        "choose_meeting_book": "📅 Wähle den <b>diskutierten</b> Eintrag, um Anwesenheit zu erfassen oder zu ändern:",
        "no_discussed_for_meeting": "📭 Noch keine diskutierten Einträge — zuerst als diskutiert markieren.",
        "meeting_no_discussed_date": "⚠️ Dieser Eintrag hat kein Diskussionsdatum — zuerst mit Datum als diskutiert markieren.",
        "meeting_attendees_prompt": (
            "👥 <b>Wer war da?</b> (Diskussionsdatum: <b>{date}</b>)\n"
            "Namen antippen zum Umschalten. Vorschläge: Abstimmende und Bot-Nutzer.\n"
            "Ausgewählt: <b>{count}</b>"
        ),
        "meeting_attendee_done_btn": "✅ Treffen speichern",
        "meeting_attendee_add_id_btn": "➕ Per Telegram-ID hinzufügen",
        "meeting_attendee_add_id_prompt": "Sende die <b>numerische Telegram-User-ID</b> der Person (oder /cancel):",
        "meeting_attendee_invalid_id": "⚠️ Eine positive numerische Telegram-User-ID senden.",
        "meeting_attendee_added_id": "✅ {name} hinzugefügt.",
        "meeting_saved": "✅ Treffen gespeichert: <b>{title}</b>, {date} — <b>{count}</b> Teilnehmer.",
        "no_meetings": "📭 Es wurden noch keine Treffen erfasst.",
        "choose_meeting_view": "👥 Wähle ein Treffen, um die Teilnehmer zu sehen (bearbeiten oder löschen):",
        "meeting_view_title": "👥 <b>{title}</b>\n📅 Treffen: {date}\n\n<b>Teilnehmer ({count}):</b>\n",
        "meeting_view_empty": "<i>Keine Teilnehmer erfasst.</i>",
        "meeting_attendee_line": "• {name}",
        "meeting_view_edit_btn": "✏️ Anwesenheit bearbeiten",
        "meeting_view_delete_btn": "🗑 Treffen löschen",
        "meeting_view_back_btn": "◀️ Zurück zur Liste",
        "meeting_delete_confirm": (
            "🗑 Treffen für <b>{title}</b> am {date} löschen?\n"
            "Die Anwesenheit für dieses Treffen wird entfernt."
        ),
        "meeting_delete_yes_btn": "✅ Treffen löschen",
        "meeting_deleted": "✅ Treffen gelöscht.",
        "bot_name": "{bot_name}",
        "card_icon": "{card_icon}",
        "subtitle_icon": "{subtitle_icon}",
        "all_voted": "Du hast für alle {pl} abgestimmt!",
        "info_msg": (
            "🤖 <b>{bot_name}</b>\n\n"
            "📅 <b>Letztes Update:</b> {last_commit}\n"
            "🔗 <b>Quellcode:</b> {github_repo}\n\n"
            "💬 Schreib @antmaxi bei Verbesserungsvorschlägen oder wenn etwas "
            "am Bot nicht funktioniert."
        ),
    },
}

# Vocabulary that is not just the entity noun (runtime vs pages, feature vs fiction).
ENTITY_STRING_OVERLAYS: dict[str, dict[str, dict[str, TranslationValue]]] = {
    "film": {
        "en": {
            "ask_pages": "⏱ How long is it (<b>runtime in minutes</b>)? (enter a number)",
            "invalid_pages": "⚠️ Please enter a valid runtime in minutes (e.g. 120):",
            "ask_fiction": "📂 Is it a <b>feature film</b> or a <b>documentary</b>?",
            "ask_creation_year": "📅 <b>Release year</b> (4 digits, or /skip if unknown):",
            "fiction_btn": "🎬 Feature",
            "nonfiction_btn": "📽 Documentary",
            "pages_label": "min",
            "edit_invalid_pages": "⚠️ Must be a positive number of minutes. Send again:",
            "field_pages": "Runtime (min)",
            "field_fiction": "Feature / Documentary",
            "field_creation_year": "Release year",
            "fiction_label": "Feature",
            "nonfiction_label": "Documentary",
        },
        "ru": {
            "ask_pages": "⏱ Сколько <b>минут</b> длится фильм? (введите число)",
            "invalid_pages": "⚠️ Введите корректную длительность в минутах (например, 120):",
            "ask_fiction": "📂 Это <b>художественный фильм</b> или <b>документальный</b>?",
            "ask_creation_year": "📅 <b>Год выхода</b> (4 цифры, или /skip, если не знаете):",
            "fiction_btn": "🎬 Худ. фильм",
            "nonfiction_btn": "📽 Документальный",
            "pages_label": "мин",
            "edit_invalid_pages": "⚠️ Должно быть положительное число минут. Отправьте снова:",
            "field_pages": "Длительность (мин)",
            "field_creation_year": "Год выхода",
            "field_fiction": "Худ. / документальный",
            "fiction_label": "Худ. фильм",
            "nonfiction_label": "Документальный",
            "admin_mark_btn": "📌 Отметить обсуждённым",
        },
        "de": {
            "ask_pages": "⏱ Wie lange dauert er (<b>Laufzeit in Minuten</b>)? (Zahl eingeben)",
            "invalid_pages": "⚠️ Bitte eine gültige Laufzeit in Minuten eingeben (z. B. 120):",
            "ask_fiction": "📂 Ist es ein <b>Spielfilm</b> oder ein <b>Dokumentarfilm</b>?",
            "ask_creation_year": "📅 <b>Erscheinungsjahr</b> (4 Ziffern, oder /skip wenn unbekannt):",
            "fiction_btn": "🎬 Spielfilm",
            "nonfiction_btn": "📽 Dokumentarfilm",
            "pages_label": "Min",
            "edit_invalid_pages": "⚠️ Muss eine positive Minutenzahl sein. Nochmal senden:",
            "field_pages": "Laufzeit (Min)",
            "field_fiction": "Spielfilm / Dokumentarfilm",
            "field_creation_year": "Erscheinungsjahr",
            "fiction_label": "Spielfilm",
            "nonfiction_label": "Dokumentarfilm",
        },
    },
}

COMMAND_SPECS: dict[str, list[tuple[str, str]]] = {
    "en": [
        ("add", "➕ Add a {sg}"),
        ("list_and_vote", "📋 List {pl} & vote inline"),
        ("top", "🏆 Top rated {pl}"),
        ("settings", "⚙️ Settings"),
        ("discussed", "✅ {Pl} already discussed"),
        ("edit", "✏️ Edit a {sg} entry"),
        ("delete", "🗑 Delete a {sg}"),
        ("adminconsole", "🛠 Admin console"),
        ("cancel", "❌ Cancel current action"),
        ("help", "❓ Show help"),
        ("info", "ℹ️ About the bot"),
    ],
    "ru": [
        ("add", "➕ Добавить {acc}"),
        ("list_and_vote", "📋 Список {gen_pl} и голосование"),
        ("top", "🏆 Топ {gen_pl}"),
        ("settings", "⚙️ Настройки"),
        ("discussed", "✅ Обсуждённые {pl}"),
        ("edit", "✏️ Редактировать запись"),
        ("delete", "🗑 Удалить {acc}"),
        ("adminconsole", "🛠 Админ-панель"),
        ("cancel", "❌ Отменить действие"),
        ("help", "❓ Показать помощь"),
        ("info", "ℹ️ О боте"),
    ],
    "de": [
        ("add", "➕ {acc} hinzufügen"),
        ("list_and_vote", "📋 {Pl} auflisten & abstimmen"),
        ("top", "🏆 Top-{Pl}"),
        ("settings", "⚙️ Einstellungen"),
        ("discussed", "✅ Bereits diskutierte {Pl}"),
        ("edit", "✏️ Eintrag bearbeiten"),
        ("delete", "🗑 {acc} löschen"),
        ("adminconsole", "🛠 Admin-Konsole"),
        ("cancel", "❌ Aktuelle Aktion abbrechen"),
        ("help", "❓ Hilfe anzeigen"),
        ("info", "ℹ️ Über den Bot"),
    ],
}


def _apply_entity_string_overlays(entity: str) -> None:
    for lang, keys in ENTITY_STRING_OVERLAYS.get(entity, {}).items():
        T[lang].update(keys)


_apply_entity_string_overlays(CLUB_ENTITY)

PM = "HTML"


def get_lang(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    lang = str(ctx.user_data.get("lang", "ru"))
    if lang not in T:
        return "ru"
    return lang


def tr(ctx_or_lang: ContextTypes.DEFAULT_TYPE | str, key: str, **kwargs: Any) -> str:
    lang = ctx_or_lang if isinstance(ctx_or_lang, str) else get_lang(ctx_or_lang)
    val = T[lang][key]
    merged = {**format_defaults(lang), **kwargs}
    if callable(val):
        result = val(**merged)
        return str(result)
    return format_ui(lang, str(val), **kwargs)


def s(lang: str, key: str) -> str:
    """Plain-string translation (not callable)."""
    val = T[lang][key]
    if not isinstance(val, str):
        raise TypeError(f"translation {key!r} is not a plain string")
    return format_ui(lang, val)


_VOTE_LABEL_KEYS = {1: "want_label", 0: "meh_label", -1: "no_label"}


def vote_label_text(lang: str, score: int | None) -> str:
    if score not in _VOTE_LABEL_KEYS:
        raise ValueError(f"invalid vote score: {score!r}")
    return s(lang, _VOTE_LABEL_KEYS[score])
