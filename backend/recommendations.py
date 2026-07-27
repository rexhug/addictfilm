"""Local-first, explainable and deterministic recommendation engine.

There is no model call and no hidden profile.  Results are derived from a
versioned question graph, catalog metadata and the user's own ratings.  The
module is intentionally small enough to unit-test with a handful of films.
"""
from __future__ import annotations

import math
import os
import re
import secrets

import database as db
import media_type
import mood
import reasons as reason_codes
import search
from recommendation_questions import answer_state

TAG_VERSION = "v1"

# Values represent weak, explainable signals.  They are never used as absolute
# assertions (for example a Drama may be emotional, but it is not guaranteed).
GENRE_TAGS: dict[str, tuple[str, ...]] = {
    "драма": ("drama", "emotion", "character_driven"),
    "комедия": ("comedy", "humor", "light_humor"),
    "триллер": ("thriller", "tension", "mystery"),
    "боевик": ("action", "fast_pace", "tension"),
    "криминал": ("crime", "mystery", "tension"),
    "детектив": ("mystery", "detective", "crime"),
    "ужасы": ("horror", "tension", "supernatural"),
    "фантастика": ("fantasy", "high_concept", "visuals"),
    "фэнтези": ("fantasy", "visuals", "adventure"),
    "приключения": ("adventure", "fast_pace"),
    "мелодрама": ("romance", "relationships", "emotion"),
    "семейный": ("family", "warm", "comfort"),
    "биография": ("inspiration", "character_driven"),
    "история": ("plot_driven", "drama"),
    "документальный": ("realism", "plot_driven"),
    "мультфильм": ("warm", "comfort", "family"),
    "аниме": ("visuals", "fantasy", "originality"),
    "военный": ("war", "tension", "drama"),
}
PLOT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "mystery": ("тайн", "загад", "mystery", "detective"),
    "psychological": ("психолог", "psycholog", "mind"),
    "survival": ("выжив", "surviv", "катастроф", "disaster"),
    "family": ("семь", "family"),
    "friendship": ("друж", "friendship", "friends"),
    "romance": ("любов", "роман", "romance", "relationship"),
    "crime": ("преступ", "crime", "убий", "murder"),
    "supernatural": ("сверхъест", "призрак", "supernatural", "ghost"),
    "dystopia": ("антиутоп", "dystop"),
    "conspiracy": ("заговор", "conspiracy"),
    "dreamlike": ("сон", "dream"),
    "puzzle": ("головолом", "puzzle"),
}


def _num(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return default


def _int(value: object) -> int | None:
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group()) if match else None


def _runtime(value: object) -> int | None:
    match = re.search(r"\d{2,3}", str(value or ""))
    return int(match.group()) if match else None


def _genres(value: object) -> list[str]:
    return [part.strip().casefold() for part in str(value or "").split(",") if part.strip()]


def film_tags(film: dict) -> dict[str, float]:
    tags: dict[str, float] = {}
    for genre in _genres(film.get("genres")):
        # The catalog stores both RU and EN genres; database normalizes browse
        # aliases, but the recommendation layer remains self-contained.
        canonical = {"drama": "драма", "comedy": "комедия", "thriller": "триллер", "action": "боевик", "crime": "криминал", "mystery": "детектив", "detective": "детектив", "horror": "ужасы", "sci-fi": "фантастика", "science fiction": "фантастика", "fantasy": "фэнтези", "adventure": "приключения", "romance": "мелодрама", "family": "семейный", "animation": "мультфильм", "biography": "биография", "history": "история", "documentary": "документальный", "war": "военный"}.get(genre, genre)
        tags[canonical] = max(tags.get(canonical, 0.0), 1.0)
        for tag in GENRE_TAGS.get(canonical, ()):
            tags[tag] = max(tags.get(tag, 0.0), 0.72)
    plot = str(film.get("plot") or "").casefold()
    for tag, words in PLOT_KEYWORDS.items():
        if any(word in plot for word in words):
            tags[tag] = max(tags.get(tag, 0.0), 0.55)
    return tags


# Оценка достоверна ровно настолько, насколько велика выборка: 10.0 по двум
# голосам — это не «лучший фильм каталога». Байесовское сглаживание тянет такие
# оценки к среднему по каталогу, пока голосов мало, и почти не трогает фильмы с
# тысячами голосов.
# Константы откалиброваны по реальному каталогу (288 фильмов): средний рейтинг
# 6.96, нижний квартиль голосов ~1100. Не «универсальные» числа из головы:
# слишком большой порог доверия сплющил бы весь каталог к среднему.
_GLOBAL_MEAN_RATING = 6.96     # среднее по фильмам с оценкой
_CONFIDENCE_VOTES = 1100       # p25 голосов: столько нужно для половинного доверия
# Часть каталога просто не имеет заполненного поля голосов. Это «нет данных», а
# не «никто не голосовал», поэтому такие фильмы сглаживаем умеренно, а не топим.
_MISSING_VOTES_ASSUMPTION = 900


def bayesian_rating(rating: float, votes: float, *, global_mean: float = _GLOBAL_MEAN_RATING,
                    confidence_votes: int = _CONFIDENCE_VOTES) -> float:
    votes = max(0.0, votes)
    minimum = max(1, confidence_votes)
    return (votes / (votes + minimum)) * rating + (minimum / (votes + minimum)) * global_mean


def _quality(film: dict) -> float:
    """0…10, но с поправкой на доверие к выборке голосов."""
    rating = max(_num(film.get("imdb_rating")), _num(film.get("kp_rating")))
    raw_votes = max(0.0, _num(film.get("imdb_votes")))
    if rating <= 0:
        return 0.0
    votes = raw_votes if raw_votes > 0 else _MISSING_VOTES_ASSUMPTION
    adjusted = bayesian_rating(max(0.0, min(10.0, rating)), votes)
    # Небольшая надбавка за подтверждённость выборки — но она уже не может
    # вытащить наверх фильм с двумя голосами.
    return adjusted + min(0.6, math.log10(raw_votes + 1) / 10)


def _display_rating(film: dict) -> float | None:
    """Return a provider rating for UI, never the internal quality score.

    ``_quality`` deliberately includes a small votes adjustment for ranking
    stability. It must not leak into the product UI, where a user reasonably
    expects the displayed number to be the actual IMDb/Kinopoisk rating.
    """
    rating = max(_num(film.get("imdb_rating")), _num(film.get("kp_rating")))
    return round(rating, 1) if rating > 0 else None


def _matches_filters(film: dict, filters: dict, *, relaxed: bool = False) -> bool:
    runtime = _runtime(film.get("runtime"))
    year = _int(film.get("year"))
    runtime_max = filters.get("runtime_max")
    if runtime_max and runtime and runtime > int(runtime_max) + (25 if relaxed else 0):
        return False
    if filters.get("year_min") and year and year < int(filters["year_min"]) - (4 if relaxed else 0):
        return False
    if filters.get("year_max") and year and year > int(filters["year_max"]) + (4 if relaxed else 0):
        return False
    return True


def _genre_affinity(film: dict, preferences: dict) -> float:
    """-1…1. Отрицательный результат — жанр, который пользователь стабильно
    оценивает низко; такой фильм должен опускаться, а не просто «не расти»."""
    scores = preferences.get("genres", {})
    if not scores:
        return 0.0
    scale = max((abs(value) for value in scores.values()), default=1.0) or 1.0
    genres = _genres(film.get("genres"))
    if not genres:
        return 0.0
    affinity = sum(scores.get(genre, 0.0) / scale for genre in genres)
    return max(-1.0, min(1.0, affinity / 2.0))


def _people_affinity(film: dict, preferences: dict) -> float:
    """-1…1 по самому выраженному знакомому имени в титрах."""
    scores = preferences.get("people", {})
    if not scores:
        return 0.0
    scale = max((abs(value) for value in scores.values()), default=1.0) or 1.0
    people = [p.strip().casefold() for p in (str(film.get("actors") or "") + "," + str(film.get("directors") or "")).split(",") if p.strip()]
    known = [scores.get(person, 0.0) / scale for person in people if person in scores]
    if not known:
        return 0.0
    return max(-1.0, min(1.0, max(known, key=abs)))


def profile_confidence(preferences: dict) -> float:
    """0…1 — насколько мы вправе опираться на личные вкусы. У нового человека
    доверие низкое, и вес должен уходить в качество и запрос, а не в выдуманный
    «твой любимый жанр»."""
    return max(0.0, min(1.0, float(preferences.get("count", 0) or 0) / 25.0))


def pair_fairness(a: float, b: float) -> float:
    """Фильм, который один обожает, а второй терпеть не может, — плохой выбор
    для пары. Минимум важнее среднего, поэтому он весит больше."""
    return 0.55 * min(a, b) + 0.45 * ((a + b) / 2)


def score_film(film: dict, weights: dict[str, float], preferences: dict, *, risk: str = "medium") -> dict:
    """Разбор оценки на понятные слагаемые: запрос, качество, личные вкусы.

    Веса зависят от того, сколько мы про человека знаем: у новичка личный вклад
    почти не работает (и не должен — выдумывать «твой любимый жанр» не на чем),
    поэтому его доля уходит в качество и соответствие запросу.
    """
    tags = film_tags(film)
    confidence = profile_confidence(preferences)
    direct = sum(weight * tags.get(tag, 0.0) for tag, weight in weights.items())
    max_direct = max(1.0, sum(max(0.0, value) for value in weights.values()))
    # Чем меньше знаем о вкусах — тем больше решает сам запрос и качество.
    match = min(38.0, (38.0 + 8.0 * (1.0 - confidence)) * direct / max_direct)
    quality = _quality(film)
    quality_score = min(25.0 + 6.0 * (1.0 - confidence), max(0.0, (quality - 5.2) * 5.2))
    genre_affinity = _genre_affinity(film, preferences)
    people_affinity = _people_affinity(film, preferences)
    # Отрицательная афинность теперь реально опускает фильм, а не обнуляется.
    affinity = (genre_affinity * 12.0 + people_affinity * 5.0) * confidence
    wishlist = 5.0 if film.get("in_wishlist") else 0.0
    votes = max(0.0, _num(film.get("imdb_votes")))
    # ``less_known`` is a soft diversity signal, never a quality penalty.
    popularity = min(1.0, math.log10(votes + 1) / 6.0)
    novelty = (1.0 - popularity) * (5.0 if risk in ("medium", "high") else 1.5)
    return {**film, "_tags": tags, "_quality": quality,
            "_score": round(match + quality_score + affinity + wishlist + novelty, 3),
            "_match": match, "_novelty": novelty, "_affinity": affinity,
            "_confidence": confidence, "_quality_score": quality_score}


def film_similarity(a: dict, b: dict) -> float:
    """0…1 по пересечению жанров (Жаккар) плюс надбавка за общего режиссёра.
    Нужна, чтобы «три варианта» не оказались тремя почти одинаковыми фильмами."""
    genres_a, genres_b = set(_genres(a.get("genres"))), set(_genres(b.get("genres")))
    jaccard = len(genres_a & genres_b) / len(genres_a | genres_b) if (genres_a | genres_b) else 0.0
    directors_a = {p.strip().casefold() for p in str(a.get("directors") or "").split(",") if p.strip()}
    directors_b = {p.strip().casefold() for p in str(b.get("directors") or "").split(",") if p.strip()}
    same_director = 0.35 if directors_a & directors_b else 0.0
    return min(1.0, jaccard + same_director)


def _select_distinct(ranked: list[dict], selected: list[dict], key: str,
                     *, relevance: float = 0.78) -> dict | None:
    """Maximum Marginal Relevance: берём сильного кандидата, но штрафуем за
    похожесть на уже выбранные — иначе «неожиданный вариант» оказывается
    очередным триллером того же режиссёра."""
    selected_ids = {item["id"] for item in selected}
    selected_titles = {str(item.get("title") or "").casefold() for item in selected}
    pool = [item for item in ranked
            if item["id"] not in selected_ids
            and str(item.get("title") or "").casefold() not in selected_titles]
    if not pool:
        return None
    if not selected:
        return max(pool, key=lambda movie: (movie[key], -movie["id"]))
    scale = max((abs(item[key]) for item in pool), default=1.0) or 1.0
    def mmr(movie: dict) -> float:
        penalty = max((film_similarity(movie, chosen) for chosen in selected), default=0.0)
        return relevance * (movie[key] / scale) - (1.0 - relevance) * penalty
    return max(pool, key=lambda movie: (mmr(movie), -movie["id"]))


# Измерение → код причины и направление, в котором оно должно быть выражено.
# Причина ставится, только если человек САМ назвал это измерение главным и фильм
# действительно попадает в нужную сторону — иначе она ничего не доказывает.
_DIMENSION_REASONS: dict[str, tuple[str, str, float]] = {
    "tension":      (reason_codes.HIGH_TENSION, "high", 0.66),
    "complexity":   (reason_codes.INTELLECTUAL, "high", 0.66),
    "emotionality": (reason_codes.EMOTIONAL_STORY, "high", 0.66),
    "humor":        (reason_codes.LIGHT_HUMOR, "high", 0.66),
    "pace":         (reason_codes.FAST_PACE, "high", 0.68),
    "realism":      (reason_codes.GROUNDED_STORY, "high", 0.70),
}
_LOW_DIMENSION_REASONS: dict[str, str] = {
    "pace": reason_codes.SLOW_ATMOSPHERE,
    "realism": reason_codes.UNREAL_WORLD,
}
_HIGH_QUALITY_REASON_FLOOR = 7.4
_HIDDEN_GEM_VOTES = 40000


def _mood_reason_codes(movie: dict, intent, requirements) -> list[str]:
    """Доказательства именно для ЭТОГО фильма, а не пересказ анкеты."""
    features = movie.get("_mood_features")
    if features is None:
        return []
    codes: list[str] = []
    for requirement in requirements:
        strength = mood.compound_evidence(requirement, features)
        if strength <= mood.EVIDENCE_NONE or requirement.code != "DARK_HUMOR":
            continue
        if strength == mood.EVIDENCE_STRONG:
            codes.append(reason_codes.ABSURD_DARK_HUMOR if "satire" in features.markers
                         else reason_codes.DARK_COMEDY_TONE)
        else:
            codes.append(reason_codes.SATIRICAL_HUMOR)
    for dimension in mood.primary_intent_dimensions(intent):
        target = float(intent.targets.get(dimension, 0.5))
        value = features.get(dimension)
        mapping = _DIMENSION_REASONS.get(dimension)
        if mapping and target >= 0.6 and value >= mapping[2]:
            codes.append(mapping[0])
        elif dimension in _LOW_DIMENSION_REASONS and target <= 0.35 and value <= 0.35:
            codes.append(_LOW_DIMENSION_REASONS[dimension])
    # «Спокойный тон» — утверждение сразу про два измерения: боевик с невысокой
    # мрачностью спокойным не становится.
    if float(intent.targets.get("darkness", 1.0)) <= 0.4 \
            and features.get("darkness") <= 0.30 and features.get("tension") <= 0.40:
        codes.append(reason_codes.COZY_TONE)
    return codes


def build_reasons(movie: dict, *, intent=None, requirements=(), pair: bool = False,
                  mood_threshold: float = 0.7, fallback: str = reason_codes.MATCHES_MOOD) -> list[str]:
    """До трёх кодов: сначала смысл запроса, потом вкусы и качество."""
    codes = _mood_reason_codes(movie, intent, requirements) if intent is not None else []
    if not codes and intent is not None and float(movie.get("_mood") or 0) >= mood_threshold:
        codes.append(reason_codes.MATCHES_MOOD)
    if float(movie.get("_affinity") or 0) > 0:
        codes.append(reason_codes.MATCHES_USER_TASTE)
    if pair:
        codes.append(reason_codes.PAIR_FRIENDLY)
    if float(movie.get("_quality") or 0) >= _HIGH_QUALITY_REASON_FLOOR:
        codes.append(reason_codes.HIGH_QUALITY)
    if _num(movie.get("imdb_votes")) and _num(movie.get("imdb_votes")) < _HIDDEN_GEM_VOTES:
        codes.append(reason_codes.HIDDEN_GEM)
    if movie.get("in_wishlist"):
        codes.append(reason_codes.IN_WISHLIST)
    return reason_codes.sanitize(codes) or [fallback]


def public_movie(movie: dict, *, role: str, codes: list[str], language: str) -> dict:
    clean = reason_codes.sanitize(codes)
    return {
        "id": movie["id"], "imdb_id": movie.get("imdb_id"), "kp_id": movie.get("kp_id"),
        "title": movie.get("title"), "title_original": movie.get("title_original"),
        "year": movie.get("year"), "runtime": movie.get("runtime"), "genres": movie.get("genres"),
        "rating": _display_rating(movie),
        "plot": movie.get("plot"), "poster_url": movie.get("poster_url"), "backdrop_url": movie.get("backdrop_url"),
        "role": role, "score": round(float(movie.get("_score") or 0), 1),
        # reasons — источник правды для интерфейса; explanation остаётся строкой
        # для клиентов, которые ещё не умеют в коды, и собирается из тех же кодов.
        "reasons": clean, "explanation": reason_codes.describe(clean, language),
    }


def _discovery_query(weights: dict[str, float]) -> str:
    """Choose one conservative catalog-search seed for a cold local catalog.

    This is deliberately a *single* existing cached-search call, never a
    provider fan-out.  It simply warms the permanent catalog with a genre that
    best matches the current request; ``search.cached_search`` owns the quota,
    per-user throttling and the Kinopoisk → OMDb fallback chain.
    """
    seeds = (
        (("horror", "supernatural", "nightmare"), "ужасы"),
        (("mystery", "detective", "crime"), "детектив"),
        (("action", "survival", "fast_pace", "adventure"), "боевик"),
        (("humor", "comedy", "situational_humor", "dark_humor"), "комедия"),
        (("romance", "relationships", "family", "emotion"), "драма"),
        (("fantasy", "visuals", "originality", "high_concept", "surreal"), "фантастика"),
        (("tension", "psychological", "thriller"), "триллер"),
    )
    for tags, query in seeds:
        if any(float(weights.get(tag, 0)) > 0 for tag in tags):
            return query
    return "драма"


async def _warm_catalog_if_sparse(user_id: int, partner_id: int | None, weights: dict[str, float],
                                  candidates: list[dict], minimum: int) -> list[dict]:
    """Use the existing quota-protected discovery pipeline only when needed."""
    if len(candidates) >= minimum:
        return candidates
    try:
        # Ignore a provider/cache failure here: local recommendations must stay
        # available even during a third-party outage.
        await search.cached_search(_discovery_query(weights), user_id=user_id)
    except Exception:  # noqa: BLE001
        # Search itself logs provider detail.  A recommendation should never
        # fail solely because the optional catalog warmer could not run.
        pass
    return await db.get_recommendation_candidates(user_id, partner_id)


async def ranked_candidates(user_id: int, *, partner_id: int | None, weights: dict[str, float], filters: dict,
                            context: dict, minimum: int = 18) -> list[dict]:
    candidates = await db.get_recommendation_candidates(user_id, partner_id)
    candidates = await _warm_catalog_if_sparse(user_id, partner_id, weights, candidates, minimum)
    # Подбор ФИЛЬМОВ: сериалы, эпизоды и короткий метр здесь неуместны, каким бы
    # высоким ни был их рейтинг. Тип берётся из метаданных провайдера, а не из
    # длительности (22 минуты — это и мультсериал, и короткометражка).
    movies_only = [film for film in candidates if media_type.is_movie_flow_eligible(film)]
    # Пустой результат означал бы, что тип не проставлен вообще ни у чего, —
    # тогда лучше отдать прежний пул, чем показать человеку пустой экран.
    candidates = movies_only or candidates
    # First strict, then gently relaxed so a narrow runtime/year choice never
    # leaves a real user with an empty screen.
    strict = [film for film in candidates if _matches_filters(film, filters)]
    pool = strict if len(strict) >= minimum else [film for film in candidates if _matches_filters(film, filters, relaxed=True)]
    if not pool:
        pool = candidates
    preferences = await db.get_recommendation_preferences(user_id)
    risk = str(context.get("risk") or "medium")
    if partner_id is None:
        ranked = [score_film(film, weights, preferences, risk=risk) for film in pool]
    else:
        # Для пары считаем ДВА профиля отдельно и сводим их честно: раньше веса
        # просто складывались, и фильм, который один обожает, а второй не
        # выносит, всё равно всплывал наверх.
        partner_preferences = await db.get_recommendation_preferences(partner_id)
        mine = {film["id"]: score_film(film, weights, preferences, risk=risk) for film in pool}
        theirs = {film["id"]: score_film(film, weights, partner_preferences, risk=risk) for film in pool}
        ranked = []
        for film_id, scored in mine.items():
            other = theirs[film_id]
            fair = pair_fairness(scored["_score"], other["_score"])
            ranked.append({**scored, "_score": round(fair, 3),
                           "_pair_min": round(min(scored["_score"], other["_score"]), 3)})
    # Persist a very small derived-tag sample for auditability; no broad write
    # on a read path. Chosen result tags are enough to inspect rules later.
    return sorted(ranked, key=lambda movie: (-movie["_score"], str(movie.get("title") or ""), movie["id"]))


# Слой настроения выключён по умолчанию. Признаки фильмов прошли ручной обзор
# на реальном каталоге, но в обзоре сценариев блокбастеры с высоким базовым
# счётом всё ещё вытесняют более подходящие по настроению фильмы. Включаем
# только после подтверждения на живых сценариях: MOOD_LAYER_ENABLED=1.
MOOD_LAYER_ENABLED = os.getenv("MOOD_LAYER_ENABLED", "").strip().lower() in ("1", "true", "yes")
# Обкатка на админах до глобального включения. Признак админа приходит ТОЛЬКО с
# сервера (см. main._effective_role) — клиент на это влиять не может.
MOOD_LAYER_ADMIN_PREVIEW = os.getenv("MOOD_LAYER_ADMIN_PREVIEW", "").strip().lower() in ("1", "true", "yes")


def mood_layer_active(*, is_admin: bool = False) -> bool:
    return MOOD_LAYER_ENABLED or (MOOD_LAYER_ADMIN_PREVIEW and is_admin)

# Потолок базового счёта: match(38) + quality(25) + affinity(17) + wishlist(5)
# + novelty(5). Используется только для перевода в 0…1 и обратно.
_MAX_BASE_SCORE = 90.0


def apply_mood_layer(ranked: list[dict], answers: dict, preferences_count: int) -> list[dict]:
    """Переранжирование по близости к запросу настроения.

    Базовый _score (качество, вкусы, отрицательные сигналы, честность к паре)
    НЕ пересчитывается — он нормализуется и смешивается с настроением. Вес
    настроения зависит от того, сколько мы знаем о человеке: новичку важнее то,
    что он выбрал сейчас, «старожилу» — ещё и его история.
    """
    intent = mood.build_mood_intent(answers)
    if not intent.targets:
        return ranked          # анкета ничего не уточнила — базовый порядок
    confidence = mood.clamp01(preferences_count / 25.0)
    rescored = []
    for movie in ranked:
        features = mood.build_movie_features(movie)
        raw = mood.mood_similarity(intent, features)
        adjusted = mood.confidence_adjusted(raw, features.confidence)
        # Нормализуем по СТАБИЛЬНОЙ шкале, а не min-max по текущему пулу:
        # иначе разрыв в пару баллов между двумя кандидатами растягивался бы на
        # всю шкалу, и порядок менялся бы от состава выборки.
        base_normalized = mood.clamp01(movie["_score"] / _MAX_BASE_SCORE)
        combined = mood.combine_scores(base_score=base_normalized, mood_score=adjusted,
                                       profile_confidence=confidence)
        rescored.append({**movie, "_mood": round(adjusted, 4), "_mood_raw": round(raw, 4),
                         "_mood_features": features,
                         "_mood_confidence": features.confidence,
                         "_base_score": movie["_score"],
                         "_score": round(combined * _MAX_BASE_SCORE, 3)})
    return sorted(rescored, key=lambda m: (-m["_score"], str(m.get("title") or ""), m["id"]))


def mood_eligible(ranked: list[dict], intent, floors: dict, role: str,
                  *, fallback_level: int = 0, requirements: tuple = ()) -> list[dict]:
    """Кто ВООБЩЕ уместен для этого запроса. Высокий базовый счёт здесь не
    помогает: сначала уместность, потом уже качество и вкусы."""
    threshold = mood.resolve_mood_threshold(role, intent, fallback_level=fallback_level)
    primary_floor = mood.PRIMARY_INTENT_FLOORS.get(role, 0.65) + \
        mood.FALLBACK_THRESHOLD_ADJUSTMENTS.get(fallback_level, 0.0)
    eligible = []
    for movie in ranked:
        features = movie.get("_mood_features")
        if features is None:
            continue
        # Составные требования проверяем ПЕРВЫМИ и не ослабляем никогда: «чёрный
        # юмор» — это подтверждённое сочетание юмора и мрачности, а не «в фильме
        # есть комедия».
        if not mood.passes_compound_requirements(features, requirements):
            continue
        if movie.get("_mood", 0.0) < threshold:
            continue
        # Уместность не отменяет качества: иначе выдача сползает в нишевое кино
        # только потому, что сильные фильмы реже совпадают с узким запросом.
        quality_floor = max(mood.MINIMUM_QUALITY_SAFETY_FLOOR,
                            mood.MOOD_QUALITY_FLOORS.get(role, 6.5)
                            + mood.FALLBACK_THRESHOLD_ADJUSTMENTS.get(fallback_level, 0.0) * 8)
        if movie.get("_quality", 0.0) < quality_floor:
            continue
        if mood.primary_intent_match(intent, features) < primary_floor:
            continue
        # Явные требования («чёрный юмор» → юмор обязан быть) не ослабляются
        # никогда: это то, что человек сказал прямым текстом.
        if not mood.passes_dimension_floors(features, floors):
            continue
        contradictions = mood.find_critical_contradictions(intent, features)
        if contradictions and role != "unexpected":
            continue
        if len(contradictions) > 1:
            continue      # «неожиданный» терпит одно расхождение, но не два
        eligible.append(movie)
    return eligible


def select_mood_role(ranked: list[dict], intent, floors: dict, role: str,
                     selected: list[dict], key: str, *, requirements: tuple = (),
                     evidence_first: bool = False) -> tuple[dict | None, int]:
    """Подбираем роль, постепенно ослабляя ТОЛЬКО второстепенное. Требования по
    явным ответам и грубые противоречия не ослабляются на любом уровне.

    evidence_first — для «лучшего совпадения»: сначала берём фильмы с прямым
    подтверждением запроса и только потом косвенные, чтобы роль доставалась
    самому доказанному варианту, а не самому рейтинговому.
    """
    for level in sorted(mood.FALLBACK_THRESHOLD_ADJUSTMENTS):
        pool = mood_eligible(ranked, intent, floors, role, fallback_level=level,
                             requirements=requirements)
        tiers = [pool]
        if evidence_first and requirements:
            strong = [m for m in pool
                      if mood.compound_evidence_strength(m["_mood_features"], requirements)
                      == mood.EVIDENCE_STRONG]
            tiers = [strong, pool] if strong else [pool]
        for tier in tiers:
            pick = _select_distinct(tier, selected, key)
            if pick is not None:
                return pick, level
    return None, len(mood.FALLBACK_THRESHOLD_ADJUSTMENTS) - 1


async def quiz_results(user_id: int, answers: dict[str, str], language: str,
                       partner_id: int | None = None, *, is_admin: bool = False) -> list[dict]:
    weights, filters, context = answer_state(answers)
    ranked = await ranked_candidates(user_id, partner_id=partner_id, weights=weights, filters=filters, context=context)
    if not ranked:
        return []
    mood_on = mood_layer_active(is_admin=is_admin)
    if mood_on:
        preferences = await db.get_recommendation_preferences(user_id)
        ranked = apply_mood_layer(ranked, answers, int(preferences.get("count", 0) or 0))
    intent = None
    requirements: tuple = ()
    if mood_on and ranked and "_mood" in ranked[0]:
        # Двухэтапно: настроение решает, кто уместен; базовый движок — порядок.
        intent = mood.build_mood_intent(answers)
        floors = mood.collect_dimension_floors(answers)
        requirements = mood.collect_compound_requirements(answers)
        # У «лучшего совпадения» настроение весит больше базового счёта.
        best_ranked = [{**m, "_best": 0.58 * m["_mood"] + 0.42 * (m["_score"] / _MAX_BASE_SCORE)} for m in ranked]
        best, _ = select_mood_role(best_ranked, intent, floors, "best", [], "_best",
                                   requirements=requirements, evidence_first=True)
        # У «надёжного» — наоборот, но только среди уместных по настроению.
        reliable_ranked = [{**m, "_reliable": 0.38 * m["_mood"] + 0.62 * (m["_score"] / _MAX_BASE_SCORE)} for m in ranked]
        reliable, _ = select_mood_role(reliable_ranked, intent, floors, "reliable",
                                       [best] if best else [], "_reliable", requirements=requirements)
        unexpected_ranked = [{**m, "_unexpected": 0.50 * m["_mood"] + 0.28 * (m["_novelty"] / 5.0)
                              + 0.22 * (m["_score"] / _MAX_BASE_SCORE)} for m in ranked]
        unexpected, _ = select_mood_role(unexpected_ranked, intent, floors, "unexpected",
                                         [i for i in (best, reliable) if i], "_unexpected",
                                         requirements=requirements)
    else:
        best = _select_distinct(ranked, [], "_score")
        reliable_ranked = [{**movie, "_reliable": movie["_score"] + movie["_quality"] * 2.2 - movie["_novelty"]} for movie in ranked]
        reliable = _select_distinct(reliable_ranked, [best] if best else [], "_reliable")
        unexpected_ranked = [{**movie, "_unexpected": movie["_score"] * 0.72 + movie["_novelty"] * 5.2} for movie in ranked]
        unexpected = _select_distinct(unexpected_ranked, [item for item in (best, reliable) if item], "_unexpected")
    labelled = (("best", best), ("reliable", reliable), ("unexpected", unexpected))
    results = [public_movie(movie, role=role, language=language,
                            codes=build_reasons(movie, intent=intent, requirements=requirements,
                                                pair=partner_id is not None))
               for role, movie in labelled if movie]
    for movie in (best, reliable, unexpected):
        if movie:
            await db.save_recommendation_tags(movie["id"], movie["_tags"], version=TAG_VERSION)
    return results


async def random_recommendation(user_id: int, language: str, partner_id: int | None = None) -> dict | None:
    # A neutral but quality-conscious profile.  Random mode deliberately does
    # not infer a mood the user did not provide.
    ranked = await ranked_candidates(user_id, partner_id=partner_id, weights={}, filters={}, context={"risk": "medium"}, minimum=12)
    if not ranked:
        return None
    qualified = [movie for movie in ranked if _quality(movie) >= 6.5]
    pool = qualified or [movie for movie in ranked if _quality(movie) >= 6.0] or ranked
    # Bounded weighted lottery: quality matters, but the same three blockbusters
    # are not returned every time.  History exclusion is handled by DB filters.
    weights = [max(0.1, (_quality(movie) - 5.2) ** 2 + movie.get("_novelty", 0.0)) for movie in pool[:80]]
    movie = secrets.SystemRandom().choices(pool[:80], weights=weights, k=1)[0]
    await db.save_recommendation_tags(movie["id"], movie["_tags"], version=TAG_VERSION)
    # Случайный режим намеренно не выдумывает настроение, которого человек не
    # называл, — поэтому и причины здесь только фактические.
    codes = [reason_codes.UNSEEN_PICK] + build_reasons(
        movie, pair=partner_id is not None, fallback=reason_codes.HIGH_QUALITY)
    return public_movie(movie, role="random", codes=codes, language=language)
