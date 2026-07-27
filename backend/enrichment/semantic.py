"""Семантический классификатор: необязательное усиление, а не основа.

Работает ТОЛЬКО во время асинхронного обогащения. Во время запроса рекомендаций
не вызывается никогда — иначе выдача начинает зависеть от загрузки модели.

Реализации выбираются конфигурацией:

* ``disabled``             — семантики нет, только детерминированный слой;
* ``lexical`` (по умолчанию) — сравнение с проверенными описаниями меток без
  внешних зависимостей: работает на 256-мегабайтной машине Fly, где локальная
  трансформерная модель физически не помещается;
* ``sentence_transformers`` — эмбеддинги sentence-transformers; включается
  только там, где памяти достаточно (см. docs/ENRICHMENT.md).

Общий контракт: отказ классификатора НЕ ломает ни обогащение, ни добавление
фильма в каталог.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Protocol

from .mappings import normalize_label
from .models import FeatureEvidence, NormalizedMovieSource
from .taxonomy import CanonicalTone, EvidenceSource

logger = logging.getLogger(__name__)

SEMANTIC_MODEL_VERSION = "semantic-lexical-v1"

# Проверенные описания меток. Несколько формулировок на метку: одна фраза плохо
# покрывает разные способы рассказать об одном и том же.
SEMANTIC_LABEL_DESCRIPTIONS: dict[str, tuple[str, ...]] = {
    CanonicalTone.DARK_COMEDY: (
        "комедия построена вокруг смерти, преступления, морального уродства или табу",
        "смешная история о мрачных, неудобных и жестоких вещах",
        "a comedy built around dark morbid uncomfortable or taboo subject matter",
    ),
    CanonicalTone.SATIRICAL: (
        "фильм высмеивает власть, общество или человеческие пороки",
        "сатира на политику, войну, корпорации или нравы",
        "a satirical story mocking politics society or human vice",
    ),
    CanonicalTone.COZY: (
        "тёплая добрая история без напряжения, в которой хочется остаться",
        "спокойное уютное кино с ощущением безопасности",
        "a warm comforting gentle and emotionally safe viewing experience",
    ),
    CanonicalTone.THOUGHT_PROVOKING: (
        "фильм заставляет думать о морали, личности, обществе и природе реальности",
        "интеллектуальное кино, которое хочется обсуждать после просмотра",
        "a movie intended to provoke reflection about morality identity society or reality",
    ),
    CanonicalTone.SLOW_BURN: (
        "неспешная история, напряжение и чувства нарастают постепенно",
        "a deliberately paced story that builds tension or emotion gradually",
    ),
    CanonicalTone.PSYCHOLOGICAL: (
        "герой теряет связь с реальностью, психика и рассудок под давлением",
        "a psychological story about the mind paranoia and unreliable perception",
    ),
    CanonicalTone.BLEAK: (
        "безысходная история без надежды на хороший исход",
        "a bleak hopeless story with no comforting resolution",
    ),
    CanonicalTone.INSPIRING: (
        "история о преодолении, которая вдохновляет",
        "an inspiring story about overcoming the odds",
    ),
}


@dataclass(frozen=True)
class SemanticThreshold:
    """Верхняя метка сама по себе ничего не значит: нужен и уровень, и отрыв."""
    minimum_score: float
    minimum_margin: float


# Гипотезы, откалиброванные на реальном каталоге (scripts/evaluate_movie_features.py).
SEMANTIC_THRESHOLDS: dict[str, SemanticThreshold] = {
    CanonicalTone.DARK_COMEDY:       SemanticThreshold(0.34, 0.07),
    CanonicalTone.SATIRICAL:         SemanticThreshold(0.32, 0.06),
    CanonicalTone.COZY:              SemanticThreshold(0.32, 0.06),
    CanonicalTone.THOUGHT_PROVOKING: SemanticThreshold(0.32, 0.05),
    CanonicalTone.SLOW_BURN:         SemanticThreshold(0.34, 0.06),
    CanonicalTone.PSYCHOLOGICAL:     SemanticThreshold(0.32, 0.06),
    CanonicalTone.BLEAK:             SemanticThreshold(0.34, 0.06),
    CanonicalTone.INSPIRING:         SemanticThreshold(0.32, 0.06),
}
DEFAULT_THRESHOLD = SemanticThreshold(0.36, 0.08)
MINIMUM_TEXT_WORDS = 12


def accept_semantic_label(*, score: float, second_best_score: float,
                          threshold: SemanticThreshold) -> bool:
    return score >= threshold.minimum_score and (score - second_best_score) >= threshold.minimum_margin


class SemanticMovieClassifier(Protocol):
    model_version: str

    def classify(self, source: NormalizedMovieSource) -> tuple[FeatureEvidence, ...]:
        ...


class DisabledSemanticClassifier:
    """Семантики нет. Продукт обязан работать и так."""
    model_version = "semantic-disabled"

    def classify(self, source: NormalizedMovieSource) -> tuple[FeatureEvidence, ...]:
        return ()


_WORD = re.compile(r"[\w-]+", re.UNICODE)
# Служебные слова портят пересечение: они есть в любом тексте.
_STOPWORDS = frozenset(["и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли", "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя", "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому", "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти", "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех", "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над", "больше", "тот", "через", "эти", "нас", "про", "них", "какая", "много", "разве", "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with", "from", "by", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these", "those", "his", "her", "their", "our", "your", "which", "who"])


def _tokens(text: str) -> list[str]:
    return [word for word in _WORD.findall(normalize_label(text))
            if len(word) > 2 and word not in _STOPWORDS]


def _stem(word: str) -> str:
    """Грубое усечение окончания: «мрачного» и «мрачный» должны совпасть.

    Полноценная лемматизация тянула бы ещё одну зависимость ради выигрыша,
    который на этом объёме текста не измеряется.
    """
    return word[:6] if len(word) > 7 else word


class LexicalSemanticClassifier:
    """Сравнение с описаниями меток через взвешенное пересечение слов.

    Это не эмбеддинги и не претендует на их качество. Задача скромнее: дать
    контролируемый семантический сигнал там, где проверенный маркер не сработал,
    не добавляя в 256-мегабайтный контейнер полтора гигабайта весов.
    """
    model_version = SEMANTIC_MODEL_VERSION

    def __init__(self, descriptions: dict[str, tuple[str, ...]] | None = None) -> None:
        self._descriptions = descriptions or SEMANTIC_LABEL_DESCRIPTIONS
        # Профили меток считаем один раз на процесс, а не на каждый фильм.
        self._label_profiles: dict[str, dict[str, float]] = {
            label: self._profile(" ".join(texts)) for label, texts in self._descriptions.items()
        }

    @staticmethod
    def _profile(text: str) -> dict[str, float]:
        counts: dict[str, float] = {}
        for token in _tokens(text):
            key = _stem(token)
            counts[key] = counts.get(key, 0.0) + 1.0
        norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
        return {key: value / norm for key, value in counts.items()}

    def scores(self, text: str) -> dict[str, float]:
        document = self._profile(text)
        if not document:
            return {}
        return {label: round(sum(weight * document.get(key, 0.0) for key, weight in profile.items()), 4)
                for label, profile in self._label_profiles.items()}

    def classify(self, source: NormalizedMovieSource) -> tuple[FeatureEvidence, ...]:
        text = source.text()
        if len(_tokens(text)) < MINIMUM_TEXT_WORDS:
            return ()      # по обрывку текста семантику не строим
        scores = self.scores(text)
        if not scores:
            return ()
        ranked = sorted(scores.items(), key=lambda item: -item[1])
        accepted: list[FeatureEvidence] = []
        for index, (label, score) in enumerate(ranked):
            runner_up = ranked[index + 1][1] if index + 1 < len(ranked) else 0.0
            threshold = SEMANTIC_THRESHOLDS.get(label, DEFAULT_THRESHOLD)
            if accept_semantic_label(score=score, second_best_score=runner_up, threshold=threshold):
                accepted.append(FeatureEvidence(
                    EvidenceSource.SEMANTIC, label, score, min(1.0, score * 1.4),
                    {"kind": "tone", "margin": round(score - runner_up, 4)}))
        return tuple(accepted)


class SentenceTransformerSemanticClassifier:
    """Эмбеддинги sentence-transformers. Включается только явно.

    Замер на текущей машине Fly (shared-cpu-1x, 256 МБ) показал, что модель туда
    не помещается: см. docs/ENRICHMENT.md. Класс оставлен рабочим, чтобы
    переключение было конфигурацией, а не переписыванием.
    """

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # локальный импорт: не тянем в web
        self.model_version = f"semantic-st-{model_name}"
        self._model = SentenceTransformer(model_name)
        self._labels = list(SEMANTIC_LABEL_DESCRIPTIONS)
        # Эмбеддинги меток считаем один раз на процесс.
        self._label_embeddings = self._model.encode(
            [" ".join(SEMANTIC_LABEL_DESCRIPTIONS[label]) for label in self._labels],
            normalize_embeddings=True)

    def scores(self, text: str) -> dict[str, float]:
        embedding = self._model.encode([text], normalize_embeddings=True)[0]
        return {label: float(embedding @ self._label_embeddings[index])
                for index, label in enumerate(self._labels)}

    def classify(self, source: NormalizedMovieSource) -> tuple[FeatureEvidence, ...]:
        text = source.text()
        if len(_tokens(text)) < MINIMUM_TEXT_WORDS:
            return ()
        ranked = sorted(self.scores(text).items(), key=lambda item: -item[1])
        accepted = []
        for index, (label, score) in enumerate(ranked):
            runner_up = ranked[index + 1][1] if index + 1 < len(ranked) else 0.0
            threshold = SEMANTIC_THRESHOLDS.get(label, DEFAULT_THRESHOLD)
            if accept_semantic_label(score=score, second_best_score=runner_up, threshold=threshold):
                accepted.append(FeatureEvidence(EvidenceSource.SEMANTIC, label, score,
                                                min(1.0, score), {"kind": "tone"}))
        return tuple(accepted)


def build_classifier(kind: str | None = None, model_name: str | None = None):
    """Выбор реализации по конфигурации. Любая ошибка загрузки — это откат к
    детерминированному слою, а не падение обогащения."""
    # По умолчанию ВЫКЛЮЧЕН — по результатам замера на реальном каталоге, а не
    # из осторожности: лексический вариант не принял ни одной метки из 288
    # фильмов, а на пониженном пороге называл «Человека-паука» сатирой;
    # трансформерная модель требует ~1.1 ГБ RSS против 256 МБ на машине.
    # Подробности и цифры — docs/ENRICHMENT.md.
    kind = (kind or os.getenv("MOVIE_SEMANTIC_CLASSIFIER", "disabled")).strip().lower()
    if kind in ("", "disabled", "none", "off"):
        return DisabledSemanticClassifier()
    if kind in ("sentence_transformers", "st"):
        name = model_name or os.getenv("MOVIE_SEMANTIC_MODEL_NAME",
                                       "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        try:
            return SentenceTransformerSemanticClassifier(name)
        except Exception:
            logger.warning("enrichment: sentence-transformers недоступен (%s) — "
                           "продолжаем на лексическом классификаторе", name, exc_info=True)
            return LexicalSemanticClassifier()
    return LexicalSemanticClassifier()
