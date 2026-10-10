"""Fetch and verify catalog/review pages used during /add."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from bookclub.config import GITHUB_REPO
from bookclub.logging_setup import logger
from bookclub.ui import is_valid_url

FETCH_TIMEOUT_SECONDS = 12.0
FETCH_MAX_BYTES = 512_000
PAGE_TEXT_LIMIT = 12_000
_STOP_WORDS = {
    "the",
    "a",
    "an",
    "and",
    "of",
    "or",
    "in",
    "on",
    "to",
    "for",
    "и",
    "в",
    "на",
    "или",
    "der",
    "die",
    "das",
    "und",
    "von",
    "im",
}
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_FETCH_HOST_SUFFIXES = (
    "wikipedia.org",
    "wikimedia.org",
    "openlibrary.org",
    "goodreads.com",
    "litres.ru",
    "imdb.com",
    "kinopoisk.ru",
    "letterboxd.com",
    "media-imdb.com",
    "wikidata.org",
    "books.google.com",
    "books.google.ru",
    "books.google.de",
    "play.google.com",
    "googleapis.com",
)
_WIKI_LANG = {
    "en": "en",
    "ru": "ru",
    "de": "de",
    "fr": "fr",
    "es": "es",
    "it": "it",
    "pl": "pl",
    "uk": "uk",
    "zh": "zh",
    "ja": "ja",
    "ko": "ko",
    "ar": "ar",
    "he": "he",
    "el": "el",
}
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_BR_RE = re.compile(r"(?is)<br\s*/?>")
_P_RE = re.compile(r"(?is)</p>")
_TAG_RE = re.compile(r"(?is)<[^>]+>")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_DISAMBIG_RE = re.compile(r"(?i)\bdisambiguation\b|\bmay refer to\b|\bзначения\b")


def user_agent() -> str:
    repo = (GITHUB_REPO or "").strip() or "https://github.com/antmaxi/club-voting-bot"
    return f"club-voting-bot/1.0 ({repo}; Telegram book-club bot)"


def host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host:
        return False
    return any(
        host == suffix or host.endswith("." + suffix) for suffix in _FETCH_HOST_SUFFIXES
    )


def html_to_text(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _BR_RE.sub("\n", text)
    text = _P_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(title: str) -> list[str]:
    words = [w.casefold() for w in _WORD_RE.findall(title)]
    return [w for w in words if len(w) >= 3 and w not in _STOP_WORDS]


def page_mentions_title(text: str, title: str) -> bool:
    """True when ``title`` appears in ``text`` (substring or most tokens)."""
    hay = " ".join(text.casefold().split())
    needle = " ".join(title.casefold().split())
    if needle and needle in hay:
        return True
    tokens = title_tokens(title)
    if not tokens:
        return bool(needle) and needle[:24] in hay
    hits = sum(1 for token in tokens if token in hay)
    need = len(tokens) if len(tokens) <= 2 else max(2, (len(tokens) * 2 + 2) // 3)
    return hits >= need


def http_get(
    url: str, *, timeout: float = FETCH_TIMEOUT_SECONDS
) -> tuple[str, str] | None:
    """GET ``url``; return (final_url, decoded body) or None."""
    if not is_valid_url(url) or not host_allowed(url):
        return None
    req = Request(
        url,
        headers={
            "User-Agent": user_agent(),
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            final = str(resp.geturl() or url)
            if not host_allowed(final):
                return None
            raw = resp.read(FETCH_MAX_BYTES + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
    except HTTPError as e:
        logger.info("review page GET HTTP %s for %s", e.code, urlparse(url).hostname)
        return None
    except (TimeoutError, URLError, OSError, ValueError) as e:
        logger.info("review page GET failed for %s: %s", urlparse(url).hostname, e)
        return None
    if len(raw) > FETCH_MAX_BYTES:
        raw = raw[:FETCH_MAX_BYTES]
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    return final, body


def fetch_review_text(url: str) -> tuple[str, str] | None:
    """Fetch an allowlisted catalog page and return (final_url, plain text)."""
    got = http_get(url)
    if got is None:
        return None
    final, body = got
    text = html_to_text(body)[:PAGE_TEXT_LIMIT]
    if not text:
        return None
    return final, text


def first_verified_review_url(candidates: list[str], title: str) -> str | None:
    seen: set[str] = set()
    for raw in candidates:
        url = raw.strip()
        if not url or url in seen or not is_valid_url(url):
            continue
        seen.add(url)
        got = fetch_review_text(url)
        if got is None:
            continue
        final, text = got
        if page_mentions_title(text, title):
            return final
    return None


def prefers_russian_catalog(title: str, lang: str) -> bool:
    """True when the work looks Russian (Cyrillic title or Russian UI language)."""
    return lang == "ru" or bool(_CYRILLIC_RE.search(title))


@dataclass(frozen=True)
class CatalogHit:
    """One catalog page that can be offered as a review link."""

    url: str
    title: str
    review_count: int | None = None
    author: str = ""
    detail: str = ""


_CHOICE_LIMIT = 8


def catalog_choice_dict(hit: CatalogHit) -> dict[str, Any]:
    return {
        "url": hit.url,
        "title": hit.title,
        "review_count": hit.review_count,
        "author": hit.author,
        "detail": hit.detail,
    }


def _catalog_sources(
    title: str, lang: str, entity: str
) -> list[Callable[[], list[CatalogHit]]]:
    if entity == "film":
        if prefers_russian_catalog(title, lang):
            return [
                lambda: _kinopoisk_hits(title, lang),
                lambda: _imdb_hits(title),
                lambda: _wikipedia_source(title, lang, film=True),
            ]
        return [
            lambda: _imdb_hits(title),
            lambda: _kinopoisk_hits(title, lang),
            lambda: _wikipedia_source(title, lang, film=True),
        ]
    if prefers_russian_catalog(title, lang):
        return [
            lambda: _litres_hits(title),
            lambda: _goodreads_hits(title),
            lambda: _wikipedia_source(title, lang, film=False),
            lambda: _google_books_hits(title),
            lambda: _openlibrary_hits(title),
        ]
    return [
        lambda: _goodreads_hits(title),
        lambda: _litres_hits(title),
        lambda: _wikipedia_source(title, lang, film=False),
        lambda: _google_books_hits(title),
        lambda: _openlibrary_hits(title),
    ]


def catalog_review_options(title: str, *, lang: str, entity: str) -> list[CatalogHit]:
    """Same-title pages from the first catalog that has a match.

    Prefers an exact title over a longer variant. When several pages share
    that title, they are ordered by rating count (highest first) when the
    catalog provides one.
    """
    for source in _catalog_sources(title, lang, entity):
        choices = _same_title_choices(source(), title)
        if choices:
            return choices
    return []


def pick_catalog_review_url(title: str, *, lang: str, entity: str) -> str | None:
    """Best live catalog URL whose listed title matches ``title``.

    When several pages share that title, this is the one with the most ratings.
    """
    options = catalog_review_options(title, lang=lang, entity=entity)
    return options[0].url if options else None


def _https_url(url: str) -> str | None:
    url = url.strip()
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    if url and is_valid_url(url) and host_allowed(url):
        return url
    return None


def catalog_review_candidates(title: str, *, lang: str, entity: str) -> list[str]:
    return [hit.url for hit in catalog_review_options(title, lang=lang, entity=entity)]


def _collapsed_title(title: str) -> str:
    return " ".join(title.casefold().split())


def _optional_count(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _same_title_choices(hits: list[CatalogHit], book_title: str) -> list[CatalogHit]:
    """Hits that share the best listed title, most ratings first."""
    needle = _collapsed_title(book_title)
    matching: list[CatalogHit] = []
    seen: set[str] = set()
    for hit in hits:
        url = _https_url(hit.url)
        listed = hit.title.strip()
        if url is None or url in seen or not listed:
            continue
        if not page_mentions_title(listed, book_title):
            continue
        seen.add(url)
        matching.append(replace(hit, url=url, title=listed))
    if not matching:
        return []
    exact = [hit for hit in matching if _collapsed_title(hit.title) == needle]
    pool = exact
    if not pool:
        best = min(
            matching,
            key=lambda hit: abs(len(_collapsed_title(hit.title)) - len(needle)),
        )
        target = _collapsed_title(best.title)
        pool = [hit for hit in matching if _collapsed_title(hit.title) == target]
    pool.sort(key=lambda hit: (hit.review_count is None, -(hit.review_count or 0)))
    return pool[:_CHOICE_LIMIT]


def _imdb_hits(title: str) -> list[CatalogHit]:
    slug = title.strip().casefold()
    if not slug:
        return []
    api = f"https://v3.sg.media-imdb.com/suggestion/x/{quote(slug)}.json"
    got = http_get(api)
    if got is None:
        return []
    try:
        parsed = json.loads(got[1])
    except json.JSONDecodeError:
        return []
    rows = parsed.get("d") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        return []
    items: list[CatalogHit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        imdb_id = row.get("id")
        if not isinstance(imdb_id, str) or not imdb_id.startswith("tt"):
            continue
        qid = str(row.get("qid") or "")
        if qid and qid not in {"movie", "tvMovie", "short"}:
            continue
        listed = str(row.get("l") or "")
        year = row.get("y")
        stars = row.get("s")
        items.append(
            CatalogHit(
                url=f"https://www.imdb.com/title/{imdb_id}/",
                title=listed,
                author=stars.strip() if isinstance(stars, str) else "",
                detail=str(year) if isinstance(year, int) else "",
            )
        )
    return items


def _kinopoisk_hits(title: str, lang: str) -> list[CatalogHit]:
    search_lang = (
        "ru" if prefers_russian_catalog(title, lang) else _WIKI_LANG.get(lang, "en")
    )
    search_url = (
        "https://www.wikidata.org/w/api.php?action=wbsearchentities"
        f"&search={quote(title)}&language={search_lang}&uselang={search_lang}"
        "&type=item&limit=8&format=json"
    )
    got = http_get(search_url)
    if got is None:
        return []
    try:
        parsed = json.loads(got[1])
    except json.JSONDecodeError:
        return []
    hits = parsed.get("search") if isinstance(parsed, dict) else None
    if not isinstance(hits, list):
        return []
    ids = [
        str(hit.get("id"))
        for hit in hits
        if isinstance(hit, dict) and isinstance(hit.get("id"), str)
    ]
    if not ids:
        return []
    get_url = (
        "https://www.wikidata.org/w/api.php?action=wbgetentities"
        f"&ids={'|'.join(ids)}&props=labels|claims&languages=en|ru|{search_lang}"
        "&format=json"
    )
    got = http_get(get_url)
    if got is None:
        return []
    try:
        parsed = json.loads(got[1])
    except json.JSONDecodeError:
        return []
    entities = parsed.get("entities") if isinstance(parsed, dict) else None
    if not isinstance(entities, dict):
        return []
    items: list[CatalogHit] = []
    for entity_id in ids:
        entity = entities.get(entity_id)
        if not isinstance(entity, dict):
            continue
        listed = _wikidata_label(entity, search_lang)
        kp = _wikidata_external_id(entity, "P2605")
        if not listed or not kp or not kp.isdigit():
            continue
        items.append(
            CatalogHit(
                url=f"https://www.kinopoisk.ru/film/{kp}/",
                title=listed,
                detail=_wikidata_year(entity),
            )
        )
    return items


def _wikidata_label(entity: dict[str, object], lang: str) -> str:
    labels = entity.get("labels")
    if not isinstance(labels, dict):
        return ""
    for code in (lang, "ru", "en"):
        entry = labels.get(code)
        if isinstance(entry, dict):
            value = entry.get("value")
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _wikidata_external_id(entity: dict[str, object], prop: str) -> str | None:
    claims = entity.get("claims")
    if not isinstance(claims, dict):
        return None
    snaks = claims.get(prop)
    if not isinstance(snaks, list) or not snaks:
        return None
    first = snaks[0]
    if not isinstance(first, dict):
        return None
    mainsnak = first.get("mainsnak")
    if not isinstance(mainsnak, dict):
        return None
    datavalue = mainsnak.get("datavalue")
    if not isinstance(datavalue, dict):
        return None
    value = datavalue.get("value")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _wikidata_year(entity: dict[str, object]) -> str:
    claims = entity.get("claims")
    if not isinstance(claims, dict):
        return ""
    snaks = claims.get("P577")
    if not isinstance(snaks, list):
        return ""
    for snak in snaks:
        if not isinstance(snak, dict):
            continue
        mainsnak = snak.get("mainsnak")
        if not isinstance(mainsnak, dict):
            continue
        datavalue = mainsnak.get("datavalue")
        if not isinstance(datavalue, dict):
            continue
        value = datavalue.get("value")
        if not isinstance(value, dict):
            continue
        time = value.get("time")
        if not isinstance(time, str):
            continue
        year = time.lstrip("+-")[:4]
        if year.isdigit():
            return year
    return ""


def _wikipedia_source(title: str, lang: str, *, film: bool) -> list[CatalogHit]:
    for wiki_lang, query in _wiki_queries(title, lang, film=film):
        hits = _wikipedia_hits(query, wiki_lang, title)
        if hits:
            return hits
    return []


def _wiki_lang_codes(lang: str) -> list[str]:
    primary = _WIKI_LANG.get(lang, "en")
    codes = [primary]
    if primary != "en":
        codes.append("en")
    return codes


def _wiki_queries(title: str, lang: str, *, film: bool) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    suffix = " (film)" if film else " (novel)"
    for wiki_lang in _wiki_lang_codes(lang):
        queries.append((wiki_lang, f"{title}{suffix}"))
        queries.append((wiki_lang, title))
    return queries


def _wikipedia_hits(query: str, wiki_lang: str, book_title: str) -> list[CatalogHit]:
    api = (
        f"https://{wiki_lang}.wikipedia.org/w/api.php"
        f"?action=opensearch&search={quote(query)}"
        "&limit=5&namespace=0&format=json&redirects=resolve"
    )
    got = http_get(api)
    if got is None:
        return []
    try:
        parsed = json.loads(got[1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list) or len(parsed) < 4:
        return []
    names = parsed[1]
    descs = parsed[2] if len(parsed) > 2 else []
    urls = parsed[3]
    if not isinstance(names, list) or not isinstance(urls, list):
        return []
    hits: list[CatalogHit] = []
    for i, url in enumerate(urls):
        if not isinstance(url, str):
            continue
        name = names[i] if i < len(names) and isinstance(names[i], str) else ""
        desc = ""
        if isinstance(descs, list) and i < len(descs) and isinstance(descs[i], str):
            desc = descs[i]
        blob = f"{name} {desc}"
        if _DISAMBIG_RE.search(blob):
            continue
        if name and page_mentions_title(name, book_title):
            hits.append(CatalogHit(url=url, title=name))
    return hits


def _absolute_catalog_url(base: str, path: str) -> str:
    path = path.strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return base.rstrip("/") + path


def _person_name(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name") or value.get("full_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _first_named(value: object) -> str:
    if isinstance(value, list):
        for item in value:
            name = _person_name(item)
            if name:
                return name
        return ""
    return _person_name(value)


def _litres_author(persons: object) -> str:
    if not isinstance(persons, list):
        return ""
    for person in persons:
        if not isinstance(person, dict) or person.get("role") != "author":
            continue
        name = _person_name(person)
        if name:
            return name
    return ""


def _litres_hits(title: str) -> list[CatalogHit]:
    api = (
        "https://api.litres.ru/foundation/api/search"
        f"?limit=10&types=text_book&q={quote(title)}"
    )
    got = http_get(api)
    if got is None:
        return []
    try:
        parsed = json.loads(got[1])
    except json.JSONDecodeError:
        return []
    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    items: list[CatalogHit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        instance = row.get("instance")
        if not isinstance(instance, dict):
            continue
        listed = str(instance.get("title") or "")
        path = instance.get("url")
        if not isinstance(path, str) or not path.strip():
            continue
        rating = instance.get("rating")
        count = None
        if isinstance(rating, dict):
            count = _optional_count(rating.get("rated_total_count"))
        subtitle = instance.get("subtitle")
        items.append(
            CatalogHit(
                url=_absolute_catalog_url("https://www.litres.ru", path),
                title=listed,
                review_count=count,
                author=_litres_author(instance.get("persons")),
                detail=subtitle.strip() if isinstance(subtitle, str) else "",
            )
        )
    return items


def _goodreads_hits(title: str) -> list[CatalogHit]:
    api = (
        "https://www.goodreads.com/book/auto_complete" f"?format=json&q={quote(title)}"
    )
    got = http_get(api)
    if got is None:
        return []
    try:
        parsed = json.loads(got[1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[CatalogHit] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        listed = str(row.get("bookTitleBare") or row.get("title") or "")
        path = row.get("bookUrl")
        if not isinstance(path, str) or not path.strip():
            continue
        items.append(
            CatalogHit(
                url=_absolute_catalog_url("https://www.goodreads.com", path),
                title=listed,
                review_count=_optional_count(row.get("ratingsCount")),
                author=_person_name(row.get("author")),
            )
        )
    return items


def _google_books_hits(title: str) -> list[CatalogHit]:
    api = (
        "https://www.googleapis.com/books/v1/volumes"
        f"?q=intitle:{quote(title)}&maxResults=10&printType=books"
    )
    got = http_get(api)
    if got is None:
        return []
    try:
        parsed = json.loads(got[1])
    except json.JSONDecodeError:
        return []
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []
    hits: list[CatalogHit] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        info = item.get("volumeInfo")
        if not isinstance(info, dict):
            continue
        listed = str(info.get("title") or "")
        link = ""
        for key in ("canonicalVolumeLink", "infoLink", "previewLink"):
            raw = info.get(key)
            if isinstance(raw, str) and raw.strip():
                link = raw.strip()
                break
        if not link:
            continue
        subtitle = info.get("subtitle")
        hits.append(
            CatalogHit(
                url=link,
                title=listed,
                review_count=_optional_count(info.get("ratingsCount")),
                author=_first_named(info.get("authors")),
                detail=subtitle.strip() if isinstance(subtitle, str) else "",
            )
        )
    return hits


def _openlibrary_hits(title: str) -> list[CatalogHit]:
    api = (
        "https://openlibrary.org/search.json"
        f"?title={quote(title)}&limit=10&fields=key,title,author_name,ratings_count"
    )
    got = http_get(api)
    if got is None:
        return []
    try:
        parsed = json.loads(got[1])
    except json.JSONDecodeError:
        return []
    docs = parsed.get("docs") if isinstance(parsed, dict) else None
    if not isinstance(docs, list):
        return []
    hits: list[CatalogHit] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        listed = str(doc.get("title") or "")
        key = doc.get("key")
        if not isinstance(key, str) or not key.startswith("/"):
            continue
        hits.append(
            CatalogHit(
                url=f"https://openlibrary.org{key}",
                title=listed,
                review_count=_optional_count(doc.get("ratings_count")),
                author=_first_named(doc.get("author_name")),
            )
        )
    return hits
