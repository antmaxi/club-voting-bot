"""Tests for LLM-backed admin add-book suggestions."""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import bookclub.config as cfg
import bookclub.logging_setup as log_setup
from bookclub import llm


class TestExtractJson(unittest.TestCase):
    def test_log_redaction_removes_provider_and_bearer_secrets(self):
        text = "sk-secret Authorization: Bearer crsr_token"
        redacted = llm.redact_llm_secrets(text)
        self.assertNotIn("sk-secret", redacted)
        self.assertNotIn("crsr_token", redacted)

    def test_plain_object(self):
        self.assertEqual(llm.extract_json_object('{"author": "A"}'), {"author": "A"})

    def test_fenced_block(self):
        text = 'Sure.\n```json\n{"pages": 100}\n```\n'
        self.assertEqual(llm.extract_json_object(text), {"pages": 100})

    def test_embedded_object(self):
        text = 'Here you go: {"fiction": true} thanks'
        self.assertEqual(llm.extract_json_object(text), {"fiction": True})

    def test_rejects_non_object(self):
        with self.assertRaises(ValueError):
            llm.extract_json_object("[1, 2]")


class TestNormalizeSuggestions(unittest.TestCase):
    def test_keeps_enabled_fields_and_coerces_types(self):
        raw = {
            "author": "  Leo Tolstoy ",
            "pages": "1,225",
            "fiction": "fiction",
            "review_link": "https://en.wikipedia.org/wiki/War_and_Peace",
            "original_language": "русский",
            "creation_year": 1869,
            "language_levels": ["b2", "C1", "nope"],
            "description": " An epic. ",
        }
        enabled = frozenset(cfg.OPTIONAL_ENTRY_FIELDS)
        got = llm.normalize_suggestions(raw, enabled=enabled)
        self.assertEqual(got["author"], "Leo Tolstoy")
        self.assertEqual(got["pages"], 1225)
        self.assertTrue(got["fiction"])
        self.assertEqual(
            got["review_link"], "https://en.wikipedia.org/wiki/War_and_Peace"
        )
        self.assertEqual(got["original_language"], "Russian")
        self.assertEqual(got["creation_year"], 1869)
        self.assertEqual(got["language_levels"], {"B2", "C1"})
        self.assertEqual(got["description"], "An epic.")

    def test_drops_disabled_and_invalid_values(self):
        raw = {
            "author": "A",
            "pages": 0,
            "review_link": "not-a-url",
            "creation_year": 99,
            "fiction": "maybe",
        }
        got = llm.normalize_suggestions(raw, enabled=frozenset({"author"}))
        self.assertEqual(got, {"author": "A"})

    def test_film_aliases(self):
        raw = {"director": "Nolan", "runtime_minutes": 148, "is_feature_film": True}
        got = llm.normalize_suggestions(
            raw, enabled=frozenset({"author", "pages", "fiction"})
        )
        self.assertEqual(got["author"], "Nolan")
        self.assertEqual(got["pages"], 148)
        self.assertTrue(got["fiction"])

    def test_original_language_from_ui_label(self):
        self.assertEqual(llm.normalize_original_language("🇩🇪 Deutsch"), "German")
        self.assertEqual(llm.normalize_original_language("de"), "German")
        self.assertEqual(llm.normalize_original_language("Klingon"), "Klingon")


class TestSuggestBookFields(unittest.TestCase):
    def setUp(self):
        self._provider = patch.object(cfg, "LLM_PROVIDER", "chat")
        self._provider.start()
        self.addCleanup(self._provider.stop)

    def test_not_configured(self):
        with patch.object(cfg, "LLM_API_KEY", ""):
            fields, error = llm.suggest_book_fields("Title", lang="en")
        self.assertEqual(fields, {})
        self.assertEqual(error, "not_configured")

    def test_no_optional_fields(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion") as mocked,
        ):
            fields, error = llm.suggest_book_fields(
                "Title", lang="en", enabled_fields=frozenset()
            )
        self.assertEqual(fields, {})
        self.assertIsNone(error)
        mocked.assert_not_called()

    def test_request_failed(self):
        # Do not wrap with assertLogs: that disables propagate, so the root
        # ERROR-alert handler would not see the record.
        log_setup._alert_buffer.clear()
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(cfg, "LLM_MODEL", "test-model"),
            patch.object(cfg, "LLM_API_BASE", "https://example.test/v1"),
            patch.object(
                llm, "chat_completion", side_effect=llm.LlmRequestError("boom")
            ),
        ):
            fields, error = llm.suggest_book_fields("War and Peace", lang="en")
        self.assertEqual(fields, {})
        self.assertIsNotNone(error)
        self.assertIn("boom", error or "")
        self.assertTrue(error and error.startswith("request:"))
        self.assertTrue(log_setup.ERROR_ALERTS)
        joined = "\n".join(log_setup._alert_buffer)
        self.assertIn("LLM book suggestions failed [request]", joined)
        self.assertIn("boom", joined)
        self.assertIn("War and Peace", joined)
        self.assertIn("test-model", joined)

    def test_unusable_json_logs_error(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="not json at all"),
            self.assertLogs("bookclub.logging_setup", level="ERROR") as captured,
        ):
            fields, error = llm.suggest_book_fields("Title", lang="en")
        self.assertEqual(fields, {})
        self.assertIsNotNone(error)
        self.assertTrue(any("unusable_json" in line for line in captured.output))
        self.assertTrue(error and error.startswith("unusable_json:"))

    def test_not_configured_does_not_log_error(self):
        with (
            patch.object(cfg, "LLM_API_KEY", ""),
            patch.object(llm.logger, "error") as mock_error,
        ):
            fields, error = llm.suggest_book_fields("Title", lang="en")
        self.assertEqual(error, "not_configured")
        mock_error.assert_not_called()

    def test_parses_completion(self):
        payload = json.dumps({"author": "A", "pages": 10, "fiction": True})
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value=payload),
            patch.object(
                cfg, "ENTRY_FIELDS", frozenset({"author", "pages", "fiction"})
            ),
        ):
            fields, error = llm.suggest_book_fields("Title", lang="en")
        self.assertIsNone(error)
        self.assertEqual(fields["author"], "A")
        self.assertEqual(fields["pages"], 10)
        self.assertTrue(fields["fiction"])

    def test_film_prompt_mentions_director(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields(
                "Inception",
                lang="en",
                entity="film",
                enabled_fields=frozenset({"author", "pages"}),
            )
        messages = mocked.call_args[0][0]
        user = messages[1]["content"]
        self.assertIn("director", user)
        self.assertIn("runtime", user)

    def test_book_prompt_review_mentions_goodreads_or_litres(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields(
                "War and Peace",
                lang="en",
                entity="book",
                enabled_fields=frozenset({"review"}),
            )
        user = mocked.call_args[0][0][1]["content"]
        self.assertIn("Goodreads", user)
        self.assertIn("LitRes", user)
        self.assertIn("Prefer a Goodreads page", user)
        self.assertNotIn("IMDb", user)
        self.assertNotIn("Wikipedia article", user)

    def test_book_prompt_review_prefers_litres_for_russian(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields(
                "Война и мир",
                lang="en",
                entity="book",
                enabled_fields=frozenset({"review"}),
            )
        user = mocked.call_args[0][0][1]["content"]
        self.assertIn("Prefer a LitRes page", user)
        self.assertIn("Goodreads", user)

    def test_film_prompt_review_mentions_catalog_sites(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields(
                "Inception",
                lang="en",
                entity="film",
                enabled_fields=frozenset({"review"}),
            )
        user = mocked.call_args[0][0][1]["content"]
        self.assertIn("IMDb", user)
        self.assertIn("Kinopoisk", user)
        self.assertIn("Prefer an IMDb page", user)
        self.assertNotIn("Wikipedia article", user)

    def test_film_prompt_review_prefers_kinopoisk_for_russian(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields(
                "Брат",
                lang="en",
                entity="film",
                enabled_fields=frozenset({"review"}),
            )
        user = mocked.call_args[0][0][1]["content"]
        self.assertIn("Prefer a Kinopoisk page", user)
        self.assertIn("IMDb", user)

    def test_book_prompt_pages_from_review_page(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields(
                "War and Peace",
                lang="en",
                entity="book",
                enabled_fields=frozenset({"pages", "review"}),
            )
        messages = mocked.call_args[0][0]
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("page count", system)
        self.assertIn("catalog/review page", user)
        self.assertIn("Goodreads", user)
        self.assertIn("LitRes", user)
        self.assertIn("same review_link page", user)
        self.assertNotIn("IMDb", user)

    def test_film_prompt_runtime_from_review_page(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields(
                "Inception",
                lang="en",
                entity="film",
                enabled_fields=frozenset({"pages", "review"}),
            )
        messages = mocked.call_args[0][0]
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("runtime", system)
        self.assertIn("catalog/review page", user)
        self.assertIn("IMDb", user)
        self.assertIn("same review_link page", user)
        self.assertNotIn("Goodreads", user)

    def test_book_prompt_pages_only_still_nudges_catalog_page(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields(
                "War and Peace",
                lang="en",
                entity="book",
                enabled_fields=frozenset({"pages"}),
            )
        messages = mocked.call_args[0][0]
        user = messages[1]["content"]
        self.assertIn("catalog/review page", user)
        self.assertNotIn("same review_link page", user)
        self.assertIn("page count", messages[0]["content"])


class TestSuggestReviewLink(unittest.TestCase):
    def test_prefers_catalog_without_calling_llm(self):
        wiki = "https://en.wikipedia.org/wiki/War_and_Peace"
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch("bookclub.llm.pick_catalog_review_url", return_value=wiki),
            patch.object(llm, "chat_completion") as mocked,
        ):
            url, error = llm.suggest_review_link("War and Peace", lang="en")
        self.assertEqual(url, wiki)
        self.assertIsNone(error)
        mocked.assert_not_called()

    def test_drops_unverified_llm_url(self):
        fake = "https://www.goodreads.com/book/show/999.War_and_Peace"
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch("bookclub.llm.pick_catalog_review_url", return_value=None),
            patch.object(
                llm, "chat_completion", return_value=json.dumps({"review_link": fake})
            ),
            patch(
                "bookclub.llm.first_verified_review_url", return_value=None
            ) as verify,
        ):
            url, error = llm.suggest_review_link("War and Peace", lang="en")
        self.assertIsNone(url)
        self.assertIsNone(error)
        verify.assert_called_once()
        self.assertEqual(verify.call_args[0][0], [fake])

    def test_from_page_prompt_copies_count_from_excerpt(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(llm, "chat_completion", return_value="{}") as mocked,
        ):
            llm.suggest_book_fields_from_page(
                "War and Peace",
                "Leo Tolstoy. 1225 pages.",
                "https://en.wikipedia.org/wiki/War_and_Peace",
                lang="en",
                entity="book",
                enabled_fields=frozenset({"pages", "author"}),
            )
        messages = mocked.call_args[0][0]
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("1225 pages", user)
        self.assertIn("do not guess", system.casefold())
        self.assertIn("page count", user)


class TestChatCompletion(unittest.TestCase):
    def setUp(self):
        self._provider = patch.object(cfg, "LLM_PROVIDER", "chat")
        self._provider.start()
        self.addCleanup(self._provider.stop)

    def _response(self, payload: dict) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_reads_message_content(self):
        payload = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(cfg, "LLM_API_BASE", "https://example.test/v1"),
            patch.object(cfg, "LLM_MODEL", "test-model"),
            patch(
                "urllib.request.urlopen", return_value=self._response(payload)
            ) as mocked,
        ):
            content = llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(content, '{"ok": true}')
        req = mocked.call_args[0][0]
        self.assertEqual(req.full_url, "https://example.test/v1/chat/completions")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "test-model")
        self.assertIn("Bearer sk-test", req.get_header("Authorization") or "")
        self.assertEqual(body["temperature"], 0.2)
        self.assertNotIn("reasoning_effort", body)

    def test_grok_omits_temperature_and_uses_low_effort(self):
        payload = {"choices": [{"message": {"content": "{}"}}]}
        with (
            patch.object(cfg, "LLM_API_KEY", "xai-test"),
            patch.object(cfg, "LLM_API_BASE", "https://api.x.ai/v1"),
            patch.object(cfg, "LLM_MODEL", "grok-4.6"),
            patch.object(cfg, "LLM_REASONING_EFFORT", ""),
            patch(
                "urllib.request.urlopen", return_value=self._response(payload)
            ) as mocked,
        ):
            llm.chat_completion([{"role": "user", "content": "hi"}])
        body = json.loads(mocked.call_args[0][0].data.decode("utf-8"))
        self.assertNotIn("temperature", body)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(
            mocked.call_args[0][0].full_url, "https://api.x.ai/v1/chat/completions"
        )

    def test_reads_json_from_reasoning_content(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": 'thinking...\n{"author": "A"}\n',
                    }
                }
            ]
        }
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(cfg, "LLM_API_BASE", "https://example.test/v1"),
            patch.object(cfg, "LLM_MODEL", "test-model"),
            patch("urllib.request.urlopen", return_value=self._response(payload)),
        ):
            content = llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertIn('"author": "A"', content)

    def test_http_error(self):
        err = HTTPError(
            "https://example.test/v1/chat/completions",
            401,
            "Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error":"nope"}'),
        )
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch("urllib.request.urlopen", side_effect=err),
            self.assertRaises(llm.LlmRequestError) as caught,
        ):
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(str(caught.exception), "auth: HTTP 401: nope")
        self.assertEqual(caught.exception.kind, "auth")

    def test_urlerror_timeout(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch(
                "urllib.request.urlopen",
                side_effect=URLError(TimeoutError("timed out")),
            ),
            self.assertRaises(llm.LlmRequestError) as caught,
        ):
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertIn("timed out after", str(caught.exception))

    def test_network_error(self):
        with (
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch("urllib.request.urlopen", side_effect=URLError("down")),
            self.assertRaises(llm.LlmRequestError) as caught,
        ):
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "network")
        self.assertIn("down", str(caught.exception))


class TestApplySuggestions(unittest.TestCase):
    def test_copies_keys(self):
        nb = {"title": "T"}
        filled = llm.apply_suggestions_to_book(nb, {"author": "A", "pages": 3})
        self.assertEqual(nb["author"], "A")
        self.assertEqual(nb["title"], "T")
        self.assertEqual(filled, {"author", "pages"})

    def test_preserve_skips_user_edited_keys(self):
        nb = {"title": "T", "author": "Mine"}
        filled = llm.apply_suggestions_to_book(
            nb, {"author": "Leo", "pages": 10}, preserve={"author"}
        )
        self.assertEqual(nb["author"], "Mine")
        self.assertEqual(nb["pages"], 10)
        self.assertEqual(filled, {"pages"})


class TestResolveLlmSettings(unittest.TestCase):
    def test_cursor_key_is_not_reused_as_provider_credential(self):
        with patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "",
                "XAI_API_KEY": "",
                "OPENAI_API_KEY": "",
                "CURSOR_API_KEY": "crsr_should_not_leave_the_ide",
            },
            clear=False,
        ):
            self.assertEqual(cfg.resolve_llm_api_key(), "")

    def test_rejects_insecure_remote_llm_base(self):
        with (
            patch.dict(
                os.environ,
                {"LLM_API_BASE": "http://example.com/v1"},
                clear=False,
            ),
            self.assertRaises(ValueError),
        ):
            cfg.resolve_llm_api_base("sk-test")

    def test_xai_key_infers_base_and_model(self):
        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "",
                "LLM_API_KEY": "xai-secret",
                "XAI_API_KEY": "",
                "LLM_API_BASE": "",
                "LLM_MODEL": "",
            },
            clear=False,
        ):
            key = cfg.resolve_llm_api_key()
            base = cfg.resolve_llm_api_base(key)
            model = cfg.resolve_llm_model(base, key)
        self.assertEqual(key, "xai-secret")
        self.assertEqual(base, "https://api.x.ai/v1")
        self.assertEqual(model, "grok-4.6")

    def test_xai_api_key_alias(self):
        with patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "",
                "OPENAI_API_KEY": "",
                "CURSOR_API_KEY": "",
                "XAI_API_KEY": "xai-from-alias",
                "LLM_API_BASE": "",
                "LLM_MODEL": "",
            },
            clear=False,
        ):
            key = cfg.resolve_llm_api_key()
            self.assertEqual(key, "xai-from-alias")
            self.assertEqual(cfg.resolve_llm_api_base(key), "https://api.x.ai/v1")

    def test_xai_key_overrides_leftover_openai_defaults(self):
        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "",
                "LLM_API_KEY": "xai-secret",
                "XAI_API_KEY": "",
                "LLM_API_BASE": "https://api.openai.com/v1",
                "LLM_MODEL": "gpt-4o-mini",
                "LLM_TIMEOUT_SECONDS": "",
            },
            clear=False,
        ):
            key = cfg.resolve_llm_api_key()
            base = cfg.resolve_llm_api_base(key)
            model = cfg.resolve_llm_model(base, key)
            timeout = cfg.resolve_llm_timeout_seconds(model)
        self.assertEqual(base, "https://api.x.ai/v1")
        self.assertEqual(model, "grok-4.6")
        self.assertEqual(timeout, 120.0)

    def test_ui_llm_error_redacts_keys(self):
        text = llm.ui_llm_error(
            'HTTP 401: Incorrect API key provided: xai-abc123SECRET {"error":"nope"}'
        )
        self.assertIn("401", text)
        self.assertNotIn("abc123SECRET", text)
        self.assertIn("[redacted]", text)

    def test_ui_llm_error_redacts_exact_configured_key_and_bearer_token(self):
        secret = "provider-secret-with-unrecognized-prefix"
        with patch.object(cfg, "LLM_API_KEY", secret):
            text = llm.ui_llm_error(f"Authorization: Bearer opaque-token key={secret}")
        self.assertNotIn(secret, text)
        self.assertNotIn("opaque-token", text)


class TestLlmErrorKinds(unittest.TestCase):
    def test_classifies_http_kinds(self):
        cases = [
            (401, "HTTP 401: nope", "auth"),
            (400, "HTTP 400: Incorrect API key provided", "auth"),
            (429, "HTTP 429: Too Many Requests", "rate_limit"),
            (404, "HTTP 404: The model `gpt-4o-mini` does not exist", "bad_model"),
            (404, "HTTP 404: no such endpoint", "not_found"),
            (400, "HTTP 400: Invalid argument", "bad_request"),
            (503, "HTTP 503: unavailable", "server"),
        ]
        for code, detail, kind in cases:
            with self.subTest(detail=detail):
                self.assertEqual(llm._http_error_kind(code, detail), kind)

    def test_split_llm_error(self):
        self.assertEqual(
            llm.split_llm_error("auth: HTTP 401: Incorrect API key"),
            ("auth", "HTTP 401: Incorrect API key"),
        )
        self.assertEqual(
            llm.split_llm_error("HTTP 401: leftover"),
            ("request", "HTTP 401: leftover"),
        )

    def test_empty_reply_kind(self):
        payload = {"choices": [{"message": {"content": ""}}]}
        with (
            patch.object(cfg, "LLM_PROVIDER", "chat"),
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
            patch.object(cfg, "LLM_API_BASE", "https://example.test/v1"),
            patch.object(cfg, "LLM_MODEL", "test-model"),
            patch(
                "urllib.request.urlopen",
                return_value=TestChatCompletion()._response(payload),
            ),
            self.assertRaises(llm.LlmRequestError) as caught,
        ):
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "empty_reply")


class TestCursorProvider(unittest.TestCase):
    def test_chat_provider_aliases(self):
        for raw in ("", "openai", "chat", "xai"):
            with (
                self.subTest(raw=raw),
                patch.dict(os.environ, {"LLM_PROVIDER": raw}, clear=False),
            ):
                self.assertEqual(cfg.resolve_llm_provider(), "chat")

    def test_unknown_provider_falls_back_to_chat(self):
        with (
            patch.dict(os.environ, {"LLM_PROVIDER": "nope"}, clear=False),
            patch("builtins.print") as printed,
        ):
            self.assertEqual(cfg.resolve_llm_provider(), "chat")
        self.assertTrue(
            any("unknown LLM_PROVIDER" in str(call) for call in printed.call_args_list)
        )

    def test_cursor_default_model(self):
        with patch.dict(os.environ, {"LLM_MODEL": ""}, clear=False):
            self.assertEqual(cfg.resolve_llm_model(provider="cursor"), "composer-2.5")

    def test_cursor_replaces_leftover_chat_models(self):
        for leftover in ("gpt-4o-mini", "grok-4.6"):
            with (
                self.subTest(leftover=leftover),
                patch.dict(os.environ, {"LLM_MODEL": leftover}, clear=False),
            ):
                self.assertEqual(
                    cfg.resolve_llm_model(provider="cursor"), "composer-2.5"
                )

    def test_cursor_keeps_explicit_model(self):
        with patch.dict(os.environ, {"LLM_MODEL": "composer-2.5-fast"}, clear=False):
            self.assertEqual(
                cfg.resolve_llm_model(provider="cursor"), "composer-2.5-fast"
            )

    def test_llm_configured_cursor_uses_cursor_key_only(self):
        with (
            patch.object(cfg, "LLM_PROVIDER", "cursor"),
            patch.object(cfg, "CURSOR_API_KEY", ""),
            patch.object(cfg, "LLM_API_KEY", "sk-test"),
        ):
            self.assertFalse(cfg.llm_configured())
        with (
            patch.object(cfg, "LLM_PROVIDER", "cursor"),
            patch.object(cfg, "CURSOR_API_KEY", "crsr_x"),
            patch.object(cfg, "LLM_API_KEY", ""),
        ):
            self.assertTrue(cfg.llm_configured())

    def test_provider_origin_is_cursor(self):
        with patch.object(cfg, "LLM_PROVIDER", "cursor"):
            self.assertEqual(llm._provider_origin(), "cursor")

    def test_redacts_configured_cursor_key(self):
        secret = "cursor-secret-without-prefix"
        with patch.object(cfg, "CURSOR_API_KEY", secret):
            text = llm.redact_llm_secrets(f"key={secret}")
        self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)


class TestCursorCompletion(unittest.TestCase):
    def _install_sdk(self, prompt):
        class AgentOptions:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class LocalAgentOptions:
            def __init__(self, *, cwd):
                self.cwd = cwd

        class CursorAgentError(Exception):
            def __init__(self, message, is_retryable=False):
                super().__init__(message)
                self.message = message
                self.is_retryable = is_retryable

        class Agent:
            @staticmethod
            def prompt(message, options):
                return prompt(message, options)

        patcher = patch.object(
            llm,
            "_cursor_sdk",
            return_value=(Agent, AgentOptions, CursorAgentError, LocalAgentOptions),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return CursorAgentError

    def _configured(self, api_key="crsr_x"):
        return (
            patch.object(cfg, "LLM_PROVIDER", "cursor"),
            patch.object(cfg, "CURSOR_API_KEY", api_key),
            patch.object(cfg, "LLM_MODEL", "composer-2.5"),
        )

    def test_concatenates_prompt_and_disables_tools(self):
        captured = {}

        class FakeResult:
            status = "finished"
            result = "  hi  "
            id = "run-1"

        def prompt(message, options):
            captured["message"] = message
            captured["options"] = options
            self.assertTrue(os.path.isdir(options.local.cwd))
            return FakeResult()

        self._install_sdk(prompt)
        provider, key, model = self._configured()
        with provider, key, model:
            text = llm.chat_completion(
                [
                    {"role": "system", "content": "  sys  "},
                    {"role": "user", "content": "  usr  "},
                ]
            )
        self.assertEqual(text, "hi")
        self.assertEqual(captured["message"], "sys\n\nusr")
        self.assertEqual(captured["options"].api_key, "crsr_x")
        self.assertEqual(captured["options"].model, "composer-2.5")
        self.assertEqual(captured["options"].tools, [])

    def test_missing_key_raises_before_sdk(self):
        provider, key, model = self._configured(api_key="")
        with (
            provider,
            key,
            model,
            patch.object(llm, "_cursor_sdk", side_effect=AssertionError("sdk")),
            self.assertRaises(llm.LlmRequestError) as caught,
        ):
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("API key is not set", str(caught.exception))

    def test_missing_sdk_raises(self):
        import sys

        with (
            patch.dict(sys.modules, {"cursor_sdk": None}),
            self.assertRaises(llm.LlmRequestError) as caught,
        ):
            llm._cursor_sdk()
        self.assertEqual(caught.exception.kind, "request")
        self.assertIn("cursor-sdk", str(caught.exception))

    def test_run_error_raises(self):
        class FakeResult:
            status = "error"
            result = ""
            id = "run-9"

        self._install_sdk(lambda message, options: FakeResult())
        provider, key, model = self._configured()
        with provider, key, model, self.assertRaises(llm.LlmRequestError) as caught:
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "request")
        self.assertIn("Cursor run error (run-9)", str(caught.exception))

    def test_empty_response_raises(self):
        class FakeResult:
            status = "finished"
            result = "  "
            id = "run-1"

        self._install_sdk(lambda message, options: FakeResult())
        provider, key, model = self._configured()
        with provider, key, model, self.assertRaises(llm.LlmRequestError) as caught:
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "empty_reply")

    def test_startup_error_is_auth_when_key_is_rejected(self):
        holder: dict = {}

        def boom(message, options):
            raise holder["err"]("invalid key")

        holder["err"] = self._install_sdk(boom)
        provider, key, model = self._configured()
        with provider, key, model, self.assertRaises(llm.LlmRequestError) as caught:
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("Cursor: invalid key", str(caught.exception))

    def test_startup_error_without_auth_is_request(self):
        holder: dict = {}

        def boom(message, options):
            raise holder["err"]("bridge failed")

        holder["err"] = self._install_sdk(boom)
        provider, key, model = self._configured()
        with provider, key, model, self.assertRaises(llm.LlmRequestError) as caught:
            llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "request")
        self.assertIn("Cursor: bridge failed", str(caught.exception))
