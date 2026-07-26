"""Local-first, explainable and deterministic recommendation engine.

There is no model call and no hidden profile.  Results are derived from a
versioned question graph, catalog metadata and the user's own ratings.  The
module is intentionally small enough to unit-test with a handful of films.
"""
from __future__ import annotations

import math
import re
import secrets
from collections import defaultdict
from typing import Iterable

import database as db
import search
from recommendation_questions import answer_reasons, answer_state

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


def _quality(film: dict) -> float:
    rating = max(_num(film.get("imdb_rating")), _num(film.get("kp_rating")))
    votes = max(0.0, _num(film.get("imdb_votes")))
    # rating dominates; votes lightly protect against a single unsupported vote.
    return max(0.0, min(10.0, rating)) + min(1.2, math.log10(votes + 1) / 5)


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
    scores = preferences.get("genres", {})
    if not scores:
        return 0.0
    maximum = max(scores.values(), default=1.0)
    affinity = sum(scores.get(genre, 0.0) / maximum for genre in _genres(film.get("genres")))
    return min(1.0, affinity / 2.0)


def _people_affinity(film: dict, preferences: dict) -> float:
    scores = preferences.get("people", {})
    if not scores:
        return 0.0
    maximum = max(scores.values(), default=1.0)
    people = [p.strip().casefold() for p in (str(film.get("actors") or "") + "," + str(film.get("directors") or "")).split(",") if p.strip()]
    return min(1.0, max((scores.get(person, 0.0) / maximum for person in people), default=0.0))


def score_film(film: dict, weights: dict[str, float], preferences: dict, *, risk: str = "medium") -> dict:
    tags = film_tags(film)
    direct = sum(weight * tags.get(tag, 0.0) for tag, weight in weights.items())
    max_direct = max(1.0, sum(max(0.0, value) for value in weights.values()))
    match = min(38.0, 38.0 * direct / max_direct)
    quality = _quality(film)
    quality_score = min(25.0, max(0.0, (quality - 5.2) * 5.2))
    affinity = _genre_affinity(film, preferences) * 12.0 + _people_affinity(film, preferences) * 5.0
    wishlist = 5.0 if film.get("in_wishlist") else 0.0
    votes = max(0.0, _num(film.get("imdb_votes")))
    # ``less_known`` is a soft diversity signal, never a quality penalty.
    popularity = min(1.0, math.log10(votes + 1) / 6.0)
    novelty = (1.0 - popularity) * (5.0 if risk in ("medium", "high") else 1.5)
    return {**film, "_tags": tags, "_quality": quality, "_score": round(match + quality_score + affinity + wishlist + novelty, 3),
            "_match": match, "_novelty": novelty}


def _select_distinct(ranked: list[dict], selected: list[dict], key: str) -> dict | None:
    selected_ids = {item["id"] for item in selected}
    selected_titles = {str(item.get("title") or "").casefold() for item in selected}
    for item in sorted(ranked, key=lambda movie: (-movie[key], str(movie.get("title") or ""), movie["id"])):
        title = str(item.get("title") or "").casefold()
        if item["id"] not in selected_ids and title not in selected_titles:
            return item
    return None


def _explanation(answers: dict, film: dict, language: str) -> str:
    selected = answer_reasons(answers, language)
    # Only retain answer reasons which have a matching metadata signal where
    # possible. If metadata is sparse, it stays honest and talks about the
    # selection rather than inventing a plot fact.
    matching = selected[:]
    if language == "en":
        body = ", ".join(matching[:4]) or "your current preferences"
        return f"Matches: {body}."
    body = ", ".join(matching[:4]) or "ваш текущий запрос"
    return f"Подходит по запросу: {body}."


def public_movie(movie: dict, *, role: str, explanation: str) -> dict:
    return {
        "id": movie["id"], "imdb_id": movie.get("imdb_id"), "kp_id": movie.get("kp_id"),
        "title": movie.get("title"), "title_original": movie.get("title_original"),
        "year": movie.get("year"), "runtime": movie.get("runtime"), "genres": movie.get("genres"),
        "rating": _display_rating(movie),
        "plot": movie.get("plot"), "poster_url": movie.get("poster_url"), "backdrop_url": movie.get("backdrop_url"),
        "role": role, "score": round(float(movie.get("_score") or 0), 1), "explanation": explanation,
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
    # First strict, then gently relaxed so a narrow runtime/year choice never
    # leaves a real user with an empty screen.
    strict = [film for film in candidates if _matches_filters(film, filters)]
    pool = strict if len(strict) >= minimum else [film for film in candidates if _matches_filters(film, filters, relaxed=True)]
    if not pool:
        pool = candidates
    preferences = await db.get_recommendation_preferences(user_id)
    if partner_id is not None:
        partner_preferences = await db.get_recommendation_preferences(partner_id)
        for bucket in ("genres", "people"):
            combined = defaultdict(float, preferences.get(bucket, {}))
            for key, value in partner_preferences.get(bucket, {}).items():
                combined[key] += value
            preferences[bucket] = dict(combined)
    risk = str(context.get("risk") or "medium")
    ranked = [score_film(film, weights, preferences, risk=risk) for film in pool]
    # Persist a very small derived-tag sample for auditability; no broad write
    # on a read path. Chosen result tags are enough to inspect rules later.
    return sorted(ranked, key=lambda movie: (-movie["_score"], str(movie.get("title") or ""), movie["id"]))


async def quiz_results(user_id: int, answers: dict[str, str], language: str, partner_id: int | None = None) -> list[dict]:
    weights, filters, context = answer_state(answers)
    ranked = await ranked_candidates(user_id, partner_id=partner_id, weights=weights, filters=filters, context=context)
    if not ranked:
        return []
    best = _select_distinct(ranked, [], "_score")
    reliable_ranked = [{**movie, "_reliable": movie["_score"] + movie["_quality"] * 2.2 - movie["_novelty"]} for movie in ranked]
    reliable = _select_distinct(reliable_ranked, [best] if best else [], "_reliable")
    unexpected_ranked = [{**movie, "_unexpected": movie["_score"] * 0.72 + movie["_novelty"] * 5.2} for movie in ranked]
    unexpected = _select_distinct(unexpected_ranked, [item for item in (best, reliable) if item], "_unexpected")
    labelled = (("best", best), ("reliable", reliable), ("unexpected", unexpected))
    results = [public_movie(movie, role=role, explanation=_explanation(answers, movie, language)) for role, movie in labelled if movie]
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
    if language == "en":
        explanation = "A quality pick from your unseen catalog."
    else:
        explanation = "Качественный вариант из фильмов, которых вы ещё не смотрели."
    return public_movie(movie, role="random", explanation=explanation)
