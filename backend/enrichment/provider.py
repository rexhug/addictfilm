"""Адаптер каталога и провайдеров к NormalizedMovieSource.

ВАЖНО про источник метаданных: в проекте нет и не может быть TMDb — ключ
получить не удалось (см. CLAUDE.md), поэтому провайдеры здесь kinopoisk.dev и
OMDb, уже используемые продуктом. Модель специально сделана провайдеро-
нейтральной: смена источника не должна протекать в правила извлечения.

Дополнительный запрос к провайдеру делается ТОЛЬКО когда в каталоге не хватает
данных для профиля, и всегда через существующий бюджет вызовов (ratelimit.py).
Обогащение никогда не ходит в сеть во время запроса рекомендаций.
"""
from __future__ import annotations

import logging
import re

import kinopoisk
import media_type as media_type_module
import omdb
import ratelimit

from .models import NormalizedMovieSource
from .taxonomy import ContentType

logger = logging.getLogger(__name__)

_CONTENT_TYPE_FROM_CATALOG = {
    media_type_module.MOVIE: ContentType.MOVIE,
    media_type_module.SERIES: ContentType.TV,
    media_type_module.EPISODE: ContentType.EPISODE,
    media_type_module.SHORT: ContentType.SHORT,
}


def _int_or_none(value: object) -> int | None:
    match = re.search(r"\d{2,4}", str(value or ""))
    return int(match.group()) if match else None


def _runtime_minutes(value: object) -> int | None:
    match = re.search(r"\d{1,3}", str(value or ""))
    return int(match.group()) if match else None


def _split(value: object) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


def content_type_of(film: dict) -> ContentType:
    """Тип записи берём из проверенного каталожного поля, не из длительности."""
    resolved = media_type_module.resolve(film)
    return _CONTENT_TYPE_FROM_CATALOG.get(resolved, ContentType.UNKNOWN)


def from_catalog(film: dict) -> NormalizedMovieSource:
    """Нормализованный источник из того, что уже лежит в каталоге."""
    provider = "kinopoisk" if film.get("kp_id") else "omdb"
    plot = film.get("plot") or None
    return NormalizedMovieSource(
        film_id=int(film.get("id") or 0),
        provider=provider,
        provider_id=str(film.get("kp_id") or film.get("imdb_id") or ""),
        content_type=content_type_of(film),
        title=str(film.get("title") or ""),
        original_title=film.get("title_original") or None,
        overview=plot,
        tagline=film.get("tagline") or None,
        genre_names=_split(film.get("genres")),
        keywords=(),
        runtime_minutes=_runtime_minutes(film.get("runtime")),
        release_year=_int_or_none(film.get("year")),
        certification=film.get("certification") or None,
        age_rating=_int_or_none(film.get("age_rating")),
        original_language=film.get("original_language") or None,
        countries=_split(film.get("countries")),
        director_names=_split(film.get("directors")),
        cast_names=_split(film.get("actors")),
    )


def _needs_refresh(source: NormalizedMovieSource) -> bool:
    """Стоит ли тратить вызов провайдера: без описания и жанров профиль всё
    равно получится низкоуверенным."""
    return not source.overview or not source.genre_names or source.content_type is ContentType.UNKNOWN


async def fetch_missing_metadata(film: dict, source: NormalizedMovieSource) -> NormalizedMovieSource:
    """Дотянуть недостающее у провайдера, уважая дневной бюджет вызовов.

    Отказ провайдера — не ошибка задания: детерминированный слой обязан
    работать и на тех данных, что уже есть в каталоге.
    """
    if not _needs_refresh(source):
        return source
    enriched = source
    kp_id = film.get("kp_id")
    if kp_id and await ratelimit.try_spend_search():
        try:
            doc = await kinopoisk.get_movie(str(kp_id))
        except Exception:  # noqa: BLE001 — провайдер не должен ронять обогащение
            logger.warning("enrichment: kinopoisk недоступен для %s", kp_id, exc_info=True)
            doc = None
        if doc:
            enriched = _merge_kinopoisk(enriched, doc)
    imdb_id = str(film.get("imdb_id") or "")
    if imdb_id.startswith("tt") and (not enriched.overview or not enriched.certification):
        try:
            data = await omdb.get_movie(imdb_id)
        except Exception:  # noqa: BLE001
            logger.warning("enrichment: OMDb недоступен для %s", imdb_id, exc_info=True)
            data = None
        if data:
            enriched = _merge_omdb(enriched, data)
    return enriched


def _replace(source: NormalizedMovieSource, **changes) -> NormalizedMovieSource:
    payload = {field: getattr(source, field) for field in NormalizedMovieSource.__dataclass_fields__}
    payload.update({key: value for key, value in changes.items() if value not in (None, (), "")})
    return NormalizedMovieSource(**payload)


def _merge_kinopoisk(source: NormalizedMovieSource, doc: dict) -> NormalizedMovieSource:
    genres = tuple(g["name"] for g in (doc.get("genres") or []) if g.get("name"))
    return _replace(
        source,
        overview=source.overview or doc.get("description") or doc.get("shortDescription"),
        tagline=source.tagline or doc.get("slogan"),
        genre_names=source.genre_names or genres,
        content_type=(source.content_type if source.content_type is not ContentType.UNKNOWN
                      else _CONTENT_TYPE_FROM_CATALOG.get(media_type_module.from_kinopoisk(doc),
                                                          ContentType.UNKNOWN)),
        certification=source.certification or doc.get("ratingMpaa"),
        age_rating=source.age_rating or doc.get("ageRating"),
        runtime_minutes=source.runtime_minutes or doc.get("movieLength"),
    )


def _merge_omdb(source: NormalizedMovieSource, data: dict) -> NormalizedMovieSource:
    def clean(value):
        return None if value in ("N/A", "", None) else value
    rated = clean(data.get("Rated"))
    return _replace(
        source,
        overview=source.overview or clean(data.get("Plot")),
        genre_names=source.genre_names or _split(clean(data.get("Genre"))),
        certification=source.certification or (rated if rated not in ("Not Rated", "Unrated") else None),
        countries=source.countries or _split(clean(data.get("Country"))),
        original_language=source.original_language or clean(data.get("Language")),
    )
