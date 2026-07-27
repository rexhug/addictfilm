"""Реестр версий подбора и правило их выбора.

Главное правило, из которого следует всё остальное:

    переменная окружения решает версию ТОЛЬКО в момент создания сессии;
    все последующие запросы диспетчеризуются по версии, записанной в сессии.

Иначе выключение флага перевело бы уже начатую сессию на другой движок посреди
опроса: ответы даны одной семантикой, а результат посчитан другой.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import smart_random
from enrichment.taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION
from recommendation_questions import QUESTION_VERSION

# V2 выключен глобально и доступен только проверенному на СЕРВЕРЕ администратору.
RECOMMENDATION_ENGINE_V2_ENABLED = os.getenv(
    "RECOMMENDATION_ENGINE_V2_ENABLED", "").strip().lower() in ("1", "true", "yes")
RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW = os.getenv(
    "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", "").strip().lower() in ("1", "true", "yes")

ENGINE_V1 = "recommendation-v1"
ENGINE_V2 = "recommendation-v2"


@dataclass(frozen=True)
class EngineSpec:
    """Полный набор версий одного движка. Пишется в сессию целиком."""
    engine_version: str
    question_version: str
    policy_version: str
    taxonomy_version: str
    feature_version: str

    def as_session_versions(self) -> dict:
        return {
            "question_version": self.question_version,
            "engine_version": self.engine_version,
            "policy_version": self.policy_version,
            "taxonomy_version": self.taxonomy_version,
            "feature_version": self.feature_version,
        }


V1 = EngineSpec(
    engine_version=ENGINE_V1,
    question_version=QUESTION_VERSION,
    policy_version=smart_random.SMART_RANDOM_POLICY_VERSION,
    taxonomy_version=MOVIE_TAXONOMY_VERSION,
    feature_version=MOVIE_FEATURE_VERSION,
)

# V2 переиспользует тот же граф вопросов: он уже приведён в порядок (ноль
# декоративных ответов). Отличие V2 — в СЕМАНТИКЕ: канонический
# RecommendationIntent вместо параллельных весов и порогов.
V2 = EngineSpec(
    engine_version=ENGINE_V2,
    question_version=QUESTION_VERSION,
    policy_version=smart_random.SMART_RANDOM_POLICY_VERSION,
    taxonomy_version=MOVIE_TAXONOMY_VERSION,
    feature_version=MOVIE_FEATURE_VERSION,
)

REGISTRY: dict[str, EngineSpec] = {ENGINE_V1: V1, ENGINE_V2: V2}


def v2_available(*, is_admin: bool) -> bool:
    return RECOMMENDATION_ENGINE_V2_ENABLED or (RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW and is_admin)


def resolve_engine_for_new_session(*, is_admin: bool = False) -> EngineSpec:
    """Единственное место, где смотрят на переменные окружения."""
    return V2 if v2_available(is_admin=is_admin) else V1


def resolve_for_session(session: dict | None) -> EngineSpec:
    """Движок уже начатой сессии. Флаг здесь не участвует принципиально.

    NULL-версия — сессия старше версионирования: это legacy V1, и переводить её
    на V2 нельзя, даже если V2 сейчас доступен.
    """
    stored = (session or {}).get("engine_version")
    if not stored:
        return V1
    return REGISTRY.get(str(stored), V1)


def uses_v2(session: dict | None) -> bool:
    return resolve_for_session(session).engine_version == ENGINE_V2


def engine_state(*, is_admin: bool = False) -> dict:
    """Для админской диагностики: что сейчас получит новая сессия."""
    return {
        "v2_enabled": RECOMMENDATION_ENGINE_V2_ENABLED,
        "v2_admin_preview": RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW,
        "engine_for_new_session": resolve_engine_for_new_session(is_admin=is_admin).engine_version,
        "known_engines": sorted(REGISTRY),
    }
