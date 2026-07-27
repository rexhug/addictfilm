"""Выполнение одного задания обогащения — от фильма до сохранённого профиля."""
from __future__ import annotations

import logging
import time

import database as db

from . import extraction, merge, provider, queue, repository, validation
from .models import JOB_FULL, PROFILE_FAILED, NormalizedMovieSource, build_source_hash
from .semantic import DisabledSemanticClassifier
from .taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION, RULE_EXTRACTOR_VERSION, ContentType

logger = logging.getLogger(__name__)

_merger = merge.MovieFeatureMerger()


class PermanentEnrichmentError(Exception):
    """Повторять бессмысленно: данные не станут другими."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def enqueue_for_film(film_id: int, *, priority: int = 0, connection=None) -> bool:
    """Единственная точка постановки задания на текущие версии."""
    return await queue.enqueue(
        film_id, feature_version=MOVIE_FEATURE_VERSION, taxonomy_version=MOVIE_TAXONOMY_VERSION,
        job_type=JOB_FULL, priority=priority, connection=connection)


async def build_profile(film: dict, *, classifier=None, fetch_provider: bool = True
                        ) -> tuple[NormalizedMovieSource, dict]:
    """Посчитать профиль фильма. Возвращает (источник, поля профиля).

    Ошибка семантического классификатора здесь НЕ поднимается: детерминированный
    слой обязан отработать в любом случае, иначе один сбойный компонент
    выключает обогащение целиком.
    """
    # Два источника с РАЗНЫМ назначением, и путать их нельзя:
    #
    # catalog_source  — только то, что лежит в каталоге. Это воспроизводимая
    #                   личность записи для пересчёта: согласование считает хеш
    #                   ровно из неё и обязано получить то же число.
    # resolved_source — то же плюс временно дотянутое у провайдера. Богаче, но
    #                   невоспроизводимо: провайдер может ответить иначе или не
    #                   ответить вовсе.
    #
    # Если считать хеш из resolved_source, согласование никогда не совпадёт с
    # сохранённым значением и будет вечно переставлять фильм в очередь:
    # changed_source → задание → фетч → тот же профиль → снова changed_source.
    catalog_source = provider.from_catalog(film)
    if not catalog_source.title.strip():
        raise PermanentEnrichmentError("missing_title", "у записи нет названия")
    source = catalog_source
    if fetch_provider:
        source = await provider.fetch_missing_metadata(film, catalog_source)

    deterministic = extraction.extract(source)

    classifier = classifier or DisabledSemanticClassifier()
    semantic: tuple = ()
    semantic_version = getattr(classifier, "model_version", None)
    try:
        semantic = classifier.classify(source)
    except Exception:
        logger.warning("enrichment: семантический классификатор упал на film_id=%s",
                       source.film_id, exc_info=True)
        semantic_version = f"{semantic_version}:failed" if semantic_version else None

    override = await repository.get_override(source.film_id)
    features, contradictions = _merger.merge(deterministic=deterministic, semantic=semantic,
                                             overrides=override)
    outcome = validation.validate(features)

    confidence = merge.calculate_profile_confidence(
        has_valid_content_type=features.content_type is not ContentType.UNKNOWN,
        genre_count=len(features.genres),
        reviewed_marker_count=len(deterministic.tones) + len(deterministic.themes),
        has_overview=bool(source.overview),
        semantic_confidence=max((item.confidence for item in semantic), default=0.0),
        agreement_score=merge.agreement(deterministic, semantic),
        contradiction_count=contradictions + sum(
            1 for warning in outcome.warnings if not warning.startswith("out_of_range")),
    )
    features = type(features)(
        content_type=features.content_type, genres=features.genres, themes=features.themes,
        tones=features.tones, audience_flags=features.audience_flags, mood=features.mood,
        confidence=confidence, evidence=features.evidence)
    status = merge.resolve_status(confidence, invalid=outcome.blocks_recommendations)
    return source, {
        "features": features,
        "status": status,
        "validation_level": outcome.level,
        "warnings": outcome.warnings,
        "semantic_model_version": semantic_version,
        # Хеш — из КАТАЛОЖНОГО источника, не из дотянутого: см. комментарий выше.
        # Ручная правка входит в него как полноправный вход, иначе пересчёт
        # после правки выглядел бы как «ничего не изменилось» и пропускался.
        # Формула обязана совпадать с reconciliation._needs_rebuild.
        "source_hash": build_source_hash({**catalog_source.source_payload(), "override": override}),
    }


async def process_job(job, *, classifier=None, fetch_provider: bool = True) -> dict:
    """Обработать одно задание. Внешняя работа идёт ВНЕ транзакции очереди."""
    started = time.monotonic()
    film = await db.get_film(job.film_id)
    if not film:
        raise PermanentEnrichmentError("malformed_source", "фильм удалён из каталога")

    existing = await repository.get_profile(job.film_id)
    source, result = await build_profile(film, classifier=classifier, fetch_provider=fetch_provider)

    unchanged = (
        existing
        and existing["source_hash"] == result["source_hash"]
        and existing["feature_version"] == MOVIE_FEATURE_VERSION
        and existing["taxonomy_version"] == MOVIE_TAXONOMY_VERSION
        and existing["extractor_version"] == RULE_EXTRACTOR_VERSION
        and existing["semantic_model_version"] == result["semantic_model_version"]
        and existing["status"] != PROFILE_FAILED
    )
    if unchanged:
        # Ничего не изменилось — не переписываем строку ради той же строки.
        return {"film_id": job.film_id, "status": existing["status"], "skipped": True,
                "duration_ms": int((time.monotonic() - started) * 1000)}

    await repository.save_profile(
        job.film_id, result["features"], status=result["status"],
        source_hash=result["source_hash"], feature_version=MOVIE_FEATURE_VERSION,
        taxonomy_version=MOVIE_TAXONOMY_VERSION, extractor_version=RULE_EXTRACTOR_VERSION,
        semantic_model_version=result["semantic_model_version"],
        validation_level=result["validation_level"], warnings=result["warnings"])
    return {"film_id": job.film_id, "status": result["status"], "skipped": False,
            "confidence": round(result["features"].confidence, 4),
            "content_type": str(result["features"].content_type),
            "warnings": list(result["warnings"]),
            "duration_ms": int((time.monotonic() - started) * 1000)}


def classify_error(error: BaseException) -> tuple[str, str]:
    """Ошибка → (код, безопасное сообщение). Токенов и стеков здесь не бывает."""
    if isinstance(error, PermanentEnrichmentError):
        return error.code, str(error)
    name = type(error).__name__
    text = str(error)[:200]
    if "Timeout" in name or "timeout" in text.lower():
        return "provider_timeout", text
    if "429" in text:
        return "provider_rate_limit", text
    if "Client" in name or "aiohttp" in name.lower():
        return "provider_5xx", text
    if "Operational" in name or "asyncpg" in name.lower():
        return "database_unavailable", text
    return "unknown", f"{name}: {text}"
