"""Tests for catalog lookup and review-page fetch/verify."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from bookclub import review_page


class TestTitleMatch(unittest.TestCase):
    def test_substring_and_tokens(self):
        self.assertTrue(
            review_page.page_mentions_title(
                "War and Peace is a novel by Leo Tolstoy.", "War and Peace"
            )
        )
        self.assertFalse(
            review_page.page_mentions_title(
                "Anna Karenina is a novel by Leo Tolstoy.", "War and Peace"
            )
        )

    def test_html_to_text_strips_tags(self):
        text = review_page.html_to_text(
            "<html><script>ignore</script><p>War and Peace</p><br>1225 pages</html>"
        )
        self.assertIn("War and Peace", text)
        self.assertIn("1225 pages", text)
        self.assertNotIn("ignore", text)
        self.assertNotIn("<p>", text)


class TestHostAllowlist(unittest.TestCase):
    def test_allows_wikipedia_and_rejects_localhost(self):
        self.assertTrue(
            review_page.host_allowed("https://en.wikipedia.org/wiki/War_and_Peace")
        )
        self.assertTrue(
            review_page.host_allowed("https://api.litres.ru/foundation/api/search")
        )
        self.assertTrue(
            review_page.host_allowed("https://v3.sg.media-imdb.com/suggestion/x/x.json")
        )
        self.assertTrue(review_page.host_allowed("https://www.wikidata.org/w/api.php"))
        self.assertTrue(review_page.host_allowed("https://www.kinopoisk.ru/film/1/"))
        self.assertFalse(review_page.host_allowed("https://127.0.0.1/secret"))
        self.assertFalse(review_page.host_allowed("http://localhost/wiki"))
        self.assertFalse(
            review_page.host_allowed("https://en.wikipedia.org.evil.example/wiki")
        )


class TestRussianCatalogPreference(unittest.TestCase):
    def test_cyrillic_title_or_russian_ui(self):
        self.assertTrue(review_page.prefers_russian_catalog("Война и мир", "en"))
        self.assertTrue(review_page.prefers_russian_catalog("War and Peace", "ru"))
        self.assertFalse(review_page.prefers_russian_catalog("War and Peace", "en"))
        self.assertFalse(review_page.prefers_russian_catalog("Der Prozess", "de"))


class TestCatalogLookup(unittest.TestCase):
    def test_wikipedia_opensearch_match(self):
        payload = json.dumps(
            [
                "War and Peace",
                ["War and Peace"],
                ["novel by Leo Tolstoy"],
                ["https://en.wikipedia.org/wiki/War_and_Peace"],
            ]
        )

        def fake_get(url: str, **_kwargs: object) -> tuple[str, str] | None:
            if "wikipedia.org" in url:
                return url, payload
            return None

        with patch.object(review_page, "http_get", side_effect=fake_get):
            url = review_page.pick_catalog_review_url(
                "War and Peace", lang="en", entity="book"
            )
        self.assertEqual(url, "https://en.wikipedia.org/wiki/War_and_Peace")

    def test_russian_book_prefers_litres(self):
        litres = {
            "payload": {
                "data": [
                    {
                        "instance": {
                            "title": "Война и Мир. Том 1-2",
                            "url": "/book/lev-tolstoy/voyna-i-mir-tom-1-2-1/",
                        }
                    },
                    {
                        "instance": {
                            "title": "Война и мир",
                            "url": "/book/lev-tolstoy/voyna-i-mir-2/",
                        }
                    },
                ]
            }
        }
        goodreads = [
            {
                "bookTitleBare": "War and Peace",
                "bookUrl": "/book/show/656.War_and_Peace",
            }
        ]

        def fake_get(url: str, **_kwargs: object) -> tuple[str, str] | None:
            if "api.litres.ru" in url:
                return url, json.dumps(litres)
            if "goodreads.com" in url:
                return url, json.dumps(goodreads)
            return None

        with patch.object(review_page, "http_get", side_effect=fake_get):
            url = review_page.pick_catalog_review_url(
                "Война и мир", lang="en", entity="book"
            )
        self.assertEqual(url, "https://www.litres.ru/book/lev-tolstoy/voyna-i-mir-2/")

    def test_non_russian_book_prefers_goodreads(self):
        litres = {
            "payload": {
                "data": [
                    {
                        "instance": {
                            "title": "War and Peace",
                            "url": "/book/lev-tolstoy/voyna-i-mir-2/",
                        }
                    }
                ]
            }
        }
        goodreads = [
            {
                "bookTitleBare": "War and Peace",
                "bookUrl": "/book/show/656.War_and_Peace",
            }
        ]

        def fake_get(url: str, **_kwargs: object) -> tuple[str, str] | None:
            if "goodreads.com" in url:
                return url, json.dumps(goodreads)
            if "api.litres.ru" in url:
                return url, json.dumps(litres)
            return None

        with patch.object(review_page, "http_get", side_effect=fake_get):
            url = review_page.pick_catalog_review_url(
                "War and Peace", lang="en", entity="book"
            )
        self.assertEqual(url, "https://www.goodreads.com/book/show/656.War_and_Peace")

    def test_russian_ui_language_prefers_litres_for_latin_title(self):
        litres = {
            "payload": {
                "data": [
                    {
                        "instance": {
                            "title": "War and Peace",
                            "url": "/book/lev-tolstoy/voyna-i-mir-2/",
                        }
                    }
                ]
            }
        }

        def fake_get(url: str, **_kwargs: object) -> tuple[str, str] | None:
            if "api.litres.ru" in url:
                return url, json.dumps(litres)
            if "goodreads.com" in url:
                self.fail("Goodreads should not be queried first for lang=ru")
            return None

        with patch.object(review_page, "http_get", side_effect=fake_get):
            url = review_page.pick_catalog_review_url(
                "War and Peace", lang="ru", entity="book"
            )
        self.assertEqual(url, "https://www.litres.ru/book/lev-tolstoy/voyna-i-mir-2/")

    def test_russian_film_prefers_kinopoisk(self):
        wikidata_search = {
            "search": [{"id": "Q123", "label": "Брат", "description": "1997 film"}]
        }
        wikidata_entities = {
            "entities": {
                "Q123": {
                    "labels": {"ru": {"value": "Брат"}},
                    "claims": {
                        "P2605": [
                            {
                                "mainsnak": {
                                    "datavalue": {"value": "41519", "type": "string"}
                                }
                            }
                        ],
                        "P345": [
                            {
                                "mainsnak": {
                                    "datavalue": {
                                        "value": "tt0118767",
                                        "type": "string",
                                    }
                                }
                            }
                        ],
                    },
                }
            }
        }
        imdb = {
            "d": [{"id": "tt0118767", "l": "Brat", "qid": "movie"}],
        }

        def fake_get(url: str, **_kwargs: object) -> tuple[str, str] | None:
            if "wbsearchentities" in url:
                return url, json.dumps(wikidata_search)
            if "wbgetentities" in url:
                return url, json.dumps(wikidata_entities)
            if "media-imdb.com" in url:
                return url, json.dumps(imdb)
            return None

        with patch.object(review_page, "http_get", side_effect=fake_get):
            url = review_page.pick_catalog_review_url("Брат", lang="en", entity="film")
        self.assertEqual(url, "https://www.kinopoisk.ru/film/41519/")

    def test_non_russian_film_prefers_imdb(self):
        imdb = {
            "d": [
                {"id": "tt1375666", "l": "Inception: The Cobol Job", "qid": "video"},
                {"id": "tt1375666", "l": "Inception", "qid": "movie"},
            ]
        }
        wikidata_search = {"search": [{"id": "Q25188", "label": "Inception"}]}
        wikidata_entities = {
            "entities": {
                "Q25188": {
                    "labels": {"en": {"value": "Inception"}},
                    "claims": {
                        "P2605": [
                            {
                                "mainsnak": {
                                    "datavalue": {"value": "447301", "type": "string"}
                                }
                            }
                        ]
                    },
                }
            }
        }

        def fake_get(url: str, **_kwargs: object) -> tuple[str, str] | None:
            if "media-imdb.com" in url:
                return url, json.dumps(imdb)
            if "wikidata.org" in url:
                return url, json.dumps(
                    wikidata_search if "wbsearchentities" in url else wikidata_entities
                )
            return None

        with patch.object(review_page, "http_get", side_effect=fake_get):
            url = review_page.pick_catalog_review_url(
                "Inception", lang="en", entity="film"
            )
        self.assertEqual(url, "https://www.imdb.com/title/tt1375666/")

    def test_skips_disambiguation(self):
        payload = json.dumps(
            [
                "Dune",
                ["Dune (disambiguation)"],
                ["Dune may refer to"],
                ["https://en.wikipedia.org/wiki/Dune_(disambiguation)"],
            ]
        )
        with patch.object(review_page, "http_get", return_value=("https://x", payload)):
            url = review_page.pick_catalog_review_url("Dune", lang="en", entity="film")
        self.assertIsNone(url)

    def test_verified_url_requires_title_on_page(self):
        def fake_fetch(url: str) -> tuple[str, str] | None:
            if "wrong-id" in url:
                return url, "Some other book entirely. 12 pages."
            return url, "War and Peace is a novel. 1225 pages."

        with patch.object(review_page, "fetch_review_text", side_effect=fake_fetch):
            chosen = review_page.first_verified_review_url(
                [
                    "https://www.goodreads.com/book/show/1.wrong-id",
                    "https://en.wikipedia.org/wiki/War_and_Peace",
                ],
                "War and Peace",
            )
        self.assertEqual(chosen, "https://en.wikipedia.org/wiki/War_and_Peace")
