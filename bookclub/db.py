from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

import bookclub.config as config
from bookclub.cefr import format_language_levels
from bookclub.types import BookLike

IMPORTED_USER_ID = config.IMPORTED_USER_ID
_CREATION_YEAR_MIN = 1000
_CREATION_YEAR_MAX = 2100


def _ensure_db_writable() -> None:
    """Fail fast when the database directory is not writable (common in Docker)."""
    db_path = os.path.abspath(config.DB_PATH)
    parent = os.path.dirname(db_path) or "."
    probe = os.path.join(parent, ".db_write_test")
    try:
        os.makedirs(parent, exist_ok=True)
        with open(probe, "a", encoding="utf-8"):
            pass
        os.remove(probe)
    except OSError as exc:
        raise SystemExit(
            f"Cannot write the database at {db_path}: {exc}\n"
            "The data directory must be writable by the process user. "
            "For Docker bind mounts, own the host folders:\n"
            '  export BOT_UID="$(id -u)" BOT_GID="$(id -g)"\n'
            '  sudo chown -R "$BOT_UID:$BOT_GID" data logs'
        ) from exc


def init_db() -> None:
    global _votes_use_attendance_cache
    _votes_use_attendance_cache = None
    _ensure_db_writable()
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT NOT NULL,
                author        TEXT NOT NULL,
                pages         INTEGER NOT NULL DEFAULT 0,
                fiction       INTEGER NOT NULL DEFAULT 1,
                review_link   TEXT NOT NULL DEFAULT '',
                description   TEXT DEFAULT '',
                hidden        INTEGER NOT NULL DEFAULT 0,
                discussed     INTEGER NOT NULL DEFAULT 0,
                discussed_at  TEXT DEFAULT NULL,
                added_by      INTEGER NOT NULL,
                added_by_name     TEXT NOT NULL,
                added_by_username TEXT DEFAULT NULL,
                added_at          TEXT NOT NULL
            )
        """)
        # Migrate existing DB: add book columns if missing
        for col, definition in [
            ("pages", "INTEGER NOT NULL DEFAULT 0"),
            ("fiction", "INTEGER NOT NULL DEFAULT 1"),
            ("review_link", "TEXT NOT NULL DEFAULT ''"),
            ("hidden", "INTEGER NOT NULL DEFAULT 0"),
            ("discussed", "INTEGER NOT NULL DEFAULT 0"),
            ("discussed_at", "TEXT DEFAULT NULL"),
            ("added_by_username", "TEXT DEFAULT NULL"),
            ("notify_sent", "INTEGER NOT NULL DEFAULT 1"),
            ("notify_after", "TEXT DEFAULT NULL"),
            ("notify_adder_id", "INTEGER DEFAULT NULL"),
            ("original_language", "TEXT DEFAULT NULL"),
            ("creation_year", "INTEGER DEFAULT NULL"),
            ("language_levels", "TEXT DEFAULT NULL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE books ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass

        # Migrate votes table: rename stars→score, clear old 1-5 data
        try:
            conn.execute("ALTER TABLE votes RENAME COLUMN stars TO score")
            conn.execute("DELETE FROM votes WHERE score NOT IN (-1, 0, 1)")
        except sqlite3.OperationalError:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                user_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                score   INTEGER NOT NULL,
                PRIMARY KEY (user_id, book_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id      INTEGER NOT NULL,
                setting_key  TEXT NOT NULL,
                setting_val  INTEGER NOT NULL,
                PRIMARY KEY (user_id, setting_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS club_users (
                user_id      INTEGER PRIMARY KEY,
                full_name    TEXT NOT NULL DEFAULT '',
                username     TEXT,
                last_seen_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                meeting_date TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                created_by   INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meeting_attendees (
                meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                user_id    INTEGER NOT NULL,
                PRIMARY KEY (meeting_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS add_drafts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                payload    TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_add_drafts_user ON add_drafts(user_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_votes_book_id ON votes(book_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_books_state "
            "ON books(discussed, hidden, added_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_settings_lookup "
            "ON user_settings(setting_key, setting_val, user_id)"
        )
        _dedupe_meetings_one_per_book(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_book_id ON meetings(book_id)"
        )
        conn.execute("""
            INSERT OR IGNORE INTO club_users (user_id, full_name, username, last_seen_at)
            SELECT DISTINCT user_id, '', NULL, datetime('now') FROM votes
        """)
        conn.execute("""
            INSERT OR IGNORE INTO club_users (user_id, full_name, username, last_seen_at)
            SELECT DISTINCT user_id, '', NULL, datetime('now') FROM user_settings
        """)
        # Votes/settings backfill creates empty names first; fill them from books
        # when we have added_by_name (INSERT OR IGNORE would skip those rows).
        conn.execute("""
            INSERT INTO club_users (user_id, full_name, username, last_seen_at)
            SELECT DISTINCT added_by, added_by_name, added_by_username, added_at FROM books
            WHERE added_by > 0
            ON CONFLICT(user_id) DO UPDATE SET
              full_name = CASE
                WHEN club_users.full_name = '' AND excluded.full_name != ''
                THEN excluded.full_name
                ELSE club_users.full_name END,
              username = COALESCE(club_users.username, excluded.username)
        """)
        # The person who added a book counts as a yes vote, unless they
        # already voted something else.
        conn.execute("""
            INSERT OR IGNORE INTO votes (user_id, book_id, score)
            SELECT added_by, id, 1 FROM books
            WHERE added_by > 0
        """)
        conn.commit()
    db_rebuild_attendance_surplus()


def _dedupe_meetings_one_per_book(conn: sqlite3.Connection) -> None:
    """Keep one meeting per book (lowest id); merge attendees from duplicates."""
    dupes = conn.execute("""SELECT book_id FROM meetings
           GROUP BY book_id HAVING COUNT(*) > 1""").fetchall()
    for (book_id,) in dupes:
        ids = [
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM meetings WHERE book_id=? ORDER BY id ASC",
                (book_id,),
            )
        ]
        keep, extras = ids[0], ids[1:]
        for extra in extras:
            conn.execute(
                """INSERT OR IGNORE INTO meeting_attendees (meeting_id, user_id)
                   SELECT ?, user_id FROM meeting_attendees WHERE meeting_id=?""",
                (keep, extra),
            )
            conn.execute("DELETE FROM meetings WHERE id=?", (extra,))


ADD_DRAFTS_MAX = 20


def db_insert_add_draft(user_id: int, title: str, payload: dict[str, Any]) -> int:
    with sqlite3.connect(config.DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO add_drafts (user_id, title, payload, updated_at) "
            "VALUES (?,?,?,datetime('now'))",
            (user_id, title, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()
        draft_id = cur.lastrowid
    if draft_id is None:
        raise RuntimeError("add_draft insert did not return id")
    return int(draft_id)


def db_update_add_draft(
    draft_id: int, user_id: int, title: str, payload: dict[str, Any]
) -> bool:
    with sqlite3.connect(config.DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE add_drafts SET title=?, payload=?, updated_at=datetime('now') "
            "WHERE id=? AND user_id=?",
            (title, json.dumps(payload, ensure_ascii=False), draft_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def db_get_add_draft(draft_id: int, user_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT payload FROM add_drafts WHERE id=? AND user_id=?",
            (draft_id, user_id),
        ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def db_list_add_drafts(user_id: int) -> list[tuple[int, str]]:
    with sqlite3.connect(config.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, title FROM add_drafts WHERE user_id=? "
            "ORDER BY updated_at DESC, id DESC",
            (user_id,),
        ).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]


def db_count_add_drafts(user_id: int) -> int:
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM add_drafts WHERE user_id=?", (user_id,)
        ).fetchone()
    return int(row[0]) if row else 0


def db_delete_add_draft(draft_id: int, user_id: int) -> bool:
    with sqlite3.connect(config.DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM add_drafts WHERE id=? AND user_id=?", (draft_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


def db_add_book(
    title: str,
    author: str,
    pages: int,
    fiction: bool,
    review_link: str,
    description: str,
    user_id: int,
    user_name: str,
    username: str | None = None,
    original_language: str | None = None,
    creation_year: int | None = None,
    language_levels: str | None = None,
) -> int | None:
    lang = (original_language or "").strip() or None
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            """INSERT INTO books
               (title, author, pages, fiction, review_link, description,
                original_language, creation_year, language_levels,
                added_by, added_by_name, added_by_username, added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                title,
                author,
                pages,
                int(fiction),
                review_link,
                description,
                lang,
                creation_year,
                language_levels,
                user_id,
                user_name,
                username,
                datetime.now().strftime("%Y-%m-%d"),
            ),
        )
        conn.commit()
        book_id = cur.lastrowid
    if book_id is not None:
        db_upsert_club_user(user_id, user_name, username)
    return book_id


def db_seed_book_exists(title: str, review_link: str) -> bool:
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM books WHERE title=? AND review_link=? LIMIT 1",
            (title, review_link),
        ).fetchone()
        return row is not None


def db_insert_seed_book(
    *,
    title: str,
    author: str,
    pages: int,
    fiction: bool,
    review_link: str,
    description: str,
    original_language: str | None,
    added_at: str,
    added_by_username: str,
    added_by_name: str,
) -> int:
    """Insert a pre-seeded entry without scheduling new-book notifications."""
    lang = (original_language or "").strip() or None
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            """INSERT INTO books
               (title, author, pages, fiction, review_link, description,
                original_language, hidden, discussed, discussed_at,
                notify_sent, notify_after, notify_adder_id,
                added_by, added_by_name, added_by_username, added_at)
               VALUES (?,?,?,?,?,?,?,0,0,NULL,1,NULL,NULL,?,?,?,?)""",
            (
                title,
                author,
                pages,
                int(fiction),
                review_link,
                description,
                lang,
                IMPORTED_USER_ID,
                added_by_name,
                added_by_username.lstrip("@"),
                added_at,
            ),
        )
        conn.commit()
        if cur.lastrowid is None:
            raise RuntimeError("seed insert did not return id")
        return int(cur.lastrowid)


# Admin setting: 0 = count every vote (default); 1 = only votes from users
# whose running attendance surplus is at least 1. Surplus is walked in
# meeting order: visit +1, miss −1, never below 0. Coming back after a
# long gap can restore voting. Meetings dated after today (club display
# timezone) are ignored until that date. With no past meetings recorded,
# every vote still counts (otherwise the list would go empty).
VOTES_USE_ATTENDANCE_KEY = "votes_use_attendance"
_votes_use_attendance_cache: bool | None = None

# user_id → surplus after the last meeting on or before _attendance_as_of.
# Rebuilt at startup, whenever a meeting is recorded, and when the calendar
# date changes. Users who never attended are omitted (surplus 0).
_attendance_surplus: dict[int, int] = {}
_attendance_meeting_count: int = 0
_attendance_as_of: str = ""


def club_today_date() -> str:
    """Club calendar date (YYYY-MM-DD) in the configured display timezone."""
    return datetime.now(config.display_timezone()).strftime("%Y-%m-%d")


def db_votes_use_attendance() -> bool:
    global _votes_use_attendance_cache
    if _votes_use_attendance_cache is None:
        _votes_use_attendance_cache = (
            db_get_admin_setting(VOTES_USE_ATTENDANCE_KEY, 0) == 1
        )
    return _votes_use_attendance_cache


def db_rebuild_attendance_surplus() -> None:
    """Precompute running attendance surplus for every known attendee."""
    global _attendance_surplus, _attendance_meeting_count, _attendance_as_of
    today = club_today_date()
    with sqlite3.connect(config.DB_PATH) as conn:
        meetings = conn.execute(
            """SELECT id FROM meetings
               WHERE meeting_date <= ?
               ORDER BY meeting_date ASC, id ASC""",
            (today,),
        ).fetchall()
        _attendance_meeting_count = len(meetings)
        _attendance_as_of = today
        if not meetings:
            _attendance_surplus = {}
            return
        attendees_by_meeting: dict[int, set[int]] = {row[0]: set() for row in meetings}
        all_users: set[int] = set()
        for meeting_id, user_id in conn.execute(
            "SELECT meeting_id, user_id FROM meeting_attendees"
        ):
            present = attendees_by_meeting.get(meeting_id)
            if present is None:
                continue
            present.add(user_id)
            all_users.add(user_id)
        surplus = dict.fromkeys(all_users, 0)
        for (meeting_id,) in meetings:
            present = attendees_by_meeting[meeting_id]
            for user_id in all_users:
                if user_id in present:
                    surplus[user_id] += 1
                elif surplus[user_id] > 0:
                    surplus[user_id] -= 1
        _attendance_surplus = surplus


def _ensure_attendance_surplus_current() -> None:
    if _attendance_as_of != club_today_date():
        db_rebuild_attendance_surplus()


def db_attendance_surplus(user_id: int) -> int:
    _ensure_attendance_surplus_current()
    return _attendance_surplus.get(user_id, 0)


def _votes_join_sql() -> str:
    _ensure_attendance_surplus_current()
    if not db_votes_use_attendance() or _attendance_meeting_count == 0:
        return "LEFT JOIN votes v ON b.id = v.book_id"
    eligible = [uid for uid, score in _attendance_surplus.items() if score >= 1]
    if not eligible:
        return "LEFT JOIN votes v ON b.id = v.book_id AND 0"
    ids = ",".join(str(int(uid)) for uid in eligible)
    return f"LEFT JOIN votes v ON b.id = v.book_id AND v.user_id IN ({ids})"


def _books_query(
    extra_where: str = "",
    order: str = "avg_score DESC, vote_count DESC, b.added_at DESC",
) -> str:
    # Note: avg_score is actually the SUM of weighted scores:
    # 1.0 for 'want', 0.5 for 'don''t care', -1.0 for 'don''t want'
    return f"""
        SELECT b.*,
               COALESCE(
                    SUM(
                        CASE    
                            WHEN v.score = 1  THEN 1
                            WHEN v.score = 0  THEN 0.5
                            WHEN v.score = -1 THEN -1
                        END
                    ), 
                0) AS avg_score,
               COUNT(v.user_id)                                    AS vote_count,
               COALESCE(SUM(CASE WHEN v.score=1  THEN 1 ELSE 0 END),0) AS votes_yes,
               COALESCE(SUM(CASE WHEN v.score=0  THEN 1 ELSE 0 END),0) AS votes_meh,
               COALESCE(SUM(CASE WHEN v.score=-1 THEN 1 ELSE 0 END),0) AS votes_no
        FROM books b
        {_votes_join_sql()}
        {extra_where}
        GROUP BY b.id
        ORDER BY {order}
    """


def db_get_books(
    discussed: bool = False,
    user_id_unvoted: int | None = None,
    include_hidden: bool = False,
) -> list[sqlite3.Row]:
    """Return books. discussed=False → undiscussed, discussed=True → discussed.
    If user_id_unvoted is provided, only return books that this user has NOT voted for yet.
    """
    flag = 1 if discussed else 0
    where = "WHERE b.discussed = ?"
    params = [flag]
    if not include_hidden:
        where += " AND b.hidden = 0"
    if user_id_unvoted:
        where += " AND b.id NOT IN (SELECT book_id FROM votes WHERE user_id = ?)"
        params.append(user_id_unvoted)

    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn.execute(
            _books_query(
                where,
                (
                    "b.discussed_at DESC"
                    if discussed
                    else "avg_score DESC, vote_count DESC, b.added_at DESC"
                ),
            ),
            tuple(params),
        ).fetchall()


def db_get_books_metadata(
    discussed: bool = False, *, include_hidden: bool = False
) -> list[sqlite3.Row]:
    """Return book rows without vote aggregation for picker/list metadata."""
    where = "WHERE discussed = ?"
    params: list[int] = [1 if discussed else 0]
    if not include_hidden:
        where += " AND hidden = 0"
    order = "discussed_at DESC" if discussed else "added_at DESC"
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            f"SELECT * FROM books {where} ORDER BY {order}", tuple(params)
        ).fetchall()


def db_get_book(book_id: int) -> sqlite3.Row | None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return cast(
            sqlite3.Row | None,
            conn.execute(_books_query("WHERE b.id = ?"), (book_id,)).fetchone(),
        )


def db_book_is_votable(book_id: int) -> bool:
    """Return whether a book exists and still accepts votes."""
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT discussed, hidden FROM books WHERE id=?", (book_id,)
        ).fetchone()
    return bool(row is not None and not row[0] and not row[1])


TITLE_SIMILARITY_THRESHOLD = 0.5


def title_words(title: str) -> set[str]:
    return {w for w in re.split(r"\W+", title.casefold()) if w}


def title_word_similarity_ratio(a: str, b: str) -> float:
    """Share of words in the longer title that also appear in the other title."""
    wa, wb = title_words(a), title_words(b)
    if not wa or not wb:
        return 0.0
    overlap = len(wa & wb)
    return overlap / max(len(wa), len(wb))


def find_similar_book_titles(
    new_title: str,
    *,
    min_ratio: float = TITLE_SIMILARITY_THRESHOLD,
) -> list[tuple[int, str, float]]:
    """Return existing books whose titles share enough words with new_title."""
    with sqlite3.connect(config.DB_PATH) as conn:
        rows = conn.execute("SELECT id, title FROM books").fetchall()
    matches: list[tuple[int, str, float]] = []
    for book_id, title in rows:
        ratio = title_word_similarity_ratio(new_title, str(title))
        if ratio >= min_ratio:
            matches.append((int(book_id), str(title), ratio))
    matches.sort(key=lambda m: (-m[2], m[1].casefold()))
    return matches


def db_update_book_field(book_id: int, field: str, value: Any) -> None:
    """Update a single whitelisted field."""
    allowed = {
        "title",
        "author",
        "pages",
        "fiction",
        "review_link",
        "description",
        "original_language",
        "creation_year",
        "language_levels",
    }
    if field not in allowed:
        raise ValueError(f"Field {field!r} not editable")
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        # Field name is whitelisted above, so this is safe from injection.
        conn.execute(f"UPDATE books SET {field}=? WHERE id=?", (value, book_id))
        conn.commit()


def db_mark_discussed(book_id: int, date_str: str) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "UPDATE books SET discussed=1, discussed_at=? WHERE id=?",
            (date_str, book_id),
        )
        conn.commit()


def db_set_discussed_at(book_id: int, date_str: str) -> bool:
    """Update discussion date for an already-discussed book. Returns False if not found."""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            "UPDATE books SET discussed_at=? WHERE id=? AND discussed=1",
            (date_str, book_id),
        )
        conn.commit()
        return cur.rowcount > 0


def db_set_hidden(book_id: int, hidden: bool) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "UPDATE books SET hidden = ? WHERE id = ?",
            (int(hidden), book_id),
        )
        conn.commit()


def db_set_new_book_notify_pending(
    book_id: int, adder_id: int, notify_after: datetime
) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """UPDATE books SET notify_sent=0, notify_adder_id=?, notify_after=?
               WHERE id=?""",
            (adder_id, notify_after.isoformat(timespec="seconds"), book_id),
        )
        conn.commit()


def db_mark_new_book_notify_done(book_id: int) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "UPDATE books SET notify_sent=1 WHERE id=?",
            (book_id,),
        )
        conn.commit()


def db_begin_new_book_notify(book_id: int) -> int | None:
    """Mark notify as sent; return adder to exclude from blast, or None if already done."""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute(
            "SELECT notify_adder_id FROM books WHERE id=? AND notify_sent=0",
            (book_id,),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE books SET notify_sent=1 WHERE id=? AND notify_sent=0",
            (book_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return int(row[0]) if row[0] is not None else 0


def db_get_books_pending_notify() -> list[sqlite3.Row]:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn.execute("""SELECT b.* FROM books b
               WHERE b.notify_sent=0 AND b.notify_after IS NOT NULL""").fetchall()


def db_toggle_hidden(book_id: int) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "UPDATE books SET hidden = 1 - hidden WHERE id = ?",
            (book_id,),
        )
        conn.commit()


def db_delete_book(book_id: int) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM books WHERE id=?", (book_id,))
        conn.commit()


def db_upsert_club_user(
    user_id: int,
    full_name: str = "",
    username: str | None = None,
) -> None:
    if user_id <= 0:
        return
    name = str(full_name or "").strip()
    uname = username.strip() if isinstance(username, str) and username.strip() else None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """INSERT INTO club_users (user_id, full_name, username, last_seen_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name
                                  ELSE club_users.full_name END,
                 username = COALESCE(excluded.username, club_users.username),
                 last_seen_at = excluded.last_seen_at""",
            (user_id, name, uname, now),
        )
        conn.commit()


def db_meeting_user_suggestions(book_id: int) -> list[sqlite3.Row]:
    """Users to suggest as attendees: most meetings first, then shown name."""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT cu.user_id, cu.full_name, cu.username,
                      CASE WHEN v.user_id IS NOT NULL THEN 1 ELSE 0 END AS voted,
                      COALESCE(ac.attendance_count, 0) AS attendance_count
               FROM club_users cu
               LEFT JOIN votes v ON v.user_id = cu.user_id AND v.book_id = ?
               LEFT JOIN (
                   SELECT user_id, COUNT(*) AS attendance_count
                   FROM meeting_attendees
                   GROUP BY user_id
               ) ac ON ac.user_id = cu.user_id""",
            (book_id,),
        ).fetchall()
    return sorted(
        rows,
        key=lambda r: (
            -int(r["attendance_count"]),
            format_club_user_display(
                int(r["user_id"]), r["full_name"], r["username"]
            ).casefold(),
            int(r["user_id"]),
        ),
    )


def db_create_meeting(
    book_id: int,
    meeting_date: str,
    created_by: int,
    attendee_ids: Sequence[int],
) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            """INSERT INTO meetings (book_id, meeting_date, created_at, created_by)
               VALUES (?,?,?,?)""",
            (book_id, meeting_date, now, created_by),
        )
        meeting_id = cur.lastrowid
        if meeting_id is None:
            raise RuntimeError("meeting insert did not return id")
        meeting_id = int(meeting_id)
        for uid in attendee_ids:
            if uid > 0:
                conn.execute(
                    "INSERT OR IGNORE INTO meeting_attendees (meeting_id, user_id) VALUES (?,?)",
                    (meeting_id, uid),
                )
        conn.commit()
    db_rebuild_attendance_surplus()
    return meeting_id


def db_get_meeting_for_book(book_id: int) -> sqlite3.Row | None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return cast(
            sqlite3.Row | None,
            conn.execute(
                """SELECT m.*, b.title, b.author
               FROM meetings m
               JOIN books b ON b.id = m.book_id
               WHERE m.book_id = ?""",
                (book_id,),
            ).fetchone(),
        )


def db_set_meeting_attendees(meeting_id: int, attendee_ids: Sequence[int]) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM meeting_attendees WHERE meeting_id=?", (meeting_id,))
        for uid in attendee_ids:
            if uid > 0:
                conn.execute(
                    "INSERT OR IGNORE INTO meeting_attendees (meeting_id, user_id) VALUES (?,?)",
                    (meeting_id, uid),
                )
        conn.commit()
    db_rebuild_attendance_surplus()


def db_save_meeting(
    book_id: int,
    meeting_date: str,
    created_by: int,
    attendee_ids: Sequence[int],
) -> int:
    """Create the meeting for this book, or replace attendees if one already exists."""
    existing = db_get_meeting_for_book(book_id)
    if existing is None:
        return db_create_meeting(book_id, meeting_date, created_by, attendee_ids)
    meeting_id = int(existing["id"])
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "UPDATE meetings SET meeting_date=? WHERE id=?",
            (meeting_date, meeting_id),
        )
        conn.commit()
    db_set_meeting_attendees(meeting_id, attendee_ids)
    return meeting_id


def db_delete_meeting(meeting_id: int) -> bool:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        conn.commit()
        deleted = cur.rowcount > 0
    if deleted:
        db_rebuild_attendance_surplus()
    return deleted


def db_list_meetings() -> list[sqlite3.Row]:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn.execute("""SELECT m.id, m.book_id, m.meeting_date, m.created_at,
                      b.title, b.author,
                      (SELECT COUNT(*) FROM meeting_attendees ma WHERE ma.meeting_id = m.id)
                        AS attendee_count
               FROM meetings m
               JOIN books b ON b.id = m.book_id
               ORDER BY m.meeting_date DESC, m.id DESC""").fetchall()


def db_get_meeting(meeting_id: int) -> sqlite3.Row | None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return cast(
            sqlite3.Row | None,
            conn.execute(
                """SELECT m.*, b.title, b.author
               FROM meetings m
               JOIN books b ON b.id = m.book_id
               WHERE m.id = ?""",
                (meeting_id,),
            ).fetchone(),
        )


def db_get_meeting_attendee_rows(meeting_id: int) -> list[sqlite3.Row]:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """SELECT ma.user_id, cu.full_name, cu.username
               FROM meeting_attendees ma
               LEFT JOIN club_users cu ON cu.user_id = ma.user_id
               WHERE ma.meeting_id = ?
               ORDER BY cu.full_name COLLATE NOCASE, ma.user_id""",
            (meeting_id,),
        ).fetchall()


def club_user_has_shown_name(full_name: str | None, username: str | None) -> bool:
    return bool((full_name or "").strip() or (username or "").strip())


def format_club_user_display(
    user_id: int, full_name: str | None, username: str | None
) -> str:
    """Prefer Telegram shown name, then @username; numeric ID only as last resort."""
    name = (full_name or "").strip()
    uname = (username or "").strip()
    if name and uname:
        return f"{name} (@{uname})"
    if name:
        return name
    if uname:
        return f"@{uname}"
    return str(user_id)


BOOK_EXPORT_FORMAT = "bookclub-bot-book"
BOOK_EXPORT_VERSION = 1


def book_to_export_payload(book: BookLike) -> str:
    """Serialize a book row to JSON for transfer to another bot instance."""
    payload = {
        "format": BOOK_EXPORT_FORMAT,
        "version": BOOK_EXPORT_VERSION,
        "entity": config.CLUB_ENTITY,
        "book": {
            "title": book["title"],
            "author": book["author"],
            "pages": int(book["pages"]),
            "fiction": bool(book["fiction"]),
            "review_link": book["review_link"] or "",
            "description": book["description"] or "",
            "original_language": book["original_language"] or "",
            "creation_year": book["creation_year"],
            "language_levels": book["language_levels"] or "",
            "hidden": bool(book["hidden"]),
            "discussed": bool(book["discussed"]),
            "discussed_at": book["discussed_at"],
            "added_by_name": book["added_by_name"],
            "added_by_username": book["added_by_username"],
            "added_at": book["added_at"],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_exported_book(raw: Mapping[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title", "")).strip()
    author = str(raw.get("author", "")).strip()
    if not title or not author:
        raise ValueError("missing title or author")
    try:
        pages = int(raw.get("pages", 0))
    except (TypeError, ValueError) as e:
        raise ValueError("invalid pages") from e
    if pages < 0:
        raise ValueError("invalid pages")
    fiction_raw = raw.get("fiction", True)
    if isinstance(fiction_raw, bool):
        fiction = int(fiction_raw)
    else:
        fiction = 1 if int(fiction_raw) else 0
    review_link = str(raw.get("review_link", "") or "")
    description = str(raw.get("description", "") or "")
    original_language = str(raw.get("original_language", "") or "").strip() or None
    creation_year_raw = raw.get("creation_year")
    creation_year: int | None
    if creation_year_raw is None or creation_year_raw == "":
        creation_year = None
    else:
        try:
            creation_year = int(creation_year_raw)
        except (TypeError, ValueError) as e:
            raise ValueError("invalid creation_year") from e
        if creation_year < _CREATION_YEAR_MIN or creation_year > _CREATION_YEAR_MAX:
            raise ValueError("invalid creation_year")
    language_levels_raw = raw.get("language_levels", "")
    if language_levels_raw is None or language_levels_raw == "":
        language_levels = None
    else:
        language_levels = format_language_levels(str(language_levels_raw))
    hidden = 1 if raw.get("hidden") else 0
    discussed = 1 if raw.get("discussed") else 0
    discussed_at = raw.get("discussed_at")
    if discussed_at is not None:
        discussed_at = str(discussed_at).strip() or None
    added_by_name = str(raw.get("added_by_name", "imported") or "imported").strip()
    username = raw.get("added_by_username")
    if username is not None:
        username = str(username).lstrip("@").strip() or None
    added_at = str(raw.get("added_at", "") or "").strip()
    if not added_at:
        added_at = datetime.now().strftime("%Y-%m-%d")
    return {
        "title": title,
        "author": author,
        "pages": pages,
        "fiction": fiction,
        "review_link": review_link,
        "description": description,
        "original_language": original_language,
        "creation_year": creation_year,
        "language_levels": language_levels,
        "hidden": hidden,
        "discussed": discussed,
        "discussed_at": discussed_at,
        "added_by_name": added_by_name,
        "added_by_username": username,
        "added_at": added_at,
    }


def parse_book_import(text: str) -> tuple[dict[str, Any], str | None]:
    """Parse export JSON. Returns (normalized book fields, source entity or None)."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty payload")
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError("invalid JSON") from e
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    source_entity: str | None = None
    if isinstance(data.get("book"), dict):
        raw = data["book"]
        ent = data.get("entity")
        if isinstance(ent, str):
            source_entity = ent
        fmt = data.get("format")
        if fmt is not None and fmt != BOOK_EXPORT_FORMAT:
            raise ValueError(f"unknown format {fmt!r}")
        version = data.get("version")
        if version is not None and version != BOOK_EXPORT_VERSION:
            raise ValueError(f"unsupported version {version!r}")
    elif "title" in data and "author" in data:
        raw = data
    else:
        raise ValueError("missing book object")
    return _normalize_exported_book(raw), source_entity


def db_import_book(book_data: Mapping[str, Any]) -> int:
    """Insert a book from export data. Votes are not imported."""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            """INSERT INTO books
               (title, author, pages, fiction, review_link, description,
                original_language, creation_year, language_levels,
                hidden, discussed, discussed_at,
                added_by, added_by_name, added_by_username, added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                book_data["title"],
                book_data["author"],
                book_data["pages"],
                book_data["fiction"],
                book_data["review_link"],
                book_data["description"],
                book_data.get("original_language"),
                book_data.get("creation_year"),
                book_data.get("language_levels"),
                book_data["hidden"],
                book_data["discussed"],
                book_data["discussed_at"],
                IMPORTED_USER_ID,
                book_data["added_by_name"],
                book_data["added_by_username"],
                book_data["added_at"],
            ),
        )
        conn.commit()
        if cur.lastrowid is None:
            raise RuntimeError("import insert did not return id")
        return int(cur.lastrowid)


def db_cast_vote(user_id: int, book_id: int, score: int) -> int | None:
    """Store a vote and return its previous value, if any."""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        previous = conn.execute(
            "SELECT score FROM votes WHERE user_id=? AND book_id=?",
            (user_id, book_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO votes (user_id,book_id,score) VALUES (?,?,?) "
            "ON CONFLICT(user_id,book_id) DO UPDATE SET score=excluded.score",
            (user_id, book_id, score),
        )
        conn.commit()
        return int(previous[0]) if previous else None


def db_get_user_vote(user_id: int, book_id: int) -> int | None:
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT score FROM votes WHERE user_id=? AND book_id=?", (user_id, book_id)
        ).fetchone()
        return row[0] if row else None


def db_get_user_votes(user_id: int, book_ids: Sequence[int]) -> dict[int, int]:
    """Load one user's votes for a set of books in a single query."""
    ids = [int(book_id) for book_id in book_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with sqlite3.connect(config.DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT book_id, score FROM votes "
            f"WHERE user_id=? AND book_id IN ({placeholders})",
            (user_id, *ids),
        ).fetchall()
    return {int(book_id): int(score) for book_id, score in rows}


def db_get_voted_pairs(
    user_ids: Sequence[int], book_ids: Sequence[int]
) -> set[tuple[int, int]]:
    """Load existing (user, book) vote pairs in bounded batched queries."""
    users = [int(user_id) for user_id in user_ids]
    books = [int(book_id) for book_id in book_ids]
    if not users or not books:
        return set()
    pairs: set[tuple[int, int]] = set()
    chunk_size = 450
    with sqlite3.connect(config.DB_PATH) as conn:
        for user_start in range(0, len(users), chunk_size):
            user_chunk = users[user_start : user_start + chunk_size]
            user_placeholders = ",".join("?" for _ in user_chunk)
            for book_start in range(0, len(books), chunk_size):
                book_chunk = books[book_start : book_start + chunk_size]
                book_placeholders = ",".join("?" for _ in book_chunk)
                rows = conn.execute(
                    f"SELECT user_id, book_id FROM votes "
                    f"WHERE user_id IN ({user_placeholders}) "
                    f"AND book_id IN ({book_placeholders})",
                    (*user_chunk, *book_chunk),
                ).fetchall()
                pairs.update((int(user_id), int(book_id)) for user_id, book_id in rows)
    return pairs


def db_get_users_missing_votes(
    user_ids: Sequence[int], book_ids: Sequence[int]
) -> list[int]:
    """Return users missing at least one selected-book vote."""
    users = [int(user_id) for user_id in user_ids]
    books = [int(book_id) for book_id in book_ids]
    voted = db_get_voted_pairs(users, books)
    return [
        user_id
        for user_id in users
        if any((user_id, book_id) not in voted for book_id in books)
    ]


def db_get_user_setting(user_id: int, key: str, default: int = -1) -> int:
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT setting_val FROM user_settings WHERE user_id=? AND setting_key=?",
            (user_id, key),
        ).fetchone()
        return row[0] if row is not None else default


def db_set_user_setting(user_id: int, key: str, value: int) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, setting_key, setting_val) VALUES (?,?,?) "
            "ON CONFLICT(user_id, setting_key) DO UPDATE SET setting_val=excluded.setting_val",
            (user_id, key, value),
        )
        conn.commit()


def db_get_users_with_setting(key: str, value: int) -> list[int]:
    with sqlite3.connect(config.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM user_settings WHERE setting_key=? AND setting_val=?",
            (key, value),
        ).fetchall()
        return [r[0] for r in rows]


ADMIN_USER_ID = 0


def db_get_admin_setting(key: str, default: int = 0) -> int:
    return db_get_user_setting(ADMIN_USER_ID, key, default)


def db_set_admin_setting(key: str, value: int) -> None:
    global _votes_use_attendance_cache
    db_set_user_setting(ADMIN_USER_ID, key, value)
    if key == VOTES_USE_ATTENDANCE_KEY:
        _votes_use_attendance_cache = value == 1
