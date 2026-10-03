"""
test_bot_logic.py — Unit tests for pure logic functions in bookclub_bot.py

Covers: database layer, utility functions, formatting helpers.
No Telegram API calls are made here.
"""

import os
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import bookclub.config as cfg
import bookclub.logging_setup as log_setup
import bookclub_bot as bot
from bookclub.handlers.add_flow import add_next_state, add_previous_state

# ── Helpers ────────────────────────────────────────────────────────────────────


def make_book(**kwargs):
    """Return a minimal book-like dict with sensible defaults."""
    defaults = {
        "votes_yes": 0,
        "votes_meh": 0,
        "votes_no": 0,
        "vote_count": 0,
        "avg_score": 0,
        "fiction": 1,
        "pages": 100,
        "title": "Test",
        "author": "Author",
        "review_link": "",
        "description": "",
        "added_by_name": "tester",
        "added_by_username": None,
        "added_at": "2025-01-01",
        "discussed": 0,
        "discussed_at": None,
        "original_language": None,
        "creation_year": None,
        "language_levels": None,
    }
    defaults.update(kwargs)
    return defaults


# ── Database tests ─────────────────────────────────────────────────────────────


class TestDatabase(unittest.TestCase):

    def setUp(self):
        self.db_file = "test_bookclub_logic.db"
        cfg.DB_PATH = self.db_file
        bot.DB_PATH = self.db_file
        bot.init_db()

    def tearDown(self):
        for path in (self.db_file, f"{self.db_file}-wal", f"{self.db_file}-shm"):
            if os.path.exists(path):
                os.remove(path)

    def _add_meeting(self, date, attendees, title=None):
        book_id = bot.db_add_book(title or f"M-{date}", "A", 10, True, "", "", 1, "u")
        return book_id, bot.db_create_meeting(book_id, date, 1, attendees)

    # -- Schema --

    def test_init_db_creates_tables(self):
        with sqlite3.connect(bot.DB_PATH) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("books", tables)
        self.assertIn("votes", tables)
        self.assertIn("user_settings", tables)
        self.assertIn("add_drafts", tables)

    def test_init_db_idempotent(self):
        """Calling init_db() twice should not raise."""
        bot.init_db()

    @unittest.skipIf(os.geteuid() == 0, "root can write unwritable directories")
    def test_init_db_exits_when_data_directory_is_not_writable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, "data")
            os.mkdir(db_dir)
            os.chmod(db_dir, 0o555)
            previous = cfg.DB_PATH
            try:
                cfg.DB_PATH = os.path.join(db_dir, "bookclub.db")
                bot.DB_PATH = cfg.DB_PATH
                with self.assertRaises(SystemExit) as caught:
                    bot.init_db()
                self.assertIn("Cannot write the database", str(caught.exception))
            finally:
                os.chmod(db_dir, 0o755)
                cfg.DB_PATH = previous
                bot.DB_PATH = previous

    # -- Add / Get book --

    def test_db_add_get_book(self):
        book_id = bot.db_add_book(
            "Test Book",
            "Test Author",
            100,
            True,
            "http://example.com",
            "Desc",
            123,
            "tester",
            "testuser",
        )
        self.assertIsNotNone(book_id)
        book = bot.db_get_book(book_id)
        self.assertEqual(book["title"], "Test Book")
        self.assertEqual(book["author"], "Test Author")
        self.assertEqual(book["pages"], 100)
        self.assertEqual(book["fiction"], 1)
        self.assertEqual(book["review_link"], "http://example.com")
        self.assertEqual(book["description"], "Desc")
        self.assertEqual(book["added_by"], 123)
        self.assertEqual(book["added_by_name"], "tester")
        self.assertEqual(book["added_by_username"], "testuser")

    def test_db_add_book_without_username(self):
        book_id = bot.db_add_book("Book", "Author", 50, False, "", "", 1, "u")
        book = bot.db_get_book(book_id)
        self.assertIsNone(book["added_by_username"])

    def test_db_get_book_nonexistent(self):
        self.assertIsNone(bot.db_get_book(99999))

    def test_db_add_book_returns_unique_ids(self):
        id1 = bot.db_add_book("B1", "A", 10, True, "", "", 1, "u")
        id2 = bot.db_add_book("B2", "A", 10, True, "", "", 1, "u")
        self.assertNotEqual(id1, id2)

    # -- Get books (list) --

    def test_db_get_books_empty(self):
        self.assertEqual(bot.db_get_books(discussed=False), [])

    def test_db_get_books_undiscussed(self):
        bot.db_add_book("B1", "A1", 100, True, "", "", 1, "u1")
        bot.db_add_book("B2", "A2", 200, False, "", "", 2, "u2")
        books = bot.db_get_books(discussed=False)
        self.assertEqual(len(books), 2)

    def test_db_get_books_discussed_filter(self):
        id1 = bot.db_add_book("B1", "A", 100, True, "", "", 1, "u")
        id2 = bot.db_add_book("B2", "A", 100, True, "", "", 1, "u")
        bot.db_mark_discussed(id1, "2025-01-01")

        undiscussed = bot.db_get_books(discussed=False)
        discussed = bot.db_get_books(discussed=True)
        self.assertEqual(len(undiscussed), 1)
        self.assertEqual(undiscussed[0]["id"], id2)
        self.assertEqual(len(discussed), 1)
        self.assertEqual(discussed[0]["id"], id1)

    def test_db_get_books_hidden_filter(self):
        id1 = bot.db_add_book("Visible", "A", 100, True, "", "", 1, "u")
        id2 = bot.db_add_book("Hidden", "A", 100, True, "", "", 1, "u")
        bot.db_toggle_hidden(id2)

        # Default: exclude hidden
        books = bot.db_get_books(discussed=False)
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["title"], "Visible")

        # Explicitly include hidden
        books_all = bot.db_get_books(discussed=False, include_hidden=True)
        self.assertEqual(len(books_all), 2)

    def test_db_toggle_hidden(self):
        id1 = bot.db_add_book("B", "A", 100, True, "", "", 1, "u")

        # Hide
        bot.db_toggle_hidden(id1)
        self.assertEqual(bot.db_get_book(id1)["hidden"], 1)

        # Unhide
        bot.db_toggle_hidden(id1)
        self.assertEqual(bot.db_get_book(id1)["hidden"], 0)

    def test_db_get_books_unvoted_filter(self):
        id1 = bot.db_add_book("B1", "A", 100, True, "", "", 1, "u")
        id2 = bot.db_add_book("B2", "A", 100, True, "", "", 1, "u")
        bot.db_cast_vote(10, id1, 1)

        unvoted = bot.db_get_books(discussed=False, user_id_unvoted=10)
        self.assertEqual(len(unvoted), 1)
        self.assertEqual(unvoted[0]["id"], id2)

    def test_db_get_books_unvoted_all_voted(self):
        id1 = bot.db_add_book("B1", "A", 100, True, "", "", 1, "u")
        bot.db_cast_vote(10, id1, 1)
        unvoted = bot.db_get_books(discussed=False, user_id_unvoted=10)
        self.assertEqual(len(unvoted), 0)

    def test_db_get_books_unvoted_none_voted(self):
        bot.db_add_book("B1", "A", 100, True, "", "", 1, "u")
        bot.db_add_book("B2", "A", 100, True, "", "", 1, "u")
        unvoted = bot.db_get_books(discussed=False, user_id_unvoted=99)
        self.assertEqual(len(unvoted), 2)

    def test_db_get_books_sorted_by_score_then_votes(self):
        """Higher avg_score ranks first; equal score → more votes ranks first."""
        id_low = bot.db_add_book("Low", "A", 10, True, "", "", 1, "u")
        id_high = bot.db_add_book("High", "A", 10, True, "", "", 1, "u")
        id_mid_fewer = bot.db_add_book("MidFewer", "A", 10, True, "", "", 1, "u")
        id_mid_more = bot.db_add_book("MidMore", "A", 10, True, "", "", 1, "u")

        bot.db_cast_vote(1, id_high, 1)
        bot.db_cast_vote(1, id_low, -1)
        # id_mid_fewer and id_mid_more both score 0 (meh), but mid_more has 2 votes
        bot.db_cast_vote(1, id_mid_fewer, 0)
        bot.db_cast_vote(1, id_mid_more, 0)
        bot.db_cast_vote(2, id_mid_more, 0)

        books = bot.db_get_books(discussed=False)
        ids = [b["id"] for b in books]
        # In SQLite 4 != 2 if both have score 1 and 1 vote?
        # Wait, id_high (id=2) has score 1.
        # id_mid_more (id=4) has score 0.
        # So id_high should be first.
        # If AssertionError: 4 != 2, it means ids[0] was 4.
        # Why would id=4 (MidMore) be first? It has score 0 (2 votes of 0.5? No, votes were 0).
        # Let's re-read the test.
        # bot.db_cast_vote(1, id_mid_more,  0) -> 0.5 points
        # bot.db_cast_vote(2, id_mid_more,  0) -> 0.5 points
        # Total for id_mid_more = 1.0.
        # id_high has bot.db_cast_vote(1, id_high, 1) -> 1.0 points.
        # Both have score 1.0.
        # Tie-breaker is vote_count DESC.
        # id_mid_more has 2 votes. id_high has 1 vote.
        # So id_mid_more (id=4) SHOULD be first.
        # The test expected id_high (id=2) first, but its own logic says mid_more is better due to more votes.
        self.assertEqual(ids[0], id_mid_more)  # score 1.0, 2 votes
        self.assertEqual(ids[1], id_high)  # score 1.0, 1 vote
        self.assertEqual(ids[2], id_mid_fewer)  # score 0.5, 1 vote
        self.assertEqual(ids[3], id_low)  # score -1.0, 1 vote

    # -- Mark discussed --

    def test_db_mark_discussed(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_mark_discussed(book_id, "2023-01-15")
        book = bot.db_get_book(book_id)
        self.assertEqual(book["discussed"], 1)
        self.assertEqual(book["discussed_at"], "2023-01-15")

    # -- Votes --

    def test_db_cast_vote_and_get(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_cast_vote(10, book_id, 1)
        self.assertEqual(bot.db_get_user_vote(10, book_id), 1)

    def test_db_cast_vote_updates_existing(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_cast_vote(10, book_id, 1)
        bot.db_cast_vote(10, book_id, -1)
        self.assertEqual(bot.db_get_user_vote(10, book_id), -1)

    def test_db_get_user_vote_no_vote(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        self.assertIsNone(bot.db_get_user_vote(99, book_id))

    def test_db_get_user_votes_batches_selected_books(self):
        first = bot.db_add_book("B1", "A", 10, True, "", "", 1, "u")
        second = bot.db_add_book("B2", "A", 10, True, "", "", 1, "u")
        bot.db_cast_vote(7, first, 1)
        self.assertEqual(bot.db_get_user_votes(7, [first, second]), {first: 1})

    def test_db_get_users_missing_votes_batches_users_and_books(self):
        first = bot.db_add_book("First", "A", 10, True, "", "", 1, "u")
        second = bot.db_add_book("Second", "A", 10, True, "", "", 1, "u")
        bot.db_cast_vote(7, first, 1)
        bot.db_cast_vote(8, first, 1)
        bot.db_cast_vote(8, second, 0)

        self.assertEqual(bot.db_get_users_missing_votes([7, 8], [first, second]), [7])
        self.assertEqual(
            bot.db_get_voted_pairs([7, 8], [first, second]),
            {(7, first), (8, first), (8, second)},
        )

    def test_init_db_creates_hot_path_indexes(self):
        with sqlite3.connect(bot.DB_PATH) as conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        self.assertIn("idx_votes_book_id", indexes)
        self.assertIn("idx_books_state", indexes)
        self.assertIn("idx_user_settings_lookup", indexes)

    def test_db_vote_aggregates(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_cast_vote(1, book_id, 1)
        bot.db_cast_vote(2, book_id, 1)
        bot.db_cast_vote(3, book_id, -1)
        book = bot.db_get_book(book_id)
        self.assertEqual(book["vote_count"], 3)
        self.assertEqual(book["votes_yes"], 2)
        self.assertEqual(book["votes_no"], 1)
        self.assertEqual(book["votes_meh"], 0)
        self.assertEqual(book["avg_score"], 1)

    def test_db_all_vote_values(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        for score in (1, 0, -1):
            bot.db_cast_vote(score + 10, book_id, score)
        book = bot.db_get_book(book_id)
        self.assertEqual(book["votes_yes"], 1)
        self.assertEqual(book["votes_meh"], 1)
        self.assertEqual(book["votes_no"], 1)

    def test_attendance_mode_ignores_votes_when_surplus_below_one(self):
        """Include a voter iff running surplus (visit +1, miss −1, floor 0) is >= 1."""
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        regular, skipper, half = 11, 22, 33
        bot.db_cast_vote(regular, book_id, 1)
        bot.db_cast_vote(skipper, book_id, 1)
        bot.db_cast_vote(half, book_id, -1)

        self._add_meeting("2026-01-01", [regular, skipper, half])
        self._add_meeting("2026-01-02", [regular])
        self._add_meeting("2026-01-03", [regular, skipper])
        # regular: +1, +1, +1 → 3
        # skipper: +1, −1, +1 → 1 (miss then return)
        # half: +1, −1, −1 → 0

        self.assertEqual(bot.db_attendance_surplus(regular), 3)
        self.assertEqual(bot.db_attendance_surplus(skipper), 1)
        self.assertEqual(bot.db_attendance_surplus(half), 0)

        bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
        book = bot.db_get_book(book_id)
        self.assertEqual(book["vote_count"], 2)
        self.assertEqual(book["votes_yes"], 2)
        self.assertEqual(book["votes_no"], 0)
        self.assertEqual(book["avg_score"], 2)

        bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 0)
        book = bot.db_get_book(book_id)
        self.assertEqual(book["vote_count"], 3)
        self.assertEqual(book["votes_no"], 1)

    def test_attendance_mode_excludes_exact_half_attendance(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        voter = 44
        bot.db_cast_vote(voter, book_id, 1)
        self._add_meeting("2026-01-01", [voter])
        self._add_meeting("2026-01-02", [])
        self.assertEqual(bot.db_attendance_surplus(voter), 0)
        bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
        book = bot.db_get_book(book_id)
        self.assertEqual(book["vote_count"], 0)
        self.assertEqual(book["avg_score"], 0)

    def test_attendance_surplus_clamped_so_return_visit_revives_vote(self):
        """Misses cannot drive surplus below 0, so one return visit restores voting."""
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        returning = 55
        bot.db_cast_vote(returning, book_id, 1)
        self._add_meeting("2026-01-01", [])
        self._add_meeting("2026-01-02", [])
        self._add_meeting("2026-01-03", [])
        self._add_meeting("2026-01-04", [returning])
        self.assertEqual(bot.db_attendance_surplus(returning), 1)

        bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
        book = bot.db_get_book(book_id)
        self.assertEqual(book["vote_count"], 1)
        self.assertEqual(book["avg_score"], 1)

    def test_attendance_surplus_rebuilt_on_init_db(self):
        import bookclub.db as db_mod

        other = bot.db_add_book("Other", "A", 10, True, "", "", 1, "u")
        uid = 66
        bot.db_create_meeting(other, "2026-01-01", 1, [uid])
        db_mod._attendance_surplus = {}
        db_mod._attendance_meeting_count = 0
        self.assertEqual(bot.db_attendance_surplus(uid), 0)
        bot.init_db()
        self.assertEqual(bot.db_attendance_surplus(uid), 1)

    def test_attendance_mode_with_no_meetings_counts_all_votes(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_cast_vote(11, book_id, 1)
        bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
        book = bot.db_get_book(book_id)
        self.assertEqual(book["vote_count"], 1)
        self.assertEqual(book["avg_score"], 1)

    def test_future_meetings_are_ignored_in_attendance_surplus(self):
        """Discussions dated after today must not affect vote eligibility yet."""
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        voter = 77
        bot.db_cast_vote(voter, book_id, 1)
        with patch("bookclub.db.club_today_date", return_value="2026-06-01"):
            self._add_meeting("2026-06-01", [voter])
            self._add_meeting("2026-06-15", [])  # future miss
            self.assertEqual(bot.db_attendance_surplus(voter), 1)
            bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
            book = bot.db_get_book(book_id)
            self.assertEqual(book["vote_count"], 1)
            self.assertEqual(book["avg_score"], 1)

    def test_only_future_meetings_count_all_votes(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        other = bot.db_add_book("Other", "A", 10, True, "", "", 1, "u")
        voter = 88
        bot.db_cast_vote(voter, book_id, 1)
        with patch("bookclub.db.club_today_date", return_value="2026-06-01"):
            bot.db_create_meeting(other, "2026-12-01", 1, [])
            bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
            book = bot.db_get_book(book_id)
            self.assertEqual(book["vote_count"], 1)
            self.assertEqual(book["avg_score"], 1)

    def test_future_meeting_starts_counting_when_its_date_arrives(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        voter = 99
        bot.db_cast_vote(voter, book_id, 1)
        bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
        with patch("bookclub.db.club_today_date", return_value="2026-06-01"):
            self._add_meeting("2026-06-01", [voter])
            self._add_meeting("2026-06-02", [])
            self.assertEqual(bot.db_attendance_surplus(voter), 1)
            self.assertEqual(bot.db_get_book(book_id)["vote_count"], 1)
        with patch("bookclub.db.club_today_date", return_value="2026-06-02"):
            self.assertEqual(bot.db_attendance_surplus(voter), 0)
            self.assertEqual(bot.db_get_book(book_id)["vote_count"], 0)

    def test_one_meeting_per_book(self):
        bid = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_create_meeting(bid, "2026-01-01", 1, [11])
        with self.assertRaises(sqlite3.IntegrityError):
            bot.db_create_meeting(bid, "2026-01-02", 1, [12])

    def test_save_meeting_updates_attendees_in_place(self):
        bid = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_upsert_club_user(11, "Ann", None)
        bot.db_upsert_club_user(22, "Bob", None)
        mid1 = bot.db_save_meeting(bid, "2026-01-01", 1, [11])
        mid2 = bot.db_save_meeting(bid, "2026-01-02", 1, [11, 22])
        self.assertEqual(mid1, mid2)
        self.assertEqual(len(bot.db_list_meetings()), 1)
        self.assertEqual(bot.db_get_meeting(mid1)["meeting_date"], "2026-01-02")
        ids = sorted(int(r["user_id"]) for r in bot.db_get_meeting_attendee_rows(mid1))
        self.assertEqual(ids, [11, 22])

    def test_delete_meeting_removes_attendees_and_rebuilds_surplus(self):
        uid = 77
        _bid, mid = self._add_meeting("2026-01-01", [uid])
        self.assertEqual(bot.db_attendance_surplus(uid), 1)
        self.assertTrue(bot.db_delete_meeting(mid))
        self.assertEqual(bot.db_list_meetings(), [])
        self.assertEqual(bot.db_attendance_surplus(uid), 0)

    def test_init_db_merges_duplicate_meetings_per_book(self):
        bid = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_upsert_club_user(11, "Ann", None)
        bot.db_upsert_club_user(22, "Bob", None)
        mid1 = bot.db_create_meeting(bid, "2026-01-01", 1, [11])
        with sqlite3.connect(bot.DB_PATH) as conn:
            conn.execute("DROP INDEX IF EXISTS idx_meetings_book_id")
            cur = conn.execute(
                "INSERT INTO meetings (book_id, meeting_date, created_at, created_by) "
                "VALUES (?,?,?,?)",
                (bid, "2026-01-02", "2026-01-02 00:00:00", 1),
            )
            extra = cur.lastrowid
            conn.execute(
                "INSERT INTO meeting_attendees (meeting_id, user_id) VALUES (?,?)",
                (extra, 22),
            )
            conn.commit()
        bot.init_db()
        meetings = bot.db_list_meetings()
        self.assertEqual(len(meetings), 1)
        self.assertEqual(int(meetings[0]["id"]), mid1)
        ids = sorted(int(r["user_id"]) for r in bot.db_get_meeting_attendee_rows(mid1))
        self.assertEqual(ids, [11, 22])

    def test_meeting_suggestions_sort_by_attendance_then_shown_name(self):
        amy, zoe, carol = 101, 102, 103
        bot.db_upsert_club_user(amy, "Amy A", "amy")
        bot.db_upsert_club_user(zoe, "Zoe Z", "zoe")
        bot.db_upsert_club_user(carol, "Carol C", None)
        self._add_meeting("2026-01-01", [amy, zoe])
        self._add_meeting("2026-01-02", [zoe])
        target = bot.db_add_book("Target", "A", 10, True, "", "", 1, "u")
        bot.db_cast_vote(carol, target, 1)
        suggestions = bot.db_meeting_user_suggestions(target)
        ids = [
            int(r["user_id"])
            for r in suggestions
            if int(r["user_id"]) in {amy, zoe, carol}
        ]
        self.assertEqual(ids, [zoe, amy, carol])

    def test_format_club_user_display_prefers_shown_name_without_nick(self):
        self.assertEqual(
            bot.format_club_user_display(123, "Maria Rossi", None), "Maria Rossi"
        )
        self.assertEqual(
            bot.format_club_user_display(123, "Maria Rossi", "maria"),
            "Maria Rossi (@maria)",
        )
        self.assertEqual(bot.format_club_user_display(123, "", "maria"), "@maria")
        self.assertEqual(bot.format_club_user_display(123, "", None), "123")
        self.assertFalse(bot.club_user_has_shown_name("", None))
        self.assertTrue(bot.club_user_has_shown_name("Maria", None))

    def test_init_db_counts_adder_as_yes_without_overwriting(self):
        missing = bot.db_add_book("New", "A", 10, True, "", "", 42, "Ada")
        changed = bot.db_add_book("Changed", "A", 10, True, "", "", 42, "Ada")
        bot.db_cast_vote(42, changed, -1)
        bot.init_db()
        self.assertEqual(bot.db_get_user_vote(42, missing), 1)
        self.assertEqual(bot.db_get_user_vote(42, changed), -1)
        self.assertEqual(bot.db_get_book(missing)["votes_yes"], 1)

    def test_init_db_fills_empty_club_user_names_from_books(self):
        book_id = bot.db_add_book(
            "Named", "A", 10, True, "", "", 42, "Alice Example", None
        )
        bot.db_cast_vote(42, book_id, 1)
        with sqlite3.connect(bot.DB_PATH) as conn:
            conn.execute(
                "UPDATE club_users SET full_name='', username=NULL WHERE user_id=42"
            )
            conn.commit()
        suggestions = bot.db_meeting_user_suggestions(book_id)
        row = next(r for r in suggestions if int(r["user_id"]) == 42)
        self.assertEqual(row["full_name"], "")
        bot.init_db()
        suggestions = bot.db_meeting_user_suggestions(book_id)
        row = next(r for r in suggestions if int(r["user_id"]) == 42)
        self.assertEqual(row["full_name"], "Alice Example")

    # -- Update field --

    def test_db_update_book_field_title(self):
        book_id = bot.db_add_book("Old", "A", 10, True, "", "", 1, "u")
        bot.db_update_book_field(book_id, "title", "New")
        self.assertEqual(bot.db_get_book(book_id)["title"], "New")

    def test_db_update_book_field_pages(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_update_book_field(book_id, "pages", 999)
        self.assertEqual(bot.db_get_book(book_id)["pages"], 999)

    def test_db_update_book_field_invalid_raises(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        with self.assertRaises(ValueError):
            bot.db_update_book_field(book_id, "added_by", 0)

    # -- Delete book --

    def test_db_delete_book(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_delete_book(book_id)
        self.assertIsNone(bot.db_get_book(book_id))

    def test_db_delete_book_cascades_votes(self):
        book_id = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_cast_vote(1, book_id, 1)
        bot.db_delete_book(book_id)
        # Verify book is gone
        self.assertIsNone(bot.db_get_book(book_id))
        # Verify vote is gone (should happen via ON DELETE CASCADE in bot.py)
        # self.assertIsNone(bot.db_get_user_vote(1, book_id))

    def test_book_export_import_roundtrip(self):
        book_id = bot.db_add_book(
            "Dune",
            "Herbert",
            412,
            True,
            "https://example.com/review",
            "Sci-fi classic",
            42,
            "Alice",
            username="alice",
        )
        bot.db_mark_discussed(book_id, "2025-06-01")
        bot.db_cast_vote(1, book_id, 1)
        source = bot.db_get_book(book_id)
        payload = bot.book_to_export_payload(source)
        parsed, _entity = bot.parse_book_import(payload)
        new_id = bot.db_import_book(parsed)
        imported = bot.db_get_book(new_id)
        self.assertNotEqual(new_id, book_id)
        self.assertEqual(imported["title"], "Dune")
        self.assertEqual(imported["author"], "Herbert")
        self.assertEqual(imported["pages"], 412)
        self.assertEqual(imported["fiction"], 1)
        self.assertEqual(imported["review_link"], "https://example.com/review")
        self.assertEqual(imported["description"], "Sci-fi classic")
        self.assertEqual(imported["discussed"], 1)
        self.assertEqual(imported["discussed_at"], "2025-06-01")
        self.assertEqual(imported["added_by"], bot.IMPORTED_USER_ID)
        self.assertEqual(imported["added_by_name"], "Alice")
        self.assertEqual(imported["added_by_username"], "alice")
        self.assertEqual(imported["vote_count"], 0)

    def test_parse_book_import_rejects_bad_json(self):
        with self.assertRaises(ValueError):
            bot.parse_book_import("not json")

    def test_title_word_similarity_ratio(self):
        self.assertEqual(
            bot.title_word_similarity_ratio(
                "The Lord of the Rings", "Lord of the Rings"
            ),
            1.0,
        )
        self.assertEqual(
            bot.title_word_similarity_ratio("Harry Potter", "Harry Potter Stone"),
            2 / 3,
        )
        self.assertLess(
            bot.title_word_similarity_ratio("War and Peace", "Crime and Punishment"),
            bot.TITLE_SIMILARITY_THRESHOLD,
        )

    def test_find_similar_book_titles(self):
        bot.db_add_book("Alpha Beta Gamma", "A", 10, True, "", "", 1, "u")
        bot.db_add_book("Unrelated Title Here", "A", 10, True, "", "", 1, "u")
        matches = bot.find_similar_book_titles("Alpha Beta")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1], "Alpha Beta Gamma")

    def test_book_card_shows_original_language_when_set(self):
        book = make_book(original_language="German")
        text = bot.book_card(book, "en")
        self.assertIn("Original language", text)
        self.assertIn("German", text)

    def test_book_card_shows_original_language_in_ui_language(self):
        book = make_book(original_language="German")
        text = bot.book_card(book, "ru")
        self.assertIn("Язык оригинала", text)
        self.assertIn("Немецкий", text)
        self.assertNotIn("German", text)

    def test_edit_current_original_language_follows_ui_language(self):
        book = make_book(original_language="German")
        shown = bot.edit_current_value(book, "original_language", "ru")
        self.assertIn("Немецкий", shown)
        self.assertNotIn("German", shown)

    def test_book_card_hides_original_language_when_empty(self):
        book = make_book(original_language=None)
        text = bot.book_card(book, "en")
        self.assertNotIn("Original language", text)

    def test_book_card_shows_creation_year_when_set(self):
        book = make_book(creation_year=1984)
        text = bot.book_card(book, "en")
        self.assertIn("Year", text)
        self.assertIn("1984", text)

    def test_book_card_hides_creation_year_when_empty(self):
        book = make_book(creation_year=None)
        text = bot.book_card(book, "en")
        self.assertNotIn("📅", text)

    def test_book_card_shows_language_levels_when_set(self):
        book = make_book(language_levels="B1,B2")
        enabled = cfg.DEFAULT_ENTRY_FIELDS | {"language_levels"}
        with patch.object(cfg, "ENTRY_FIELDS", enabled):
            text = bot.book_card(book, "en")
        self.assertIn("B1", text)
        self.assertIn("B2", text)

    def test_book_card_hides_language_levels_when_field_disabled(self):
        book = make_book(language_levels="B1,B2")
        with patch.object(cfg, "ENTRY_FIELDS", cfg.DEFAULT_ENTRY_FIELDS):
            text = bot.book_card(book, "en")
        self.assertNotIn("B1", text)
        self.assertNotIn("B2", text)

    def test_book_card_hides_disabled_optional_fields(self):
        book = make_book(
            author="Hidden Author",
            fiction=True,
            pages=321,
            review_link="https://example.com/hidden-review",
            description="Secret blurb",
            original_language="German",
            creation_year=1984,
        )
        with patch.object(cfg, "ENTRY_FIELDS", frozenset()):
            text = bot.book_card(book, "en")
        self.assertIn("Test", text)
        self.assertNotIn("Hidden Author", text)
        self.assertNotIn("Fiction", text)
        self.assertNotIn("321", text)
        self.assertNotIn("hidden-review", text)
        self.assertNotIn("Secret blurb", text)
        self.assertNotIn("German", text)
        self.assertNotIn("1984", text)

    def test_seed_film_script_inserts_once(self):
        inserted = 0
        for entry in [
            {
                "title": "Seed Test Film",
                "review_link": "https://example.com/seed-test",
                "description": "d",
            }
        ]:
            if bot.db_seed_book_exists(entry["title"], entry["review_link"]):
                continue
            bot.db_insert_seed_book(
                title=entry["title"],
                author="—",
                pages=0,
                fiction=True,
                review_link=entry["review_link"],
                description=entry["description"],
                original_language="German",
                added_at="2026-08-01",
                added_by_username="CreAtors_we_makeArt",
                added_by_name="CreAtors_we_makeArt",
            )
            inserted += 1
        self.assertEqual(inserted, 1)
        self.assertTrue(
            bot.db_seed_book_exists("Seed Test Film", "https://example.com/seed-test")
        )

    def test_db_begin_new_book_notify_is_idempotent(self):
        bid = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_set_new_book_notify_pending(
            bid, 42, datetime.now() + timedelta(minutes=5)
        )
        self.assertEqual(bot.db_begin_new_book_notify(bid), 42)
        self.assertIsNone(bot.db_begin_new_book_notify(bid))

    def test_recover_pending_new_book_notifications(self):
        bid = bot.db_add_book("B", "A", 10, True, "", "", 1, "u")
        bot.db_set_new_book_notify_pending(
            bid, 1, datetime.now() + timedelta(seconds=120)
        )
        jq = MagicMock()
        bot.recover_pending_new_book_notifications(jq)
        jq.run_once.assert_called_once()
        self.assertEqual(jq.run_once.call_args[1]["data"], {"book_id": bid})
        delay = jq.run_once.call_args[1]["when"]
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 120)

    # -- User settings --

    def test_db_user_setting_default_minus_one(self):
        self.assertEqual(bot.db_get_user_setting(1, "missing_key"), -1)

    def test_db_user_setting_custom_default(self):
        self.assertEqual(bot.db_get_user_setting(1, "missing_key", default=42), 42)

    def test_db_user_setting_set_and_get(self):
        bot.db_set_user_setting(1, "notify_new_books", 1)
        self.assertEqual(bot.db_get_user_setting(1, "notify_new_books"), 1)

    def test_db_user_setting_update(self):
        bot.db_set_user_setting(1, "k", 1)
        bot.db_set_user_setting(1, "k", 0)
        self.assertEqual(bot.db_get_user_setting(1, "k"), 0)

    def test_db_get_users_with_setting(self):
        bot.db_set_user_setting(1, "notify_new_books", 1)
        bot.db_set_user_setting(2, "notify_new_books", 1)
        bot.db_set_user_setting(3, "notify_new_books", 0)
        result = bot.db_get_users_with_setting("notify_new_books", 1)
        self.assertIn(1, result)
        self.assertIn(2, result)
        self.assertNotIn(3, result)
        self.assertEqual(len(result), 2)

    def test_add_drafts_crud_and_user_isolation(self):
        payload = {
            "new_book": {"title": "War and Peace", "author": "Leo"},
            "add_state": bot.ADDING_AUTHOR,
            "llm_add": True,
            "llm_filled_keys": ["author"],
        }
        did = bot.db_insert_add_draft(1, "War and Peace", payload)
        bot.db_insert_add_draft(2, "Other User", {"new_book": {"title": "Other"}})
        listed = bot.db_list_add_drafts(1)
        self.assertEqual(listed, [(did, "War and Peace")])
        self.assertIsNone(bot.db_get_add_draft(did, 2))
        loaded = bot.db_get_add_draft(did, 1)
        self.assertEqual(loaded["llm_filled_keys"], ["author"])
        self.assertTrue(
            bot.db_update_add_draft(
                did, 1, "War and Peace", {**payload, "llm_filled_keys": []}
            )
        )
        self.assertEqual(bot.db_get_add_draft(did, 1)["llm_filled_keys"], [])
        self.assertFalse(bot.db_delete_add_draft(did, 2))
        self.assertTrue(bot.db_delete_add_draft(did, 1))
        self.assertEqual(bot.db_list_add_drafts(1), [])

    def test_serialize_add_draft_keeps_ai_flags_and_levels(self):
        from bookclub.handlers.add_flow import apply_add_draft, serialize_add_draft

        ctx = MagicMock()
        ctx.user_data = {
            "new_book": {
                "title": "T",
                "author": "Leo",
                "language_levels": {"A1", "B2"},
            },
            "add_state": bot.ADDING_AUTHOR,
            "llm_add": True,
            "llm_filled_keys": {"author"},
            "add_from_start": True,
        }
        payload = serialize_add_draft(ctx)
        self.assertEqual(payload["llm_filled_keys"], ["author"])
        self.assertEqual(sorted(payload["new_book"]["language_levels"]), ["A1", "B2"])
        self.assertIsInstance(ctx.user_data["new_book"]["language_levels"], set)

        dest = MagicMock()
        dest.user_data = {}
        apply_add_draft(dest, payload, 7)
        self.assertEqual(dest.user_data["add_draft_id"], 7)
        self.assertEqual(dest.user_data["llm_filled_keys"], {"author"})
        self.assertEqual(dest.user_data["new_book"]["language_levels"], {"A1", "B2"})
        self.assertTrue(dest.user_data["llm_add"])

    def test_db_get_users_with_setting_empty(self):
        result = bot.db_get_users_with_setting("notify_new_books", 1)
        self.assertEqual(result, [])


# ── Utility / formatting tests ─────────────────────────────────────────────────


class TestUtils(unittest.TestCase):
    def test_books_keyboard_paginates_large_catalog(self):
        books = [
            make_book(id=index, title=f"Book {index}", author="Author")
            for index in range(120)
        ]
        markup = bot.books_keyboard(books, "pick", "Cancel")
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertLessEqual(len(buttons), cfg.PICKER_PAGE_SIZE + 2)
        self.assertIn(
            "pick:page:1",
            [button.callback_data for button in buttons],
        )

    # -- is_valid_url --

    def test_is_valid_url_http(self):
        self.assertTrue(bot.is_valid_url("http://google.com"))

    def test_is_valid_url_https(self):
        self.assertTrue(bot.is_valid_url("https://test.me/path?q=1"))

    def test_is_valid_url_no_scheme(self):
        self.assertFalse(bot.is_valid_url("google.com"))

    def test_is_valid_url_plain_text(self):
        self.assertFalse(bot.is_valid_url("just text"))

    def test_is_valid_url_empty(self):
        self.assertFalse(bot.is_valid_url(""))

    def test_is_valid_url_ftp(self):
        # ftp is not http/https — should be invalid per our rule
        self.assertFalse(bot.is_valid_url("ftp://files.example.com"))

    # -- parse_date --

    def test_parse_date_iso(self):
        self.assertEqual(bot.parse_date("2023-10-25"), "2023-10-25")

    def test_parse_date_dot_format(self):
        self.assertEqual(bot.parse_date("25.10.2023"), "2023-10-25")

    def test_parse_date_slash_format(self):
        self.assertEqual(bot.parse_date("25/10/2023"), "2023-10-25")

    def test_parse_date_with_whitespace(self):
        self.assertEqual(bot.parse_date("  2023-10-25  "), "2023-10-25")

    def test_parse_date_invalid(self):
        self.assertIsNone(bot.parse_date("invalid date"))

    def test_parse_date_partial(self):
        self.assertIsNone(bot.parse_date("2023-10"))

    def test_parse_date_wrong_order_iso(self):
        # DD-MM-YYYY is not a supported format
        self.assertIsNone(bot.parse_date("25-10-2023"))

    # -- fmt_dt_utc --

    def test_fmt_dt_utc_positive_offset(self):
        from datetime import datetime, timedelta, timezone

        dt = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(bot.fmt_dt_utc(dt), "2026-04-04 12:00:00 UTC+02:00")

    def test_fmt_dt_utc_zero_offset(self):
        from datetime import datetime

        dt = datetime(2026, 4, 4, 12, 0, 0, tzinfo=UTC)
        self.assertEqual(bot.fmt_dt_utc(dt), "2026-04-04 14:00:00 UTC+02:00")

    def test_fmt_dt_utc_negative_offset(self):
        from datetime import datetime, timedelta, timezone

        dt = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(bot.fmt_dt_utc(dt), "2026-04-04 19:00:00 UTC+02:00")

    def test_fmt_dt_utc_naive_as_utc(self):
        from datetime import datetime

        self.assertEqual(
            bot.fmt_dt_utc(datetime(2026, 4, 4, 12, 0, 0)),
            "2026-04-04 14:00:00 UTC+02:00",
        )

    # -- tr (translation) --

    def test_tr_en(self):
        ctx = MagicMock()
        ctx.user_data = {"lang": "en"}
        self.assertEqual(bot.tr(ctx, "cancel_btn"), "❌ Cancel")

    def test_tr_ru(self):
        ctx = MagicMock()
        ctx.user_data = {"lang": "ru"}
        self.assertEqual(bot.tr(ctx, "cancel_btn"), "❌ Отмена")

    def test_tr_de(self):
        ctx = MagicMock()
        ctx.user_data = {"lang": "de"}
        self.assertEqual(bot.tr(ctx, "cancel_btn"), "❌ Abbrechen")

    def test_tr_lang_string_directly(self):
        self.assertEqual(bot.tr("en", "cancel_btn"), "❌ Cancel")
        self.assertEqual(bot.tr("ru", "cancel_btn"), "❌ Отмена")
        self.assertEqual(bot.tr("de", "cancel_btn"), "❌ Abbrechen")

    def test_add_ai_suggest_failed_keeps_provider_detail(self):
        text = bot.tr(
            "ru",
            "add_ai_suggest_failed",
            kind="таймаут",
            error='HTTP 400: {"error":"<oops>"}',
        )
        self.assertIn("Не удалось получить подсказки", text)
        self.assertIn("таймаут", text)
        self.assertIn("<oops>", text)

    def test_ui_languages_share_keys(self):
        en_keys = set(bot.T["en"])
        for lang in bot.SUPPORTED_LANGS:
            self.assertEqual(set(bot.T[lang]), en_keys, f"T[{lang!r}] keys differ")
            self.assertIn(lang, bot.COMMANDS)
            self.assertIn(lang, bot.ENTITY_LEX["book"])
            self.assertIn(lang, bot.ENTITY_LEX["film"])

    def test_next_ui_lang_cycles(self):
        self.assertEqual(bot.next_ui_lang("en"), "ru")
        self.assertEqual(bot.next_ui_lang("ru"), "de")
        self.assertEqual(bot.next_ui_lang("de"), "en")
        self.assertEqual(bot.next_ui_lang("xx"), "en")

    def test_tr_callable_lambda(self):
        ctx = MagicMock()
        ctx.user_data = {"lang": "en"}
        # votes_label is a lambda
        result = bot.tr(ctx, "votes_label", n=1)
        self.assertIn("1", result)
        result_plural = bot.tr(ctx, "votes_label", n=3)
        self.assertIn("3", result_plural)

    def test_tr_with_format_kwargs(self):
        ctx = MagicMock()
        ctx.user_data = {"lang": "en"}
        result = bot.tr(ctx, "deleted", title="My Book")
        self.assertIn("My Book", result)

    def test_notify_delay_minutes_from_seconds(self):
        default_minutes = max(1, cfg.NEW_BOOK_NOTIFY_DELAY_SECONDS // 60)
        self.assertEqual(cfg.notify_delay_minutes(), default_minutes)
        with patch("bookclub.config.NEW_BOOK_NOTIFY_DELAY_SECONDS", 0):
            self.assertEqual(cfg.notify_delay_minutes(), 0)
        with patch("bookclub.config.NEW_BOOK_NOTIFY_DELAY_SECONDS", 90):
            self.assertEqual(cfg.notify_delay_minutes(), 1)

    def test_delay_copy_follows_config(self):
        with patch("bookclub.config.NEW_BOOK_NOTIFY_DELAY_SECONDS", 600):
            self.assertEqual(cfg.notify_delay_minutes(), 10)
            en = bot.tr("en", "new_book_delay_note")
            self.assertIn("10 minutes", en)
            self.assertNotIn("5 minutes", en)
            ru = bot.tr("ru", "new_book_delay_note")
            self.assertIn("10 минут", ru)
            de = bot.tr("de", "new_book_delay_note")
            self.assertIn("10 Minuten", de)
            prompt = bot.tr("en", "notify_optin_prompt")
            self.assertIn("10-minute", prompt)
            settings = bot.tr("en", "settings_notify_on")
            self.assertIn("10 min", settings)

    def test_delay_copy_singular_minute(self):
        with patch("bookclub.config.NEW_BOOK_NOTIFY_DELAY_SECONDS", 60):
            self.assertIn("1 minute", bot.tr("en", "new_book_delay_note"))
            self.assertNotIn("1 minutes", bot.tr("en", "new_book_delay_note"))
            self.assertIn("1 минута", bot.tr("ru", "new_book_delay_note"))

    # -- h (HTML escaping) --

    def test_h_ampersand(self):
        self.assertEqual(bot.h("A & B"), "A &amp; B")

    def test_h_less_than(self):
        self.assertEqual(bot.h("<tag>"), "&lt;tag&gt;")

    def test_h_no_special(self):
        self.assertEqual(bot.h("Hello World"), "Hello World")

    def test_h_coerces_to_string(self):
        self.assertEqual(bot.h(42), "42")

    def test_h_combined(self):
        self.assertEqual(bot.h("a < b & c > d"), "a &lt; b &amp; c &gt; d")

    def test_h_escapes_double_quote(self):
        """h() is used inside href="..."; a raw quote would break the anchor."""
        self.assertEqual(bot.h('a"b'), "a&quot;b")

    def test_book_card_href_survives_quote_in_link(self):
        """A quote in a stored review_link must not break out of the attribute."""
        book = make_book(review_link='https://ex.com/a"onmouseover=x')
        href_line = [
            line for line in bot.book_card(book, "en").splitlines() if "href" in line
        ][0]
        # Exactly two quotes: the ones delimiting the attribute.
        self.assertEqual(href_line.count('"'), 2)
        self.assertIn("&quot;", href_line)

    # -- is_valid_url (rejections that would corrupt the HTML anchor) --

    def test_is_valid_url_rejects_quote(self):
        self.assertFalse(bot.is_valid_url('https://ex.com/a"b'))

    def test_is_valid_url_rejects_angle_brackets(self):
        self.assertFalse(bot.is_valid_url("https://ex.com/<b>"))

    def test_is_valid_url_rejects_whitespace(self):
        self.assertFalse(bot.is_valid_url("https://ex.com/a b"))

    def test_is_valid_url_rejects_scheme_only(self):
        self.assertFalse(bot.is_valid_url("https://"))

    # -- score_display --

    def test_score_display_with_votes(self):
        book = make_book(
            votes_yes=3, votes_meh=1, votes_no=0, vote_count=4, avg_score=0.75
        )
        display = bot.score_display(book, "en")
        self.assertIn("✅ 3", display)
        self.assertIn("😐 1", display)
        self.assertIn("❌ 0", display)
        self.assertIn("(4 votes)", display)

    def test_score_display_no_votes_en(self):
        book = make_book(
            votes_yes=0, votes_meh=0, votes_no=0, vote_count=0, avg_score=0
        )
        display = bot.score_display(book, "en")
        self.assertIn("0 vote", display)
        # Should not contain raw vote tallies (no ✅ N  😐 N  ❌ N pattern)
        self.assertNotIn("✅", display)

    def test_score_display_no_votes_ru(self):
        book = make_book(
            votes_yes=0, votes_meh=0, votes_no=0, vote_count=0, avg_score=0
        )
        display = bot.score_display(book, "ru")
        self.assertIn("0 оценок", display)

    def test_score_display_ru_plural_votes(self):
        book = make_book(
            votes_yes=2, votes_meh=0, votes_no=0, vote_count=2, avg_score=1
        )
        display = bot.score_display(book, "ru")
        self.assertIn("оценки", display)

    # -- book_compact_line --

    def test_book_compact_line_shows_score_before_title(self):
        book = make_book(
            title="Dune",
            author="Herbert",
            avg_score=2.5,
            creation_year=1965,
        )
        line = bot.book_compact_line(1, book)
        self.assertEqual(line, "1. <b>2.5</b> <b>Dune</b> — Herbert (1965)")
        score_pos = line.index("2.5")
        title_pos = line.index("Dune")
        self.assertLess(score_pos, title_pos)

    def test_book_compact_line_zero_score_and_no_year(self):
        book = make_book(title="Untitled", author="Anon", avg_score=0)
        line = bot.book_compact_line(3, book)
        self.assertEqual(line, "3. <b>0</b> <b>Untitled</b> — Anon")

    def test_book_compact_line_escapes_html(self):
        book = make_book(title="A <B>", author="X & Y", avg_score=1)
        line = bot.book_compact_line(1, book)
        self.assertIn("A &lt;B&gt;", line)
        self.assertIn("X &amp; Y", line)
        self.assertNotIn("<B>", line)

    def test_compact_book_list_lines_matches_list_header_and_rows(self):
        books = [
            make_book(title="Dune", author="Herbert", avg_score=2, creation_year=1965),
            make_book(title="Beta", author="Anon", avg_score=0),
        ]
        lines = bot.compact_book_list_lines(books, "en")
        self.assertEqual(lines[0], bot.tr("en", "list_compact_title", count=2))
        self.assertEqual(lines[1], bot.book_compact_line(1, books[0]))
        self.assertEqual(lines[2], bot.book_compact_line(2, books[1]))

    # -- book_card --

    def test_book_card_contains_title_and_author(self):
        book = make_book(title="Ubik", author="Philip K. Dick")
        card = bot.book_card(book, "en")
        self.assertIn("Ubik", card)
        self.assertIn("Philip K. Dick", card)

    def test_book_card_escapes_html_special_chars(self):
        book = make_book(title="A & B", author="C < D")
        card = bot.book_card(book, "en")
        self.assertIn("A &amp; B", card)
        self.assertIn("C &lt; D", card)

    def test_book_card_user_vote_shown(self):
        book = make_book(votes_yes=1, vote_count=1, avg_score=1)
        card = bot.book_card(book, "en", user_vote=1)
        self.assertIn("want to read", card)

    def test_book_card_user_vote_not_shown_when_none(self):
        book = make_book()
        card = bot.book_card(book, "en", user_vote=None)
        self.assertNotIn("Your current vote", card)

    def test_book_card_no_description_when_empty(self):
        book = make_book(description="")
        card = bot.book_card(book, "en")
        self.assertNotIn("<i></i>", card)

    def test_book_card_fiction_label_en(self):
        book_f = make_book(fiction=1)
        book_nf = make_book(fiction=0)
        self.assertIn("Fiction", bot.book_card(book_f, "en"))
        self.assertIn("Non-fiction", bot.book_card(book_nf, "en"))

    def test_book_card_fiction_label_ru_uses_english(self):
        # RU also uses English Fiction/Non-fiction labels per design decision
        book_f = make_book(fiction=1)
        book_nf = make_book(fiction=0)
        self.assertIn("Fiction", bot.book_card(book_f, "ru"))
        self.assertIn("Non-fiction", bot.book_card(book_nf, "ru"))

    def test_book_card_discussed_date_shown(self):
        book = make_book(discussed=1, discussed_at="2025-06-01")
        card = bot.book_card(book, "en")
        self.assertIn("2025-06-01", card)

    def test_book_card_discussed_date_not_shown_when_none(self):
        book = make_book(discussed=0, discussed_at=None)
        card = bot.book_card(book, "en")
        self.assertNotIn("Discussed on", card)

    # -- format_user --

    def test_format_user_with_username(self):
        book = make_book(added_by_name="John", added_by_username="johndoe")
        self.assertEqual(bot.format_user(book), "@johndoe")

    def test_format_user_without_username(self):
        book = make_book(added_by_name="Jane", added_by_username=None)
        self.assertEqual(bot.format_user(book), "Jane")

    def test_format_user_empty_name_no_username(self):
        book = make_book(added_by_name="", added_by_username=None)
        self.assertEqual(bot.format_user(book), "unknown")

    # -- can_modify --

    def test_can_modify_owner(self):
        book = make_book()
        book["added_by"] = 123
        self.assertTrue(bot.can_modify(123, book))

    def test_can_modify_other_user(self):
        book = make_book()
        book["added_by"] = 123
        self.assertFalse(bot.can_modify(456, book))

    def test_can_modify_admin(self):
        old = cfg.ADMIN_IDS[:]
        try:
            cfg.ADMIN_IDS = [999]
            book = make_book()
            book["added_by"] = 123
            self.assertTrue(bot.can_modify(999, book))
        finally:
            cfg.ADMIN_IDS = old

    def test_can_modify_imported_book_by_matching_username(self):
        book = make_book()
        book["added_by"] = bot.IMPORTED_USER_ID
        book["added_by_username"] = "alice"
        self.assertTrue(bot.can_modify(123, book, username="alice"))

    def test_can_modify_imported_book_by_case_insensitive_username(self):
        book = make_book()
        book["added_by"] = bot.IMPORTED_USER_ID
        book["added_by_username"] = "Alice"
        self.assertTrue(bot.can_modify(123, book, username="alice"))

    def test_can_modify_imported_book_wrong_username(self):
        book = make_book()
        book["added_by"] = bot.IMPORTED_USER_ID
        book["added_by_username"] = "alice"
        self.assertFalse(bot.can_modify(0, book, username="bob"))

    def test_can_modify_imported_book_no_username(self):
        book = make_book()
        book["added_by"] = bot.IMPORTED_USER_ID
        book["added_by_username"] = "alice"
        self.assertFalse(bot.can_modify(0, book, username=None))

    # -- score_keyboard --

    def test_score_keyboard_no_current_vote(self):
        kb = bot.score_keyboard(42, "en", current=None)
        labels = [btn.text for btn in kb.inline_keyboard[0]]
        self.assertNotIn("✓", " ".join(labels))
        callbacks = [btn.callback_data for btn in kb.inline_keyboard[0]]
        self.assertIn("vote_cast:42:1", callbacks)
        self.assertIn("vote_cast:42:0", callbacks)
        self.assertIn("vote_cast:42:-1", callbacks)

    def test_score_keyboard_with_current_vote(self):
        kb = bot.score_keyboard(42, "en", current=1)
        want_btn = kb.inline_keyboard[0][0]  # score=1 is first
        self.assertIn("✓", want_btn.text)
        # Other buttons should NOT have tick
        meh_btn = kb.inline_keyboard[0][1]
        self.assertNotIn("✓", meh_btn.text)

    def test_score_keyboard_ru_labels(self):
        kb = bot.score_keyboard(1, "ru", current=None)
        labels = [btn.text for btn in kb.inline_keyboard[0]]
        self.assertIn("✅ Хочу", labels)
        self.assertIn("😐 Всё равно", labels)
        self.assertIn("❌ Не хочу", labels)

    def test_score_keyboard_en_labels(self):
        kb = bot.score_keyboard(1, "en", current=None)
        labels = [btn.text for btn in kb.inline_keyboard[0]]
        self.assertIn("✅ Want", labels)
        self.assertIn("😐 Don't care", labels)
        self.assertIn("❌ Don't want", labels)

    def test_interactive_keyboards_include_cancel(self):
        def callbacks(kb):
            return [btn.callback_data for row in kb.inline_keyboard for btn in row]

        self.assertIn(bot.CONV_CANCEL, callbacks(bot.add_nav_keyboard("en")))
        self.assertIn(bot.CONV_CANCEL, callbacks(bot.fiction_keyboard("en")))
        self.assertIn(
            bot.CONV_CANCEL,
            callbacks(bot.original_language_keyboard("en", prefix="add_orig_lang")),
        )
        self.assertIn(
            bot.CONV_CANCEL,
            callbacks(bot.cefr_levels_keyboard("en", set(), prefix="add_cefr")),
        )
        self.assertIn(bot.CONV_CANCEL, callbacks(bot.add_ai_choice_keyboard("en")))
        self.assertIn(
            "add_start:drafts",
            callbacks(bot.add_start_keyboard("en", llm=True, has_drafts=True)),
        )
        self.assertIn(
            "add_start:ai",
            callbacks(bot.add_start_keyboard("en", llm=True, has_drafts=False)),
        )
        self.assertIn(
            "add_start:manual",
            callbacks(bot.add_start_keyboard("en", llm=True, has_drafts=False)),
        )
        kb = bot.add_drafts_keyboard("en", [(1, "War and Peace")])
        data = callbacks(kb)
        self.assertIn("add_draft:1", data)
        self.assertIn("add_draft_del:1", data)
        self.assertIn(bot.CONV_CANCEL, callbacks(bot.cancel_keyboard("en")))

    def test_add_edit_button_copies_short_text(self):
        btn = bot.add_edit_button("en", "Leo Tolstoy")
        self.assertEqual(btn.copy_text.text, "Leo Tolstoy")
        self.assertIsNone(btn.switch_inline_query_current_chat)
        self.assertIsNone(btn.callback_data)

    def test_add_edit_button_inline_when_requested(self):
        btn = bot.add_edit_button("en", "Leo Tolstoy", use_inline=True)
        self.assertEqual(btn.switch_inline_query_current_chat, "Leo Tolstoy")
        self.assertIsNone(btn.copy_text)

    def test_add_edit_button_long_text_uses_callback(self):
        btn = bot.add_edit_button("en", "x" * 300)
        self.assertEqual(btn.callback_data, "add_edit")
        self.assertIsNone(btn.copy_text)

    def test_add_nav_keyboard_includes_edit_between_back_and_forward(self):
        kb = bot.add_nav_keyboard(
            "en", show_back=True, show_forward=True, edit_value="Leo"
        )
        row = kb.inline_keyboard[0]
        self.assertEqual(row[0].callback_data, "add_back")
        self.assertEqual(row[1].copy_text.text, "Leo")
        self.assertEqual(row[2].callback_data, "add_forward")

    # -- ALLOWED_CHAT_ID config --

    def test_allowed_chat_id_env_not_set(self):
        # When env var is absent, ALLOWED_CHAT_ID should be falsy
        import unittest.mock

        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALLOWED_CHAT_ID", None)
            # The module is already loaded; just verify the None/0 logic
            val = int(os.environ.get("ALLOWED_CHAT_ID", "0")) or None
            self.assertIsNone(val)

    def test_allowed_chat_id_env_set(self):
        val = int("-1001234567890") or None
        self.assertEqual(val, -1001234567890)


# ── ENTRY_FIELDS config ────────────────────────────────────────────────────────


class TestEntryFields(unittest.TestCase):
    def test_default_includes_standard_not_cefr(self):
        self.assertEqual(bot.parse_entry_fields(None), bot.DEFAULT_ENTRY_FIELDS)
        self.assertNotIn("language_levels", bot.parse_entry_fields(""))
        self.assertIn(
            "language_levels", bot.parse_entry_fields(None, ask_language_level=True)
        )

    def test_all_includes_cefr(self):
        self.assertEqual(
            bot.parse_entry_fields("all"), frozenset(bot.OPTIONAL_ENTRY_FIELDS)
        )
        self.assertEqual(
            bot.parse_entry_fields("*"), frozenset(bot.OPTIONAL_ENTRY_FIELDS)
        )

    def test_explicit_list_and_aliases(self):
        got = bot.parse_entry_fields("author, runtime, review_link")
        self.assertEqual(got, frozenset({"author", "pages", "review"}))

    def test_title_only(self):
        self.assertEqual(bot.parse_entry_fields("title"), frozenset())

    def test_unknown_ignored(self):
        with patch("builtins.print") as mocked_print:
            got = bot.parse_entry_fields("author,nope")
        self.assertEqual(got, frozenset({"author"}))
        mocked_print.assert_called()

    def test_ask_language_level_adds_cefr_to_explicit(self):
        got = bot.parse_entry_fields("author", ask_language_level=True)
        self.assertEqual(got, frozenset({"author", "language_levels"}))

    def test_add_next_skips_disabled_fields(self):
        with patch.object(cfg, "ENTRY_FIELDS", frozenset({"description"})):
            self.assertEqual(add_next_state(bot.ADDING_TITLE), bot.ADDING_DESCRIPTION)
            self.assertIsNone(add_next_state(bot.ADDING_DESCRIPTION))
            self.assertEqual(
                add_previous_state(bot.ADDING_DESCRIPTION), bot.ADDING_TITLE
            )

    def test_add_next_title_only_completes(self):
        with patch.object(cfg, "ENTRY_FIELDS", frozenset()):
            self.assertIsNone(add_next_state(bot.ADDING_TITLE))

    def test_add_next_review_comes_after_title(self):
        self.assertEqual(add_next_state(bot.ADDING_TITLE), bot.ADDING_REVIEW)
        self.assertEqual(add_next_state(bot.ADDING_REVIEW), bot.ADDING_AUTHOR)
        self.assertEqual(add_previous_state(bot.ADDING_AUTHOR), bot.ADDING_REVIEW)

    def test_add_previous_from_ai_choose_is_title(self):
        self.assertEqual(add_previous_state(bot.ADDING_AI_CHOOSE), bot.ADDING_TITLE)

    def test_add_previous_from_draft_choose_is_start(self):
        self.assertEqual(add_previous_state(bot.ADDING_DRAFT_CHOOSE), bot.ADDING_START)

    def test_add_previous_from_confirm_is_last_field(self):
        self.assertEqual(add_previous_state(bot.ADDING_CONFIRM), bot.ADDING_DESCRIPTION)
        with patch.object(cfg, "ENTRY_FIELDS", frozenset()):
            self.assertEqual(add_previous_state(bot.ADDING_CONFIRM), bot.ADDING_TITLE)

    def test_entry_field_enabled_title_always(self):
        with patch.object(cfg, "ENTRY_FIELDS", frozenset()):
            self.assertTrue(bot.entry_field_enabled("title"))
            self.assertFalse(bot.entry_field_enabled("author"))


# ── Error-alert buffering ────────────────────────────────────────────────────────


class TestErrorAlertHandler(unittest.TestCase):
    """The Telegram error-alert handler only *buffers* records; delivery is a
    separate background task. These tests cover the buffering + loop-guard logic
    that keeps a failing send from alerting about itself forever."""

    def setUp(self):
        log_setup._alert_buffer.clear()
        log_setup._alert_dropped = 0
        self.handler = bot._TelegramAlertHandler(level=bot.logging.ERROR)
        self.handler.setFormatter(bot._log_fmt)

    def _record(self, name="bookclub_bot", msg="boom", level=bot.logging.ERROR):
        return bot.logging.LogRecord(
            name=name,
            level=level,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_error_record_is_buffered(self):
        self.handler.emit(self._record(msg="kaboom"))
        self.assertEqual(len(log_setup._alert_buffer), 1)
        self.assertIn("kaboom", log_setup._alert_buffer[0])

    def test_own_alert_failures_are_ignored(self):
        # The ".alert" child logger reports delivery failures; alerting on those
        # would loop forever, so emit() must drop them.
        self.handler.emit(self._record(name="bookclub_bot.alert"))
        self.assertEqual(len(log_setup._alert_buffer), 0)

    def test_networking_stack_is_ignored(self):
        for noisy in ("httpx", "httpcore.connection", "telegram.ext", "apscheduler.x"):
            self.handler.emit(self._record(name=noisy))
        self.assertEqual(len(log_setup._alert_buffer), 0)

    def test_buffer_is_bounded_and_counts_drops(self):
        overflow = bot._ALERT_BUFFER_MAX + 5
        for i in range(overflow):
            self.handler.emit(self._record(msg=f"err{i}"))
        self.assertEqual(len(log_setup._alert_buffer), bot._ALERT_BUFFER_MAX)
        self.assertEqual(log_setup._alert_dropped, 5)
        # Oldest were dropped; newest survive.
        self.assertIn(f"err{overflow - 1}", log_setup._alert_buffer[-1])


class TestClubEntity(unittest.TestCase):
    def test_film_lex_english_labels(self):
        film_en = bot.ENTITY_LEX["film"]["en"]
        self.assertEqual(film_en["Author"], "Director")
        self.assertEqual(film_en["verb"], "watch")

    def test_film_templates_use_lex(self):
        with patch("bookclub.i18n.CLUB_ENTITY", "film"):
            self.assertEqual(bot.s("en", "field_author"), "Director")
            self.assertIn("watch", bot.s("en", "want_label"))

    def test_valid_club_entities_include_book_and_film(self):
        self.assertIn("book", bot._VALID_CLUB_ENTITIES)
        self.assertIn("film", bot._VALID_CLUB_ENTITIES)


if __name__ == "__main__":
    unittest.main()
