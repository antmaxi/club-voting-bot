"""OpenAI-compatible LLM client for admin add-book field suggestions."""

from __future__ import annotations

import json
import re
import tempfile
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

import bookclub.config as config
from bookclub.cefr import CEFR_LEVELS, parse_language_levels
from bookclub.config import CLUB_ENTITY, ENTRY_FIELDS, OPTIONAL_ENTRY_FIELDS
from bookclub.logging_setup import logger
from bookclub.original_languages import (
    ORIGINAL_LANGUAGE_CODES,
    STORED_ORIGINAL_LANGUAGE,
    original_language_code_for_stored,
)
from bookclub.review_page import (
    fetch_review_text,
    first_verified_review_url,
    pick_catalog_review_url,
    prefers_russian_catalog,
)
from bookclub.ui import is_valid_url

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_CREATION_YEAR_MIN = 1000
_CREATION_YEAR_MAX = 2100
_DESC_LANG = {"en": "English", "ru": "Russian", "de": "German"}
_TRUE_WORDS = {
    "1",
    "true",
    "yes",
    "fiction",
    "feature",
    "feature film",
    "художественная",
    "художественный",
    "худ. фильм",
    "belletristik",
    "spielfilm",
}
_FALSE_WORDS = {
    "0",
    "false",
    "no",
    "nonfiction",
    "non-fiction",
    "non fiction",
    "documentary",
    "нехудожественная",
    "документальный",
    "sachbuch",
    "dokumentarfilm",
}


# Stable codes used in ERROR logs, Telegram copy, and i18n keys (llm_err_{kind}).
LLM_ERROR_KINDS = frozenset(
    {
        "auth",
        "rate_limit",
        "timeout",
        "network",
        "bad_model",
        "bad_request",
        "not_found",
        "server",
        "empty_reply",
        "unusable_json",
        "provider_non_json",
        "http",
        "request",
    }
)


class LlmRequestError(Exception):
    """The Chat Completions request failed or returned unusable content."""

    def __init__(self, detail: str, *, kind: str = "request") -> None:
        self.kind = kind if kind in LLM_ERROR_KINDS else "request"
        self.detail = redact_llm_secrets(detail)
        super().__init__(self.detail)

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def suggest_book_fields(
    title: str,
    *,
    lang: str,
    entity: str | None = None,
    enabled_fields: frozenset[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Return (normalized field dict, error).

    ``error`` is None on success (the dict may still be empty),
    ``not_configured``, or a short failure description for the admin UI.
    """
    if not config.llm_configured():
        return {}, "not_configured"
    enabled = enabled_fields if enabled_fields is not None else ENTRY_FIELDS
    wanted = [name for name in OPTIONAL_ENTRY_FIELDS if name in enabled]
    if not wanted:
        return {}, None
    club_entity = entity or CLUB_ENTITY
    try:
        content = chat_completion(
            _suggestion_messages(title, lang=lang, entity=club_entity, wanted=wanted)
        )
        raw = extract_json_object(content)
        return normalize_suggestions(raw, enabled=frozenset(wanted)), None
    except LlmRequestError as e:
        _log_suggestion_failure(e.kind, title, e.detail)
        return {}, str(e)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        _log_suggestion_failure("unusable_json", title, str(e))
        return {}, f"unusable_json: {e}"


def suggest_review_link(
    title: str,
    *,
    lang: str,
    entity: str | None = None,
) -> tuple[str | None, str | None]:
    """Return (verified catalog URL or None, error or None).

    Prefers live catalog search (LitRes / Kinopoisk for Russian-language
    works, Goodreads / IMDb otherwise, then Wikipedia) over model-invented
    IDs. LLM candidates are fetched and dropped when the page text is not about
    ``title``.
    """
    club_entity = entity or CLUB_ENTITY
    catalog = pick_catalog_review_url(title, lang=lang, entity=club_entity)
    if catalog:
        return catalog, None
    if not config.llm_configured():
        return None, "not_configured"
    try:
        content = chat_completion(
            _review_link_messages(title, lang=lang, entity=club_entity)
        )
        raw = extract_json_object(content)
        candidates = _review_link_candidates(raw)
    except LlmRequestError as e:
        _log_suggestion_failure(e.kind, title, e.detail)
        return None, str(e)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        _log_suggestion_failure("unusable_json", title, str(e))
        return None, f"unusable_json: {e}"
    verified = first_verified_review_url(candidates, title)
    return verified, None


def suggest_fields_after_review(
    title: str,
    review_url: str,
    *,
    lang: str,
    entity: str | None = None,
    enabled_fields: frozenset[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Fill remaining /add fields from the confirmed review page when possible."""
    enabled = enabled_fields if enabled_fields is not None else ENTRY_FIELDS
    wanted = [
        name for name in OPTIONAL_ENTRY_FIELDS if name in enabled and name != "review"
    ]
    if not wanted:
        return {}, None
    club_entity = entity or CLUB_ENTITY
    fetched = fetch_review_text(review_url)
    if fetched is not None:
        _final, page_text = fetched
        return suggest_book_fields_from_page(
            title,
            page_text,
            review_url,
            lang=lang,
            entity=club_entity,
            enabled_fields=frozenset(wanted),
        )
    return suggest_book_fields(
        title, lang=lang, entity=club_entity, enabled_fields=frozenset(wanted)
    )


def suggest_book_fields_from_page(
    title: str,
    page_text: str,
    review_url: str,
    *,
    lang: str,
    entity: str | None = None,
    enabled_fields: frozenset[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    if not config.llm_configured():
        return {}, "not_configured"
    enabled = enabled_fields if enabled_fields is not None else ENTRY_FIELDS
    wanted = [
        name for name in OPTIONAL_ENTRY_FIELDS if name in enabled and name != "review"
    ]
    if not wanted:
        return {}, None
    club_entity = entity or CLUB_ENTITY
    try:
        content = chat_completion(
            _page_suggestion_messages(
                title,
                page_text,
                review_url,
                lang=lang,
                entity=club_entity,
                wanted=wanted,
            )
        )
        raw = extract_json_object(content)
        return normalize_suggestions(raw, enabled=frozenset(wanted)), None
    except LlmRequestError as e:
        _log_suggestion_failure(e.kind, title, e.detail)
        return {}, str(e)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        _log_suggestion_failure("unusable_json", title, str(e))
        return {}, f"unusable_json: {e}"


def chat_completion(messages: list[dict[str, str]]) -> str:
    """Return assistant text from the configured provider."""
    if config.LLM_PROVIDER == "cursor":
        return _cursor_completion(messages)
    return _chat_completions(messages)


def _cursor_sdk() -> tuple[Any, Any, Any, Any]:
    """Lazy import so chat-completions deploys do not need cursor-sdk."""
    try:
        from cursor_sdk import (
            Agent,
            AgentOptions,
            CursorAgentError,
            LocalAgentOptions,
        )
    except ImportError as e:
        raise LlmRequestError(
            "Cursor provider requires the cursor-sdk package. "
            "Install with: pip install -r requirements-cursor.txt",
            kind="request",
        ) from e
    return Agent, AgentOptions, CursorAgentError, LocalAgentOptions


def _messages_to_prompt(messages: list[dict[str, str]]) -> str:
    """Join chat messages into one prompt. The SDK has no system role."""
    parts = [(message.get("content") or "").strip() for message in messages]
    return "\n\n".join(part for part in parts if part)


def _cursor_error_kind(message: str) -> str:
    text = message.casefold()
    if any(
        token in text
        for token in (
            "api key",
            "invalid key",
            "unauthorized",
            "authentication",
            "not authenticated",
        )
    ):
        return "auth"
    if "model" in text and any(
        token in text for token in ("not found", "does not exist", "unknown", "invalid")
    ):
        return "bad_model"
    return "request"


def _cursor_completion(messages: list[dict[str, str]]) -> str:
    """One-shot local Cursor agent billed to the subscription, not a chat API.

    ``tools=[]`` and a temp cwd keep the agent from editing this repo.
    ``Agent.prompt`` disposes the client.
    """
    if not config.CURSOR_API_KEY:
        raise LlmRequestError("Cursor API key is not set", kind="auth")
    Agent, AgentOptions, CursorAgentError, LocalAgentOptions = _cursor_sdk()
    prompt = _messages_to_prompt(messages)

    def run(cwd: str) -> Any:
        try:
            return Agent.prompt(
                prompt,
                AgentOptions(
                    api_key=config.CURSOR_API_KEY,
                    model=config.LLM_MODEL,
                    tools=[],
                    local=LocalAgentOptions(cwd=cwd),
                ),
            )
        except CursorAgentError as e:
            message = str(getattr(e, "message", None) or e)
            raise LlmRequestError(
                f"Cursor: {message}", kind=_cursor_error_kind(message)
            ) from e

    with tempfile.TemporaryDirectory(prefix="bookclub-cursor-") as cwd:
        result = run(cwd)
    status = str(getattr(result, "status", "") or "")
    if status != "finished":
        run_id = str(getattr(result, "id", "") or "")
        suffix = f" ({run_id})" if run_id else ""
        raise LlmRequestError(f"Cursor run {status}{suffix}", kind="request")
    text = str(getattr(result, "result", None) or "").strip()
    if not text:
        raise LlmRequestError("Cursor returned no text", kind="empty_reply")
    return text


def _chat_completions(messages: list[dict[str, str]]) -> str:
    """POST /chat/completions and return the assistant message content."""
    url = f"{config.LLM_API_BASE.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": config.LLM_MODEL,
        "messages": messages,
    }
    model = config.LLM_MODEL.casefold()
    if "grok" not in model:
        payload["temperature"] = 0.2
    effort = config.LLM_REASONING_EFFORT
    if not effort and "grok" in model:
        # High is xAI's default and often exceeds the HTTP timeout on this
        # small lookup. Callers can still set LLM_REASONING_EFFORT=high.
        effort = "low"
    if effort:
        payload["reasoning_effort"] = effort
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:800]
        detail = _http_error_message(e.code, err_body)
        raise LlmRequestError(detail, kind=_http_error_kind(e.code, detail)) from e
    except TimeoutError as e:
        raise LlmRequestError(_timeout_detail(), kind="timeout") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise LlmRequestError(_timeout_detail(), kind="timeout") from e
        raise LlmRequestError(str(reason), kind="network") from e
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise LlmRequestError(
            "provider returned non-JSON", kind="provider_non_json"
        ) from e
    content = _message_content(parsed)
    if not content:
        raise LlmRequestError("empty assistant message", kind="empty_reply")
    return content


_SECRET_RE = re.compile(r"(xai-|sk-or-|sk-|crsr_|gsk_)[A-Za-z0-9_\-]+")
_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+")
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:api_key|token|access_token)=)[^&#\s]+")


def redact_llm_secrets(text: str) -> str:
    """Remove provider credentials before text reaches users, logs, or alerts."""
    redacted = str(text)
    if config.LLM_API_KEY:
        redacted = redacted.replace(config.LLM_API_KEY, "[redacted]")
    if config.CURSOR_API_KEY:
        redacted = redacted.replace(config.CURSOR_API_KEY, "[redacted]")
    redacted = _SECRET_RE.sub("[redacted]", redacted)
    redacted = _BEARER_RE.sub(r"\1[redacted]", redacted)
    redacted = _QUERY_SECRET_RE.sub(r"\1[redacted]", redacted)
    return " ".join(redacted.split())[:800]


def _provider_origin() -> str:
    if config.LLM_PROVIDER == "cursor":
        return "cursor"
    parsed = urlparse(config.LLM_API_BASE)
    return (
        f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "[invalid provider]"
    )


def _log_suggestion_failure(kind: str, title: str, detail: str) -> None:
    safe_title = redact_llm_secrets(title)[:200]
    logger.error(
        "LLM book suggestions failed [%s] for %r via %s (%s): %s",
        kind,
        safe_title,
        redact_llm_secrets(config.LLM_MODEL),
        redact_llm_secrets(_provider_origin()),
        redact_llm_secrets(detail),
    )


def _timeout_detail() -> str:
    seconds = config.LLM_TIMEOUT_SECONDS
    shown = int(seconds) if float(seconds).is_integer() else seconds
    return f"timed out after {shown}s"


def _http_error_kind(code: int, detail: str) -> str:
    text = detail.casefold()
    if (
        code in (401, 403)
        or "api key" in text
        or "unauthorized" in text
        or "authentication" in text
        or "invalid api" in text
        or "incorrect api" in text
    ):
        return "auth"
    if code == 429 or "rate limit" in text or "too many requests" in text:
        return "rate_limit"
    if "model" in text and any(
        token in text for token in ("not found", "does not exist", "unknown", "invalid")
    ):
        return "bad_model"
    if code == 404:
        return "not_found"
    if 500 <= code <= 599:
        return "server"
    if 400 <= code < 500:
        return "bad_request"
    return "http"


def split_llm_error(error: str) -> tuple[str, str]:
    """Split ``kind: detail`` from ``suggest_book_fields``; unknown kinds stay raw."""
    kind, sep, detail = error.partition(": ")
    if sep and kind in LLM_ERROR_KINDS:
        return kind, detail
    return "request", error


def llm_error_kind_i18n_key(kind: str) -> str:
    if kind in LLM_ERROR_KINDS:
        return f"llm_err_{kind}"
    return "llm_err_request"


def _http_error_message(code: int, body: str) -> str:
    snippet = " ".join(body.split())
    message = snippet
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("code") or snippet)
        elif isinstance(err, str):
            message = err
        elif parsed.get("message"):
            message = str(parsed["message"])
        elif parsed.get("code"):
            message = str(parsed["code"])
    if not message:
        return f"HTTP {code}"
    return f"HTTP {code}: {message}"


def ui_llm_error(detail: str) -> str:
    """Short snippet of a provider error for Telegram (escape with h())."""
    text = redact_llm_secrets(detail)
    if len(text) > 180:
        text = text[:177] + "..."
    return text


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model reply, including markdown fences."""
    stripped = text.strip()
    fenced = _JSON_FENCE_RE.search(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in LLM reply") from None
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON was not an object")
    return parsed


def normalize_suggestions(
    raw: dict[str, Any], *, enabled: frozenset[str]
) -> dict[str, Any]:
    """Keep only enabled fields and coerce them to /add storage types."""
    out: dict[str, Any] = {}
    if "author" in enabled:
        author = raw.get("author")
        if author in (None, ""):
            author = raw.get("director")
        if isinstance(author, str) and author.strip():
            out["author"] = author.strip()
    if "pages" in enabled:
        pages = _positive_int(
            raw.get("pages", raw.get("runtime", raw.get("runtime_minutes")))
        )
        if pages is not None:
            out["pages"] = pages
    if "fiction" in enabled:
        fiction = _as_bool(
            raw.get("fiction", raw.get("is_fiction", raw.get("is_feature_film")))
        )
        if fiction is not None:
            out["fiction"] = fiction
    if "review" in enabled:
        link = raw.get("review_link") or raw.get("review") or raw.get("url")
        if not isinstance(link, str):
            links = _review_link_candidates(raw)
            link = links[0] if links else None
        if isinstance(link, str) and is_valid_url(link.strip()):
            out["review_link"] = link.strip()
    if "original_language" in enabled:
        lang_raw = raw.get("original_language") or raw.get("language")
        if isinstance(lang_raw, str) and lang_raw.strip():
            out["original_language"] = normalize_original_language(lang_raw)
    if "creation_year" in enabled:
        year = _creation_year(
            raw.get("creation_year", raw.get("year", raw.get("release_year")))
        )
        if year is not None:
            out["creation_year"] = year
    if "language_levels" in enabled:
        levels = _cefr_levels(raw.get("language_levels", raw.get("cefr")))
        if levels:
            out["language_levels"] = levels
    if "description" in enabled:
        desc = raw.get("description")
        if isinstance(desc, str) and desc.strip():
            out["description"] = desc.strip()
    return out


def normalize_original_language(raw: str) -> str:
    """Map a free-form language name to the stored English name when known."""
    text = raw.strip()
    if not text:
        return text
    lowered = text.casefold()
    for stored in STORED_ORIGINAL_LANGUAGE.values():
        if stored.casefold() == lowered:
            return stored
    if lowered in ORIGINAL_LANGUAGE_CODES:
        stored = STORED_ORIGINAL_LANGUAGE[lowered]
        return stored
    code = original_language_code_for_stored(text)
    if code is not None:
        return STORED_ORIGINAL_LANGUAGE[code]
    from bookclub.i18n import T

    for lang_table in T.values():
        for code in ORIGINAL_LANGUAGE_CODES:
            label = str(lang_table.get(f"orig_lang_{code}", ""))
            if _alnum_key(label) == _alnum_key(text):
                return STORED_ORIGINAL_LANGUAGE[code]
    aliases = {
        "русский": "Russian",
        "русском": "Russian",
        "russisch": "Russian",
        "немецкий": "German",
        "немецком": "German",
        "deutsch": "German",
        "английский": "English",
        "английском": "English",
        "englisch": "English",
        "итальянский": "Italian",
        "italienisch": "Italian",
        "французский": "French",
        "französisch": "French",
        "francais": "French",
        "français": "French",
        "испанский": "Spanish",
        "spanisch": "Spanish",
        "español": "Spanish",
        "китайский": "Chinese",
        "chinesisch": "Chinese",
        "японский": "Japanese",
        "japanisch": "Japanese",
    }
    return aliases.get(lowered, text)


def apply_suggestions_to_book(
    nb: dict[str, Any],
    suggestions: dict[str, Any],
    *,
    preserve: set[str] | None = None,
) -> set[str]:
    """Copy suggestion keys into the in-progress book; return keys that were set.

    Keys in ``preserve`` that already have a value in ``nb`` are left unchanged
    (used so a later review-page pass does not overwrite a user edit).
    """
    filled: set[str] = set()
    keep = preserve or set()
    for key, value in suggestions.items():
        if key in keep and key in nb:
            continue
        nb[key] = value
        filled.add(key)
    return filled


def _suggestion_messages(
    title: str, *, lang: str, entity: str, wanted: list[str]
) -> list[dict[str, str]]:
    desc_lang = _DESC_LANG.get(lang, "English")
    if entity == "film":
        kind = "film"
        field_help = {
            "author": "director (full name)",
            "pages": (
                "runtime in minutes (integer); prefer the number printed on the "
                "catalog/review page (IMDb, Kinopoisk, or Letterboxd usually "
                "mention it), and omit if that page does not list it"
            ),
            "fiction": "true for a feature film, false for a documentary",
            "review": (
                "review_link: omit unless you can give a URL you are sure exists. "
                + (
                    "Prefer a Kinopoisk page for this title; IMDb is a fallback. "
                    if prefers_russian_catalog(title, lang)
                    else "Prefer an IMDb page for this title; Kinopoisk is a fallback. "
                )
                + "Never invent catalog IDs (IMDb, Kinopoisk, or Letterboxd). "
                "A plausible slug is not enough — omit if you cannot verify the page"
            ),
            "original_language": (
                "original_language: English name such as English, Russian, German, "
                "French, Spanish, Italian, Chinese, Japanese, or another language name"
            ),
            "creation_year": "creation_year: 4-digit release year",
            "language_levels": "language_levels: JSON array of CEFR codes from A1–C2",
            "description": f"description: 2–4 sentences in {desc_lang}",
        }
    else:
        kind = "book"
        field_help = {
            "author": "author (full name)",
            "pages": (
                "page count (integer); prefer the number printed on the "
                "catalog/review page (Goodreads or LitRes usually mention it), "
                "and omit if that page does not list it"
            ),
            "fiction": "true for fiction, false for non-fiction",
            "review": (
                "review_link: omit unless you can give a URL you are sure exists. "
                + (
                    "Prefer a LitRes page for this title; Goodreads is a fallback. "
                    if prefers_russian_catalog(title, lang)
                    else "Prefer a Goodreads page for this title; LitRes is a fallback. "
                )
                + "Never invent catalog IDs (Goodreads, LitRes numeric paths). "
                "A plausible slug is not enough — omit if you cannot verify the page"
            ),
            "original_language": (
                "original_language: English name such as English, Russian, German, "
                "French, Spanish, Italian, Chinese, Japanese, or another language name"
            ),
            "creation_year": "creation_year: 4-digit publication year",
            "language_levels": "language_levels: JSON array of CEFR codes from A1–C2",
            "description": f"description: 2–4 sentences in {desc_lang}",
        }
    lines = [field_help[name] for name in wanted if name in field_help]
    if "pages" in wanted and "review" in wanted:
        if entity == "film":
            lines.append(
                "Copy runtime from the same review_link page when that page lists it"
            )
        else:
            lines.append(
                "Copy page count from the same review_link page when that page lists it"
            )
    if "pages" in wanted:
        if entity == "film":
            lookup_hint = (
                " When a catalog or review page lists runtime, copy that number "
                "instead of guessing."
            )
        else:
            lookup_hint = (
                " When a catalog or review page lists page count, copy that "
                "number instead of guessing."
            )
    else:
        lookup_hint = ""
    system = (
        "You look up well-known bibliographic facts. "
        "Reply with a single JSON object and no other text. "
        "Omit a field when you are not reasonably sure. "
        "Do not invent review URLs." + lookup_hint
    )
    user = (
        f"Suggest metadata for this {kind} titled {title!r}.\n"
        "JSON keys and meaning:\n- "
        + "\n- ".join(lines)
        + "\nDo not include the title."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _field_help(lang: str, entity: str) -> dict[str, str]:
    desc_lang = _DESC_LANG.get(lang, "English")
    if entity == "film":
        return {
            "author": "director (full name)",
            "pages": (
                "runtime in minutes (integer); copy the number printed on the "
                "given catalog/review page (IMDb, Kinopoisk, or Letterboxd usually "
                "mention it), and omit if that page does not list it"
            ),
            "fiction": "true for a feature film, false for a documentary",
            "original_language": (
                "original_language: English name such as English, Russian, German, "
                "French, Spanish, Italian, Chinese, Japanese, or another language name"
            ),
            "creation_year": "creation_year: 4-digit release year",
            "language_levels": "language_levels: JSON array of CEFR codes from A1–C2",
            "description": f"description: 2–4 sentences in {desc_lang}",
        }
    return {
        "author": "author (full name)",
        "pages": (
            "page count (integer); copy the number printed on the given "
            "catalog/review page (Goodreads or LitRes usually mention it), "
            "and omit if that page does not list it"
        ),
        "fiction": "true for fiction, false for non-fiction",
        "original_language": (
            "original_language: English name such as English, Russian, German, "
            "French, Spanish, Italian, Chinese, Japanese, or another language name"
        ),
        "creation_year": "creation_year: 4-digit publication year",
        "language_levels": "language_levels: JSON array of CEFR codes from A1–C2",
        "description": f"description: 2–4 sentences in {desc_lang}",
    }


def _review_link_messages(
    title: str, *, lang: str, entity: str
) -> list[dict[str, str]]:
    if entity == "film":
        kind = "film"
        invented = "IMDb, Kinopoisk, or Letterboxd numeric IDs"
        if prefers_russian_catalog(title, lang):
            catalogs = "Kinopoisk (preferred), then IMDb"
            prefer = "Prefer Kinopoisk. Use IMDb only if Kinopoisk has no page."
        else:
            catalogs = "IMDb (preferred), then Kinopoisk"
            prefer = "Prefer IMDb. Use Kinopoisk only if IMDb has no page."
    elif prefers_russian_catalog(title, lang):
        kind = "book"
        catalogs = "LitRes (preferred), then Goodreads"
        invented = "Goodreads or LitRes numeric IDs"
        prefer = "Prefer LitRes. Use Goodreads only if LitRes has no page."
    else:
        kind = "book"
        catalogs = "Goodreads (preferred), then LitRes"
        invented = "Goodreads or LitRes numeric IDs"
        prefer = "Prefer Goodreads. Use LitRes only if Goodreads has no page."
    system = (
        "You suggest catalog URLs only when you are sure the page exists and is "
        "about this title. Reply with a single JSON object and no other text. "
        f"Never invent catalog IDs. A relevant slug is not enough. {prefer} "
        "Omit the field if unsure."
    )
    user = (
        f"Suggest a catalog or review page for this {kind} titled {title!r}.\n"
        f"Preferred sites: {catalogs}.\n"
        "JSON keys:\n"
        "- review_links: JSON array of up to 3 http(s) URLs, best first; or\n"
        "- review_link: a single http(s) URL\n"
        f"Do not invent {invented}. Do not include the title."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _page_suggestion_messages(
    title: str,
    page_text: str,
    review_url: str,
    *,
    lang: str,
    entity: str,
    wanted: list[str],
) -> list[dict[str, str]]:
    kind = "film" if entity == "film" else "book"
    field_help = _field_help(lang, entity)
    lines = [field_help[name] for name in wanted if name in field_help]
    if entity == "film":
        copy_hint = "Copy runtime from this page text when it lists it; do not guess."
    else:
        copy_hint = (
            "Copy page count from this page text when it lists it; do not guess."
        )
    excerpt = page_text.strip()
    if len(excerpt) > 12_000:
        excerpt = excerpt[:12_000]
    system = (
        "You extract bibliographic facts from the supplied catalog/review page. "
        "Reply with a single JSON object and no other text. "
        "Omit a field when the page does not support it. "
        "Do not invent review URLs or numeric IDs. " + copy_hint
    )
    user = (
        f"Extract metadata for this {kind} titled {title!r}.\n"
        f"Confirmed review_link: {review_url}\n"
        "JSON keys and meaning:\n- "
        + "\n- ".join(lines)
        + "\nDo not include the title or review_link.\n"
        "Page text:\n"
        f"{excerpt}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _review_link_candidates(raw: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    links = raw.get("review_links")
    if isinstance(links, list):
        values.extend(links)
    for key in ("review_link", "review", "url"):
        values.append(raw.get(key))
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        url = value.strip()
        if url and url not in seen and is_valid_url(url):
            seen.add(url)
            out.append(url)
    return out


def _message_content(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return ""
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            ]
            joined = "".join(parts).strip()
            if joined:
                return joined
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and "{" in reasoning:
            return reasoning.strip()
    text = first.get("text")
    return text.strip() if isinstance(text, str) else ""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not value.is_integer() or value <= 0:
            return None
        return int(value)
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if stripped.isdigit() and int(stripped) > 0:
            return int(stripped)
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        key = value.strip().casefold()
        if key in _TRUE_WORDS:
            return True
        if key in _FALSE_WORDS:
            return False
    return None


def _creation_year(value: Any) -> int | None:
    number = _positive_int(value)
    if number is None:
        return None
    if number < _CREATION_YEAR_MIN or number > _CREATION_YEAR_MAX:
        return None
    return number


def _cefr_levels(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return parse_language_levels(value)
    if isinstance(value, list | tuple | set):
        parts = [str(v).strip().upper() for v in value]
        return {p for p in parts if p in CEFR_LEVELS}
    return set()


def _alnum_key(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())
