"""Search and participation statistics, without live Telegram requests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ConversationHandler

from bookclub import config, db
from bookclub.handlers import admin, commands, misc
from bookclub.i18n import COMMAND_SPECS, ENTITY_STRING_OVERLAYS, T


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setattr(config, "ADMIN_IDS", [1])
    db.init_db()

    def add(title="Title", author="Author", *, hidden=False, archived=False):
        entry = db.db_add_book(title, author, 100, True, "", "", 1, "Admin")
        if hidden:
            db.db_set_hidden(entry, True)
        if archived:
            db.db_mark_discussed(entry, "2026-01-01")
        return entry

    return add


@pytest.fixture
def interaction(catalog):
    user = SimpleNamespace(id=1, full_name="Admin", username="admin")
    update = SimpleNamespace(
        message=AsyncMock(),
        callback_query=None,
        effective_user=user,
        effective_chat=SimpleNamespace(id=1, type="private"),
    )
    ctx = SimpleNamespace(
        args=[], user_data={"lang": "en"}, bot_data={}, bot=AsyncMock()
    )
    return update, ctx


def callback(update, data, markup=None):
    query = AsyncMock()
    query.data = data
    query.from_user = update.effective_user
    query.message = SimpleNamespace(reply_markup=markup)
    update.callback_query = query
    return query


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_unicode_literal_search_and_order(catalog, monkeypatch):
    archive = catalog("Alpha", "Straße", archived=True)
    active = catalog("ЯБЛОКО", "Straße")
    catalog("Hidden", "Straße", hidden=True)
    literal = catalog("100%_real", "Толстой")
    assert [r["id"] for r in db.db_search_books(" STRASSE ")] == [active, archive]
    assert [r["id"] for r in db.db_search_books("яблоко")] == [active]
    assert [r["id"] for r in db.db_search_books("толстой")] == [literal]
    assert [r["id"] for r in db.db_search_books("%_")] == [literal]
    assert not db.db_search_books(" ")
    assert not db.db_search_books("missing")
    monkeypatch.setattr(config, "ENTRY_FIELDS", ("description",))
    assert db.db_search_books("Straße")


def test_stats_counts_and_legacy_users(catalog):
    active = catalog()
    hidden = catalog(hidden=True)
    archive = catalog(archived=True)
    deleted = catalog()
    db.db_upsert_club_user(1, "Alice", "alice")
    db.db_upsert_club_user(2, "Zero")
    for entry, score in [(active, 0), (hidden, -1), (archive, 1), (deleted, 1)]:
        db.db_cast_vote(1, entry, score)
    db.db_cast_vote(1, active, -1)
    db.db_cast_vote(3, active, 1)
    db.db_cast_vote(0, active, 1)
    db.db_delete_book(deleted)
    db.db_set_admin_setting(db.VOTES_USE_ATTENDANCE_KEY, 1)
    rows, total = db.db_user_voting_stats()
    assert total == 1
    assert [(r["user_id"], r["active_count"], r["total_count"]) for r in rows] == [
        (1, 1, 3),
        (3, 1, 1),
        (2, 0, 0),
    ]
    db.db_set_hidden(active, True)
    rows, total = db.db_user_voting_stats()
    assert total == 0 and rows[0]["active_count"] == 0


@pytest.mark.asyncio
async def test_search_prompt_empty_and_results(interaction, catalog):
    update, ctx = interaction
    assert await commands.cmd_search(update, ctx) == config.SEARCH_QUERY
    update.message.text = " "
    assert await commands.search_query_handler(update, ctx) == config.SEARCH_QUERY
    ctx.args = ["Missing"]
    assert await commands.cmd_search(update, ctx) == ConversationHandler.END
    assert "No visible" in update.message.reply_text.call_args.args[0]
    entry = catalog("<Title>")
    ctx.args = ["<Title>"]
    await commands.cmd_search(update, ctx)
    text = update.message.reply_text.call_args.args[0]
    assert "&lt;Title&gt;" in text and "Active" in text
    assert any(
        str(c).startswith(f"vote_cast:{entry}:")
        for c in callbacks(update.message.reply_text.call_args.kwargs["reply_markup"])
    )
    assert await misc.conv_cancel(update, ctx) == ConversationHandler.END


@pytest.mark.asyncio
async def test_search_navigation_rechecks_entries(interaction, catalog):
    update, ctx = interaction
    first = catalog("Title A")
    deleted = catalog("Title B")
    hidden = catalog("Title C")
    last = catalog("Title D")
    ctx.args = ["Title"]
    await commands.cmd_search(update, ctx)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    next_page = next(c for c in callbacks(markup) if c.startswith("book_page:"))
    db.db_delete_book(deleted)
    db.db_set_hidden(hidden, True)
    db.db_mark_discussed(last, "2026-01-01")
    query = callback(update, next_page)
    await commands.book_page_cb(update, ctx)
    assert "Title D" in query.edit_message_text.call_args.args[0]
    assert "Archived" in query.edit_message_text.call_args.args[0]
    assert not any(
        c.startswith("vote_cast:")
        for c in callbacks(query.edit_message_text.call_args.kwargs["reply_markup"])
    )
    db.db_set_hidden(first, True)
    db.db_delete_book(last)
    await commands.book_page_cb(update, ctx)
    assert "No visible" in query.edit_message_text.call_args.args[0]
    ctx.user_data.clear()
    ctx.user_data["lang"] = "en"
    await commands.book_page_cb(update, ctx)
    assert "expired" in query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_vote_preserves_search_navigation(interaction, catalog):
    update, ctx = interaction
    entry = catalog("Title A")
    catalog("Title B")
    ctx.args = ["Title"]
    await commands.cmd_search(update, ctx)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    query = callback(update, f"vote_cast:{entry}:0", markup)
    await misc.vote_cast_cb(update, ctx)
    assert any(
        c.startswith("book_page:")
        for c in callbacks(query.edit_message_text.call_args.kwargs["reply_markup"])
    )


@pytest.mark.asyncio
async def test_stats_pagination_back_and_authorization(interaction):
    update, ctx = interaction
    for uid in range(1, 13):
        db.db_upsert_club_user(uid, "<" * 100, f"user{uid}")
    query = callback(update, "admin:stats:0")
    assert await admin.admin_menu_cb(update, ctx) == config.ADMIN_MENU
    text = query.edit_message_text.call_args.args[0]
    assert "&lt;" in text and len(text.replace("&lt;", "<")) < 4096
    assert "admin:stats:1" in callbacks(
        query.edit_message_text.call_args.kwargs["reply_markup"]
    )
    query.data = "admin:stats:1"
    await admin.admin_menu_cb(update, ctx)
    assert "2/2" in query.edit_message_text.call_args.args[0]
    query.data = "admin:stats_back"
    await admin.admin_menu_cb(update, ctx)
    assert "admin:stats:0" in callbacks(
        query.edit_message_text.call_args.kwargs["reply_markup"]
    )
    query.reset_mock()
    update.effective_user.id = 99
    assert await admin.admin_menu_cb(update, ctx) == ConversationHandler.END
    query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_stats_empty(interaction):
    update, ctx = interaction
    query = callback(update, "admin:stats:0")
    await admin.admin_menu_cb(update, ctx)
    assert "No known" in query.edit_message_text.call_args.args[0]


def test_localized_search_and_wiring():
    from bookclub.main import register_handlers

    app = MagicMock()
    register_handlers(app)
    conversations = [
        c.args[0]
        for c in app.add_handler.call_args_list
        if isinstance(c.args[0], ConversationHandler)
    ]
    search = next(c for c in conversations if c.name == "search")
    assert "search" in search.entry_points[0].commands
    assert "cancel" in search.fallbacks[0].commands
    for lang in ("en", "ru", "de"):
        assert "search" in dict(COMMAND_SPECS[lang])
        assert "/search" in T[lang]["welcome"]
        assert "search_prompt" in ENTITY_STRING_OVERLAYS["film"][lang]
