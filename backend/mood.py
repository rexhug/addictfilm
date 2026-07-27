"""Явный, версионированный слой настроения поверх базового движка подбора.

Базовый движок (recommendations.py) отвечает за качество, личные вкусы,
отрицательные сигналы, честность к паре и анти-повторы. Этот модуль добавляет
ровно одну недостающую вещь: понимание, КАКОЙ ПРОСМОТР человек хочет сейчас,
и переранжирование по близости к этому запросу.

Ключевые решения:
* 8 интерпретируемых измерений вместо непрозрачной модели;
* у запроса есть не только цель, но и ВАЖНОСТЬ каждого измерения — то, чего
  человек не выбирал, не должно портить совпадение;
* признаки фильма детерминированы и версионированы: одинаковый вход всегда даёт
  одинаковый выход, никаких обращений к внешним API во время запроса;
* у признаков есть уверенность: плохо размеченный фильм не получает крайних
  оценок настроения, а тянется к нейтральному.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import text_matching

MOVIE_MOOD_FEATURE_VERSION = "movie-mood-v1"
MOOD_SCORING_VERSION = "mood-ranking-v1"

MOOD_DIMENSIONS = (
    "energy",        # 0 — сдержанно, 1 — заряжено
    "pace",          # 0 — медленно, 1 — быстро
    "tension",       # 0 — спокойно, 1 — напряжённо
    "darkness",      # 0 — светло, 1 — мрачно
    "humor",         # 0 — без юмора, 1 — комедийно
    "emotionality",  # 0 — отстранённо, 1 — эмоционально
    "complexity",    # 0 — просто, 1 — требует внимания
    "realism",       # 0 — фантастично, 1 — достоверно
)

# Базовые профили по жанрам — гипотезы, выверенные на реальном каталоге
# (см. scripts/review_mood_features.py). Ключи — канонические русские жанры,
# как их хранит каталог (см. database._GENRE_CANON).
GENRE_MOOD_BASE: dict[str, dict[str, float]] = {
    "боевик":         {"energy": .88, "pace": .82, "tension": .62, "darkness": .42, "humor": .20, "emotionality": .42, "complexity": .30, "realism": .50},
    "приключения":    {"energy": .72, "pace": .68, "tension": .42, "darkness": .25, "humor": .30, "emotionality": .42, "complexity": .28, "realism": .28},
    "мультфильм":     {"energy": .55, "pace": .55, "tension": .28, "darkness": .22, "humor": .48, "emotionality": .58, "complexity": .30, "realism": .18},
    "комедия":        {"energy": .58, "pace": .60, "tension": .14, "darkness": .15, "humor": .90, "emotionality": .38, "complexity": .25, "realism": .60},
    "криминал":       {"energy": .55, "pace": .55, "tension": .65, "darkness": .68, "humor": .12, "emotionality": .42, "complexity": .55, "realism": .78},
    "документальный": {"energy": .28, "pace": .30, "tension": .24, "darkness": .35, "humor": .10, "emotionality": .42, "complexity": .62, "realism": 1.0},
    "драма":          {"energy": .36, "pace": .35, "tension": .42, "darkness": .50, "humor": .12, "emotionality": .82, "complexity": .52, "realism": .78},
    "семейный":       {"energy": .48, "pace": .50, "tension": .18, "darkness": .08, "humor": .48, "emotionality": .58, "complexity": .20, "realism": .48},
    "фэнтези":        {"energy": .58, "pace": .55, "tension": .38, "darkness": .35, "humor": .22, "emotionality": .50, "complexity": .45, "realism": .08},
    "ужасы":          {"energy": .62, "pace": .52, "tension": .94, "darkness": .92, "humor": .05, "emotionality": .50, "complexity": .36, "realism": .42},
    "детектив":       {"energy": .42, "pace": .42, "tension": .70, "darkness": .58, "humor": .08, "emotionality": .38, "complexity": .76, "realism": .60},
    "мелодрама":      {"energy": .32, "pace": .36, "tension": .22, "darkness": .24, "humor": .28, "emotionality": .84, "complexity": .30, "realism": .68},
    "фантастика":     {"energy": .62, "pace": .56, "tension": .54, "darkness": .46, "humor": .14, "emotionality": .42, "complexity": .68, "realism": .16},
    "триллер":        {"energy": .68, "pace": .68, "tension": .88, "darkness": .65, "humor": .06, "emotionality": .42, "complexity": .54, "realism": .66},
    "биография":      {"energy": .34, "pace": .34, "tension": .34, "darkness": .40, "humor": .16, "emotionality": .72, "complexity": .50, "realism": .92},
    "история":        {"energy": .44, "pace": .40, "tension": .46, "darkness": .48, "humor": .10, "emotionality": .62, "complexity": .55, "realism": .84},
    "военный":        {"energy": .62, "pace": .55, "tension": .74, "darkness": .76, "humor": .05, "emotionality": .70, "complexity": .48, "realism": .82},
    "мюзикл":         {"energy": .66, "pace": .58, "tension": .18, "darkness": .18, "humor": .45, "emotionality": .70, "complexity": .25, "realism": .40},
}

# Модификаторы по сюжетным маркерам. В каталоге НЕТ провайдерских keywords,
# поэтому опираемся на устойчивые маркеры в описании — тот же приём, что уже
# используется базовым движком (PLOT_KEYWORDS), но с явным вкладом в измерения.
PLOT_MOOD_DELTAS: dict[str, dict[str, float]] = {
    "feel_good":     {"darkness": -.30, "emotionality": .18, "tension": -.18},
    "friendship":    {"emotionality": .18, "darkness": -.10},
    "family_bond":   {"emotionality": .18, "darkness": -.12},
    "dark_comedy":   {"humor": .22, "darkness": .28, "tension": .08},
    "satirical":     {"humor": .26, "complexity": .16},
    "psychological": {"complexity": .26, "tension": .20, "darkness": .16},
    "slow_burn":     {"pace": -.30, "complexity": .14, "tension": .10},
    "survival":      {"tension": .26, "energy": .14, "darkness": .12},
    "investigation": {"complexity": .22, "tension": .16, "realism": .08},
    "coming_of_age": {"emotionality": .26, "darkness": -.08},
    "philosophical": {"complexity": .32, "pace": -.08},
    "tragedy":       {"darkness": .34, "emotionality": .28, "humor": -.18},
    "dystopia":      {"darkness": .28, "complexity": .14, "realism": -.12},
    "time_travel":   {"complexity": .18, "realism": -.22},
    "revenge":       {"darkness": .22, "tension": .18},
    "supernatural":  {"realism": -.30, "tension": .14},
    "war_horror":    {"darkness": .24, "tension": .20},
}

# Маркеры ищем и по-русски, и по-английски: каталог смешанный.
# ВАЖНО: сопоставление идёт по ГРАНИЦЕ СЛОВА, а не подстрокой. Ручная проверка
# на реальном каталоге показала, что подстроки ловят случайные слова:
# «сем» находилось в «во-семь» и «в-сем-и», «мест» — в «в-мест-е» и «мест-ной»,
# из-за чего мультфильм получал маркеры «семья» и «месть».
PLOT_MARKERS: dict[str, tuple[str, ...]] = {
    "feel_good":     ("добр", "тёпл", "тепл", "уютн", "feel-good", "heartwarming"),
    "friendship":    ("друж", "товарищ", "friendship"),
    "family_bond":   ("семь", "семей", "родител", "отц", "матер", "family", "father", "mother"),
    # ТОЛЬКО полные обороты: подстрока «чёрн»/«черн» ловила «Чернику»,
    # «Черноокую Сьюзан» и «Чёрную птицу» и выдавала их за чёрные комедии.
    "dark_comedy":   ("чёрная комеди", "черная комеди", "чёрной комеди", "черной комеди",
                      "чёрный юмор", "черный юмор", "чёрного юмора", "черного юмора",
                      "чёрным юмором", "черным юмором", "трагикомед",
                      "чёрная сатира", "черная сатира",
                      "жестоким чувством юмора", "жестокое чувство юмора",
                      "жестокий юмор", "злой юмор",
                      "dark comedy", "black comedy", "dark humor", "dark humour", "tragicomed"),
    "satirical":     ("сатир", "пароди", "satire", "satirical", "parody", "высмеива", "гротеск"),
    "psychological": ("психолог", "рассудок", "psycholog", "sanity"),
    "slow_burn":     ("медленн", "неспешн", "созерцат", "slow burn"),
    "survival":      ("выжив", "катастроф", "спасен", "surviv", "disaster"),
    "investigation": ("расслед", "улик", "следовател", "investigat"),
    "coming_of_age": ("взросл", "юност", "подрост", "coming of age"),
    "philosophical": ("философ", "экзистенц", "philosoph"),
    "tragedy":       ("траге", "гибел", "смерт", "утрат", "tragedy"),
    "dystopia":      ("антиутоп", "тоталитар", "dystop"),
    "time_travel":   ("путешествие во времен", "временн петл", "time travel"),
    "revenge":       ("месть", "мести", "мстит", "отомст", "возмезди", "revenge"),
    "supernatural":  ("сверхъест", "призрак", "демон", "мистик", "supernatural", "ghost"),
    "war_horror":    ("войн", "фронт", "битв", "сражени"),
}

# Один маркер не должен переворачивать профиль: суммарный сдвиг по измерению
# ограничен, иначе длинное описание с десятком совпадений уводит фильм в край.
MAX_TOTAL_MARKER_DELTA = 0.45
# Насколько измерение подтягивается к определяющему жанру (0 — чистое среднее,
# 1 — max()). Значение выверено ручным обзором каталога.
SIGNATURE_GENRE_PULL = 0.66
# Подтягиваем только «наличие элемента»: юмор в комедии никуда не девается от
# соседних жанров. А тон (darkness) и достоверность (realism) — свойства фильма
# в целом, и семейный мультфильм не должен становиться мрачным из-за военной
# линии, поэтому там остаётся честное усреднение.
PRESENCE_DIMENSIONS = ("humor", "tension", "complexity", "energy", "pace", "emotionality")


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class MovieMoodFeatures:
    """Профиль настроения фильма. confidence — насколько метаданных хватило."""
    film_id: int
    feature_version: str
    values: dict[str, float]
    confidence: float
    markers: tuple[str, ...] = ()
    source_hash: str = ""

    def get(self, dimension: str) -> float:
        return self.values.get(dimension, 0.5)


@dataclass(frozen=True)
class MoodIntent:
    """Чего человек хочет сейчас. Важность обязательна: измерение, про которое
    он ничего не сказал, не должно создавать ложных несовпадений."""
    targets: dict[str, float] = field(default_factory=dict)
    importance: dict[str, float] = field(default_factory=dict)
    hard_constraints: dict[str, object] = field(default_factory=dict)

    def validate(self) -> None:
        for name, mapping in (("target", self.targets), ("importance", self.importance)):
            for key, value in mapping.items():
                if key not in MOOD_DIMENSIONS:
                    raise ValueError(f"Unknown mood dimension in {name}: {key}")
                if not 0.0 <= float(value) <= 1.0:
                    raise ValueError(f"Mood {name} out of range: {key}={value}")


@dataclass(frozen=True)
class MoodContribution:
    targets: dict[str, float] = field(default_factory=dict)
    importance: dict[str, float] = field(default_factory=dict)
    hard_constraints: dict[str, object] = field(default_factory=dict)


def _canon_genres(value: object) -> list[str]:
    """Каталог хранит и русские, и английские жанры — приводим к одному канону."""
    from database import _canon_genre  # локальный импорт: избегаем цикла
    return [_canon_genre(part.strip()).casefold()
            for part in str(value or "").split(",") if part.strip()]


def detect_markers(film: dict) -> tuple[str, ...]:
    """Совпадение только с НАЧАЛА слова — общая реализация в text_matching."""
    text = f"{film.get('plot') or ''} {film.get('title') or ''}"
    return tuple(sorted(text_matching.matched_keys(text, PLOT_MARKERS)))


def _runtime_minutes(value: object) -> int | None:
    match = re.search(r"\d{2,3}", str(value or ""))
    return int(match.group()) if match else None


def build_movie_features(film: dict) -> MovieMoodFeatures:
    """Детерминированный профиль настроения из локальных метаданных.

    Никаких внешних вызовов: только жанры, описание и длительность, которые уже
    лежат в каталоге. Один и тот же фильм всегда даёт один и тот же профиль.
    """
    genres = [g for g in _canon_genres(film.get("genres")) if g in GENRE_MOOD_BASE]
    if genres:
        # Взвешенное среднее, а не max(): у фильма «комедия, драма» юмор не
        # должен быть таким же, как у чистой комедии.
        values = {dim: sum(GENRE_MOOD_BASE[g][dim] for g in genres) / len(genres)
                  for dim in MOOD_DIMENSIONS}
        # Но чистое усреднение топит определяющий жанр: у «мультфильм, комедия,
        # приключения, фэнтези» юмор падал до 0.28, хотя комедия там заявлена.
        # Поэтому измерение подтягивается к самому выраженному жанру — не до
        # максимума (иначе вернёмся к max()), а на две трети расстояния.
        for dim in PRESENCE_DIMENSIONS:
            strongest = max(GENRE_MOOD_BASE[g][dim] for g in genres)
            if strongest > values[dim]:
                values[dim] += (strongest - values[dim]) * SIGNATURE_GENRE_PULL
    else:
        values = {dim: 0.5 for dim in MOOD_DIMENSIONS}

    markers = detect_markers(film)
    accumulated: dict[str, float] = {dim: 0.0 for dim in MOOD_DIMENSIONS}
    for marker in markers:
        for dimension, delta in PLOT_MOOD_DELTAS.get(marker, {}).items():
            room = MAX_TOTAL_MARKER_DELTA - abs(accumulated[dimension])
            if room <= 0:
                continue
            applied = max(-room, min(room, delta))
            accumulated[dimension] += applied
            values[dimension] = clamp01(values[dimension] + applied)

    # Длительность — слабый модификатор темпа, а не правило «длинное = медленное».
    runtime = _runtime_minutes(film.get("runtime"))
    if runtime:
        if runtime >= 150:
            values["pace"] = clamp01(values["pace"] - 0.06)
        elif runtime <= 95:
            values["pace"] = clamp01(values["pace"] + 0.05)

    confidence = clamp01(
        (0.45 if genres else 0.0)
        + min(len(markers), 5) * 0.08
        + (0.10 if runtime else 0.0)
        + (0.05 if len(genres) >= 2 else 0.0)
    )
    # Самый мрачный жанр в наборе — отдельно от усреднённого тона. Для «есть ли
    # у фильма по-настоящему мрачная составляющая» среднее не годится: у любой
    # чёрной комедии жанр «комедия» тянет darkness вниз, и вопрос «мрачно ли
    # тут вообще» остаётся без ответа.
    peak_darkness = max((GENRE_MOOD_BASE[g]["darkness"] for g in genres), default=0.5)
    payload = f"{sorted(genres)}|{sorted(markers)}|{runtime}|{MOVIE_MOOD_FEATURE_VERSION}"
    return MovieMoodFeatures(
        film_id=int(film.get("id") or 0),
        feature_version=MOVIE_MOOD_FEATURE_VERSION,
        values={**{dim: round(values[dim], 4) for dim in MOOD_DIMENSIONS},
                "peak_darkness": round(clamp01(max(values["darkness"], peak_darkness)), 4)},
        confidence=round(confidence, 4),
        markers=markers,
        source_hash=hashlib.sha1(payload.encode()).hexdigest()[:16],
    )


def mood_similarity(intent: MoodIntent, features: MovieMoodFeatures) -> float:
    """0…1 по важности: измерения, про которые человек ничего не сказал,
    в расчёт не идут вовсе."""
    total_importance = 0.0
    weighted = 0.0
    for dimension, target in intent.targets.items():
        importance = float(intent.importance.get(dimension, 0.0))
        if importance <= 0:
            continue
        weighted += (1.0 - abs(float(target) - features.get(dimension))) * importance
        total_importance += importance
    if total_importance <= 0:
        return 0.5      # запрос ничего не уточнил — настроение нейтрально
    return clamp01(weighted / total_importance)


def confidence_adjusted(similarity: float, confidence: float) -> float:
    """Плохо размеченный фильм не должен получать крайних оценок настроения."""
    confidence = clamp01(confidence)
    return clamp01(confidence * clamp01(similarity) + (1.0 - confidence) * 0.5)


def resolve_mood_weight(profile_confidence: float) -> float:
    """У новичка решает явный запрос, у «старожила» — ещё и личная история."""
    return 0.55 - 0.17 * clamp01(profile_confidence)


def combine_scores(*, base_score: float, mood_score: float, profile_confidence: float) -> float:
    """base_score уже содержит качество, вкусы и честность к паре — здесь его
    не пересчитываем, только смешиваем с настроением."""
    mood_weight = resolve_mood_weight(profile_confidence)
    return clamp01((1.0 - mood_weight) * clamp01(base_score) + mood_weight * clamp01(mood_score))


# ── Ответы анкеты → намерение ────────────────────────────────────────────────
# Мапим ID СУЩЕСТВУЮЩЕЙ (уже адаптивной, с ветками) анкеты: она работает в
# проде, и ломать её ради новых идентификаторов незачем. Каждый ответ даёт и
# цель, и важность — вес того, насколько человек этого действительно хотел.
ANSWER_CONTRIBUTIONS: dict[tuple[str, str], MoodContribution] = {
    # q1 — что хочется почувствовать
    ("q1", "relax"):   MoodContribution({"tension": .12, "darkness": .18, "pace": .32, "energy": .35},
                                        {"tension": 1.0, "darkness": .80, "pace": .70, "energy": .40}),
    ("q1", "tension"): MoodContribution({"tension": .90, "energy": .72},
                                        {"tension": 1.0, "energy": .55}),
    ("q1", "humor"):   MoodContribution({"humor": .90, "darkness": .25},
                                        {"humor": 1.0, "darkness": .45}),
    ("q1", "emotion"): MoodContribution({"emotionality": .92},
                                        {"emotionality": 1.0}),
    ("q1", "unusual"): MoodContribution({"complexity": .80, "realism": .28},
                                        {"complexity": .90, "realism": .45}),
    # relax-ветка
    ("r1", "warm"):      MoodContribution({"darkness": .12, "emotionality": .70}, {"darkness": .85, "emotionality": .50}),
    ("r1", "adventure"): MoodContribution({"energy": .70, "pace": .66, "darkness": .22}, {"energy": .60, "pace": .55, "darkness": .50}),
    ("r1", "visual"):    MoodContribution({"pace": .28, "realism": .22}, {"pace": .55, "realism": .40}),
    ("r1", "simple"):    MoodContribution({"complexity": .18, "humor": .70}, {"complexity": .80, "humor": .55}),
    ("r2", "fast"):      MoodContribution({"pace": .82, "energy": .72}, {"pace": .90, "energy": .50}),
    ("r2", "medium"):    MoodContribution({"pace": .50}, {"pace": .45}),
    ("r2", "slow"):      MoodContribution({"pace": .18, "complexity": .60}, {"pace": .90, "complexity": .35}),
    ("r3", "comfort"):   MoodContribution({"darkness": .10, "tension": .10}, {"darkness": .90, "tension": .80}),
    ("r3", "positive"):  MoodContribution({"darkness": .22, "emotionality": .60}, {"darkness": .70, "emotionality": .40}),
    ("r3", "drama"):     MoodContribution({"emotionality": .78, "darkness": .55}, {"emotionality": .70, "darkness": .40}),
    # tension-ветка
    ("t1", "survival"):      MoodContribution({"tension": .88, "energy": .80, "realism": .70}, {"tension": .95, "energy": .60, "realism": .35}),
    ("t1", "psychological"): MoodContribution({"tension": .82, "complexity": .82, "darkness": .70}, {"tension": .85, "complexity": .85, "darkness": .55}),
    ("t1", "mystery"):       MoodContribution({"tension": .74, "complexity": .80}, {"tension": .80, "complexity": .85}),
    ("t1", "horror"):        MoodContribution({"tension": .96, "darkness": .90}, {"tension": 1.0, "darkness": .85}),
    ("t4", "clear"):         MoodContribution({"complexity": .35}, {"complexity": .50}),
    ("t4", "semi"):          MoodContribution({"complexity": .60}, {"complexity": .35}),
    ("t4", "open"):          MoodContribution({"complexity": .85}, {"complexity": .60}),
    # humor-ветка
    ("h1", "situational"): MoodContribution({"humor": .82, "darkness": .15}, {"humor": .90, "darkness": .60}),
    ("h1", "sarcastic"):   MoodContribution({"humor": .80, "complexity": .55}, {"humor": .85, "complexity": .40}),
    ("h1", "absurd"):      MoodContribution({"humor": .88, "realism": .20}, {"humor": .90, "realism": .55}),
    ("h1", "dark"):        MoodContribution({"humor": .78, "darkness": .72, "complexity": .50}, {"humor": .85, "darkness": .80, "complexity": .30}),
    ("h1", "warm"):        MoodContribution({"humor": .72, "darkness": .12, "emotionality": .65}, {"humor": .80, "darkness": .80, "emotionality": .45}),
    ("h2", "none"):        MoodContribution({"complexity": .25}, {"complexity": .40}),
    ("h2", "soul"):        MoodContribution({"emotionality": .72}, {"emotionality": .55}),
    ("h2", "satire"):      MoodContribution({"complexity": .70, "darkness": .55}, {"complexity": .55, "darkness": .35}),
    # emotion-ветка
    ("e1", "romance"):    MoodContribution({"emotionality": .88, "darkness": .25}, {"emotionality": .85, "darkness": .40}),
    ("e1", "family"):     MoodContribution({"emotionality": .82, "darkness": .22}, {"emotionality": .80, "darkness": .45}),
    ("e1", "friendship"): MoodContribution({"emotionality": .78, "darkness": .22}, {"emotionality": .75, "darkness": .40}),
    ("e1", "dream"):      MoodContribution({"emotionality": .80, "energy": .60}, {"emotionality": .70, "energy": .35}),
    ("e1", "loss"):       MoodContribution({"emotionality": .92, "darkness": .72}, {"emotionality": .90, "darkness": .70}),
    ("e2", "positive"):   MoodContribution({"darkness": .18}, {"darkness": .85}),
    ("e2", "bittersweet"): MoodContribution({"darkness": .50, "emotionality": .82}, {"darkness": .55, "emotionality": .60}),
    ("e2", "tragic"):     MoodContribution({"darkness": .82, "emotionality": .92}, {"darkness": .85, "emotionality": .75}),
    ("e3", "characters"): MoodContribution({"emotionality": .82, "pace": .35}, {"emotionality": .60, "pace": .40}),
    ("e3", "plot"):       MoodContribution({"pace": .70, "complexity": .60}, {"pace": .50, "complexity": .40}),
    # tension-ветка: специализированные продолжения. Раньше все 15 вариантов
    # t2_* не влияли ни на что — человек выбирал «психологическая игра против
    # всех» и получал ровно ту же выдачу, что и при любом другом ответе.
    ("t2_psychological", "hero"):     MoodContribution({"complexity": .88, "realism": .35}, {"complexity": .80, "realism": .45}),
    ("t2_psychological", "close"):    MoodContribution({"emotionality": .78, "tension": .78}, {"emotionality": .70, "tension": .60}),
    ("t2_psychological", "everyone"): MoodContribution({"tension": .90, "darkness": .72}, {"tension": .85, "darkness": .55}),
    ("t2_psychological", "eyes"):     MoodContribution({"realism": .15, "complexity": .92}, {"realism": .75, "complexity": .85}),
    ("t2_mystery", "crime"):       MoodContribution({"complexity": .78, "realism": .78, "darkness": .65}, {"complexity": .70, "realism": .50, "darkness": .45}),
    ("t2_mystery", "missing"):     MoodContribution({"tension": .82, "emotionality": .70}, {"tension": .70, "emotionality": .50}),
    ("t2_mystery", "past"):        MoodContribution({"emotionality": .78, "pace": .30}, {"emotionality": .60, "pace": .55}),
    ("t2_mystery", "unexplained"): MoodContribution({"realism": .18, "tension": .78}, {"realism": .75, "tension": .60}),
    ("t2_survival", "people"):  MoodContribution({"tension": .88, "darkness": .72, "realism": .72}, {"tension": .80, "darkness": .55, "realism": .40}),
    ("t2_survival", "nature"):  MoodContribution({"energy": .82, "tension": .88, "realism": .78}, {"energy": .65, "tension": .80, "realism": .50}),
    ("t2_survival", "system"):  MoodContribution({"complexity": .78, "darkness": .75, "realism": .40}, {"complexity": .70, "darkness": .60, "realism": .40}),
    ("t2_survival", "self"):    MoodContribution({"complexity": .78, "emotionality": .82, "darkness": .68}, {"complexity": .65, "emotionality": .70, "darkness": .45}),
    ("t2_horror", "unknown"): MoodContribution({"realism": .22, "tension": .90}, {"realism": .70, "tension": .85}),
    ("t2_horror", "mind"):    MoodContribution({"complexity": .82, "darkness": .82, "tension": .88}, {"complexity": .75, "darkness": .65, "tension": .80}),
    ("t2_horror", "threat"):  MoodContribution({"energy": .85, "pace": .80, "realism": .75}, {"energy": .70, "pace": .65, "realism": .45}),
    # humor-ветка: h3 переписан с неизмеримых архетипов героя на темп.
    ("h3", "calm"):   MoodContribution({"pace": .25, "energy": .30}, {"pace": .80, "energy": .55}),
    ("h3", "steady"): MoodContribution({"pace": .52}, {"pace": .40}),
    ("h3", "wild"):   MoodContribution({"pace": .88, "energy": .88}, {"pace": .85, "energy": .70}),
    # unusual-ветка
    ("u1", "visual"):     MoodContribution({"realism": .20, "pace": .40}, {"realism": .60, "pace": .30}),
    ("u1", "idea"):       MoodContribution({"complexity": .88}, {"complexity": .85}),
    ("u1", "telling"):    MoodContribution({"complexity": .82, "pace": .40}, {"complexity": .75, "pace": .30}),
    ("u1", "characters"): MoodContribution({"complexity": .70, "realism": .28, "humor": .60}, {"complexity": .60, "realism": .55, "humor": .35}),
    ("u1", "reality"):    MoodContribution({"realism": .15, "complexity": .78}, {"realism": .75, "complexity": .60}),
    ("u2", "low"):        MoodContribution({"complexity": .45}, {"complexity": .45}),
    ("u2", "medium"):     MoodContribution({"complexity": .65}, {"complexity": .45}),
    ("u2", "high"):       MoodContribution({"complexity": .95, "pace": .35}, {"complexity": .90, "pace": .25}),
    ("u3", "dream"):      MoodContribution({"realism": .12, "pace": .35}, {"realism": .80, "pace": .35}),
    ("u3", "nightmare"):  MoodContribution({"realism": .18, "darkness": .82, "tension": .78}, {"realism": .60, "darkness": .75, "tension": .70}),
    ("u3", "puzzle"):     MoodContribution({"complexity": .92}, {"complexity": .85}),
    ("u3", "adventure"):  MoodContribution({"energy": .75, "pace": .70}, {"energy": .60, "pace": .55}),
}

# Явная длительность — жёсткое ограничение, а не пожелание по настроению.
DURATION_CONSTRAINTS: dict[str, dict[str, object]] = {
    "short": {"runtime_max": 100},
    "two_hours": {"runtime_max": 130},
    "long": {"runtime_min": 130},
    "any": {},
}


def build_mood_intent(answers: dict) -> MoodIntent:
    """Сводим ответы в одно намерение: цели усредняются по важности, поэтому
    ни один ответ не перекрывает остальные целиком."""
    weighted: dict[str, float] = {dim: 0.0 for dim in MOOD_DIMENSIONS}
    importance_sum: dict[str, float] = {dim: 0.0 for dim in MOOD_DIMENSIONS}
    hard: dict[str, object] = {}

    for question_id, answer in (answers or {}).items():
        values = answer if isinstance(answer, list) else [answer]
        for answer_id in values:
            if question_id == "c1":
                hard.update(DURATION_CONSTRAINTS.get(str(answer_id), {}))
                continue
            contribution = ANSWER_CONTRIBUTIONS.get((str(question_id), str(answer_id)))
            if contribution is None:
                continue      # неизвестные ответы игнорируем, а не угадываем
            for dimension, target in contribution.targets.items():
                weight = float(contribution.importance.get(dimension, 0.0))
                if weight <= 0:
                    continue
                weighted[dimension] += float(target) * weight
                importance_sum[dimension] += weight

    targets: dict[str, float] = {}
    importance: dict[str, float] = {}
    for dimension in MOOD_DIMENSIONS:
        total = importance_sum[dimension]
        if total <= 0:
            continue      # про это измерение человек ничего не сказал
        targets[dimension] = clamp01(weighted[dimension] / total)
        importance[dimension] = clamp01(total)

    intent = MoodIntent(targets=targets, importance=importance, hard_constraints=hard)
    intent.validate()
    return intent


# ── Двухэтапный отбор: сначала уместность, потом ранжирование ────────────────
# Корень прежней проблемы: сильный базовый счёт (популярность + качество) мог
# перекрыть плохое совпадение с запросом. Блокбастер с humor=0.43 выигрывал
# запрос «чёрный юмор», потому что агрегат настроения (0.71) прятал провал в
# главном измерении. Теперь настроение решает, КТО вообще уместен, и лишь потом
# базовый движок расставляет уместных по качеству и вкусам.
MOOD_ROLE_THRESHOLDS: dict[str, float] = {
    "best": 0.72,
    "reliable": 0.62,
    "unexpected": 0.68,
}
PRIMARY_INTENT_FLOORS: dict[str, float] = {
    "best": 0.72,
    "reliable": 0.62,
    "unexpected": 0.67,
}
MINIMUM_MOOD_SAFETY_FLOOR = 0.55
# Уместность по настроению — не повод рекомендовать слабое кино. Пороги взяты
# по реальному распределению каталога (медиана 7.18, p25 6.83): без них отбор
# вытаскивал нишевые фильмы с качеством около 5, потому что «подходящих по
# настроению» среди сильных оказывалось меньше.
MOOD_QUALITY_FLOORS: dict[str, float] = {
    "best": 6.6,
    "reliable": 6.8,      # «надёжный» обязан быть крепким
    "unexpected": 6.2,    # «неожиданный» — мягче, но не мусор
}
MINIMUM_QUALITY_SAFETY_FLOOR = 5.5
FALLBACK_THRESHOLD_ADJUSTMENTS = {0: 0.0, 1: -0.03, 2: -0.03, 3: -0.07, 4: -0.10}

# Явные ответы с однозначным смыслом получают прямые требования к измерению.
# Только там, где ответ действительно этого требует: у «сбалансированно» или
# «неважно» никаких полов быть не должно.
DIMENSION_FLOORS: dict[tuple[str, str], dict[str, float]] = {
    ("h1", "dark"):        {"humor_min": 0.55, "darkness_min": 0.45},
    ("h1", "situational"): {"humor_min": 0.55},
    ("h1", "absurd"):      {"humor_min": 0.55},
    ("h1", "sarcastic"):   {"humor_min": 0.50},
    ("h1", "warm"):        {"humor_min": 0.45, "darkness_max": 0.55},
    ("q1", "humor"):       {"humor_min": 0.50},
    ("u2", "high"):        {"complexity_min": 0.70},
    ("u1", "idea"):        {"complexity_min": 0.62},
    ("u3", "puzzle"):      {"complexity_min": 0.65},
    ("t1", "horror"):      {"tension_min": 0.72},
    ("t1", "psychological"): {"tension_min": 0.60, "complexity_min": 0.55},
    ("r3", "comfort"):     {"tension_max": 0.50, "darkness_max": 0.50},
    ("r1", "warm"):        {"darkness_max": 0.55},
    ("e2", "tragic"):      {"emotionality_min": 0.60},
    ("u1", "reality"):     {"realism_max": 0.55},
    ("u3", "dream"):       {"realism_max": 0.60},
}


# ── Составные требования: одного измерения мало ──────────────────────────────
# «Чёрный юмор» — не «есть юмор» и не «есть мрачность» по отдельности, а именно
# их сочетание с подтверждением. Иначе в выдачу проходит любая комедия с
# криминальной линией: боевик «К-9: Собачья работа» формально имел и юмор, и
# преступников, но чёрной комедией не является. Поэтому кроме порогов по
# измерениям нужно ПРОВЕРЕННОЕ доказательство: либо прямой маркер чёрной
# комедии/трагикомедии в описании, либо сатира вместе с действительно мрачным
# тоном. Ни жанр «комедия», ни абсурд, ни мультипликация доказательством не
# считаются.
@dataclass(frozen=True)
class CompoundRequirement:
    """Требование, которое нельзя проверить одним измерением."""
    code: str                                   # публичный код причины
    floors: dict[str, float]                    # обязательные пороги измерений
    primary_markers: tuple[str, ...]            # проверенный маркер — сильное доказательство
    support_markers: tuple[str, ...] = ()       # слабее: засчитывается только с доп. порогами
    support_floors: dict[str, float] = field(default_factory=dict)


COMPOUND_REQUIREMENTS: dict[tuple[str, str], CompoundRequirement] = {
    ("h1", "dark"): CompoundRequirement(
        code="DARK_HUMOR",
        # peak_darkness, а не darkness: усреднённый тон чёрной комедии всегда
        # низкий из-за самого жанра «комедия», и порог по нему отсекал бы как
        # раз то, что искали.
        floors={"humor_min": 0.55, "peak_darkness_min": 0.50},
        primary_markers=("dark_comedy",),
        support_markers=("satirical",),
        support_floors={"peak_darkness_min": 0.72},
    ),
}

EVIDENCE_STRONG, EVIDENCE_SUPPORTED, EVIDENCE_NONE = 2, 1, 0


def collect_compound_requirements(answers: dict) -> tuple[CompoundRequirement, ...]:
    found: list[CompoundRequirement] = []
    for question_id, answer in (answers or {}).items():
        values = answer if isinstance(answer, list) else [answer]
        for answer_id in values:
            requirement = COMPOUND_REQUIREMENTS.get((str(question_id), str(answer_id)))
            if requirement is not None and requirement not in found:
                found.append(requirement)
    return tuple(found)


def compound_evidence(requirement: CompoundRequirement, features: MovieMoodFeatures) -> int:
    """0 — доказательств нет, 1 — косвенное, 2 — прямой проверенный маркер."""
    if not passes_dimension_floors(features, requirement.floors):
        return EVIDENCE_NONE
    if any(marker in features.markers for marker in requirement.primary_markers):
        return EVIDENCE_STRONG
    if any(marker in features.markers for marker in requirement.support_markers) \
            and passes_dimension_floors(features, requirement.support_floors):
        return EVIDENCE_SUPPORTED
    return EVIDENCE_NONE


def passes_compound_requirements(features: MovieMoodFeatures,
                                 requirements: tuple[CompoundRequirement, ...]) -> bool:
    """Составные требования не ослабляются на fallback-уровнях: это ровно то,
    что человек назвал прямым текстом."""
    return all(compound_evidence(req, features) > EVIDENCE_NONE for req in requirements)


def compound_evidence_strength(features: MovieMoodFeatures,
                               requirements: tuple[CompoundRequirement, ...]) -> int:
    """Сила самого слабого из выполненных требований — по ней «лучшее
    совпадение» выбирает подтверждённый фильм, а не просто более рейтинговый."""
    if not requirements:
        return EVIDENCE_NONE
    return min(compound_evidence(req, features) for req in requirements)


def collect_dimension_floors(answers: dict) -> dict[str, float]:
    floors: dict[str, float] = {}
    for question_id, answer in (answers or {}).items():
        values = answer if isinstance(answer, list) else [answer]
        for answer_id in values:
            for key, value in DIMENSION_FLOORS.get((str(question_id), str(answer_id)), {}).items():
                # Из нескольких требований берём самое строгое.
                if key.endswith("_min"):
                    floors[key] = max(floors.get(key, 0.0), value)
                else:
                    floors[key] = min(floors.get(key, 1.0), value)
    return floors


def passes_dimension_floors(features: MovieMoodFeatures, floors: dict[str, float]) -> bool:
    for key, limit in floors.items():
        dimension, _, bound = key.rpartition("_")
        value = features.get(dimension)
        if bound == "min" and value < limit:
            return False
        if bound == "max" and value > limit:
            return False
    return True


def calculate_intent_specificity(intent: MoodIntent) -> float:
    """Насколько конкретен запрос: у «максимальное напряжение» строгость выше,
    чем у «сбалансированно, неважно»."""
    meaningful = [value for value in intent.importance.values() if value > 0]
    if not meaningful:
        return 0.0
    average = sum(meaningful) / len(meaningful)
    coverage = min(len(meaningful) / 5.0, 1.0)
    return clamp01(0.65 * average + 0.35 * coverage)


def resolve_mood_threshold(role: str, intent: MoodIntent, *, fallback_level: int = 0) -> float:
    base = MOOD_ROLE_THRESHOLDS.get(role, 0.68)
    adjustment = (calculate_intent_specificity(intent) - 0.5) * 0.08
    relaxed = FALLBACK_THRESHOLD_ADJUSTMENTS.get(fallback_level, 0.0)
    return max(MINIMUM_MOOD_SAFETY_FLOOR, min(0.82, base + adjustment + relaxed))


def primary_intent_dimensions(intent: MoodIntent, *, limit: int = 3) -> list[str]:
    return [dim for dim, _ in sorted(intent.importance.items(), key=lambda kv: -kv[1])[:limit]]


def primary_intent_match(intent: MoodIntent, features: MovieMoodFeatures) -> float:
    """Отдельная проверка ГЛАВНЫХ измерений: агрегат может спрятать провал в
    том единственном, ради чего человек и пришёл."""
    dimensions = primary_intent_dimensions(intent)
    if not dimensions:
        return 0.5
    total = 0.0
    weighted = 0.0
    for dimension in dimensions:
        importance = float(intent.importance.get(dimension, 0.0))
        if importance <= 0:
            continue
        similarity = 1.0 - abs(float(intent.targets[dimension]) - features.get(dimension))
        weighted += similarity * importance
        total += importance
    return clamp01(weighted / total) if total > 0 else 0.5


def find_critical_contradictions(intent: MoodIntent, features: MovieMoodFeatures,
                                 *, importance_floor: float = 0.75,
                                 distance_floor: float = 0.55) -> list[tuple[str, float]]:
    """Грубое расхождение в важном измерении: «максимум напряжения» не должен
    возвращать спокойную семейную комедию, даже если среднее приемлемо."""
    found = []
    for dimension, target in intent.targets.items():
        importance = float(intent.importance.get(dimension, 0.0))
        if importance < importance_floor:
            continue
        distance = abs(float(target) - features.get(dimension))
        if distance >= distance_floor:
            found.append((dimension, round(distance, 3)))
    return found
