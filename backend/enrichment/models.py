"""Провайдеро-независимые модели обогащения.

Сырые словари провайдера дальше адаптера не ходят. Всё, что считает признаки,
работает с NormalizedMovieSource — иначе смена источника (kinopoisk → OMDb и
обратно) протекает в правила извлечения, и они начинают зависеть от того, кто
именно ответил на запрос.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .taxonomy import ContentType

# Статусы профиля. pending/failed/stale НЕ означают «убрать фильм из каталога»:
# доступность в каталоге и пригодность для строгих рекомендаций — разные вещи.
PROFILE_READY = "ready"
PROFILE_LOW_CONFIDENCE = "low_confidence"
PROFILE_PENDING = "pending"
PROFILE_FAILED = "failed"
PROFILE_STALE = "stale"

JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_RETRY = "retry"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_DEAD_LETTER = "dead_letter"

JOB_ACTIVE_STATES = (JOB_PENDING, JOB_PROCESSING, JOB_RETRY)

JOB_FULL = "full_enrichment"
JOB_METADATA_REFRESH = "metadata_refresh"
JOB_FEATURE_REBUILD = "feature_rebuild"


@dataclass(frozen=True)
class NormalizedMovieSource:
    """Единый вход для всех правил извлечения."""
    film_id: int
    provider: str
    provider_id: str
    content_type: ContentType

    title: str
    original_title: str | None = None

    overview: str | None = None
    tagline: str | None = None

    genre_names: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    runtime_minutes: int | None = None
    release_year: int | None = None
    certification: str | None = None
    age_rating: int | None = None

    original_language: str | None = None
    countries: tuple[str, ...] = ()

    director_names: tuple[str, ...] = ()
    cast_names: tuple[str, ...] = ()

    def text(self) -> str:
        """Текст для лексического и семантического анализа.

        Название добавляем последним и только как слабую подсказку: по одному
        названию «Черноокая Сьюзан» уже однажды стала чёрной комедией.
        """
        parts = [self.overview or "", self.tagline or "", " ".join(self.keywords), self.title or ""]
        return " ".join(part for part in parts if part).strip()

    def source_payload(self) -> dict:
        """Только то, что реально влияет на признаки: постеры и рейтинги
        меняются постоянно и не должны запускать пересчёт."""
        return {
            "provider": self.provider,
            "provider_id": self.provider_id,
            "content_type": str(self.content_type),
            "title": self.title,
            "original_title": self.original_title,
            "overview": self.overview,
            "tagline": self.tagline,
            "genres": sorted(self.genre_names),
            "keywords": sorted(self.keywords),
            "runtime": self.runtime_minutes,
            "year": self.release_year,
            "certification": self.certification,
            "age_rating": self.age_rating,
            "language": self.original_language,
            "countries": sorted(self.countries),
            "directors": sorted(self.director_names),
            "cast": sorted(self.cast_names),
        }


def build_source_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FeatureEvidence:
    """Почему признак получил своё значение. Наружу не отдаётся никогда —
    только в админскую диагностику."""
    source: str
    canonical_id: str
    score: float
    confidence: float
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"source": self.source, "id": self.canonical_id,
                "score": round(self.score, 4), "confidence": round(self.confidence, 4),
                **({"meta": self.metadata} if self.metadata else {})}


@dataclass(frozen=True)
class ExtractedMovieFeatures:
    content_type: ContentType
    genres: frozenset[str] = frozenset()
    themes: frozenset[str] = frozenset()
    tones: frozenset[str] = frozenset()
    audience_flags: frozenset[str] = frozenset()
    mood: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    evidence: tuple[FeatureEvidence, ...] = ()

    def with_evidence(self, extra: tuple[FeatureEvidence, ...]) -> "ExtractedMovieFeatures":
        return ExtractedMovieFeatures(
            content_type=self.content_type, genres=self.genres, themes=self.themes,
            tones=self.tones, audience_flags=self.audience_flags, mood=dict(self.mood),
            confidence=self.confidence, evidence=self.evidence + extra)


@dataclass(frozen=True)
class EnrichmentJob:
    id: int
    film_id: int
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    requested_feature_version: str
    requested_taxonomy_version: str
    expected_source_hash: str | None = None


@dataclass(frozen=True)
class ValidationOutcome:
    """Результат автоматических проверок здравого смысла."""
    level: str                    # valid | warning | low_confidence | invalid
    warnings: tuple[str, ...] = ()

    @property
    def blocks_recommendations(self) -> bool:
        return self.level == "invalid"
