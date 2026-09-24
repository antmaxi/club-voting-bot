"""
test_handlers.py — Async handler tests for bookclub_bot.py

Tests Telegram command/callback handlers using mocked Update + Context.
No real Telegram API calls are made.
"""

import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

from telegram import (
    Bot,
    Chat,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ApplicationHandlerStop, ConversationHandler, InlineQueryHandler

import bookclub.config as cfg
import bookclub.membership as membership
import bookclub_bot as bot
from bookclub.handlers.add_flow import (
    add_edit_inline_query,
    add_go_edit,
    add_go_forward,
    add_go_save,
    build_add_prompt_text,
    send_add_prompt,
    typed_add_text,
)
from bookclub.notifications import vote_reminder_job

# ── Base test class with shared setUp ─────────────────────────────────────────


class BotHandlerTestCase(unittest.IsolatedAsyncioTestCase):
    """Base class: creates a fresh temp DB and standard mock update/ctx."""

    DB_FILE = "test_handlers.db"

    def setUp(self):
        cfg.DB_PATH = self.DB_FILE
        bot.DB_PATH = self.DB_FILE
        bot.init_db()

        # Disable ALLOWED_CHAT_ID so membership gate never blocks during tests
        self._orig_chat_id = cfg.ALLOWED_CHAT_ID
        cfg.ALLOWED_CHAT_ID = None
        bot.ALLOWED_CHAT_ID = None
        self._orig_llm_key = cfg.LLM_API_KEY
        cfg.LLM_API_KEY = ""
        # Chat completions stay the default even if the shell has LLM_PROVIDER=cursor.
        self._orig_llm_provider = cfg.LLM_PROVIDER
        cfg.LLM_PROVIDER = "chat"
        self._orig_activity_path = cfg.ACTIVITY_PATH
        cfg.ACTIVITY_PATH = f"{self.DB_FILE}.activity.json"

        # The membership cache is module-global; a verdict cached by one test
        # would otherwise leak into the next.
        bot._membership_cache.clear()
        membership._club_user_profiles.clear()
        membership._last_activity_write = None

        self.update = MagicMock(spec=Update)
        self.update.callback_query = None  # Explicitly set to None by default
        self.ctx = MagicMock()
        self.ctx.user_data = {"lang": "en"}
        self.ctx.bot = AsyncMock()

        self.message = AsyncMock(spec=Message)
        self.update.message = self.message
        self.update.effective_chat = MagicMock(spec=Chat)
        self.update.effective_chat.id = 12345
        self.update.effective_chat.type = "private"
        self.update.effective_user = MagicMock(spec=User)
        self.update.effective_user.id = 67890
        self.update.effective_user.full_name = "Test User"
        self.update.effective_user.username = "testuser"

        # Default: notifications off so /list_and_vote doesn't show opt-in prompt
        bot.db_set_user_setting(67890, "notify_new_books", 0)

    def tearDown(self):
        cfg.ALLOWED_CHAT_ID = self._orig_chat_id
        bot.ALLOWED_CHAT_ID = self._orig_chat_id
        cfg.LLM_API_KEY = self._orig_llm_key
        cfg.LLM_PROVIDER = self._orig_llm_provider
        cfg.ACTIVITY_PATH = self._orig_activity_path
        for path in (
            self.DB_FILE,
            f"{self.DB_FILE}-wal",
            f"{self.DB_FILE}-shm",
            f"{self.DB_FILE}.activity.json",
        ):
            if os.path.exists(path):
                os.remove(path)

    def _callback_query(self, data, user_id=None):
        """Return a mock callback query with the given data."""
        q = AsyncMock()
        q.data = data
        q.from_user = MagicMock()
        q.from_user.id = user_id or self.update.effective_user.id
        q.from_user.username = "testuser"
        self.update.callback_query = q
        return q

    def _add_book(
        self,
        title="Book",
        author="Author",
        pages=100,
        fiction=True,
        review="",
        desc="",
        user_id=1,
        username="u",
    ):
        return bot.db_add_book(
            title, author, pages, fiction, review, desc, user_id, username
        )


# ── /start and /help ───────────────────────────────────────────────────────────


class TestStartHelp(BotHandlerTestCase):

    @patch("bookclub.handlers.commands.set_user_commands", new_callable=AsyncMock)
    async def test_cmd_start_sends_welcome(self, mock_set):
        await bot.cmd_start(self.update, self.ctx)
        self.message.reply_text.assert_called_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Welcome", text)
        self.assertIn("/info", text)
        self.assertNotIn("/adminconsole", text)

    @patch("bookclub.handlers.commands.set_user_commands", new_callable=AsyncMock)
    async def test_cmd_start_admin_includes_adminconsole_in_help(self, mock_set):
        old = cfg.ADMIN_IDS[:]
        try:
            cfg.ADMIN_IDS = [self.update.effective_user.id]
            await bot.cmd_start(self.update, self.ctx)
            text = self.message.reply_text.call_args[0][0]
            self.assertIn("/adminconsole", text)
        finally:
            cfg.ADMIN_IDS = old

    @patch("bookclub.handlers.commands.set_user_commands", new_callable=AsyncMock)
    async def test_cmd_start_sets_menu(self, mock_set):
        await bot.cmd_start(self.update, self.ctx)
        mock_set.assert_called_once_with(self.ctx.bot, self.update, "en")

    @patch("bookclub.handlers.commands.set_user_commands", new_callable=AsyncMock)
    async def test_cmd_start_ru(self, mock_set):
        self.ctx.user_data["lang"] = "ru"
        await bot.cmd_start(self.update, self.ctx)
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Добро пожаловать", text)
        self.assertIn("/info", text)

    @patch("bookclub.handlers.commands.set_user_commands", new_callable=AsyncMock)
    async def test_cmd_help_delegates_to_start(self, mock_set):
        await bot.cmd_help(self.update, self.ctx)
        self.message.reply_text.assert_called_once()


# ── /info ─────────────────────────────────────────────────────────────────────


class TestInfo(BotHandlerTestCase):

    # Git reports the commit time as a Unix timestamp (--format=%ct), which the
    # handler renders via fmt_dt_utc into server-local time with a UTC offset.
    GIT_COMMIT_EPOCH = 1775649600  # 2026-04-04 12:00:00 UTC

    @patch("os.path.exists")
    @patch("subprocess.check_output")
    async def test_cmd_info_en(self, mock_git, mock_exists):
        mock_exists.return_value = True
        mock_git.return_value = f"{self.GIT_COMMIT_EPOCH}\n".encode()
        with patch.object(cfg, "GITHUB_REPO", "https://test.repo"):
            await bot.cmd_info(self.update, self.ctx)
        self.message.reply_text.assert_called_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Book Club Bot", text)
        import datetime

        expected = bot.fmt_dt_utc(
            datetime.datetime.fromtimestamp(self.GIT_COMMIT_EPOCH, tz=datetime.UTC)
        )
        self.assertIn(expected, text)
        self.assertRegex(text, r"UTC[+-]\d{2}:\d{2}")
        self.assertIn("https://test.repo", text)
        self.assertIn("@antmaxi", text)

    @patch("os.path.exists")
    @patch("subprocess.check_output")
    async def test_cmd_info_ru(self, mock_git, mock_exists):
        mock_exists.return_value = True
        self.ctx.user_data["lang"] = "ru"
        mock_git.return_value = f"{self.GIT_COMMIT_EPOCH}\n".encode()
        await bot.cmd_info(self.update, self.ctx)
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Последнее обновление", text)
        import datetime

        expected = bot.fmt_dt_utc(
            datetime.datetime.fromtimestamp(self.GIT_COMMIT_EPOCH, tz=datetime.UTC)
        )
        self.assertIn(expected, text)
        self.assertRegex(text, r"UTC[+-]\d{2}:\d{2}")
        self.assertIn("@antmaxi", text)

    @patch("os.path.exists")
    @patch("os.path.getmtime")
    @patch("subprocess.check_output")
    async def test_cmd_info_git_fallback_to_mtime(
        self, mock_git, mock_mtime, mock_exists
    ):
        # Git exists but fails
        mock_exists.return_value = True
        mock_git.side_effect = Exception("git error")
        mock_mtime.return_value = 1775728800

        await bot.cmd_info(self.update, self.ctx)

        text = self.message.reply_text.call_args[0][0]
        self.assertNotEqual(text, "unknown")
        # The exact string depends on local timezone, so build the expectation
        # the same way the handler does.
        import datetime

        expected_date = bot.fmt_dt_utc(
            datetime.datetime.fromtimestamp(1775728800, tz=datetime.UTC)
        )
        self.assertIn(expected_date, text)
        self.assertRegex(text, r"UTC[+-]\d{2}:\d{2}")

    @patch("os.path.exists")
    @patch("subprocess.check_output")
    async def test_cmd_info_no_git_dir_fallback(self, mock_git, mock_exists):
        # No .git directory
        mock_exists.return_value = False

        await bot.cmd_info(self.update, self.ctx)

        # Should not even call git
        mock_git.assert_not_called()

        text = self.message.reply_text.call_args[0][0]
        self.assertIn("20", text)  # Should have a date starting with 20...


# ── set_user_commands ──────────────────────────────────────────────────────────


class TestSetUserCommands(BotHandlerTestCase):

    @patch("bookclub.handlers.commands.BotCommandScopeChat")
    async def test_private_chat_uses_chat_scope(self, mock_scope_cls):
        mock_scope = MagicMock()
        mock_scope_cls.return_value = mock_scope
        mock_bot = AsyncMock()
        self.update.effective_chat.type = "private"

        await bot.set_user_commands(mock_bot, self.update, "en")

        mock_bot.delete_my_commands.assert_called_once_with(scope=mock_scope)
        mock_bot.set_my_commands.assert_called_once_with(
            bot.commands_for_user("en", self.update.effective_user.id),
            scope=mock_scope,
        )

    @patch("bookclub.handlers.commands.BotCommandScopeChatMember")
    async def test_group_chat_uses_member_scope(self, mock_scope_cls):
        mock_scope = MagicMock()
        mock_scope_cls.return_value = mock_scope
        mock_bot = AsyncMock()
        self.update.effective_chat.type = "supergroup"

        await bot.set_user_commands(mock_bot, self.update, "ru")

        mock_bot.set_my_commands.assert_called_once_with(
            bot.commands_for_user("ru", self.update.effective_user.id),
            scope=mock_scope,
        )

    async def test_commands_for_user_hides_adminconsole_for_members(self):
        old = cfg.ADMIN_IDS[:]
        try:
            cfg.ADMIN_IDS = [999999]
            member = bot.commands_for_user("en", 12345)
            self.assertNotIn("adminconsole", [c.command for c in member])
            admin = bot.commands_for_user("en", 999999)
            self.assertIn("adminconsole", [c.command for c in admin])
        finally:
            cfg.ADMIN_IDS = old

    async def test_set_user_commands_exception_does_not_propagate(self):
        mock_bot = AsyncMock()
        mock_bot.set_my_commands.side_effect = Exception("Network error")
        # Should not raise
        await bot.set_user_commands(mock_bot, self.update, "en")


# ── COMMANDS menu structure ────────────────────────────────────────────────────


class TestCommandsMenu(BotHandlerTestCase):

    async def test_settings_in_both_menus(self):
        for lang in bot.SUPPORTED_LANGS:
            cmds = [c.command for c in bot.COMMANDS[lang]]
            self.assertIn("settings", cmds, f"'settings' missing from {lang} menu")

    async def test_language_not_in_menus(self):
        for lang in bot.SUPPORTED_LANGS:
            cmds = [c.command for c in bot.COMMANDS[lang]]
            self.assertNotIn(
                "language", cmds, f"'language' should not be in {lang} menu"
            )

    async def test_add_command_description_en(self):
        desc = next(c.description for c in bot.COMMANDS["en"] if c.command == "add")
        self.assertEqual(desc, "➕ Add a book")

    async def test_settings_description_en(self):
        desc = next(
            c.description for c in bot.COMMANDS["en"] if c.command == "settings"
        )
        self.assertEqual(desc, "⚙️ Settings")

    async def test_settings_description_ru(self):
        desc = next(
            c.description for c in bot.COMMANDS["ru"] if c.command == "settings"
        )
        self.assertEqual(desc, "⚙️ Настройки")

    async def test_info_in_both_menus(self):
        for lang in bot.SUPPORTED_LANGS:
            cmds = [c.command for c in bot.COMMANDS[lang]]
            self.assertIn("info", cmds, f"'info' missing from {lang} menu")

    async def test_info_description_en(self):
        desc = next(c.description for c in bot.COMMANDS["en"] if c.command == "info")
        self.assertEqual(desc, "ℹ️ About the bot")

    async def test_info_description_ru(self):
        desc = next(c.description for c in bot.COMMANDS["ru"] if c.command == "info")
        self.assertEqual(desc, "ℹ️ О боте")

    async def test_info_description_de(self):
        desc = next(c.description for c in bot.COMMANDS["de"] if c.command == "info")
        self.assertEqual(desc, "ℹ️ Über den Bot")

    async def test_info_after_help_in_menu(self):
        for lang in bot.SUPPORTED_LANGS:
            cmds = [c.command for c in bot.COMMANDS[lang]]
            self.assertEqual(cmds[-2:], ["help", "info"])


# ── /list_and_vote ─────────────────────────────────────────────────────────────


class TestList(BotHandlerTestCase):

    async def test_cmd_list_shows_prompt(self):
        await bot.cmd_list(self.update, self.ctx)
        self.message.reply_text.assert_called_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Show all books", text)
        self.assertIn("reply_markup", self.message.reply_text.call_args[1])

    async def test_list_choice_all_shows_format_prompt(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        q = self._callback_query("list:all")
        await bot.list_choice_cb(self.update, self.ctx)
        q.edit_message_text.assert_called_once()
        self.assertIn("How would you like", q.edit_message_text.call_args[0][0])
        q.delete_message.assert_not_called()

    async def test_list_choice_all_sends_book(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        self._add_book("Book 1")
        q = self._callback_query("list:all:full")
        await bot.list_choice_cb(self.update, self.ctx)
        q.answer.assert_called_once()
        q.delete_message.assert_not_called()
        q.edit_message_text.assert_called_once()
        self.assertIn("Book 1", q.edit_message_text.call_args[0][0])

    async def test_list_choice_unvoted_excludes_voted_book(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        book_id = self._add_book("Book 1")
        bot.db_cast_vote(self.update.effective_user.id, book_id, 1)
        q = self._callback_query("list:unvoted:full")
        await bot.list_choice_cb(self.update, self.ctx)
        self.ctx.bot.send_message.assert_not_called()
        q.edit_message_text.assert_called_once()
        self.assertIn("voted on all", q.edit_message_text.call_args[0][0])

    async def test_list_choice_all_no_books(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        q = self._callback_query("list:all:compact")
        await bot.list_choice_cb(self.update, self.ctx)
        self.ctx.bot.send_message.assert_not_called()

    async def test_list_choice_multiple_books(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        self._add_book("Alpha")
        self._add_book("Beta")
        q = self._callback_query("list:all:full")
        await bot.list_choice_cb(self.update, self.ctx)
        self.ctx.bot.send_message.assert_not_called()
        markup = q.edit_message_text.call_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertTrue(
            any(
                callback.startswith("book_page:") and callback.endswith(":1")
                for callback in callbacks
            )
        )

    async def test_full_card_pagination_keeps_initial_order_after_vote(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        first = self._add_book("Alpha")
        self._add_book("Beta")
        q = self._callback_query("list:all:full")
        await bot.list_choice_cb(self.update, self.ctx)
        markup = q.edit_message_text.call_args.kwargs["reply_markup"]
        next_callback = next(
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if (button.callback_data or "").startswith("book_page:")
            and (button.callback_data or "").endswith(":1")
        )

        bot.db_cast_vote(self.update.effective_user.id, first, -1)
        q = self._callback_query(next_callback)
        await bot.book_page_cb(self.update, self.ctx)

        text = q.edit_message_text.call_args.args[0]
        self.assertIn("Beta", text)
        self.assertNotIn("Alpha", text)

    async def test_list_choice_compact_single_message(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        alpha_id = bot.db_add_book(
            "Alpha", "Author A", 100, True, "", "", 1, "u", creation_year=2001
        )
        bot.db_add_book("Beta", "Author B", 100, True, "", "", 1, "u")
        bot.db_cast_vote(1001, alpha_id, 1)
        bot.db_cast_vote(1002, alpha_id, 1)
        q = self._callback_query("list:all:compact")
        await bot.list_choice_cb(self.update, self.ctx)
        self.ctx.bot.send_message.assert_called_once()
        text = self.ctx.bot.send_message.call_args[1]["text"]
        self.assertIn("Alpha", text)
        self.assertIn("Author A", text)
        self.assertIn("(2001)", text)
        self.assertIn("Beta", text)
        self.assertIn("Author B", text)
        self.assertIn("<b>2</b> <b>Alpha</b>", text)
        self.assertIn("<b>0</b> <b>Beta</b>", text)

    async def test_list_triggers_optin_when_setting_missing(self):
        """First-time users without a notify setting see the opt-in prompt."""
        with sqlite3.connect(bot.DB_PATH) as conn:
            conn.execute(
                "DELETE FROM user_settings WHERE user_id=?",
                (self.update.effective_user.id,),
            )
        q = self._callback_query("list:all")
        await bot.list_choice_cb(self.update, self.ctx)
        q.edit_message_text.assert_called_once()
        self.assertIn(
            "Would you like to receive notifications",
            q.edit_message_text.call_args[0][0],
        )
        self.assertEqual(self.ctx.user_data["pending_list_choice"], "all")

    async def test_list_no_optin_when_setting_already_set(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        self._add_book("B")
        q = self._callback_query("list:all")
        await bot.list_choice_cb(self.update, self.ctx)
        q.edit_message_text.assert_called_once()
        self.assertIn("How would you like", q.edit_message_text.call_args[0][0])
        self.ctx.bot.send_message.assert_not_called()


# ── /top ──────────────────────────────────────────────────────────────────────


class TestTop(BotHandlerTestCase):

    async def test_cmd_top_no_books(self):
        await bot.cmd_top(self.update, self.ctx)
        self.message.reply_text.assert_called_once()
        self.assertIn("No undiscussed", self.message.reply_text.call_args[0][0])

    async def test_cmd_top_shows_top_5(self):
        """With 7 books of different scores, only top 5 appear."""
        for i in range(1, 6):
            bid = self._add_book(f"Book {i}", user_id=i)
            bot.db_cast_vote(1001, bid, 1)
        for i in range(6, 8):
            bid = self._add_book(f"Book {i}", user_id=i)
            bot.db_cast_vote(1001, bid, -1)

        await bot.cmd_top(self.update, self.ctx)

        # First reply_text call is the top list
        text = self.message.reply_text.call_args_list[0][0][0]
        for i in range(1, 6):
            self.assertIn(f"Book {i}", text)
        self.assertNotIn("Book 6", text)
        self.assertNotIn("Book 7", text)

    async def test_cmd_top_tie_at_5th_includes_tied_books(self):
        """If books 5 and 6 have identical score+votes, both are shown."""
        for i in range(1, 6):
            bid = self._add_book(f"Book {i}", user_id=i)
            bot.db_cast_vote(1001, bid, 1)
        # Books 6 & 7 also score 1 with 1 vote each — tied with Book 5
        for i in range(6, 8):
            bid = self._add_book(f"Book {i}", user_id=i)
            bot.db_cast_vote(1001, bid, 1)

        await bot.cmd_top(self.update, self.ctx)

        text = self.message.reply_text.call_args_list[0][0][0]
        for i in range(1, 8):
            self.assertIn(f"Book {i}", text)

    async def test_cmd_top_score_calc_button_sent(self):
        self._add_book("B")
        await bot.cmd_top(self.update, self.ctx)
        # Last call should have the score calc button
        last_call = self.message.reply_text.call_args_list[-1]
        kwargs = last_call[1]
        self.assertIn("reply_markup", kwargs)
        btn = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(btn.callback_data, "score_calc_info")

    async def test_cmd_top_score_calc_button_text_en(self):
        self._add_book("B")
        await bot.cmd_top(self.update, self.ctx)
        last_call = self.message.reply_text.call_args_list[-1]
        btn = last_call[1]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(btn.text, "📊 How a score is calculated")

    async def test_cmd_top_ru(self):
        self.ctx.user_data["lang"] = "ru"
        self._add_book("Книга 1")
        await bot.cmd_top(self.update, self.ctx)
        text = self.message.reply_text.call_args_list[0][0][0]
        self.assertIn("Топ книг", text)

    async def test_cmd_top_includes_unvoted_books(self):
        """Books with no votes should still appear in /top."""
        self._add_book("Unvoted")
        await bot.cmd_top(self.update, self.ctx)
        text = self.message.reply_text.call_args_list[0][0][0]
        self.assertIn("Unvoted", text)


# ── score_calc_cb ──────────────────────────────────────────────────────────────


class TestScoreCalc(BotHandlerTestCase):

    async def test_score_calc_cb_shows_alert(self):
        q = self._callback_query("score_calc_info")
        await bot.score_calc_cb(self.update, self.ctx)
        q.answer.assert_called_once()
        kwargs = q.answer.call_args[1]
        self.assertTrue(kwargs["show_alert"])
        self.assertIn("Want: +1 point", kwargs["text"])

    async def test_score_calc_cb_ru(self):
        self.ctx.user_data["lang"] = "ru"
        q = self._callback_query("score_calc_info")
        await bot.score_calc_cb(self.update, self.ctx)
        kwargs = q.answer.call_args[1]
        self.assertIn("Хочу: +1 балл", kwargs["text"])

    async def test_score_calc_cb_includes_attendance_when_toggled(self):
        bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
        q = self._callback_query("score_calc_info")
        await bot.score_calc_cb(self.update, self.ctx)
        text = q.answer.call_args[1]["text"]
        self.assertIn("Attendance:", text)
        self.assertIn("surplus", text)
        self.assertLessEqual(len(text), 200)

    async def test_score_calc_cb_attendance_ru_fits_alert(self):
        bot.db_set_admin_setting(bot.VOTES_USE_ATTENDANCE_KEY, 1)
        self.ctx.user_data["lang"] = "ru"
        q = self._callback_query("score_calc_info")
        await bot.score_calc_cb(self.update, self.ctx)
        text = q.answer.call_args[1]["text"]
        self.assertIn("Посещаемость:", text)
        self.assertLessEqual(len(text), 200)


# ── /add conversation ──────────────────────────────────────────────────────────


class TestAddConversation(BotHandlerTestCase):

    async def test_cmd_add_returns_adding_title(self):
        state = await bot.cmd_add(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_TITLE)
        self.message.reply_text.assert_called_once()
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        self.assertIn(bot.CONV_CANCEL, self._keyboard_callback_data(markup))

    @patch.object(cfg, "LLM_API_KEY", "sk-test")
    async def test_cmd_add_with_llm_shows_start_choices(self):
        state = await bot.cmd_add(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_START)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        data = self._keyboard_callback_data(markup)
        self.assertIn("add_start:ai", data)
        self.assertIn("add_start:manual", data)
        self.assertNotIn("add_start:drafts", data)

    async def test_cmd_add_with_drafts_offers_continue(self):
        bot.db_insert_add_draft(
            self.update.effective_user.id,
            "Saved Book",
            {"new_book": {"title": "Saved Book"}, "add_state": bot.ADDING_AUTHOR},
        )
        state = await bot.cmd_add(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_START)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        data = self._keyboard_callback_data(markup)
        self.assertIn("add_start:drafts", data)
        self.assertIn("add_start:manual", data)

    async def test_add_title_stores_and_advances(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "My Book"
        state = await bot.add_title(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["title"], "My Book")
        self.assertEqual(state, bot.ADDING_REVIEW)

    async def test_regular_add_does_not_call_llm(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "My Book"
        with (
            patch("bookclub.handlers.add.suggest_book_fields") as mocked,
            patch("bookclub.handlers.add.suggest_review_link") as mocked_link,
        ):
            await bot.add_title(self.update, self.ctx)
        mocked.assert_not_called()
        mocked_link.assert_not_called()

    @patch.object(cfg, "LLM_API_KEY", "sk-test")
    async def test_add_title_asks_ai_choice_when_llm_configured(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "My Book"
        with patch("bookclub.handlers.add.suggest_book_fields") as mocked:
            state = await bot.add_title(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_AI_CHOOSE)
        mocked.assert_not_called()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("AI help", text)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        data = self._keyboard_callback_data(markup)
        self.assertIn("add_ai:yes", data)
        self.assertIn("add_ai:no", data)
        self.assertIn(bot.CONV_CANCEL, data)

    @patch.object(cfg, "LLM_API_KEY", "sk-test")
    @patch("bookclub.handlers.add.suggest_review_link")
    async def test_add_ai_yes_applies_suggestions(self, mock_suggest):
        mock_suggest.return_value = (
            "https://en.wikipedia.org/wiki/War_and_Peace",
            None,
        )
        self.ctx.user_data["new_book"] = {"title": "War and Peace"}
        self.ctx.user_data["add_state"] = bot.ADDING_AI_CHOOSE
        self._callback_query("add_ai:yes")
        state = await bot.add_ai_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_REVIEW)
        nb = self.ctx.user_data["new_book"]
        self.assertEqual(
            nb["review_link"], "https://en.wikipedia.org/wiki/War_and_Peace"
        )
        self.assertNotIn("author", nb)
        self.assertIn("review_link", self.ctx.user_data["llm_filled_keys"])
        texts = [c[0][0] for c in self.message.reply_text.call_args_list]
        q = self.update.callback_query
        q_texts = [c[0][0] for c in q.edit_message_text.call_args_list]
        self.assertTrue(any("wikipedia.org" in t for t in texts))
        self.assertTrue(any("Suggested" in t for t in texts))
        self.assertTrue(any("Looking up a review page" in t for t in q_texts))
        mock_suggest.assert_called_once()
        self.assertEqual(mock_suggest.call_args[0][0], "War and Peace")

    @patch.object(cfg, "LLM_API_KEY", "sk-test")
    @patch("bookclub.handlers.add.suggest_review_link")
    async def test_add_ai_no_skips_llm(self, mock_suggest):
        self.ctx.user_data["new_book"] = {"title": "Mystery Title"}
        self.ctx.user_data["add_state"] = bot.ADDING_AI_CHOOSE
        q = self._callback_query("add_ai:no")
        state = await bot.add_ai_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_REVIEW)
        self.assertNotIn("review_link", self.ctx.user_data["new_book"])
        mock_suggest.assert_not_called()
        q.edit_message_text.assert_called()
        self.assertFalse(self.ctx.user_data.get("llm_add"))

    @patch("bookclub.handlers.add.suggest_review_link")
    async def test_add_ai_yes_without_llm_still_advances(self, mock_suggest):
        mock_suggest.return_value = (None, "not_configured")
        self.ctx.user_data["new_book"] = {"title": "Mystery Title"}
        self.ctx.user_data["add_state"] = bot.ADDING_AI_CHOOSE
        self._callback_query("add_ai:yes")
        state = await bot.add_ai_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_REVIEW)
        self.assertNotIn("review_link", self.ctx.user_data["new_book"])
        texts = [c[0][0] for c in self.message.reply_text.call_args_list]
        q_texts = [
            c[0][0] for c in self.update.callback_query.edit_message_text.call_args_list
        ]
        self.assertTrue(any("LLM_API_KEY" in t for t in texts + q_texts))

    @patch("bookclub.handlers.add.suggest_review_link")
    async def test_add_ai_llm_failure_still_advances(self, mock_suggest):
        mock_suggest.return_value = (None, "auth: HTTP 401: Incorrect API key")
        self.ctx.user_data["new_book"] = {"title": "Mystery Title"}
        self.ctx.user_data["add_state"] = bot.ADDING_AI_CHOOSE
        self._callback_query("add_ai:yes")
        state = await bot.add_ai_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_REVIEW)
        failed = [
            c
            for c in self.message.reply_text.call_args_list
            if "Incorrect API key" in (c[0][0] if c[0] else "")
        ]
        self.assertTrue(failed)
        failed_text = failed[0][0][0]
        self.assertIn("Could not fetch suggestions", failed_text)
        self.assertIn("API key / auth", failed_text)
        self.assertIn("401", failed_text)
        self.assertIsNone(failed[0].kwargs.get("parse_mode"))

    @patch("bookclub.handlers.add.suggest_review_link")
    async def test_add_ai_llm_failure_keeps_htmlish_detail(self, mock_suggest):
        mock_suggest.return_value = (
            None,
            'bad_request: HTTP 400: {"error":"<invalid>"}',
        )
        self.ctx.user_data["new_book"] = {"title": "Mystery Title"}
        self.ctx.user_data["add_state"] = bot.ADDING_AI_CHOOSE
        self._callback_query("add_ai:yes")
        await bot.add_ai_cb(self.update, self.ctx)
        failed = [
            c[0][0]
            for c in self.message.reply_text.call_args_list
            if "<invalid>" in (c[0][0] if c[0] else "")
        ]
        self.assertTrue(failed)
        self.assertIn("bad request", failed[0].casefold())

    async def test_add_ai_override_author_clears_suggestion_flag(self):
        self.ctx.user_data["llm_add"] = True
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Suggested"}
        self.ctx.user_data["llm_filled_keys"] = {"author", "pages"}
        self.ctx.user_data["llm_suggestions_applied"] = True
        self.message.text = "Correct Author"
        state = await bot.add_author(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_PAGES)
        self.assertEqual(self.ctx.user_data["new_book"]["author"], "Correct Author")
        self.assertNotIn("author", self.ctx.user_data["llm_filled_keys"])
        self.assertIn("pages", self.ctx.user_data["llm_filled_keys"])

    async def test_add_title_similar_warns_before_author(self):
        self._add_book("War and Peace")
        self.ctx.user_data["new_book"] = {}
        self.message.text = "War and Peace Extended"
        state = await bot.add_title(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_TITLE_CONFIRM)
        warning = self.message.reply_text.call_args[0][0]
        self.assertIn("War and Peace", warning)

    async def test_add_title_similar_confirm_advances(self):
        self._add_book("War and Peace")
        self.ctx.user_data["new_book"] = {"title": "War and Peace Extended"}
        q = self._callback_query("title_sim:yes")
        state = await bot.add_title_similar_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_REVIEW)
        q.edit_message_text.assert_called_once()

    async def test_add_author_stores_and_advances(self):
        self.ctx.user_data["new_book"] = {"title": "T"}
        self.message.text = "Jane Austen"
        state = await bot.add_author(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["author"], "Jane Austen")
        self.assertEqual(state, bot.ADDING_PAGES)

    async def test_add_author_typed_text_replaces_saved_value(self):
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Old Author"}
        self.message.text = "Jane Austen"
        state = await bot.add_author(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["author"], "Jane Austen")
        self.assertEqual(state, bot.ADDING_PAGES)

    async def test_add_title_typed_text_replaces_saved_value(self):
        self.ctx.user_data["new_book"] = {"title": "Old Title"}
        self.message.text = "New Title"
        state = await bot.add_title(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["title"], "New Title")
        self.assertEqual(state, bot.ADDING_REVIEW)

    async def test_add_pages_valid(self):
        self.ctx.user_data["new_book"] = {"title": "T", "author": "A"}
        self.message.text = "320"
        state = await bot.add_pages(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["pages"], 320)
        self.assertEqual(state, bot.ADDING_FICTION)

    async def test_add_pages_invalid_text(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "not a number"
        state = await bot.add_pages(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_PAGES)
        self.assertIn("valid number", self.message.reply_text.call_args[0][0])

    async def test_add_pages_zero_invalid(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "0"
        state = await bot.add_pages(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_PAGES)

    async def test_add_back_from_pages_returns_author(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "Author Name",
            "pages": 100,
        }
        self.ctx.user_data["add_state"] = bot.ADDING_PAGES
        self.message.text = "/back"
        state = await bot.add_go_back(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_AUTHOR)
        self.assertEqual(self.ctx.user_data["add_state"], bot.ADDING_AUTHOR)
        reply = self.message.reply_text.call_args[0][0]
        self.assertIn("Author Name", reply)

    async def test_add_back_callback_from_fiction_returns_pages(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 200,
            "fiction": True,
        }
        self.ctx.user_data["add_state"] = bot.ADDING_FICTION
        q = self._callback_query("add_back")
        state = await bot.add_go_back(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_PAGES)
        q.edit_message_text.assert_called_once()
        text = q.edit_message_text.call_args[0][0]
        self.assertIn("200", text)

    def _keyboard_callback_data(self, markup):
        return [btn.callback_data for row in markup.inline_keyboard for btn in row]

    async def test_add_forward_keeps_author_and_advances(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "Author Name",
            "pages": 100,
        }
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        q = self._callback_query("add_forward")
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_PAGES)
        self.assertEqual(self.ctx.user_data["new_book"]["author"], "Author Name")
        text = q.edit_message_text.call_args[0][0]
        self.assertIn("100", text)

    async def test_add_forward_command_from_title_skips_retype(self):
        self.ctx.user_data["new_book"] = {"title": "Kept Title", "author": "A"}
        self.ctx.user_data["add_state"] = bot.ADDING_TITLE
        self.message.text = "/forward"
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_REVIEW)
        self.assertEqual(self.ctx.user_data["new_book"]["title"], "Kept Title")
        reply = self.message.reply_text.call_args[0][0]
        self.assertIn("review", reply.lower())

    async def test_add_forward_without_value_stays(self):
        self.ctx.user_data["new_book"] = {"title": "T"}
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        q = self._callback_query("add_forward")
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_AUTHOR)
        q.answer.assert_awaited()
        self.assertTrue(q.answer.call_args[1].get("show_alert"))

    async def test_add_forward_from_description_stays(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 10,
            "fiction": True,
            "review_link": "http://x.com",
            "original_language": "German",
            "creation_year": 1984,
        }
        self.ctx.user_data["add_state"] = bot.ADDING_DESCRIPTION
        q = self._callback_query("add_forward")
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_DESCRIPTION)
        q.answer.assert_awaited()

    async def test_add_forward_from_description_with_value_completes(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 10,
            "fiction": True,
            "review_link": "http://x.com",
            "original_language": "German",
            "creation_year": 1984,
            "description": "Kept description",
        }
        self.ctx.user_data["add_state"] = bot.ADDING_DESCRIPTION
        self.ctx.job_queue = MagicMock()
        q = self._callback_query("add_forward")
        q.message = None
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_CONFIRM)
        self.assertEqual(bot.db_get_books(discussed=False), [])
        self._callback_query("add_confirm")
        state = await bot.add_confirm_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        book = bot.db_get_books(discussed=False)[0]
        self.assertEqual(book["description"], "Kept description")

    async def test_add_forward_after_skipped_year(self):
        self.ctx.user_data["new_book"] = {"creation_year": None}
        self.ctx.user_data["add_state"] = bot.ADDING_CREATION_YEAR
        q = self._callback_query("add_forward")
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_DESCRIPTION)
        self.assertIsNone(self.ctx.user_data["new_book"]["creation_year"])
        q.edit_message_text.assert_called_once()

    @patch.object(cfg, "ENTRY_FIELDS", frozenset({"description"}))
    async def test_add_title_skips_to_description_when_others_disabled(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "Only Desc Title"
        state = await bot.add_title(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_DESCRIPTION)
        self.assertEqual(self.ctx.user_data["new_book"]["title"], "Only Desc Title")

    @patch.object(cfg, "ENTRY_FIELDS", frozenset())
    async def test_add_title_completes_when_no_optional_fields(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "Title Only Book"
        self.ctx.job_queue = MagicMock()
        state = await bot.add_title(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_CONFIRM)
        self.assertIn("new_book", self.ctx.user_data)
        preview = self.message.reply_text.call_args[0][0]
        self.assertIn("Title Only Book", preview)
        self._callback_query("add_confirm")
        state = await bot.add_confirm_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        self.assertNotIn("new_book", self.ctx.user_data)
        reply = self.message.reply_text.call_args[0][0]
        self.assertIn("Title Only Book", reply)
        self.assertIn("added", reply.lower())

    async def test_add_prompt_shows_forward_when_value_saved(self):
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Author Name"}
        state = await send_add_prompt(self.update, self.ctx, bot.ADDING_AUTHOR)
        self.assertEqual(state, bot.ADDING_AUTHOR)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        data = self._keyboard_callback_data(markup)
        self.assertIn("add_back", data)
        self.assertIn("add_forward", data)
        self.assertIn("add_save", data)
        self.assertIn(bot.CONV_CANCEL, data)
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("send a new value", text)
        edit = next(
            btn
            for row in markup.inline_keyboard
            for btn in row
            if btn.copy_text is not None
        )
        self.assertEqual(edit.copy_text.text, "Author Name")

    async def test_add_prompt_shows_suggested_value_for_llm_fields(self):
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Leo"}
        self.ctx.user_data["llm_filled_keys"] = {"author"}
        await send_add_prompt(self.update, self.ctx, bot.ADDING_AUTHOR)
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Suggested", text)
        self.assertIn("Leo", text)
        self.assertIn("send a new value", text)
        self.assertIn("Edit", text)

    async def test_add_prompt_edit_copies_short_suggestion(self):
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Leo Tolstoy"}
        self.ctx.user_data["llm_filled_keys"] = {"author"}
        await send_add_prompt(self.update, self.ctx, bot.ADDING_AUTHOR)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        edit = next(
            btn
            for row in markup.inline_keyboard
            for btn in row
            if btn.copy_text is not None
        )
        self.assertEqual(edit.copy_text.text, "Leo Tolstoy")
        self.assertIn("add_forward", self._keyboard_callback_data(markup))

    async def test_add_prompt_edit_uses_inline_when_supported(self):
        self.ctx.bot.supports_inline_queries = True
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Leo Tolstoy"}
        self.ctx.user_data["llm_filled_keys"] = {"author"}
        await send_add_prompt(self.update, self.ctx, bot.ADDING_AUTHOR)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        edit = next(
            btn
            for row in markup.inline_keyboard
            for btn in row
            if btn.switch_inline_query_current_chat
        )
        self.assertEqual(edit.switch_inline_query_current_chat, "Leo Tolstoy")

    async def test_add_prompt_long_suggestion_uses_edit_callback(self):
        desc = "x" * 300
        self.ctx.user_data["new_book"] = {"title": "T", "description": desc}
        self.ctx.user_data["llm_filled_keys"] = {"description"}
        await send_add_prompt(self.update, self.ctx, bot.ADDING_DESCRIPTION)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        self.assertIn("add_edit", self._keyboard_callback_data(markup))

    async def test_add_prompt_hides_edit_without_value(self):
        self.ctx.user_data["new_book"] = {"title": "T"}
        await send_add_prompt(self.update, self.ctx, bot.ADDING_AUTHOR)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        self.assertFalse(
            any(
                btn.copy_text
                or btn.switch_inline_query_current_chat
                or btn.callback_data == "add_edit"
                for row in markup.inline_keyboard
                for btn in row
            )
        )

    async def test_add_prompt_title_edit_copies_saved_title(self):
        self.ctx.user_data["new_book"] = {"title": "War and Peace"}
        await send_add_prompt(self.update, self.ctx, bot.ADDING_TITLE)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        edit = next(
            btn
            for row in markup.inline_keyboard
            for btn in row
            if btn.copy_text is not None
        )
        self.assertEqual(edit.copy_text.text, "War and Peace")
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("send a new", text)

    async def test_add_go_edit_sends_suggestion_for_reply(self):
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Leo Tolstoy"}
        self.ctx.user_data["llm_filled_keys"] = {"author"}
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        q = self._callback_query("add_edit")
        q.message = AsyncMock()
        state = await add_go_edit(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_AUTHOR)
        q.message.reply_text.assert_awaited()
        text = q.message.reply_text.call_args[0][0]
        self.assertIn("Leo Tolstoy", text)
        self.assertIsInstance(
            q.message.reply_text.call_args[1]["reply_markup"], ForceReply
        )

    async def test_add_go_edit_own_saved_value(self):
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Jane Austen"}
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        q = self._callback_query("add_edit")
        q.message = AsyncMock()
        state = await add_go_edit(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_AUTHOR)
        text = q.message.reply_text.call_args[0][0]
        self.assertIn("Jane Austen", text)

    async def test_add_go_edit_without_suggestion_stays(self):
        self.ctx.user_data["new_book"] = {"title": "T"}
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        q = self._callback_query("add_edit")
        state = await add_go_edit(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_AUTHOR)
        q.answer.assert_awaited()
        self.assertTrue(q.answer.call_args[1].get("show_alert"))

    async def test_add_save_without_title_alerts(self):
        self.ctx.user_data["new_book"] = {}
        self.ctx.user_data["add_state"] = bot.ADDING_TITLE
        q = self._callback_query("add_save")
        state = await add_go_save(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_TITLE)
        self.assertTrue(q.answer.call_args[1].get("show_alert"))
        self.assertEqual(bot.db_list_add_drafts(self.update.effective_user.id), [])

    async def test_add_save_and_resume_keeps_unedited_ai_fields(self):
        uid = self.update.effective_user.id
        self.ctx.user_data["new_book"] = {
            "title": "War and Peace",
            "author": "Leo Tolstoy",
        }
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        self.ctx.user_data["llm_add"] = True
        self.ctx.user_data["llm_filled_keys"] = {"author"}
        q = self._callback_query("add_save")
        state = await add_go_save(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_AUTHOR)
        drafts = bot.db_list_add_drafts(uid)
        self.assertEqual(len(drafts), 1)
        draft_id, title = drafts[0]
        self.assertEqual(title, "War and Peace")
        q.answer.assert_awaited()

        start = await bot.cmd_add(self.update, self.ctx)
        self.assertEqual(start, bot.ADDING_START)
        self._callback_query("add_start:drafts")
        listed = await bot.add_start_cb(self.update, self.ctx)
        self.assertEqual(listed, bot.ADDING_DRAFT_CHOOSE)
        self._callback_query(f"add_draft:{draft_id}")
        resumed = await bot.add_draft_cb(self.update, self.ctx)
        self.assertEqual(resumed, bot.ADDING_AUTHOR)
        self.assertEqual(self.ctx.user_data["new_book"]["author"], "Leo Tolstoy")
        self.assertEqual(self.ctx.user_data["llm_filled_keys"], {"author"})
        self.assertTrue(self.ctx.user_data["llm_add"])
        text = self.update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Suggested", text)

    async def test_add_save_after_edit_drops_ai_flag_on_resume(self):
        uid = self.update.effective_user.id
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Correct Author"}
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        self.ctx.user_data["llm_add"] = True
        self.ctx.user_data["llm_filled_keys"] = set()
        await add_go_save(self.update, self.ctx)
        draft_id = bot.db_list_add_drafts(uid)[0][0]
        await bot.cmd_add(self.update, self.ctx)
        self._callback_query("add_start:drafts")
        await bot.add_start_cb(self.update, self.ctx)
        self._callback_query(f"add_draft:{draft_id}")
        await bot.add_draft_cb(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["llm_filled_keys"], set())
        text = self.update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Current", text)
        self.assertNotIn("Suggested", text)

    async def test_complete_add_deletes_draft(self):
        uid = self.update.effective_user.id
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 100,
            "fiction": True,
            "review_link": "http://x.com",
        }
        self.ctx.user_data["add_draft_id"] = bot.db_insert_add_draft(
            uid, "T", {"new_book": {"title": "T"}}
        )
        self.message.text = "Desc"
        self.ctx.job_queue = None
        await bot.add_description(self.update, self.ctx)
        self.assertEqual(len(bot.db_list_add_drafts(uid)), 1)
        self._callback_query("add_confirm")
        await bot.add_confirm_cb(self.update, self.ctx)
        self.assertEqual(bot.db_list_add_drafts(uid), [])

    async def test_add_draft_delete_removes_row(self):
        uid = self.update.effective_user.id
        draft_id = bot.db_insert_add_draft(
            uid, "Gone", {"new_book": {"title": "Gone"}, "add_state": bot.ADDING_AUTHOR}
        )
        self._callback_query(f"add_draft_del:{draft_id}")
        state = await bot.add_draft_del_cb(self.update, self.ctx)
        self.assertEqual(bot.db_list_add_drafts(uid), [])
        self.assertEqual(state, bot.ADDING_START)

    @patch.object(cfg, "LLM_API_KEY", "sk-test")
    @patch("bookclub.handlers.add.suggest_review_link")
    async def test_start_ai_choice_skips_second_ask(self, mock_suggest):
        mock_suggest.return_value = (
            "https://en.wikipedia.org/wiki/Pride_and_Prejudice",
            None,
        )
        self.ctx.user_data["new_book"] = {}
        self.ctx.user_data["llm_add"] = True
        self.ctx.user_data["add_from_start"] = True
        self.message.text = "Pride and Prejudice"
        state = await bot.add_title(self.update, self.ctx)
        mock_suggest.assert_called_once()
        self.assertEqual(
            self.ctx.user_data["new_book"]["review_link"],
            "https://en.wikipedia.org/wiki/Pride_and_Prejudice",
        )
        self.assertEqual(state, bot.ADDING_REVIEW)

    async def test_add_edit_inline_query_returns_typed_value(self):
        self.ctx.user_data["new_book"] = {"title": "T", "author": "Leo"}
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        iq = AsyncMock()
        iq.query = "Leo Tolstoy"
        iq.chat_type = "private"
        self.update.inline_query = iq
        await add_edit_inline_query(self.update, self.ctx)
        results = iq.answer.call_args[0][0]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].input_message_content.message_text, "Leo Tolstoy")

    async def test_add_edit_inline_query_empty_outside_add(self):
        iq = AsyncMock()
        iq.query = "nope"
        iq.chat_type = "private"
        self.update.inline_query = iq
        await add_edit_inline_query(self.update, self.ctx)
        results = iq.answer.call_args[0][0]
        self.assertEqual(results, [])

    def test_typed_add_text_strips_bot_username(self):
        self.ctx.bot.username = "clubbot"
        self.message.text = "@clubbot Leo Tolstoy"
        self.assertEqual(typed_add_text(self.update, self.ctx), "Leo Tolstoy")

    async def test_add_author_strips_inline_prefix(self):
        self.ctx.bot.username = "clubbot"
        self.ctx.user_data["new_book"] = {"title": "T"}
        self.message.text = "@clubbot Jane Austen"
        state = await bot.add_author(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["author"], "Jane Austen")
        self.assertEqual(state, bot.ADDING_PAGES)

    async def test_add_prompt_hides_forward_when_value_missing(self):
        self.ctx.user_data["new_book"] = {"title": "T"}
        await send_add_prompt(self.update, self.ctx, bot.ADDING_AUTHOR)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        data = self._keyboard_callback_data(markup)
        self.assertIn("add_back", data)
        self.assertNotIn("add_forward", data)
        self.assertIn(bot.CONV_CANCEL, data)

    async def test_add_fiction_prompt_has_cancel_button(self):
        self.ctx.user_data["new_book"] = {"title": "T"}
        await send_add_prompt(self.update, self.ctx, bot.ADDING_FICTION)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        data = self._keyboard_callback_data(markup)
        self.assertIn("fiction:1", data)
        self.assertIn(bot.CONV_CANCEL, data)

    async def test_add_forward_several_steps_returns_to_later_field(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 100,
            "fiction": True,
            "review_link": "http://x.com",
        }
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        self._callback_query("add_forward")
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_PAGES)
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_FICTION)
        state = await add_go_forward(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_ORIGINAL_LANGUAGE)
        self.assertEqual(self.ctx.user_data["new_book"]["pages"], 100)
        self.assertTrue(self.ctx.user_data["new_book"]["fiction"])

    async def test_add_current_original_language_follows_ui_language(self):
        self.ctx.user_data["lang"] = "ru"
        nb = {"original_language": "German"}
        text = build_add_prompt_text(self.ctx, bot.ADDING_ORIGINAL_LANGUAGE, nb)
        self.assertIn("Немецкий", text)
        self.assertNotIn("German", text)

    async def test_add_review_valid(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "https://goodreads.com/book/1"
        state = await bot.add_review(self.update, self.ctx)
        self.assertEqual(
            self.ctx.user_data["new_book"]["review_link"],
            "https://goodreads.com/book/1",
        )
        self.assertEqual(state, bot.ADDING_AUTHOR)

    @patch("bookclub.handlers.add.suggest_fields_after_review")
    async def test_add_review_ai_reads_pages_from_page(self, mock_from_page):
        mock_from_page.return_value = (
            {"author": "Leo Tolstoy", "pages": 1225, "fiction": True},
            None,
        )
        self.ctx.user_data["llm_add"] = True
        self.ctx.user_data["new_book"] = {"title": "War and Peace"}
        self.message.text = "https://en.wikipedia.org/wiki/War_and_Peace"
        state = await bot.add_review(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_AUTHOR)
        nb = self.ctx.user_data["new_book"]
        self.assertEqual(nb["pages"], 1225)
        self.assertEqual(nb["author"], "Leo Tolstoy")
        self.assertIn("pages", self.ctx.user_data["llm_filled_keys"])
        mock_from_page.assert_called_once()
        self.assertEqual(mock_from_page.call_args[0][0], "War and Peace")
        self.assertEqual(
            mock_from_page.call_args[0][1],
            "https://en.wikipedia.org/wiki/War_and_Peace",
        )
        texts = [c[0][0] for c in self.message.reply_text.call_args_list]
        self.assertTrue(any("Reading the review page" in t for t in texts))
        self.assertTrue(any("Leo Tolstoy" in t for t in texts))

    async def test_add_back_from_author_returns_review(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "review_link": "https://en.wikipedia.org/wiki/T",
            "author": "A",
        }
        self.ctx.user_data["add_state"] = bot.ADDING_AUTHOR
        self.message.text = "/back"
        state = await bot.add_go_back(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_REVIEW)
        reply = self.message.reply_text.call_args[0][0]
        self.assertIn("wikipedia.org", reply)

    async def test_add_original_language_skip(self):
        self.ctx.user_data["new_book"] = {"review_link": "http://x.com"}
        self.message.text = "/skip"
        state = await bot.add_original_language_skip(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["original_language"], "")
        self.assertEqual(state, bot.ADDING_CREATION_YEAR)

    async def test_add_original_language_picks_german(self):
        self.ctx.user_data["new_book"] = {"review_link": "http://x.com"}
        q = self._callback_query("add_orig_lang:de")
        state = await bot.add_original_language_cb(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["original_language"], "German")
        self.assertEqual(state, bot.ADDING_CREATION_YEAR)

    async def test_add_original_language_other_accepts_text(self):
        self.ctx.user_data["new_book"] = {"review_link": "http://x.com"}
        self.ctx.user_data["add_state"] = bot.ADDING_ORIGINAL_LANGUAGE_OTHER
        self.message.text = "Ukrainian"
        state = await bot.add_original_language_other(self.update, self.ctx)
        self.assertEqual(
            self.ctx.user_data["new_book"]["original_language"], "Ukrainian"
        )
        self.assertEqual(state, bot.ADDING_CREATION_YEAR)

    async def test_add_creation_year_valid(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "1984"
        state = await bot.add_creation_year(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["new_book"]["creation_year"], 1984)
        self.assertEqual(state, bot.ADDING_DESCRIPTION)

    async def test_add_creation_year_skip(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "/skip"
        state = await bot.add_creation_year(self.update, self.ctx)
        self.assertIsNone(self.ctx.user_data["new_book"]["creation_year"])
        self.assertEqual(state, bot.ADDING_DESCRIPTION)

    async def test_add_creation_year_invalid(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "84"
        state = await bot.add_creation_year(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_CREATION_YEAR)

    async def test_add_review_invalid_url(self):
        self.ctx.user_data["new_book"] = {}
        self.message.text = "not a url"
        state = await bot.add_review(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_REVIEW)

    async def test_add_description_completes_and_schedules_job(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 100,
            "fiction": True,
            "review_link": "http://x.com",
            "original_language": "German",
        }
        self.message.text = "Great book"
        self.ctx.job_queue = MagicMock()

        state = await bot.add_description(self.update, self.ctx)

        self.assertEqual(state, bot.ADDING_CONFIRM)
        self.ctx.job_queue.run_once.assert_not_called()
        preview = self.message.reply_text.call_args[0][0]
        self.assertIn("Review the entry", preview)
        self.assertIn("Great book", preview)
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        data = self._keyboard_callback_data(markup)
        self.assertIn("add_confirm", data)
        self.assertIn("add_back", data)

        self._callback_query("add_confirm")
        state = await bot.add_confirm_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        confirm = self.message.reply_text.call_args[0][0]
        self.assertIn("Book added", confirm)
        self.assertIn(bot.format_defaults("en")["minutes_phrase"], confirm)
        self.ctx.job_queue.run_once.assert_called_once()
        job_kwargs = self.ctx.job_queue.run_once.call_args[1]
        self.assertEqual(job_kwargs["when"], bot.NEW_BOOK_NOTIFY_DELAY_SECONDS)
        book_id = job_kwargs["data"]["book_id"]
        book = bot.db_get_book(book_id)
        self.assertEqual(book["notify_adder_id"], self.update.effective_user.id)
        self.assertEqual(book["notify_sent"], 0)

    async def test_add_back_from_confirm_returns_to_description(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 100,
            "fiction": True,
            "review_link": "http://x.com",
            "description": "Kept",
        }
        self.ctx.user_data["add_state"] = bot.ADDING_CONFIRM
        q = self._callback_query("add_back")
        state = await bot.add_go_back(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_DESCRIPTION)
        text = q.edit_message_text.call_args[0][0]
        self.assertIn("Kept", text)

    async def test_add_description_no_job_queue(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 100,
            "fiction": True,
            "review_link": "http://x.com",
        }
        self.message.text = "Desc"
        self.ctx.job_queue = None

        state = await bot.add_description(self.update, self.ctx)
        self.assertEqual(state, bot.ADDING_CONFIRM)
        self._callback_query("add_confirm")
        state = await bot.add_confirm_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        self.assertIn("Book added", self.message.reply_text.call_args[0][0])

    async def test_add_description_skip(self):
        self.ctx.user_data["new_book"] = {
            "title": "T",
            "author": "A",
            "pages": 100,
            "fiction": True,
            "review_link": "http://x.com",
        }
        self.message.text = "/skip"
        self.ctx.job_queue = None

        await bot.add_description(self.update, self.ctx)
        self.assertEqual(bot.db_get_books(discussed=False), [])
        self._callback_query("add_confirm")
        await bot.add_confirm_cb(self.update, self.ctx)

        # Book should be saved with empty description
        books = bot.db_get_books(discussed=False)
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["description"], "")

    async def test_add_description_missing_new_book_returns_cancelled(self):
        self.ctx.user_data = {"lang": "en"}
        self.message.text = "some text"
        self.ctx.job_queue = MagicMock()

        state = await bot.add_description(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        self.assertIn("Cancelled", self.message.reply_text.call_args[0][0])
        self.ctx.job_queue.run_once.assert_not_called()


# ── /settings ─────────────────────────────────────────────────────────────────


class TestSettings(BotHandlerTestCase):

    async def test_cmd_settings_shows_panel(self):
        await bot.cmd_settings(self.update, self.ctx)
        self.message.reply_text.assert_called_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Settings", text)
        self.assertIn("Notifications", text)

    async def test_settings_toggle_notify_minus_one_to_one(self):
        # Default is -1; first toggle → 1
        q = self._callback_query("settings:toggle_notify")
        await bot.settings_choice_cb(self.update, self.ctx)
        self.assertEqual(
            bot.db_get_user_setting(self.update.effective_user.id, "notify_new_books"),
            1,
        )

    async def test_settings_toggle_notify_one_to_zero(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 1)
        q = self._callback_query("settings:toggle_notify")
        await bot.settings_choice_cb(self.update, self.ctx)
        self.assertEqual(
            bot.db_get_user_setting(self.update.effective_user.id, "notify_new_books"),
            0,
        )

    async def test_settings_toggle_notify_zero_to_one(self):
        bot.db_set_user_setting(self.update.effective_user.id, "notify_new_books", 0)
        q = self._callback_query("settings:toggle_notify")
        await bot.settings_choice_cb(self.update, self.ctx)
        self.assertEqual(
            bot.db_get_user_setting(self.update.effective_user.id, "notify_new_books"),
            1,
        )

    @patch("bookclub.handlers.commands.set_user_commands", new_callable=AsyncMock)
    async def test_settings_toggle_lang_en_to_ru(self, mock_set):
        self.ctx.user_data["lang"] = "en"
        q = self._callback_query("settings:toggle_lang")
        await bot.settings_choice_cb(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["lang"], "ru")
        q.answer.assert_called_once_with("🇷🇺 Язык установлен: Русский.")
        mock_set.assert_called_once_with(self.ctx.bot, self.update, "ru")

    @patch("bookclub.handlers.commands.set_user_commands", new_callable=AsyncMock)
    async def test_settings_toggle_lang_ru_to_de(self, mock_set):
        self.ctx.user_data["lang"] = "ru"
        q = self._callback_query("settings:toggle_lang")
        await bot.settings_choice_cb(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["lang"], "de")
        q.answer.assert_called_once_with("🇩🇪 Sprache auf Deutsch gestellt.")
        mock_set.assert_called_once_with(self.ctx.bot, self.update, "de")

    @patch("bookclub.handlers.commands.set_user_commands", new_callable=AsyncMock)
    async def test_settings_toggle_lang_de_to_en(self, mock_set):
        self.ctx.user_data["lang"] = "de"
        q = self._callback_query("settings:toggle_lang")
        await bot.settings_choice_cb(self.update, self.ctx)
        self.assertEqual(self.ctx.user_data["lang"], "en")
        mock_set.assert_called_once_with(self.ctx.bot, self.update, "en")

    async def test_optin_yes_sets_notify_and_continues_list(self):
        with sqlite3.connect(bot.DB_PATH) as conn:
            conn.execute(
                "DELETE FROM user_settings WHERE user_id=?",
                (self.update.effective_user.id,),
            )
        self._add_book("Book 1")
        self.ctx.user_data["pending_list_choice"] = "unvoted"
        q = self._callback_query("settings:optin:1")
        await bot.settings_choice_cb(self.update, self.ctx)
        self.assertEqual(
            bot.db_get_user_setting(self.update.effective_user.id, "notify_new_books"),
            1,
        )
        q.answer.assert_called_once_with("✅ Settings saved!")
        q.edit_message_text.assert_called_once()
        self.assertIn("How would you like", q.edit_message_text.call_args[0][0])
        self.ctx.bot.send_message.assert_not_called()

    async def test_optin_no_sets_zero(self):
        with sqlite3.connect(bot.DB_PATH) as conn:
            conn.execute(
                "DELETE FROM user_settings WHERE user_id=?",
                (self.update.effective_user.id,),
            )
        self.ctx.user_data["pending_list_choice"] = "all"
        q = self._callback_query("settings:optin:0")
        await bot.settings_choice_cb(self.update, self.ctx)
        self.assertEqual(
            bot.db_get_user_setting(self.update.effective_user.id, "notify_new_books"),
            0,
        )


# ── Membership gate ────────────────────────────────────────────────────────────


class TestMembershipGate(BotHandlerTestCase):

    async def test_gate_allows_when_no_chat_id_set(self):
        cfg.ALLOWED_CHAT_ID = None
        result = await bot._check_membership(self.update, self.ctx)
        self.assertTrue(result)

    async def test_allowed_non_admin_activity_sidecar_is_private_and_throttled(self):
        cfg.ALLOWED_CHAT_ID = None
        self.update.message.left_chat_member = None
        self.update.message.new_chat_members = []
        with patch.object(cfg, "ADMIN_IDS", []):
            await bot.membership_gate(self.update, self.ctx)
            first_mtime = os.stat(cfg.ACTIVITY_PATH).st_mtime_ns
            self.assertEqual(os.stat(cfg.ACTIVITY_PATH).st_mode & 0o777, 0o600)
            await bot.membership_gate(self.update, self.ctx)
        self.assertEqual(os.stat(cfg.ACTIVITY_PATH).st_mtime_ns, first_mtime)
        with open(cfg.ACTIVITY_PATH, encoding="utf-8") as activity_file:
            self.assertIn("last_non_admin_activity", json.load(activity_file))

    async def test_gate_allows_admin_without_api_call(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        old = cfg.ADMIN_IDS[:]
        try:
            cfg.ADMIN_IDS = [self.update.effective_user.id]
            result = await bot._check_membership(self.update, self.ctx)
            self.assertTrue(result)
            # Bot API should NOT have been called for admin
            self.ctx.bot.get_chat_member.assert_not_called()
        finally:
            cfg.ADMIN_IDS = old

    async def test_gate_allows_member_status(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        for status in ("member", "administrator", "creator"):
            bot._membership_cache.clear()  # else only the first status is tested
            member = MagicMock()
            member.status = status
            self.ctx.bot.get_chat_member = AsyncMock(return_value=member)
            result = await bot._check_membership(self.update, self.ctx)
            self.assertTrue(result, f"Status '{status}' should be allowed")

    async def test_gate_allows_restricted_current_member(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        member = MagicMock(status="restricted", is_member=True)
        self.ctx.bot.get_chat_member = AsyncMock(return_value=member)
        self.assertTrue(await bot._check_membership(self.update, self.ctx))

    async def test_gate_blocks_restricted_former_member(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        member = MagicMock(status="restricted", is_member=False)
        self.ctx.bot.get_chat_member = AsyncMock(return_value=member)
        self.assertFalse(await bot._check_membership(self.update, self.ctx))

    async def test_gate_blocks_non_member(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        for status in ("left", "kicked"):
            bot._membership_cache.clear()  # else only the first status is tested
            member = MagicMock()
            member.status = status
            self.ctx.bot.get_chat_member = AsyncMock(return_value=member)
            result = await bot._check_membership(self.update, self.ctx)
            self.assertFalse(result, f"Status '{status}' should be blocked")

    async def test_blocked_user_does_not_record_activity_or_profile(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        self.update.message.left_chat_member = None
        self.update.message.new_chat_members = []
        member = MagicMock(status="left")
        self.ctx.bot.get_chat_member = AsyncMock(return_value=member)
        with (
            patch.object(cfg, "ADMIN_IDS", []),
            patch("bookclub.membership._record_non_admin_activity") as record,
            patch("bookclub.membership._upsert_club_user_if_changed") as upsert,
            self.assertRaises(ApplicationHandlerStop),
        ):
            await bot.membership_gate(self.update, self.ctx)
        record.assert_not_called()
        upsert.assert_not_called()

    async def test_gate_fails_closed_on_chat_not_found(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        self.ctx.bot.get_chat_member = AsyncMock(
            side_effect=BadRequest("Chat not found")
        )
        result = await bot._check_membership(self.update, self.ctx)
        self.assertFalse(result)

    async def test_gate_fails_closed_on_network_error(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        self.ctx.bot.get_chat_member = AsyncMock(side_effect=NetworkError("timeout"))
        result = await bot._check_membership(self.update, self.ctx)
        self.assertFalse(result)

    async def test_gate_blocks_on_unknown_api_exception(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        self.ctx.bot.get_chat_member = AsyncMock(side_effect=Exception("API error"))
        result = await bot._check_membership(self.update, self.ctx)
        self.assertFalse(result)

    async def test_gate_caches_result_to_avoid_api_call_per_update(self):
        """The gate runs on every update; it must not hit the API each time."""
        cfg.ALLOWED_CHAT_ID = -1001111111111
        member = MagicMock()
        member.status = "member"
        self.ctx.bot.get_chat_member = AsyncMock(return_value=member)
        for _ in range(5):
            self.assertTrue(await bot._check_membership(self.update, self.ctx))
        self.ctx.bot.get_chat_member.assert_awaited_once()

    async def test_gate_does_not_cache_api_failures(self):
        """A transient error must not lock a real member out for the whole TTL."""
        cfg.ALLOWED_CHAT_ID = -1001111111111
        self.ctx.bot.get_chat_member = AsyncMock(side_effect=Exception("boom"))
        self.assertFalse(await bot._check_membership(self.update, self.ctx))

        member = MagicMock()
        member.status = "member"
        self.ctx.bot.get_chat_member = AsyncMock(return_value=member)
        self.assertTrue(await bot._check_membership(self.update, self.ctx))

    async def test_leaving_evicts_user_from_membership_cache(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        uid = self.update.effective_user.id
        bot._membership_cache[uid] = (True, datetime.now())
        self.update.message.left_chat_member = MagicMock(spec=User)
        self.update.message.left_chat_member.id = uid
        await bot.membership_gate(self.update, self.ctx)
        self.assertNotIn(uid, bot._membership_cache)

    async def test_gate_ignores_new_chat_members_service_message(self):
        cfg.ALLOWED_CHAT_ID = -1001111111111
        joiner = MagicMock(spec=User)
        joiner.id = 55555
        bot._membership_cache[55555] = (False, datetime.now())
        self.update.message.left_chat_member = None
        self.update.message.new_chat_members = [joiner]
        await bot.membership_gate(self.update, self.ctx)
        self.assertNotIn(55555, bot._membership_cache)
        self.message.reply_text.assert_not_called()

    async def test_gate_ignores_left_chat_member_service_message(self):
        """A 'user left the chat' service message must not trigger a reply in the group."""
        cfg.ALLOWED_CHAT_ID = -1001111111111
        self.update.message.left_chat_member = MagicMock(spec=User)
        await bot.membership_gate(self.update, self.ctx)
        self.ctx.bot.get_chat_member.assert_not_called()
        self.message.reply_text.assert_not_called()


# ── Startup / shutdown notifications ──────────────────────────────────────────


class TestStartupShutdown(BotHandlerTestCase):

    async def test_bot_notify_startup_sends_to_first_admin(self):
        old = cfg.ADMIN_IDS[:]
        try:
            cfg.ADMIN_IDS = [111, 222]
            app = MagicMock()
            app.bot = AsyncMock()
            # bot_notify_startup schedules the error-alert drain via
            # app.create_task; close the coroutine so it isn't left un-awaited.
            app.create_task.side_effect = lambda coro: coro.close()
            await bot.bot_notify_startup(app)
            app.bot.send_message.assert_called_once()
            self.assertEqual(app.bot.send_message.call_args[1]["chat_id"], 111)
            self.assertIn("Bot is up", app.bot.send_message.call_args[1]["text"])
        finally:
            cfg.ADMIN_IDS = old

    async def test_bot_notify_shutdown_sends_to_first_admin(self):
        old = cfg.ADMIN_IDS[:]
        try:
            cfg.ADMIN_IDS = [333, 444]
            app = MagicMock()
            app.bot = AsyncMock()
            await bot.bot_notify_shutdown(app)
            app.bot.send_message.assert_called_once()
            self.assertEqual(app.bot.send_message.call_args[1]["chat_id"], 333)
            self.assertIn("Bot is down", app.bot.send_message.call_args[1]["text"])
        finally:
            cfg.ADMIN_IDS = old

    async def test_bot_notify_no_admins_does_nothing(self):
        old = cfg.ADMIN_IDS[:]
        try:
            cfg.ADMIN_IDS = []
            app = MagicMock()
            app.bot = AsyncMock()
            await bot.bot_notify_startup(app)
            app.bot.send_message.assert_not_called()
        finally:
            cfg.ADMIN_IDS = old

    async def test_bot_notify_startup_api_error_does_not_crash(self):
        old = cfg.ADMIN_IDS[:]
        try:
            cfg.ADMIN_IDS = [123]
            app = MagicMock()
            app.bot = AsyncMock()
            app.create_task.side_effect = lambda coro: coro.close()
            app.bot.send_message.side_effect = Exception("Network error")
            # Should not raise
            await bot.bot_notify_startup(app)
        finally:
            cfg.ADMIN_IDS = old


# ── /edit ─────────────────────────────────────────────────────────────────────


class TestEdit(BotHandlerTestCase):

    async def test_edit_skip_all_fields_shows_book_card(self):
        """Regression: finishing /edit must render book_card (import from bookclub.ui)."""
        bid = self._add_book(
            "Card Title",
            author="A. Writer",
            pages=200,
            user_id=self.update.effective_user.id,
            username="testuser",
        )
        q = self._callback_query(f"edit_pick:{bid}")
        state = await bot.edit_pick_cb(self.update, self.ctx)
        self.assertEqual(state, bot.EDITING_FIELD)

        for _ in bot.get_edit_fields():
            q = self._callback_query("edit_yn:no")
            state = await bot.edit_yn_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        q.edit_message_text.assert_called()
        body = q.edit_message_text.call_args[0][0]
        self.assertIn("Book updated", body)
        self.assertIn("Card Title", body)
        self.assertIn("A. Writer", body)

    async def test_edit_field_prompt_has_cancel_button(self):
        bid = self._add_book(
            "Card Title",
            user_id=self.update.effective_user.id,
            username="testuser",
        )
        q = self._callback_query(f"edit_pick:{bid}")
        await bot.edit_pick_cb(self.update, self.ctx)
        markup = q.edit_message_text.call_args[1]["reply_markup"]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("edit_yn:yes", data)
        self.assertIn(bot.CONV_CANCEL, data)


# ── /cancel ────────────────────────────────────────────────────────────────────


class TestCancel(BotHandlerTestCase):

    async def test_conv_cancel_clears_user_data(self):
        self.ctx.user_data["new_book"] = {"title": "T"}
        self.ctx.user_data["edit_book_id"] = 5
        state = await bot.conv_cancel(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(self.ctx.user_data, {})

    async def test_conv_cancel_sends_message(self):
        state = await bot.conv_cancel(self.update, self.ctx)
        self.message.reply_text.assert_called_once()
        self.assertIn("Cancelled", self.message.reply_text.call_args[0][0])

    async def test_conv_cancel_callback_edits_message(self):
        q = self._callback_query(bot.CONV_CANCEL)
        state = await bot.conv_cancel(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        q.answer.assert_called_once()
        q.edit_message_text.assert_called_once()
        self.assertIn("Cancelled", q.edit_message_text.call_args[0][0])
        self.assertEqual(self.ctx.user_data, {})


# ── /adminconsole ─────────────────────────────────────────────────────────────


class TestAdminConsole(BotHandlerTestCase):

    def setUp(self):
        super().setUp()
        self.old_admins = cfg.ADMIN_IDS[:]
        cfg.ADMIN_IDS = [self.update.effective_user.id]

    def tearDown(self):
        cfg.ADMIN_IDS = self.old_admins
        super().tearDown()

    async def test_cmd_admin_console_as_non_admin_fails(self):
        cfg.ADMIN_IDS = [999]
        state = await bot.cmd_admin_console(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        self.message.reply_text.assert_called_once()
        self.assertIn("admins only", self.message.reply_text.call_args[0][0])

    async def test_cmd_admin_console_shows_menu(self):
        state = await bot.cmd_admin_console(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MENU)
        self.message.reply_text.assert_called_once()
        self.assertIn("Admin Console", self.message.reply_text.call_args[0][0])
        markup = self.message.reply_text.call_args[1]["reply_markup"]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertNotIn("admin:add", data)
        self.assertIn(bot.CONV_CANCEL, data)

    async def test_admin_menu_cb_mark_shows_submenu(self):
        q = self._callback_query("admin:mark")
        state = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MENU)
        q.edit_message_text.assert_called_once()
        self.assertIn("Mark as discussed", q.edit_message_text.call_args[0][0])

    async def test_admin_menu_cb_mark_new_shows_books(self):
        self._add_book("Mark Me")
        q = self._callback_query("admin:mark_new")
        state = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MARK_CHOOSE)
        q.edit_message_text.assert_called_once()
        self.assertIn("Choose a book", q.edit_message_text.call_args[0][0])

    async def test_admin_menu_cb_hide_shows_books(self):
        self._add_book("Hide Me")
        q = self._callback_query("admin:hide")
        state = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_HIDE_CHOOSE)
        q.edit_message_text.assert_called_once()
        self.assertIn("Choose a book to hide", q.edit_message_text.call_args[0][0])

    async def test_admin_hide_pick_hides_book(self):
        bid = self._add_book("Ghost")
        q = self._callback_query(f"admin_hide_pick:{bid}")
        state = await bot.admin_hide_pick_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(bot.db_get_book(bid)["hidden"], 1)
        q.edit_message_text.assert_called_once()
        self.assertIn("is now hidden", q.edit_message_text.call_args[0][0])

    async def test_admin_menu_cb_unhide_shows_hidden_books(self):
        bid = self._add_book("Hidden One")
        bot.db_set_hidden(bid, True)
        q = self._callback_query("admin:unhide")
        state = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_UNHIDE_CHOOSE)
        q.edit_message_text.assert_called_once()
        self.assertIn("hidden", q.edit_message_text.call_args[0][0].lower())

    async def test_admin_unhide_pick_shows_book(self):
        bid = self._add_book("Ghost")
        bot.db_set_hidden(bid, True)
        q = self._callback_query(f"admin_unhide_pick:{bid}")
        state = await bot.admin_unhide_pick_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(bot.db_get_book(bid)["hidden"], 0)
        self.assertIn("visible", q.edit_message_text.call_args[0][0].lower())

    async def test_admin_mark_pick_advances_to_date(self):
        bid = self._add_book("Discuss Me")
        q = self._callback_query(f"admin_mark_pick:{bid}")
        state = await bot.admin_mark_pick_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MARK_DATE)
        self.assertEqual(self.ctx.user_data["mark_book_id"], bid)
        markup = q.edit_message_text.call_args[1]["reply_markup"]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn(bot.CONV_CANCEL, data)

    async def test_admin_mark_date_handler_completes(self):
        bid = self._add_book("Dated")
        self.ctx.user_data["mark_book_id"] = bid
        self.message.text = "2026-03-17"
        state = await bot.admin_mark_date_handler(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        book = bot.db_get_book(bid)
        self.assertEqual(book["discussed"], 1)
        self.assertEqual(book["discussed_at"], "2026-03-17")
        self.message.reply_text.assert_called_once()
        self.assertIn("marked as discussed", self.message.reply_text.call_args[0][0])

    async def test_admin_mark_edit_date_updates_discussed_at(self):
        bid = self._add_book("Already Discussed")
        bot.db_mark_discussed(bid, "2026-01-01")
        q = self._callback_query(f"admin_mark_edit_pick:{bid}")
        state = await bot.admin_mark_edit_pick_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MARK_DATE)
        self.assertTrue(self.ctx.user_data.get("mark_edit_date"))
        self.message.text = "2026-06-15"
        state = await bot.admin_mark_date_handler(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        book = bot.db_get_book(bid)
        self.assertEqual(book["discussed"], 1)
        self.assertEqual(book["discussed_at"], "2026-06-15")
        self.assertIn("updated to", self.message.reply_text.call_args[0][0])

    async def test_admin_notify_top_queues_reminders(self):
        # 1. Setup books
        bid1 = self._add_book("Top Book 1")
        bid2 = self._add_book("Top Book 2")

        # 2. Setup user with notifications ON
        user_id = 12345
        bot.db_set_user_setting(user_id, "notify_new_books", 1)
        # Ensure user has NOT voted for bid1 and bid2

        # 3. Trigger notify
        q = self._callback_query("admin:notify")
        state = await bot.admin_notify_top_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        self.ctx.bot.send_message.assert_not_called()
        self.ctx.job_queue.run_once.assert_called_once()
        job_data = self.ctx.job_queue.run_once.call_args.kwargs["data"]
        self.assertEqual(set(job_data["book_ids"]), {bid1, bid2})
        self.assertEqual(job_data["user_ids"], [user_id])

        # Verify confirm message to admin
        q.edit_message_text.assert_called_once()
        self.assertIn("reminder sent", q.edit_message_text.call_args[0][0])

    async def test_admin_notify_top_no_unvoted_books(self):
        # 1. Setup book
        bid = self._add_book("Voted Book")

        # 2. Setup user who already voted
        user_id = 12345
        bot.db_set_user_setting(user_id, "notify_new_books", 1)
        bot.db_cast_vote(user_id, bid, 1)

        # 3. Trigger notify
        q = self._callback_query("admin:notify")
        state = await bot.admin_notify_top_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        # No messages should be sent to user
        self.ctx.bot.send_message.assert_not_called()

        # Verify info message to admin
        q.edit_message_text.assert_called_once()
        self.assertIn("No users to notify", q.edit_message_text.call_args[0][0])

    async def test_admin_notify_pick_queues_reminders(self):
        bid = self._add_book("Target Book")
        user_id = 12345
        bot.db_set_user_setting(user_id, "notify_new_books", 1)

        self.ctx.user_data["notify_book_ids"] = {bid}
        q = self._callback_query("admin_notify_pick:done")
        state = await bot.admin_notify_pick_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        self.ctx.bot.send_message.assert_not_called()
        self.ctx.job_queue.run_once.assert_called_once()
        q.edit_message_text.assert_called_once()
        self.assertIn("reminder sent", q.edit_message_text.call_args[0][0])

    async def test_queued_vote_reminder_sends_in_background_job(self):
        bid = self._add_book("Queued Book")
        job_ctx = MagicMock()
        job_ctx.job.data = {
            "book_ids": [bid],
            "user_ids": [12345],
            "to_chat": False,
        }
        job_ctx.bot = AsyncMock()
        job_ctx.application.user_data = {}
        await vote_reminder_job(job_ctx)
        self.assertEqual(job_ctx.bot.send_message.call_count, 2)

    async def test_queued_group_reminder_retries_flood_wait(self):
        bid = self._add_book("Queued Group Book")
        cfg.ALLOWED_CHAT_ID = -100123
        job_ctx = MagicMock()
        job_ctx.job.data = {
            "book_ids": [bid],
            "user_ids": None,
            "to_chat": True,
        }
        job_ctx.bot = AsyncMock()
        job_ctx.bot.send_message.side_effect = [RetryAfter(timedelta(seconds=1)), None]
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            await vote_reminder_job(job_ctx)
        self.assertEqual(job_ctx.bot.send_message.call_count, 2)
        self.assertIn(call(1.0), sleep.await_args_list)

    async def test_admin_notify_pick_toggle_updates_selection(self):
        bid = self._add_book("Toggle Book")
        q = self._callback_query(f"admin_notify_pick:toggle:{bid}:0")
        state = await bot.admin_notify_pick_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_NOTIFY_PICK)
        self.assertEqual(self.ctx.user_data["notify_book_ids"], {bid})
        q.edit_message_text.assert_called_once()
        self.assertIn("Selected", q.edit_message_text.call_args[0][0])

    async def test_admin_notify_chat_top_queues_group_posts(self):
        bid1 = self._add_book("Chat Top 1")
        bid2 = self._add_book("Chat Top 2")
        cfg.ALLOWED_CHAT_ID = -100123

        q = self._callback_query("admin:notify_chat")
        with patch.object(cfg, "CHAT_LANG", "en"):
            state = await bot.admin_notify_chat_top_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        self.ctx.bot.send_message.assert_not_called()
        self.ctx.job_queue.run_once.assert_called_once()
        data = self.ctx.job_queue.run_once.call_args.kwargs["data"]
        self.assertEqual(set(data["book_ids"]), {bid1, bid2})
        self.assertTrue(data["to_chat"])

        q.edit_message_text.assert_called_once()
        self.assertIn("group chat", q.edit_message_text.call_args[0][0])

    async def test_admin_notify_chat_pick_queues_group_post(self):
        bid = self._add_book("Chat Pick Book")
        cfg.ALLOWED_CHAT_ID = -100123
        self.ctx.user_data["notify_book_ids"] = {bid}

        q = self._callback_query("admin_notify_chat_pick:done")
        with patch.object(cfg, "CHAT_LANG", "en"):
            state = await bot.admin_notify_chat_pick_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        self.ctx.bot.send_message.assert_not_called()
        self.ctx.job_queue.run_once.assert_called_once()
        data = self.ctx.job_queue.run_once.call_args.kwargs["data"]
        self.assertEqual(data["book_ids"], [bid])
        self.assertTrue(data["to_chat"])

    async def test_admin_notify_chat_no_allowed_chat_id(self):
        cfg.ALLOWED_CHAT_ID = None
        self._add_book("Orphan Book")

        q = self._callback_query("admin:notify_chat")
        state = await bot.admin_notify_chat_top_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        self.ctx.bot.send_message.assert_not_called()
        self.assertIn("ALLOWED_CHAT_ID", q.edit_message_text.call_args[0][0])

    async def test_admin_menu_cb_export_shows_books(self):
        self._add_book("Export Me")
        q = self._callback_query("admin:export")
        state = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_EXPORT_CHOOSE)
        q.edit_message_text.assert_called_once()
        self.assertIn("export", q.edit_message_text.call_args[0][0].lower())

    async def test_admin_export_pick_sends_json(self):
        bid = self._add_book("Export Me", author="Auth")
        q = self._callback_query(f"admin_export_pick:{bid}")
        state = await bot.admin_export_pick_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        q.edit_message_text.assert_called_once()
        body = q.edit_message_text.call_args[0][0]
        self.assertIn("Export Me", body)
        self.assertIn("bookclub-bot-book", body)

    async def test_admin_menu_cb_import_prompts(self):
        q = self._callback_query("admin:import")
        state = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_IMPORT_WAIT)
        q.edit_message_text.assert_called_once()
        self.assertIn("JSON", q.edit_message_text.call_args[0][0])
        markup = q.edit_message_text.call_args[1]["reply_markup"]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn(bot.CONV_CANCEL, data)

    async def test_admin_import_handler_inserts_book(self):
        payload = bot.book_to_export_payload(
            bot.db_get_book(self._add_book("Imported", author="Writer"))
        )
        self.update.message.text = payload
        state = await bot.admin_import_handler(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_IMPORT_CONFIRM)
        q = self._callback_query("title_sim:yes")
        state = await bot.admin_import_similar_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        books = bot.db_get_books(discussed=False, include_hidden=True)
        titles = [b["title"] for b in books]
        self.assertEqual(titles.count("Imported"), 2)

    async def test_admin_import_handler_schedules_notification_job(self):
        source = bot.db_get_book(self._add_book("Notify Me", author="Writer"))
        payload = bot.book_to_export_payload(source)
        self.update.message.text = payload
        self.ctx.job_queue = MagicMock()

        state = await bot.admin_import_handler(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_IMPORT_CONFIRM)
        q = self._callback_query("title_sim:yes")
        state = await bot.admin_import_similar_cb(self.update, self.ctx)

        self.assertEqual(state, ConversationHandler.END)
        self.ctx.job_queue.run_once.assert_called_once()
        job_kwargs = self.ctx.job_queue.run_once.call_args[1]
        self.assertEqual(job_kwargs["when"], bot.NEW_BOOK_NOTIFY_DELAY_SECONDS)
        imported_id = job_kwargs["data"]["book_id"]
        self.assertNotEqual(imported_id, source["id"])
        imported = bot.db_get_book(imported_id)
        self.assertEqual(imported["notify_adder_id"], self.update.effective_user.id)

    async def test_admin_toggle_chat_works(self):
        # Default should be 0
        self.assertEqual(bot.db_get_admin_setting("post_new_books_to_chat"), 0)

        # Toggle ON
        q = self._callback_query("admin:toggle_chat")
        await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(bot.db_get_admin_setting("post_new_books_to_chat"), 1)

        # Toggle OFF
        q = self._callback_query("admin:toggle_chat")
        await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(bot.db_get_admin_setting("post_new_books_to_chat"), 0)

    async def test_admin_toggle_votes_attendance_works(self):
        self.assertEqual(bot.db_get_admin_setting("votes_use_attendance"), 0)

        self._callback_query("admin:toggle_votes")
        await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(bot.db_get_admin_setting("votes_use_attendance"), 1)
        markup = self.update.callback_query.edit_message_text.call_args[1][
            "reply_markup"
        ]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("attendance" in label for label in labels))

        self._callback_query("admin:toggle_votes")
        await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(bot.db_get_admin_setting("votes_use_attendance"), 0)

    async def test_admin_toggle_votes_sends_compact_list_to_admin_chat(self):
        alpha_id = bot.db_add_book(
            "Alpha", "Author A", 100, True, "", "", 1, "u", creation_year=2001
        )
        bot.db_add_book("Beta", "Author B", 100, True, "", "", 1, "u")
        bot.db_cast_vote(1001, alpha_id, 1)
        bot.db_cast_vote(1002, alpha_id, 1)

        self._callback_query("admin:toggle_votes")
        await bot.admin_menu_cb(self.update, self.ctx)

        self.ctx.bot.send_message.assert_called_once()
        kwargs = self.ctx.bot.send_message.call_args[1]
        self.assertEqual(kwargs["chat_id"], self.update.effective_chat.id)
        text = kwargs["text"]
        self.assertIn("Alpha", text)
        self.assertIn("Author A", text)
        self.assertIn("(2001)", text)
        self.assertIn("Beta", text)
        self.assertIn("<b>2</b> <b>Alpha</b>", text)
        self.assertIn("<b>0</b> <b>Beta</b>", text)

    async def test_admin_toggle_votes_compact_list_uses_new_counting_mode(self):
        book_id = self._add_book("Alpha")
        regular, skipper = 11, 22
        bot.db_cast_vote(regular, book_id, 1)
        bot.db_cast_vote(skipper, book_id, 1)
        discussed_id = self._add_book("Discussed")
        bot.db_mark_discussed(discussed_id, "2026-01-01")
        bot.db_create_meeting(discussed_id, "2026-01-01", 1, [regular])

        self._callback_query("admin:toggle_votes")
        await bot.admin_menu_cb(self.update, self.ctx)

        text = self.ctx.bot.send_message.call_args[1]["text"]
        self.assertEqual(bot.db_get_admin_setting("votes_use_attendance"), 1)
        self.assertIn("<b>1</b> <b>Alpha</b>", text)
        self.assertNotIn("<b>2</b> <b>Alpha</b>", text)
        self.assertNotIn("Discussed", text)

    async def test_admin_toggle_votes_skips_compact_list_when_no_books(self):
        self._callback_query("admin:toggle_votes")
        await bot.admin_menu_cb(self.update, self.ctx)
        self.ctx.bot.send_message.assert_not_called()

    async def test_notify_new_book_job_posts_to_chat_when_enabled(self):
        bid = self._add_book("Chatty Book")
        cfg.ALLOWED_CHAT_ID = -100123
        bot.db_set_admin_setting("post_new_books_to_chat", 1)
        bot.db_set_new_book_notify_pending(
            bid, 999, datetime.now() + timedelta(minutes=5)
        )

        # Setup job mock. The adder's language is deliberately English to show
        # the shared group post does NOT follow it — group messages use CHAT_LANG.
        self.ctx.job = MagicMock()
        self.ctx.job.data = {"book_id": bid}
        self.ctx.application.user_data = {999: {"lang": "en"}}

        with patch.object(cfg, "CHAT_LANG", "en"):
            await bot.notify_new_book_job(self.ctx)

        # Should be called once for ALLOWED_CHAT_ID (and 0 users opted in by default in this test)
        self.ctx.bot.send_message.assert_called()
        args, kwargs = self.ctx.bot.send_message.call_args
        self.assertEqual(kwargs["chat_id"], -100123)
        self.assertIn("New book added", kwargs["text"])

    async def test_chat_post_uses_chat_lang_not_adder_lang(self):
        """A shared group post must not follow the adder's personal language."""
        bid = self._add_book("Shared Book")
        cfg.ALLOWED_CHAT_ID = -100123
        bot.db_set_admin_setting("post_new_books_to_chat", 1)

        self.ctx.job = MagicMock()
        self.ctx.job.data = {"book_id": bid}
        self.ctx.application.user_data = {999: {"lang": "en"}}  # adder prefers English

        bot.db_set_new_book_notify_pending(
            bid, 999, datetime.now() + timedelta(minutes=5)
        )

        with patch.object(cfg, "CHAT_LANG", "ru"):
            await bot.notify_new_book_job(self.ctx)

        kwargs = self.ctx.bot.send_message.call_args[1]
        self.assertIn("Добавлена новая книга", kwargs["text"])
        self.assertNotIn("New book added", kwargs["text"])

    async def test_notify_new_book_job_skips_hidden_book(self):
        bid = self._add_book("Hidden Book")
        cfg.ALLOWED_CHAT_ID = -100123
        bot.db_set_admin_setting("post_new_books_to_chat", 1)
        bot.db_toggle_hidden(bid)  # admin hid it inside the notify delay window
        bot.db_set_new_book_notify_pending(
            bid, 999, datetime.now() + timedelta(minutes=5)
        )

        self.ctx.job = MagicMock()
        self.ctx.job.data = {"book_id": bid}
        self.ctx.application.user_data = {}

        await bot.notify_new_book_job(self.ctx)

        self.ctx.bot.send_message.assert_not_called()

    async def test_notify_new_book_job_does_not_post_to_chat_when_disabled(self):
        bid = self._add_book("Quiet Book")
        cfg.ALLOWED_CHAT_ID = -100123
        bot.db_set_admin_setting("post_new_books_to_chat", 0)
        bot.db_set_new_book_notify_pending(
            bid, 999, datetime.now() + timedelta(minutes=5)
        )

        self.ctx.job = MagicMock()
        self.ctx.job.data = {"book_id": bid}
        self.ctx.application.user_data = {}

        await bot.notify_new_book_job(self.ctx)

        # Should NOT be called for ALLOWED_CHAT_ID
        # (It might be called if there were opted-in users, but there are none)
        self.ctx.bot.send_message.assert_not_called()

    async def test_admin_meeting_create_flow(self):
        bid = self._add_book("Discussed Book")
        bot.db_mark_discussed(bid, "2026-03-01")
        voter_id = 4242
        bot.db_upsert_club_user(voter_id, "Voter One", "voter1")
        bot.db_cast_vote(voter_id, bid, 1)

        q = self._callback_query("admin:meeting_create")
        state = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETING_BOOK)

        q = self._callback_query(f"admin_meeting_book:{bid}")
        state = await bot.admin_meeting_book_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETING_ATTENDEES)

        q = self._callback_query(f"admin_meeting_att:toggle:{voter_id}:0")
        state = await bot.admin_meeting_att_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETING_ATTENDEES)
        self.assertIn(voter_id, self.ctx.user_data["meeting_attendee_ids"])

        q = self._callback_query("admin_meeting_att:done")
        state = await bot.admin_meeting_att_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        meetings = bot.db_list_meetings()
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0]["book_id"], bid)
        attendees = bot.db_get_meeting_attendee_rows(meetings[0]["id"])
        self.assertEqual([int(r["user_id"]) for r in attendees], [voter_id])

    async def test_admin_meetings_view_lists_attendees(self):
        bid = self._add_book("Past Read")
        bot.db_mark_discussed(bid, "2026-02-01")
        uid = 7777
        bot.db_upsert_club_user(uid, "Alice", "alice")
        mid = bot.db_create_meeting(
            bid, "2026-02-15", self.update.effective_user.id, [uid]
        )

        q = self._callback_query("admin:meetings_view")
        state = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETINGS_VIEW)

        q = self._callback_query(f"admin_meeting_view:{mid}")
        state = await bot.admin_meeting_view_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETINGS_VIEW)
        body = q.edit_message_text.call_args[0][0]
        self.assertIn("Past Read", body)
        self.assertIn("Alice", body)
        markup = q.edit_message_text.call_args[1]["reply_markup"]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn(f"admin_meeting_view:edit:{mid}", data)
        self.assertIn(f"admin_meeting_view:delete:{mid}", data)

    async def test_meeting_picker_shows_display_name_when_no_nick(self):
        bid = self._add_book("Discussed Book")
        bot.db_mark_discussed(bid, "2026-03-01")
        voter_id = 4243
        bot.db_upsert_club_user(voter_id, "Maria Rossi", None)
        bot.db_cast_vote(voter_id, bid, 1)

        self._callback_query("admin:meeting_create")
        await bot.admin_menu_cb(self.update, self.ctx)
        q = self._callback_query(f"admin_meeting_book:{bid}")
        await bot.admin_meeting_book_cb(self.update, self.ctx)
        markup = q.edit_message_text.call_args[1]["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("Maria Rossi" in label for label in labels))
        self.assertFalse(any(str(voter_id) in label for label in labels))

    async def test_meeting_picker_resolves_telegram_shown_name_for_id_only_user(self):
        bid = self._add_book("Discussed Book")
        bot.db_mark_discussed(bid, "2026-03-01")
        voter_id = 4244
        bot.db_upsert_club_user(voter_id, "", None)
        bot.db_cast_vote(voter_id, bid, 1)

        profile = MagicMock()
        profile.full_name = "Ivan Petrov"
        profile.username = None
        profile.type = "private"
        profile.is_bot = False
        self.ctx.bot.get_chat = AsyncMock(return_value=profile)

        self._callback_query("admin:meeting_create")
        await bot.admin_menu_cb(self.update, self.ctx)
        q = self._callback_query(f"admin_meeting_book:{bid}")
        await bot.admin_meeting_book_cb(self.update, self.ctx)
        markup = q.edit_message_text.call_args[1]["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("Ivan Petrov" in label for label in labels))
        self.assertFalse(any(str(voter_id) in label for label in labels))

    async def test_meeting_view_resolves_shown_name_when_no_nick(self):
        bid = self._add_book("Past Read")
        bot.db_mark_discussed(bid, "2026-02-01")
        uid = 7778
        bot.db_upsert_club_user(uid, "", None)
        mid = bot.db_create_meeting(
            bid, "2026-02-15", self.update.effective_user.id, [uid]
        )
        profile = MagicMock()
        profile.full_name = "Shown Name"
        profile.username = None
        profile.type = "private"
        profile.is_bot = False
        self.ctx.bot.get_chat = AsyncMock(return_value=profile)

        q = self._callback_query(f"admin_meeting_view:{mid}")
        await bot.admin_meeting_view_cb(self.update, self.ctx)
        body = q.edit_message_text.call_args[0][0]
        self.assertIn("Shown Name", body)
        self.assertNotIn(str(uid), body)

    async def test_meeting_add_id_uses_shown_name_in_confirmation(self):
        bid = self._add_book("Discussed Book")
        bot.db_mark_discussed(bid, "2026-03-01")
        self.ctx.user_data["meeting_book_id"] = bid
        self.ctx.user_data["meeting_date"] = "2026-03-01"
        self.ctx.user_data["meeting_attendee_ids"] = set()
        profile = MagicMock()
        profile.full_name = "No Nick User"
        profile.username = None
        profile.type = "private"
        profile.is_bot = False
        self.ctx.bot.get_chat = AsyncMock(return_value=profile)
        self.message.text = "55555"

        state = await bot.admin_meeting_add_id_handler(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETING_ATTENDEES)
        texts = [c[0][0] for c in self.message.reply_text.call_args_list]
        self.assertTrue(any("No Nick User" in t for t in texts))
        self.assertFalse(any("55555" in t for t in texts))

    async def test_admin_meeting_edit_keeps_existing_attendees(self):
        bid = self._add_book("Discussed Book")
        bot.db_mark_discussed(bid, "2026-03-01")
        voter_id = 4242
        extra = 4343
        bot.db_upsert_club_user(voter_id, "Voter One", "voter1")
        bot.db_upsert_club_user(extra, "Extra Person", "ex")
        bot.db_cast_vote(voter_id, bid, 1)
        bot.db_create_meeting(
            bid, "2026-03-01", self.update.effective_user.id, [voter_id]
        )

        self._callback_query("admin:meeting_create")
        await bot.admin_menu_cb(self.update, self.ctx)
        self._callback_query(f"admin_meeting_book:{bid}")
        state = await bot.admin_meeting_book_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETING_ATTENDEES)
        self.assertEqual(self.ctx.user_data["meeting_attendee_ids"], {voter_id})

        self._callback_query(f"admin_meeting_att:toggle:{extra}:0")
        await bot.admin_meeting_att_cb(self.update, self.ctx)
        self._callback_query("admin_meeting_att:done")
        state = await bot.admin_meeting_att_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        meetings = bot.db_list_meetings()
        self.assertEqual(len(meetings), 1)
        ids = sorted(
            int(r["user_id"])
            for r in bot.db_get_meeting_attendee_rows(meetings[0]["id"])
        )
        self.assertEqual(ids, [voter_id, extra])

    async def test_admin_meeting_edit_from_view(self):
        bid = self._add_book("Past Read")
        bot.db_mark_discussed(bid, "2026-02-01")
        uid = 7777
        bot.db_upsert_club_user(uid, "Alice", "alice")
        mid = bot.db_create_meeting(
            bid, "2026-02-15", self.update.effective_user.id, [uid]
        )
        self._callback_query(f"admin_meeting_view:edit:{mid}")
        state = await bot.admin_meeting_view_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETING_ATTENDEES)
        self.assertEqual(self.ctx.user_data["meeting_attendee_ids"], {uid})

    async def test_admin_meeting_delete_from_view(self):
        bid = self._add_book("Past Read")
        bot.db_mark_discussed(bid, "2026-02-01")
        uid = 7777
        bot.db_upsert_club_user(uid, "Alice", "alice")
        mid = bot.db_create_meeting(
            bid, "2026-02-15", self.update.effective_user.id, [uid]
        )
        q = self._callback_query(f"admin_meeting_view:delete:{mid}")
        state = await bot.admin_meeting_view_cb(self.update, self.ctx)
        self.assertEqual(state, bot.ADMIN_MEETINGS_VIEW)
        body = q.edit_message_text.call_args[0][0]
        self.assertIn("Delete the meeting", body)
        q = self._callback_query(f"admin_meeting_view:delete_yes:{mid}")
        state = await bot.admin_meeting_view_cb(self.update, self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(bot.db_list_meetings(), [])


# ── Voting in a shared group message ──────────────────────────────────────────


class TestGroupVoteCard(BotHandlerTestCase):
    """A card posted to the club chat is shared, so it must stay impersonal."""

    def _vote_in(self, chat_type, book_id, score=1):
        self.update.effective_chat.type = chat_type
        q = self._callback_query(f"vote_cast:{book_id}:{score}")
        return q

    def _add_book(self, title="Group Book"):
        return bot.db_add_book(title, "Author", 100, 1, "", "", 999, "u", "u")

    async def test_group_card_omits_personal_vote(self):
        bid = self._add_book()
        q = self._vote_in("supergroup", bid)
        with patch.object(cfg, "CHAT_LANG", "en"):
            await bot.vote_cast_cb(self.update, self.ctx)
        text = q.edit_message_text.call_args[0][0]
        self.assertNotIn(
            "Your current vote",
            text,
            "shared group card must not show one member's vote",
        )

    async def test_group_card_updates_statistics_after_vote(self):
        """Group chat message must show updated aggregate stats after voting."""
        bid = self._add_book()

        # First vote: +1
        q = self._vote_in("supergroup", bid, score=1)
        with patch.object(cfg, "CHAT_LANG", "en"):
            await bot.vote_cast_cb(self.update, self.ctx)

        text = q.edit_message_text.call_args[0][0]
        self.assertIn("✅ 1", text, "Should show 1 'want' vote")

        # Second user votes: +1 more (simulate by changing mock user ID)
        self.update.effective_user.id = 99999
        q2 = self._callback_query(f"vote_cast:{bid}:1")
        with patch.object(cfg, "CHAT_LANG", "en"):
            await bot.vote_cast_cb(self.update, self.ctx)

        text2 = q2.edit_message_text.call_args[0][0]
        self.assertIn("✅ 2", text2, "Should show 2 'want' votes after second vote")
        self.assertNotIn("✅ 1", text2, "Old count must be replaced")

    async def test_group_card_reflects_changed_votes(self):
        """If a user changes their vote, statistics update accordingly."""
        bid = self._add_book()

        # User votes +1
        q = self._vote_in("supergroup", bid, score=1)
        with patch.object(cfg, "CHAT_LANG", "en"):
            await bot.vote_cast_cb(self.update, self.ctx)
        text = q.edit_message_text.call_args[0][0]
        self.assertIn("✅ 1", text)
        self.assertIn("❌ 0", text)

        # Same user changes to -1 (don't want)
        q2 = self._callback_query(f"vote_cast:{bid}:-1")
        with patch.object(cfg, "CHAT_LANG", "en"):
            await bot.vote_cast_cb(self.update, self.ctx)

        text2 = q2.edit_message_text.call_args[0][0]
        self.assertIn("✅ 0", text2, "'want' count should drop to 0")
        self.assertIn("❌ 1", text2, "'don't want' count should be 1")

    async def test_group_card_acknowledges_voter_via_toast(self):
        bid = self._add_book()
        q = self._vote_in("supergroup", bid)
        with patch.object(cfg, "CHAT_LANG", "en"):
            await bot.vote_cast_cb(self.update, self.ctx)
        q.answer.assert_called_once()
        self.assertIn("Your vote", q.answer.call_args[0][0])

    async def test_group_card_ignores_clicker_language(self):
        bid = self._add_book()
        self.ctx.user_data["lang"] = "en"  # clicker prefers English
        q = self._vote_in("supergroup", bid)
        with patch.object(cfg, "CHAT_LANG", "ru"):
            await bot.vote_cast_cb(self.update, self.ctx)
        text = q.edit_message_text.call_args[0][0]
        # Rendered in CHAT_LANG, not the clicker's "en".
        self.assertIn("Добавлено", text)
        self.assertNotIn("Added on", text)

    async def test_private_card_still_shows_personal_vote(self):
        bid = self._add_book()
        q = self._vote_in("private", bid)
        await bot.vote_cast_cb(self.update, self.ctx)
        text = q.edit_message_text.call_args[0][0]
        self.assertIn("Your current vote", text)

    async def test_pagination_session_is_preserved_after_voting(self):
        bid = self._add_book()
        query = self._vote_in("private", bid)
        query.message = MagicMock()
        query.message.reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶️", callback_data="book_page:deadbeef:1")]]
        )
        await bot.vote_cast_cb(self.update, self.ctx)
        markup = query.edit_message_text.call_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data for row in markup.inline_keyboard for button in row
        ]
        self.assertIn("book_page:deadbeef:1", callbacks)

    async def test_out_of_range_score_is_ignored(self):
        """Crafted callback data must not raise a KeyError inside the handler."""
        bid = self._add_book()
        self._vote_in("supergroup", bid, score=7)
        await bot.vote_cast_cb(self.update, self.ctx)
        self.assertIsNone(bot.db_get_user_vote(self.update.effective_user.id, bid))

    async def test_discussed_book_rejects_forged_vote_callback(self):
        bid = self._add_book()
        bot.db_mark_discussed(bid, "2026-01-01")
        query = self._vote_in("private", bid)
        await bot.vote_cast_cb(self.update, self.ctx)
        self.assertIsNone(bot.db_get_user_vote(self.update.effective_user.id, bid))
        query.answer.assert_called_once()
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])

    async def test_vote_is_recorded_in_both_chat_types(self):
        for chat_type in ("private", "supergroup"):
            bid = self._add_book(f"B-{chat_type}")
            self._vote_in(chat_type, bid)
            with patch.object(cfg, "CHAT_LANG", "en"):
                await bot.vote_cast_cb(self.update, self.ctx)
            self.assertEqual(
                bot.db_get_user_vote(self.update.effective_user.id, bid), 1
            )


# ── Admin authorization on callbacks ──────────────────────────────────────────


class TestAdminCallbackGuards(BotHandlerTestCase):

    def setUp(self):
        super().setUp()
        self._orig_admins = cfg.ADMIN_IDS[:]
        cfg.ADMIN_IDS = [11111]  # caller (67890) is NOT admin

    def tearDown(self):
        cfg.ADMIN_IDS = self._orig_admins
        super().tearDown()

    async def test_admin_menu_cb_rejects_non_admin(self):
        q = self._callback_query("admin:toggle_chat")
        before = bot.db_get_admin_setting("post_new_books_to_chat", 0)
        result = await bot.admin_menu_cb(self.update, self.ctx)
        self.assertEqual(result, ConversationHandler.END)
        q.answer.assert_called_once()
        self.assertIn("admins only", q.answer.call_args[0][0])
        self.assertEqual(
            bot.db_get_admin_setting("post_new_books_to_chat", 0),
            before,
            "non-admin must not flip the chat-posting setting",
        )

    async def test_admin_notify_pick_cb_rejects_non_admin(self):
        self._callback_query("admin_notify_pick:cancel")
        result = await bot.admin_notify_pick_cb(self.update, self.ctx)
        self.assertEqual(result, ConversationHandler.END)
        self.ctx.bot.send_message.assert_not_called()

    async def test_admin_hide_pick_cb_rejects_non_admin(self):
        bid = bot.db_add_book("H", "A", 1, 1, "", "", 999, "u", "u")
        self._callback_query(f"admin_hide_pick:{bid}")
        await bot.admin_hide_pick_cb(self.update, self.ctx)
        self.assertEqual(bot.db_get_book(bid)["hidden"], 0)

    async def test_admin_mark_date_handler_rejects_non_admin(self):
        bid = bot.db_add_book("M", "A", 1, 1, "", "", 999, "u", "u")
        self.ctx.user_data["mark_book_id"] = bid
        self.message.text = "/today"
        await bot.admin_mark_date_handler(self.update, self.ctx)
        self.assertEqual(bot.db_get_book(bid)["discussed"], 0)


# ── Stale conversation state ──────────────────────────────────────────────────


class TestStaleState(BotHandlerTestCase):

    async def test_mark_date_without_book_id_ends_gracefully(self):
        """State can be lost if the bot restarts mid-conversation."""
        cfg.ADMIN_IDS = [self.update.effective_user.id]
        try:
            self.ctx.user_data.pop("mark_book_id", None)
            self.message.text = "/today"
            result = await bot.admin_mark_date_handler(self.update, self.ctx)
            self.assertEqual(result, ConversationHandler.END)
            self.message.reply_text.assert_called_once()
        finally:
            cfg.ADMIN_IDS = []


# ── Conversation wiring ───────────────────────────────────────────────────────


class TestConversationWiring(unittest.TestCase):
    """Guards the state/fallback ordering in register_handlers().

    ConversationHandler matches state handlers BEFORE fallbacks, so a bare
    filters.TEXT in a state silently swallows /cancel. These tests assert the
    states that accept free text still let commands through to the fallback.
    """

    def _states(self, entry_command):
        app = MagicMock()
        added = []
        app.add_handler = lambda h, *a, **kw: added.append(h)
        bot.register_handlers(app)
        for h in added:
            if not isinstance(h, ConversationHandler):
                continue
            names = {
                getattr(e, "commands", None) and tuple(e.commands)
                for e in h.entry_points
            }
            if (entry_command,) in names:
                return h
        self.fail(f"No ConversationHandler with entry /{entry_command}")

    def _matches(self, handlers, text, is_command):
        """True if any handler in the state would consume this message."""
        update = MagicMock(spec=Update)
        update.callback_query = None
        msg = MagicMock(spec=Message)
        msg.text = text
        update.message = msg
        update.effective_message = msg
        update.channel_post = None
        update.edited_message = None
        # Both filters.COMMAND and CommandHandler key off the leading entity;
        # CommandHandler slices text[1:length] to read the command name.
        msg.entities = (
            [MagicMock(type="bot_command", offset=0, length=len(text))]
            if is_command
            else []
        )
        return any(h.check_update(update) for h in handlers)

    def test_description_state_lets_cancel_reach_fallback(self):
        conv = self._states("add")
        handlers = conv.states[bot.ADDING_DESCRIPTION]
        self.assertFalse(
            self._matches(handlers, "/cancel", is_command=True),
            "/cancel must not be consumed as the book description",
        )

    def test_description_state_still_accepts_skip_and_plain_text(self):
        conv = self._states("add")
        handlers = conv.states[bot.ADDING_DESCRIPTION]
        self.assertTrue(
            self._matches(handlers, "/skip", is_command=True),
            "/skip must still be handled",
        )
        self.assertTrue(self._matches(handlers, "a description", is_command=False))

    def test_author_state_handles_forward_and_lets_cancel_reach_fallback(self):
        conv = self._states("add")
        handlers = conv.states[bot.ADDING_AUTHOR]
        self.assertTrue(
            self._matches(handlers, "/forward", is_command=True),
            "/forward must be handled while adding",
        )
        self.assertFalse(
            self._matches(handlers, "/cancel", is_command=True),
            "/cancel must not be consumed as the author",
        )

    def test_author_state_handles_edit_callback(self):
        conv = self._states("add")
        handlers = conv.states[bot.ADDING_AUTHOR]
        update = MagicMock(spec=Update)
        update.message = None
        update.edited_message = None
        update.channel_post = None
        q = MagicMock()
        q.data = "add_edit"
        update.callback_query = q
        self.assertTrue(
            any(h.check_update(update) for h in handlers),
            "add_edit must be handled while adding",
        )

    def test_inline_query_handler_registered(self):
        app = MagicMock()
        added = []
        app.add_handler = lambda h, *a, **kw: added.append(h)
        bot.register_handlers(app)
        self.assertTrue(
            any(isinstance(h, InlineQueryHandler) for h in added),
            "inline queries must be handled for AI Edit",
        )

    def test_mark_date_state_lets_cancel_reach_fallback(self):
        conv = self._states("adminconsole")
        handlers = conv.states[bot.ADMIN_MARK_DATE]
        self.assertFalse(
            self._matches(handlers, "/cancel", is_command=True),
            "/cancel must not be parsed as a discussion date",
        )

    def test_mark_date_state_still_accepts_today_and_plain_date(self):
        conv = self._states("adminconsole")
        handlers = conv.states[bot.ADMIN_MARK_DATE]
        self.assertTrue(self._matches(handlers, "/today", is_command=True))
        self.assertTrue(self._matches(handlers, "2026-01-01", is_command=False))

    def test_add_ai_choose_lets_cancel_reach_fallback(self):
        conv = self._states("add")
        self.assertIn(bot.ADDING_AI_CHOOSE, conv.states)
        handlers = conv.states[bot.ADDING_AI_CHOOSE]
        self.assertTrue(
            self._matches(handlers, "/forward", is_command=True),
            "/forward must treat AI-choose as manual fill",
        )
        self.assertFalse(
            self._matches(handlers, "/cancel", is_command=True),
            "/cancel must not be consumed on the AI-choice step",
        )

    def test_add_start_and_draft_states_registered(self):
        conv = self._states("add")
        self.assertIn(bot.ADDING_START, conv.states)
        self.assertIn(bot.ADDING_DRAFT_CHOOSE, conv.states)
        self.assertIn(bot.ADDING_CONFIRM, conv.states)
        author = conv.states[bot.ADDING_AUTHOR]
        self.assertTrue(
            self._matches(author, "/save", is_command=True),
            "/save must be handled while adding",
        )

    def test_conversations_register_cancel_button_fallback(self):
        for command in ("add", "edit", "delete", "adminconsole"):
            conv = self._states(command)
            patterns = []
            for handler in conv.fallbacks:
                pat = getattr(handler, "pattern", None)
                if pat is None:
                    continue
                patterns.append(pat.pattern if hasattr(pat, "pattern") else str(pat))
            self.assertTrue(
                any(bot.CONV_CANCEL in pattern for pattern in patterns),
                f"/{command} must handle the cancel button",
            )


class TestConversationReentry(unittest.TestCase):
    """Re-sending an entry command must always work.

    ConversationHandler only consults entry_points when no conversation is
    active (unless allow_reentry). Without it, a user who opened /adminconsole
    and walked away could re-send /adminconsole forever and match nothing —
    the update fell through every handler and the bot replied with silence.

    Uses real Telegram objects rather than mocks: a MagicMock bot makes
    CommandHandler's username comparison behave differently from production.
    """

    @classmethod
    def setUpClass(cls):
        cls.bot_obj = Bot("123456:AAHkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk")
        cls.bot_obj._bot_user = User(
            id=1, first_name="B", is_bot=True, username="testbot"
        )

    def _conversations(self):
        added = []
        app = MagicMock()
        app.add_handler = lambda h, *a, **kw: added.append(h)
        bot.register_handlers(app)
        return [h for h in added if isinstance(h, ConversationHandler)]

    def _conv(self, command):
        for conv in self._conversations():
            for entry in conv.entry_points:
                if getattr(entry, "commands", None) == {command}:
                    return conv
        self.fail(f"No ConversationHandler with entry /{command}")

    def _command_update(self, text):
        chat = Chat(id=-100123, type="supergroup")
        user = User(id=99, first_name="Admin", is_bot=False)
        msg = Message(
            message_id=1,
            date=datetime.now(),
            chat=chat,
            from_user=user,
            text=text,
            entities=[MessageEntity(type="bot_command", offset=0, length=len(text))],
        )
        msg.set_bot(self.bot_obj)
        upd = Update(update_id=1, message=msg)
        upd.set_bot(self.bot_obj)
        return upd

    def _handled(self, conv, update, state):
        key = conv._get_key(update)
        conv._conversations[key] = state
        try:
            return conv.check_update(update) is not None
        finally:
            conv._conversations.pop(key, None)

    def test_adminconsole_recovers_from_every_open_state(self):
        conv = self._conv("adminconsole")
        upd = self._command_update("/adminconsole")
        for state in (
            bot.ADMIN_MENU,
            bot.ADMIN_MARK_CHOOSE,
            bot.ADMIN_MARK_DATE,
            bot.ADMIN_HIDE_CHOOSE,
            bot.ADMIN_NOTIFY_PICK,
            bot.ADMIN_NOTIFY_CHAT_PICK,
            bot.ADDING_TITLE,
            bot.ADDING_AUTHOR,
        ):
            self.assertTrue(
                self._handled(conv, upd, state),
                f"/adminconsole did nothing while stuck in state {state}",
            )

    def test_add_edit_delete_recover_from_open_state(self):
        for command, state in (
            ("add", bot.ADDING_TITLE),
            ("add", bot.ADDING_DESCRIPTION),
            ("edit", bot.EDITING_CHOOSE),
            ("edit", bot.EDITING_FIELD),
            ("delete", bot.DELETING_CHOOSE),
        ):
            conv = self._conv(command)
            upd = self._command_update(f"/{command}")
            self.assertTrue(
                self._handled(conv, upd, state),
                f"/{command} did nothing while stuck in state {state}",
            )

    def test_entry_command_still_works_with_no_conversation(self):
        conv = self._conv("adminconsole")
        upd = self._command_update("/adminconsole")
        self.assertIsNotNone(conv.check_update(upd))

    def test_all_conversations_allow_reentry(self):
        for conv in self._conversations():
            self.assertTrue(
                conv.allow_reentry, "every conversation must be re-enterable"
            )


# ── Global error handler ──────────────────────────────────────────────────────

# ── Global error handler ──────────────────────────────────────────────────────


class TestErrorHandler(BotHandlerTestCase):
    """Without an error handler the bot replies with silence when a handler
    raises, which looks identical to the bot being down.

    The updated error_handler suppresses error notifications in group chats
    (errors are still logged and sent to the admin via _TelegramAlertHandler).
    Tests that check user-facing replies must therefore run in a private-chat
    context."""

    def setUp(self):
        super().setUp()
        # The updated error_handler checks effective_message.chat.type and
        # returns early for non-private chats. Default to private so existing
        # tests exercise the reply path.
        self.update.effective_message.chat.type = "private"

    async def test_error_handler_replies_to_message(self):
        self.ctx.error = RuntimeError("boom")
        await bot.error_handler(self.update, self.ctx)
        self.update.effective_message.reply_text.assert_called_once()
        text = self.update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Something went wrong", text)

    async def test_error_handler_replies_in_russian(self):
        self.ctx.user_data["lang"] = "ru"
        self.ctx.error = RuntimeError("boom")
        await bot.error_handler(self.update, self.ctx)
        text = self.update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Что-то пошло не так", text)

    async def test_error_handler_answers_callback_query(self):
        q = self._callback_query("vote_cast:1:1")
        # effective_message is already set to private by setUp; the handler
        # will reach the callback_query branch and clear the spinner.
        self.ctx.error = RuntimeError("boom")
        await bot.error_handler(self.update, self.ctx)
        # Spinner must be cleared, otherwise the button spins forever.
        q.answer.assert_awaited_once()
        q.message.reply_text.assert_called_once()

    async def test_error_handler_ignores_non_update(self):
        """Job errors carry no update — must not raise trying to reply."""
        self.ctx.error = RuntimeError("boom")
        await bot.error_handler("not-an-update", self.ctx)
        self.message.reply_text.assert_not_called()

    async def test_error_handler_network_error_logs_warning_only(self):
        """Transient API failures must not alert the admin or message the user."""
        self.ctx.error = NetworkError("Bad Gateway")
        with (
            patch.object(bot.logger, "warning") as mock_warn,
            patch.object(bot.logger, "error") as mock_err,
        ):
            await bot.error_handler(self.update, self.ctx)
            mock_warn.assert_called_once()
            mock_err.assert_not_called()
        self.update.effective_message.reply_text.assert_not_called()

    async def test_error_handler_survives_failed_delivery(self):
        """If replying also fails, the original error must not be masked."""
        self.ctx.error = RuntimeError("boom")
        self.update.effective_message.reply_text.side_effect = Exception("network")
        await bot.error_handler(self.update, self.ctx)  # must not raise

    async def test_error_handler_suppressed_in_group_chat(self):
        """Errors in group chats must not spam members with error messages."""
        self.update.effective_message.chat.type = "supergroup"
        self.ctx.error = RuntimeError("boom")
        await bot.error_handler(self.update, self.ctx)
        self.update.effective_message.reply_text.assert_not_called()

    async def test_error_handler_suppresses_callback_query_in_group(self):
        """A callback-query error in a group must not post a reply there."""
        self._callback_query("vote_cast:1:1")
        self.update.effective_message.chat.type = "supergroup"
        self.ctx.error = RuntimeError("boom")
        await bot.error_handler(self.update, self.ctx)
        # No reply should be sent into the group chat
        self.update.effective_message.reply_text.assert_not_called()
        # The callback query answer should also not fire a visible alert
        self.update.callback_query.answer.assert_not_awaited()

    async def test_error_handler_logs_error_in_group_chat(self):
        """Even when suppressed in groups, the error must still be logged."""
        self.update.effective_message.chat.type = "supergroup"
        self.ctx.error = RuntimeError("boom")
        with patch.object(bot.logger, "error") as mock_err:
            await bot.error_handler(self.update, self.ctx)
            mock_err.assert_called_once()

    def test_error_handler_is_registered(self):
        app = MagicMock()
        registered = []
        app.add_error_handler = lambda h: registered.append(h)
        bot.register_handlers(app)
        self.assertIn(bot.error_handler, registered)


if __name__ == "__main__":
    unittest.main()
