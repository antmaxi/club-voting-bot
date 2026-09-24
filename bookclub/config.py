from __future__ import annotations

import os
from datetime import timedelta, timezone
from urllib.parse import urlparse

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
GITHUB_REPO = os.environ.get(
    "GITHUB_REPO", "https://github.com/antmaxi/club-voting-bot"
)
DB_PATH = os.environ.get("DB_PATH", "bookclub.db")
ACTIVITY_PATH = os.environ.get("ACTIVITY_PATH", "last_activity.json")

# Members of this chat are allowed to use the bot.
# Set via environment variable: export ALLOWED_CHAT_ID="-1001234567890"
# Leave empty to allow everyone (useful during initial setup).
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "0")) or None

# What members vote on: books (default) or films. Same DB schema; labels and prompts
# differ. More kinds (podcast, TV series, …) can be added via ENTITY_STRING_OVERLAYS.
_VALID_CLUB_ENTITIES = frozenset({"book", "film"})


def _club_entity_from_env() -> str:
    raw = os.environ.get("CLUB_ENTITY", "book").strip().lower()
    if raw not in _VALID_CLUB_ENTITIES:
        print(
            f"Warning: unknown CLUB_ENTITY={raw!r}, using 'book'. "
            f"Valid values: {', '.join(sorted(_VALID_CLUB_ENTITIES))}"
        )
        return "book"
    return raw


CLUB_ENTITY = _club_entity_from_env()


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


# When enabled, /add and /edit ask for estimated CEFR level(s) (A1–C2).
# Also implied by listing language_levels in ENTRY_FIELDS (or ENTRY_FIELDS=all).
ASK_LANGUAGE_LEVEL = _env_truthy("ASK_LANGUAGE_LEVEL")

# Optional entry properties (title is always required). Unset = all of these
# except language_levels; set ASK_LANGUAGE_LEVEL=1 or include language_levels
# (or ENTRY_FIELDS=all) to collect CEFR too.
OPTIONAL_ENTRY_FIELDS: tuple[str, ...] = (
    "author",
    "pages",
    "fiction",
    "review",
    "original_language",
    "creation_year",
    "language_levels",
    "description",
)
_ENTRY_FIELD_ALIASES = {
    "review_link": "review",
    "runtime": "pages",
}
DEFAULT_ENTRY_FIELDS = frozenset(
    name for name in OPTIONAL_ENTRY_FIELDS if name != "language_levels"
)


def canonical_entry_field(name: str) -> str:
    key = name.strip().lower().replace("-", "_")
    return _ENTRY_FIELD_ALIASES.get(key, key)


def parse_entry_fields(
    raw: str | None, *, ask_language_level: bool = False
) -> frozenset[str]:
    """Which optional fields to ask on /add and show on cards."""
    names: set[str]
    if raw is None or not raw.strip():
        names = set(DEFAULT_ENTRY_FIELDS)
    else:
        token = raw.strip().lower()
        if token in ("all", "*"):
            names = set(OPTIONAL_ENTRY_FIELDS)
        else:
            names = set()
            unknown: list[str] = []
            for part in raw.split(","):
                name = canonical_entry_field(part)
                if not name or name == "title":
                    continue
                if name in OPTIONAL_ENTRY_FIELDS:
                    names.add(name)
                else:
                    unknown.append(part.strip())
            if unknown:
                print(
                    f"Warning: unknown ENTRY_FIELDS {unknown!r}. "
                    f"Valid: {', '.join(OPTIONAL_ENTRY_FIELDS)} "
                    "(title is always included)."
                )
    if ask_language_level:
        names.add("language_levels")
    return frozenset(names)


ENTRY_FIELDS = parse_entry_fields(
    os.environ.get("ENTRY_FIELDS"), ask_language_level=ASK_LANGUAGE_LEVEL
)


def entry_field_enabled(name: str) -> bool:
    """True for title always; otherwise whether the optional field is on."""
    key = canonical_entry_field(name)
    if key == "title":
        return True
    return key in ENTRY_FIELDS


def language_level_prompt_enabled() -> bool:
    return entry_field_enabled("language_levels")


def _display_utc_offset_hours_from_env() -> int:
    raw = os.environ.get("DISPLAY_UTC_OFFSET_HOURS", "2").strip()
    try:
        hours = int(raw)
    except ValueError:
        print(f"Warning: invalid DISPLAY_UTC_OFFSET_HOURS={raw!r}, using 2 (UTC+2).")
        return 2
    if hours < -12 or hours > 14:
        print(
            f"Warning: DISPLAY_UTC_OFFSET_HOURS={hours} out of range, using 2 (UTC+2)."
        )
        return 2
    return hours


# Wall-clock times in bot messages (e.g. /info, admin console) use this UTC offset.
DISPLAY_UTC_OFFSET_HOURS = _display_utc_offset_hours_from_env()


def display_timezone() -> timezone:
    return timezone(timedelta(hours=DISPLAY_UTC_OFFSET_HOURS))


_ENTITY_DEFAULT_CHAT_NAMES = {"book": "Книжный клуб", "film": "Киноклуб"}
ALLOWED_CHAT_NAME = (
    os.environ.get("ALLOWED_CHAT_NAME") or _ENTITY_DEFAULT_CHAT_NAMES[CLUB_ENTITY]
)

# Language for messages the bot posts into the group chat (en, ru, or de).
# Group messages are shared, so they can't follow any single user's language.
CHAT_LANG = os.environ.get("CHAT_LANG", "ru")

# Delay before broadcasting a new-book card to opted-in users (and optional group chat).
NEW_BOOK_NOTIFY_DELAY_SECONDS = int(
    os.environ.get("NEW_BOOK_NOTIFY_DELAY_SECONDS", "300")
)


def _first_env(*names: str) -> str:
    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw
    return ""


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"Warning: invalid {name}={raw!r}, using {default}.")
        return default
    if value <= 0:
        print(f"Warning: {name}={raw!r} must be positive, using {default}.")
        return default
    return value


# /add field suggestions: OpenAI-compatible Chat Completions, or a Cursor
# subscription agent (LLM_PROVIDER=cursor). CURSOR_API_KEY is never reused
# as the chat-completions bearer.
_OPENAI_CHAT_BASE = "https://api.openai.com/v1"
_XAI_CHAT_BASE = "https://api.x.ai/v1"
_CURSOR_MODEL = "composer-2.5"
_CHAT_PROVIDERS = frozenset({"", "openai", "chat", "xai"})


def resolve_llm_provider() -> str:
    raw = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if raw in _CHAT_PROVIDERS:
        return "chat"
    if raw == "cursor":
        return "cursor"
    print(f"Warning: unknown LLM_PROVIDER={raw!r}, using chat completions.")
    return "chat"


def resolve_llm_api_key() -> str:
    return _first_env("LLM_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY")


def resolve_llm_api_base(key: str = "") -> str:
    raw = os.environ.get("LLM_API_BASE", "").strip().rstrip("/")
    token = key or resolve_llm_api_key()
    parsed_host = urlparse(raw).hostname if raw else ""
    is_openai_host = bool(
        parsed_host
        and (parsed_host == "openai.com" or parsed_host.endswith(".openai.com"))
    )
    # An xai-… key against the OpenAI default (or a leftover README copy) 401s.
    if token.startswith("xai-") and (not raw or is_openai_host):
        if raw:
            print(
                "Warning: xAI API key with LLM_API_BASE pointing at OpenAI; "
                f"using {_XAI_CHAT_BASE} instead."
            )
        return _XAI_CHAT_BASE
    if raw:
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LLM_API_BASE must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("LLM_API_BASE must not contain credentials")
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
        }:
            raise ValueError("LLM_API_BASE must use HTTPS except for localhost")
        return raw
    return _OPENAI_CHAT_BASE


def resolve_llm_model(base: str = "", key: str = "", provider: str = "") -> str:
    raw = os.environ.get("LLM_MODEL", "").strip()
    chosen = provider or resolve_llm_provider()
    if chosen == "cursor":
        # Leftover chat-completions defaults are not Cursor model ids.
        if not raw or raw.startswith("gpt-") or raw == "grok-4.6":
            if raw:
                print(
                    f"Warning: LLM_PROVIDER=cursor with LLM_MODEL={raw!r}; "
                    f"using {_CURSOR_MODEL} instead."
                )
            return _CURSOR_MODEL
        return raw
    host = base or resolve_llm_api_base(key)
    token = key or resolve_llm_api_key()
    looks_xai = "api.x.ai" in host or token.startswith("xai-")
    if looks_xai and (not raw or raw.startswith("gpt-")):
        if raw.startswith("gpt-"):
            print(
                f"Warning: xAI endpoint with LLM_MODEL={raw!r}; using grok-4.6 instead."
            )
        return "grok-4.6"
    if raw:
        return raw
    return "gpt-4o-mini"


def resolve_llm_timeout_seconds(model: str = "") -> float:
    default = 120.0 if "grok" in (model or "").casefold() else 45.0
    return _positive_float_env("LLM_TIMEOUT_SECONDS", default)


CURSOR_API_KEY = os.environ.get("CURSOR_API_KEY", "").strip()
LLM_PROVIDER = resolve_llm_provider()
LLM_API_KEY = resolve_llm_api_key()
LLM_API_BASE = resolve_llm_api_base(LLM_API_KEY)
LLM_MODEL = resolve_llm_model(LLM_API_BASE, LLM_API_KEY, LLM_PROVIDER)
LLM_TIMEOUT_SECONDS = resolve_llm_timeout_seconds(LLM_MODEL)
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "").strip()


def llm_configured() -> bool:
    if LLM_PROVIDER == "cursor":
        return bool(CURSOR_API_KEY)
    return bool(LLM_API_KEY)


def notify_delay_minutes() -> int:
    """Whole minutes for UI copy, derived from NEW_BOOK_NOTIFY_DELAY_SECONDS."""
    secs = NEW_BOOK_NOTIFY_DELAY_SECONDS
    if secs <= 0:
        return 0
    return max(1, secs // 60)


# Conversation states
(
    ADDING_TITLE,
    ADDING_AUTHOR,
    ADDING_PAGES,
    ADDING_FICTION,
    ADDING_REVIEW,
    ADDING_ORIGINAL_LANGUAGE,
    ADDING_CREATION_YEAR,
    ADDING_DESCRIPTION,
) = range(8)
ADDING_TITLE_CONFIRM = 25
ADMIN_IMPORT_CONFIRM = 26
ADDING_LANGUAGE_LEVEL = 27
ADDING_ORIGINAL_LANGUAGE_OTHER = 28
ADDING_AI_CHOOSE = 29
ADDING_START = 30
ADDING_DRAFT_CHOOSE = 31
ADDING_CONFIRM = 32
EDITING_CHOOSE = 8
EDITING_FIELD = 9  # waiting for new value of current field
DELETING_CHOOSE = 10
(
    ADMIN_MENU,
    ADMIN_MARK_CHOOSE,
    ADMIN_MARK_DATE,
    ADMIN_HIDE_CHOOSE,
    ADMIN_UNHIDE_CHOOSE,
    ADMIN_NOTIFY_PICK,
    ADMIN_NOTIFY_CHAT_PICK,
    ADMIN_EXPORT_CHOOSE,
    ADMIN_IMPORT_WAIT,
    ADMIN_MEETING_BOOK,
    ADMIN_MEETING_DATE,
    ADMIN_MEETING_ATTENDEES,
    ADMIN_MEETING_ADD_ID,
    ADMIN_MEETINGS_VIEW,
) = range(11, 25)

MEETING_ATTENDEES_PAGE_SIZE = 7
NOTIFY_BOOKS_PAGE_SIZE = 8
PICKER_PAGE_SIZE = 12

LOG_FILE = os.environ.get("LOG_FILE", "logs/bookclub_bot.log")

ERROR_ALERTS = os.environ.get("ERROR_ALERTS", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "")

IMPORTED_USER_ID = 0  # books imported without a real Telegram user
