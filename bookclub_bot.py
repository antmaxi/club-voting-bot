#!/usr/bin/env python3
"""
Club Voting Bot — EN/RU/DE
=========================================
Fields per book:
  - title (always required)
  - optional via ENTRY_FIELDS: author, pages, fiction, review_link, sources
    (up to 3 links), original_language, creation_year, language_levels, description
  - review link and sources can be skipped on /add
  - added_at, added_by
  - discussed (flag, admin-only), discussed_at (date)

Features:
  - Language support (English, Russian, German).
  - Add and manage books for the club.
  - Vote on books: "Want", "Don't care", "Don't want".
  - Ranking system (Top books) based on average score and vote count.
  - New book notifications: receive a voting card for new books after 5 minutes.
  - User settings to opt-in or out of notifications.

Commands:
  /start / /help   - Welcome message and command overview
  /add             - Add a new book (optional AI help for the other fields)
  /list_and_vote   - List all undiscussed books (all or only unvoted)
  /top             - View top-rated undiscussed books
  /settings        - Manage notification and language preferences
  /info            - Information about the bot and source code
  /edit            - Edit a book's details (owner/admin only)
  /delete          - Delete a book (owner/admin only)
  /discussed       - View the archive of discussed books
  /adminconsole    - Admin console: mark discussed, hide, meetings
  /cancel          - Cancel the current operation

Implementation lives in the :mod:`bookclub` package; this module re-exports its
public API so tests and deploy scripts can keep ``import bookclub_bot``.
"""

from __future__ import annotations

from bookclub import *  # noqa: F403
from bookclub.config import _VALID_CLUB_ENTITIES, _club_entity_from_env  # noqa: F401
from bookclub.logging_setup import (  # noqa: F401 — tests
    _ALERT_BUFFER_MAX,
    _alert_buffer,
    _alert_dropped,
    _log_fmt,
    _TelegramAlertHandler,
)
from bookclub.main import main
from bookclub.membership import _check_membership, _membership_cache  # noqa: F401

if __name__ == "__main__":
    main()
