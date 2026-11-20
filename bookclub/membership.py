from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from telegram import Update
from telegram.error import NetworkError
from telegram.ext import ApplicationHandlerStop, ContextTypes

import bookclub.config as config
from bookclub.db import db_upsert_club_user
from bookclub.i18n import PM, get_lang, s, tr
from bookclub.logging_setup import logger
from bookclub.ui import h

MEMBERSHIP_CACHE_TTL = 300  # seconds
MEMBERSHIP_CACHE_MAX = 4096
ACTIVITY_WRITE_INTERVAL_SECONDS = 30
_membership_cache: OrderedDict[int, tuple[bool, datetime]] = OrderedDict()
_club_user_profiles: OrderedDict[int, tuple[str, str | None]] = OrderedDict()
_last_activity_write: datetime | None = None


def _membership_cache_evict(user_id: int) -> None:
    _membership_cache.pop(user_id, None)


def _cache_membership(user_id: int, allowed: bool, checked_at: datetime) -> None:
    _membership_cache[user_id] = (allowed, checked_at)
    _membership_cache.move_to_end(user_id)
    while len(_membership_cache) > MEMBERSHIP_CACHE_MAX:
        _membership_cache.popitem(last=False)


def _upsert_club_user_if_changed(
    user_id: int, full_name: str, username: str | None
) -> None:
    profile = (full_name, username)
    if _club_user_profiles.get(user_id) == profile:
        _club_user_profiles.move_to_end(user_id)
        return
    db_upsert_club_user(user_id, full_name, username)
    _club_user_profiles[user_id] = profile
    _club_user_profiles.move_to_end(user_id)
    while len(_club_user_profiles) > MEMBERSHIP_CACHE_MAX:
        _club_user_profiles.popitem(last=False)


_activity_writes: dict[str, datetime] = {}


def initialize_activity() -> None:
    """Start a new tracking epoch, retaining historical category timestamps."""
    global _last_activity_write
    _last_activity_write = None
    _activity_writes.clear()
    _write_activity("started_at", datetime.now(UTC))


def _write_activity(key: str, now: datetime) -> bool:
    activity_path = Path(config.ACTIVITY_PATH)
    tmp_path: Path | None = None
    try:
        try:
            data = json.loads(activity_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.update(version=2)
        data[key] = now.isoformat()
        activity_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{activity_path.name}.",
            suffix=".tmp",
            dir=activity_path.parent,
            text=True,
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, activity_path)
        return True
    except OSError as exc:
        logger.warning(
            "Could not persist activity timestamp to %s: %s", activity_path, exc
        )
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _record_activity(ctx: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    global _last_activity_write
    now = datetime.now(UTC)
    previous = (
        _last_activity_write
        if key == "last_non_admin_activity"
        else _activity_writes.get(key)
    )
    if (
        previous is not None
        and (now - previous).total_seconds() < ACTIVITY_WRITE_INTERVAL_SECONDS
    ):
        return
    ctx.bot_data[key] = now
    if _write_activity(key, now):
        _activity_writes[key] = now
        if key == "last_non_admin_activity":
            _last_activity_write = now


def _record_non_admin_activity(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _record_activity(ctx, "last_non_admin_activity")


async def _check_membership(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the user is a member of ALLOWED_CHAT_ID (or no restriction set)."""
    if not config.ALLOWED_CHAT_ID:
        return True
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return False
    # Admins always pass
    if user_id in config.ADMIN_IDS:
        return True

    cached = _membership_cache.get(user_id)
    if cached is not None:
        allowed, checked_at = cached
        if (datetime.now() - checked_at).total_seconds() < MEMBERSHIP_CACHE_TTL:
            _membership_cache.move_to_end(user_id)
            return allowed
        _membership_cache_evict(user_id)

    try:
        member = await ctx.bot.get_chat_member(config.ALLOWED_CHAT_ID, user_id)
        allowed = member.status in ("member", "administrator", "creator") or (
            member.status == "restricted"
            and getattr(member, "is_member", False) is True
        )
        _cache_membership(user_id, allowed, datetime.now())
        return allowed
    except Exception as e:
        # Keep failures uncached so a recovered API can admit the user immediately.
        logger.error(
            "Membership check failed closed for user %s in ALLOWED_CHAT_ID=%s: %s",
            user_id,
            config.ALLOWED_CHAT_ID,
            e,
        )
        return False


async def membership_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Block users not in the allowed chat and tell them why."""
    if update.message and update.message.left_chat_member:
        # Service message for a user leaving the chat — their membership status
        # is now "left", which would otherwise look like a blocked non-member.
        # Nothing to gate here, and we must not reply into the group over this.
        _membership_cache_evict(update.message.left_chat_member.id)
        return

    if update.message and update.message.new_chat_members:
        # Someone just joined — drop any stale "not a member" verdict for them.
        for m in update.message.new_chat_members:
            _membership_cache_evict(m.id)
        return

    if await _check_membership(update, ctx):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id and update.effective_user.is_bot is not True:
            if config.ADMIN_IDS and user_id == config.ADMIN_IDS[0]:
                _record_activity(ctx, "last_main_admin_activity")
            elif user_id in config.ADMIN_IDS:
                _record_activity(ctx, "last_other_admin_activity")
            else:
                _record_non_admin_activity(ctx)
        if user_id and update.effective_user:
            _upsert_club_user_if_changed(
                user_id,
                update.effective_user.full_name or "",
                update.effective_user.username,
            )
        return
    blocked_uid = update.effective_user.id if update.effective_user else None
    logger.info(
        f"Blocked user {blocked_uid or '?'} — not a member of chat {config.ALLOWED_CHAT_ID}"
    )
    lang = get_lang(ctx) if ctx.user_data else "ru"
    text = s(lang, "not_member").format(chat=h(config.ALLOWED_CHAT_NAME))
    try:
        if update.callback_query:
            await update.callback_query.answer(
                config.ALLOWED_CHAT_NAME + " — members only", show_alert=True
            )
        elif update.message:
            await update.message.reply_text(text, parse_mode=PM)
    except Exception as e:
        logger.warning(f"Could not send not-member message to {blocked_uid}: {e}")
    raise ApplicationHandlerStop


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any unhandled handler exception and tell the user something broke.

    Without this, python-telegram-bot swallows the traceback into the log and
    the user gets no reply at all — the bot just goes silent mid-command, which
    is indistinguishable from it being down.

    Note: Error messages are suppressed in group chats to avoid spamming
    members with technical notifications that belong in logs only.

    Transient Telegram API failures (e.g. 502 during long polling) are logged
    at WARNING only — they are retried by the library and should not page the admin.
    """
    if isinstance(ctx.error, NetworkError):
        logger.warning(
            "Telegram network error while processing update:", exc_info=ctx.error
        )
        return

    logger.error("Unhandled exception while processing update:", exc_info=ctx.error)

    if not isinstance(update, Update):
        return  # e.g. a job error — nobody to reply to

    # Suppress error notifications in group chats — they should only go to logs
    # or to the admin via the dedicated alert mechanism (_TelegramAlertHandler).
    if update.effective_message and update.effective_message.chat.type != "private":
        logger.info(
            f"Suppressed error notification in group chat {update.effective_message.chat.id}"
        )
        return

    lang = get_lang(ctx) if ctx.user_data is not None else "ru"
    text = tr(lang, "unexpected_error")

    try:
        if update.callback_query:
            # answer() clears the button's loading spinner; a plain message
            # would leave it spinning even though we did reply.
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text, parse_mode=PM)
        elif update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=PM)
    except Exception as e:
        # Never let the error handler itself raise — that loses the original error.
        logger.warning(f"Could not deliver error notice to user: {e}")
