"""Прогон сценариев подбора по РЕАЛЬНОМУ каталогу — без сети и без БД.

Выгрузка каталога (scripts/export_catalog.py или ssh-выгрузка) подаётся файлом,
дальше повторяется ровно тот же путь, что в проде: базовый счёт → слой
настроения → двухэтапный отбор ролей. Нужен, чтобы смотреть на НАЗВАНИЯ, а не
на «тесты зелёные»: семантику рекомендации никакой unit-тест не подтвердит.

    python scripts/review_mood_scenarios.py catalog.json [--scenario dark_humor]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import media_type  # noqa: E402
import mood  # noqa: E402
import recommendations as rec  # noqa: E402
from recommendation_questions import answer_state  # noqa: E402

SCENARIOS: dict[str, dict[str, str]] = {
    "dark_humor":    {"q1": "humor", "h1": "dark", "h2": "satire", "h3": "cynical", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "situational":   {"q1": "humor", "h1": "situational", "h2": "none", "h3": "loser", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "warm_humor":    {"q1": "humor", "h1": "warm", "h2": "soul", "h3": "friends", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "cozy":          {"q1": "relax", "r1": "warm", "r2": "slow", "r3": "comfort", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "max_tension":   {"q1": "tension", "t1": "horror", "t2_horror": "unknown", "t4": "open", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "psychological": {"q1": "tension", "t1": "psychological", "t2_psychological": "hero", "t4": "open", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "intellectual":  {"q1": "unusual", "u1": "idea", "u2": "high", "u3": "puzzle", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "sci_fi":        {"q1": "unusual", "u1": "visual", "u2": "medium", "u3": "dream", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "tragic":        {"q1": "emotion", "e1": "loss", "e2": "tragic", "e3": "characters", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
    "romance":       {"q1": "emotion", "e1": "romance", "e2": "positive", "e3": "characters", "c1": "any", "c2": "any", "c3": "safe", "c4": "solo"},
}


def _rank(films: list[dict], answers: dict) -> list[dict]:
    weights, filters, _ = answer_state(answers)
    pool = [f for f in films if media_type.is_movie_flow_eligible(f)]
    pool = [f for f in pool if rec._matches_filters(f, filters)] or pool
    preferences = {"genres": {}, "people": {}, "count": 0}
    ranked = [rec.score_film(f, weights, preferences) for f in pool]
    return sorted(ranked, key=lambda m: -m["_score"])


def run(films: list[dict], name: str, answers: dict) -> dict:
    ranked = rec.apply_mood_layer(_rank(films, answers), answers, preferences_count=0)
    intent = mood.build_mood_intent(answers)
    floors = mood.collect_dimension_floors(answers)
    requirements = mood.collect_compound_requirements(answers)
    picks, levels = [], []
    for role, key, weights in (("best", "_best", (0.58, 0.42, 0.0)),
                               ("reliable", "_reliable", (0.38, 0.62, 0.0)),
                               ("unexpected", "_unexpected", (0.50, 0.22, 0.28))):
        scored = [{**m, key: weights[0] * m["_mood"] + weights[1] * (m["_score"] / rec._MAX_BASE_SCORE)
                   + weights[2] * (m["_novelty"] / 5.0)} for m in ranked]
        pick, level = rec.select_mood_role(scored, intent, floors, role, picks, key,
                                           requirements=requirements,
                                           evidence_first=(role == "best"))
        if pick:
            picks.append(pick)
            levels.append(level)
    eligible = rec.mood_eligible(ranked, intent, floors, "best", requirements=requirements)
    return {"name": name, "picks": picks, "levels": levels, "eligible": len(eligible),
            "requirements": requirements, "intent": intent}


def report(result: dict) -> None:
    print(f"\n=== {result['name']}  (уместных на строгом уровне: {result['eligible']})")
    if not result["picks"]:
        print("    НИ ОДНОГО подходящего фильма — честнее показать пусто, чем случайный")
    for movie, level in zip(result["picks"], result["levels"]):
        features = movie["_mood_features"]
        strength = mood.compound_evidence_strength(features, result["requirements"]) \
            if result["requirements"] else "—"
        codes = rec.build_reasons(movie, intent=result["intent"], requirements=result["requirements"])
        print(f"    {str(movie.get('title'))[:38]:38} | mood {movie['_mood']:.2f} "
              f"| качество {movie['_quality']:.2f} | fallback {level} | доказательство {strength}")
        print(f"       {str(movie.get('genres'))[:60]:60} | {','.join(codes)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog")
    parser.add_argument("--scenario", default=None)
    args = parser.parse_args()
    films = json.load(open(args.catalog, encoding="utf-8"))
    names = [args.scenario] if args.scenario else list(SCENARIOS)
    results = [run(films, name, SCENARIOS[name]) for name in names]
    for result in results:
        report(result)
    complete = sum(1 for r in results if len(r["picks"]) == 3)
    picked = [m for r in results for m in r["picks"]]
    quality = sum(m["_quality"] for m in picked) / len(picked) if picked else 0.0
    print(f"\nполных троек: {complete}/{len(results)} | среднее качество отобранных: {quality:.2f}")


if __name__ == "__main__":
    main()
