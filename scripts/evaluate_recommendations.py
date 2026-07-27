"""Измеримая база подбора: одни и те же сценарии на V1 и V2.

Зачем: «стало лучше» без чисел — это мнение. Здесь фиксируется базовая линия V1,
и после подключения V2 та же прогонка показывает конкретную разницу, а не общее
впечатление.

Данные пользователей НЕ меняются: скрипт работает на копии каталога в памяти,
ничего не пишет в recommendation_history и не трогает чужие сессии.

    python scripts/evaluate_recommendations.py catalog.json
    python scripts/evaluate_recommendations.py catalog.json --engine v2
    python scripts/evaluate_recommendations.py catalog.json --diff
    python scripts/evaluate_recommendations.py catalog.json --scenario dark_humor
"""
import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import media_type
import mood
import recommendations as rec
from recommendation.intent import build_intent
from recommendation_questions import answer_state, next_question_id

# Фиксированные профили: холодный старт, сложившийся вкус и пара. Сравнивать
# движки можно только на одинаковых входах.
PROFILES = {
    "cold_start":  {"genres": {}, "people": {}, "count": 0},
    "established": {"genres": {"драма": 6.0, "триллер": 4.0, "ужасы": -5.0},
                    "people": {"дэвид финчер": 5.0}, "count": 60},
    "pair":        {"genres": {"комедия": 5.0, "ужасы": -6.0}, "people": {}, "count": 40},
}

_COMMON = {"c1": "any", "c2": "any", "c3": "safe", "c4": "solo"}


def _scenario(name, answers, *, profile="cold_start", expect=()):
    return {"name": name, "answers": {**answers, **_COMMON}, "profile": profile,
            "expect": expect}


# 30 полных сценариев: все корневые ветки, все специализированные продолжения
# напряжения и все три профиля.
SCENARIOS = [
    _scenario("relax_warm", {"q1": "relax", "r1": "warm", "r2": "slow", "r3": "comfort"},
              expect=("cozy",)),
    _scenario("relax_adventure", {"q1": "relax", "r1": "adventure", "r2": "fast", "r3": "positive"}),
    _scenario("relax_visual", {"q1": "relax", "r1": "visual", "r2": "slow", "r3": "positive"}),
    _scenario("relax_simple", {"q1": "relax", "r1": "simple", "r2": "medium", "r3": "positive"}),
    _scenario("relax_drama", {"q1": "relax", "r1": "warm", "r2": "medium", "r3": "drama"}),
    _scenario("tension_survival_people", {"q1": "tension", "t1": "survival",
                                          "t2_survival": "people", "t4": "clear"},
              expect=("tension",)),
    _scenario("tension_survival_nature", {"q1": "tension", "t1": "survival",
                                          "t2_survival": "nature", "t4": "semi"},
              expect=("tension",)),
    _scenario("tension_survival_system", {"q1": "tension", "t1": "survival",
                                          "t2_survival": "system", "t4": "open"}),
    _scenario("tension_survival_self", {"q1": "tension", "t1": "survival",
                                        "t2_survival": "self", "t4": "open"}),
    _scenario("tension_psych_hero", {"q1": "tension", "t1": "psychological",
                                     "t2_psychological": "hero", "t4": "open"},
              expect=("complexity",)),
    _scenario("tension_psych_close", {"q1": "tension", "t1": "psychological",
                                      "t2_psychological": "close", "t4": "semi"}),
    _scenario("tension_psych_everyone", {"q1": "tension", "t1": "psychological",
                                         "t2_psychological": "everyone", "t4": "clear"},
              expect=("tension",)),
    _scenario("tension_psych_eyes", {"q1": "tension", "t1": "psychological",
                                     "t2_psychological": "eyes", "t4": "open"},
              expect=("complexity",)),
    _scenario("tension_mystery_crime", {"q1": "tension", "t1": "mystery",
                                        "t2_mystery": "crime", "t4": "clear"}),
    _scenario("tension_mystery_missing", {"q1": "tension", "t1": "mystery",
                                          "t2_mystery": "missing", "t4": "semi"}),
    _scenario("tension_mystery_past", {"q1": "tension", "t1": "mystery",
                                       "t2_mystery": "past", "t4": "semi"}),
    _scenario("tension_mystery_unexplained", {"q1": "tension", "t1": "mystery",
                                              "t2_mystery": "unexplained", "t4": "open"}),
    _scenario("tension_horror_unknown", {"q1": "tension", "t1": "horror",
                                         "t2_horror": "unknown", "t4": "open"},
              expect=("tension",)),
    _scenario("tension_horror_mind", {"q1": "tension", "t1": "horror",
                                      "t2_horror": "mind", "t4": "open"},
              expect=("tension", "complexity")),
    _scenario("tension_horror_threat", {"q1": "tension", "t1": "horror",
                                        "t2_horror": "threat", "t4": "clear"},
              expect=("tension",)),
    _scenario("humor_dark", {"q1": "humor", "h1": "dark", "h2": "satire", "h3": "wild"},
              expect=("dark_humor",)),
    _scenario("humor_situational", {"q1": "humor", "h1": "situational", "h2": "none",
                                    "h3": "steady"}, expect=("humor",)),
    _scenario("humor_sarcastic", {"q1": "humor", "h1": "sarcastic", "h2": "satire",
                                  "h3": "calm"}, expect=("humor",)),
    _scenario("humor_absurd", {"q1": "humor", "h1": "absurd", "h2": "none", "h3": "wild"},
              expect=("humor",)),
    _scenario("humor_warm", {"q1": "humor", "h1": "warm", "h2": "soul", "h3": "calm"},
              expect=("humor",)),
    _scenario("emotion_romance", {"q1": "emotion", "e1": "romance", "e2": "positive",
                                  "e3": "characters"}, expect=("positive_ending",)),
    _scenario("emotion_loss", {"q1": "emotion", "e1": "loss", "e2": "tragic",
                               "e3": "characters"}),
    _scenario("unusual_idea", {"q1": "unusual", "u1": "idea", "u2": "high", "u3": "puzzle"},
              expect=("complexity",)),
    _scenario("unusual_dream", {"q1": "unusual", "u1": "visual", "u2": "medium",
                                "u3": "dream"}),
    _scenario("established_dark_humor", {"q1": "humor", "h1": "dark", "h2": "satire",
                                         "h3": "wild"}, profile="established",
              expect=("dark_humor",)),
    _scenario("pair_relax", {"q1": "relax", "r1": "warm", "r2": "medium", "r3": "comfort"},
              profile="pair", expect=("cozy",)),
]

# Проверяем СВОЙСТВА, а не названия: список фильмов будет меняться, а «уютное не
# бывает мрачным» обязано выполняться всегда.
PROPERTY_CHECKS = {
    "cozy":            lambda f: f.get("darkness") <= 0.55 and f.get("tension") <= 0.55,
    "tension":         lambda f: f.get("tension") >= 0.55,
    "complexity":      lambda f: f.get("complexity") >= 0.50,
    "humor":           lambda f: f.get("humor") >= 0.50,
    "dark_humor":      lambda f: "dark_comedy" in f.markers or "satirical" in f.markers,
    "positive_ending": lambda f: f.get("darkness") <= 0.65,
}


def _validate_answers(answers: dict) -> None:
    """Сценарий обязан быть проходимым по реальному графу анкеты."""
    walked, state = [], {}
    while (question_id := next_question_id(state)) is not None:
        if question_id not in answers:
            raise SystemExit(f"сценарий не покрывает вопрос {question_id}: {answers}")
        state[question_id] = answers[question_id]
        walked.append(question_id)
    extra = set(answers) - set(walked)
    if extra:
        raise SystemExit(f"лишние ответы вне маршрута: {sorted(extra)}")


def _rank(films, answers, preferences):
    weights, filters, context = answer_state(answers)
    pool = [f for f in films if media_type.is_movie_flow_eligible(f)]
    pool = [f for f in pool if rec._matches_filters(f, filters)] or pool
    ranked = [rec.score_film(f, weights, preferences, risk=str(context.get("risk") or "medium"))
              for f in pool]
    return sorted(ranked, key=lambda m: -m["_score"])


def evaluate(films, scenario, engine):
    answers, preferences = scenario["answers"], PROFILES[scenario["profile"]]
    ranked = rec.apply_mood_layer(_rank(films, answers, preferences), answers,
                                  preferences_count=preferences["count"])
    mood_intent = mood.build_mood_intent(answers)
    floors = mood.collect_dimension_floors(answers)
    requirements = mood.collect_compound_requirements(answers)
    canonical = build_intent(answers)

    eligible = rec.mood_eligible(ranked, mood_intent, floors, "best", requirements=requirements)
    picks, levels = [], []
    for role, key, weights in (("best", "_b", (.58, .42, 0.0)),
                               ("reliable", "_r", (.38, .62, 0.0)),
                               ("unexpected", "_u", (.50, .22, .28))):
        scored = [{**m, key: weights[0] * m["_mood"] + weights[1] * rec.normalized_base_score(m)
                   + weights[2] * mood.clamp01(m["_novelty"] / 5.0)} for m in ranked]
        pick, level = rec.select_mood_role(scored, mood_intent, floors, role, picks, key,
                                           requirements=requirements,
                                           evidence_first=(role == "best"))
        if pick:
            picks.append(pick)
            levels.append(level)

    failures, violations, property_failures = [], [], []
    for movie in picks:
        features = movie["_mood_features"]
        profile_tones = set(features.markers)
        failures += [name for name in canonical.required_features
                     if name not in profile_tones and name not in _genres_of(movie)]
        violations += [name for name in canonical.forbidden_features if name in profile_tones]
        for expectation in scenario["expect"]:
            if not PROPERTY_CHECKS[expectation](features):
                property_failures.append(f"{expectation}:{movie.get('title')}")

    genres = {g for movie in picks for g in _genres_of(movie)}
    return {
        "scenario": scenario["name"], "engine": engine, "profile": scenario["profile"],
        "eligible_count": len(eligible), "result_count": len(picks),
        "partial_result": 0 < len(picks) < 3, "no_result": not picks,
        "average_quality": round(statistics.mean([m["_quality"] for m in picks]), 3) if picks else 0.0,
        "primary_intent_match": round(statistics.mean(
            [mood.primary_intent_match(mood_intent, m["_mood_features"]) for m in picks]), 3)
            if picks else 0.0,
        "critical_contradictions": sum(
            len(mood.find_critical_contradictions(mood_intent, m["_mood_features"])) for m in picks),
        "required_feature_failures": len(failures),
        "forbidden_feature_violations": len(violations),
        "property_failures": property_failures,
        "role_overlap": len(picks) - len({m["id"] for m in picks}),
        "genre_diversity": len(genres),
        "fallback_level": max(levels) if levels else 0,
        "titles": [str(m.get("title"))[:34] for m in picks],
    }


def _genres_of(movie) -> set[str]:
    from enrichment.mappings import canonical_genres
    canonical, _ = canonical_genres(str(movie.get("genres") or "").split(","))
    return set(canonical)


def summarize(rows):
    picked = [row for row in rows if row["result_count"]]
    return {
        "scenarios": len(rows),
        "no_result": sum(1 for row in rows if row["no_result"]),
        "partial_result": sum(1 for row in rows if row["partial_result"]),
        "average_quality": round(statistics.mean([r["average_quality"] for r in picked]), 3) if picked else 0.0,
        "primary_intent_match": round(statistics.mean([r["primary_intent_match"] for r in picked]), 3) if picked else 0.0,
        "critical_contradictions": sum(r["critical_contradictions"] for r in rows),
        "required_feature_failures": sum(r["required_feature_failures"] for r in rows),
        "forbidden_feature_violations": sum(r["forbidden_feature_violations"] for r in rows),
        "property_failures": sum(len(r["property_failures"]) for r in rows),
        "role_overlap": sum(r["role_overlap"] for r in rows),
        "fallbacks_used": sum(1 for r in rows if r["fallback_level"] > 0),
        "average_genre_diversity": round(statistics.mean([r["genre_diversity"] for r in picked]), 2) if picked else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog")
    parser.add_argument("--engine", default="v1", choices=("v1", "v2"))
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--diff", action="store_true", help="сравнить V1 и V2")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    with open(args.catalog, encoding="utf-8") as handle:
        films = json.load(handle)
    scenarios = [s for s in SCENARIOS if not args.scenario or s["name"] == args.scenario]
    for scenario in scenarios:
        _validate_answers(scenario["answers"])

    engines_to_run = ("v1", "v2") if args.diff else (args.engine,)
    results = {engine: [evaluate(films, scenario, engine) for scenario in scenarios]
               for engine in engines_to_run}

    if args.json:
        print(json.dumps({engine: {"rows": rows, "summary": summarize(rows)}
                          for engine, rows in results.items()}, ensure_ascii=False, indent=2))
        return

    for engine, rows in results.items():
        print(f"\n=== движок {engine}: {len(rows)} сценариев, каталог {len(films)}")
        for row in rows:
            flag = "ПУСТО" if row["no_result"] else ("частично" if row["partial_result"] else "")
            print(f"  {row['scenario']:28} {row['profile']:12} "
                  f"уместных {row['eligible_count']:3} → {row['result_count']} {flag}")
            if row["property_failures"]:
                print(f"      НАРУШЕНИЯ СВОЙСТВ: {row['property_failures']}")
            if row["forbidden_feature_violations"]:
                print(f"      запрещённые признаки: {row['forbidden_feature_violations']}")
            print(f"      {', '.join(row['titles']) or '—'}")
        print("  итого:", json.dumps(summarize(rows), ensure_ascii=False))

    if args.diff:
        first, second = summarize(results["v1"]), summarize(results["v2"])
        print("\n=== разница V2 − V1")
        for key in first:
            if isinstance(first[key], (int, float)) and first[key] != second[key]:
                print(f"  {key}: {first[key]} → {second[key]}")
        changed = [(a["scenario"], a["titles"], b["titles"])
                   for a, b in zip(results["v1"], results["v2"], strict=True)
                   if a["titles"] != b["titles"]]
        print(f"  сценариев с другой выдачей: {len(changed)}/{len(results['v1'])}")
        for name, before, after in changed[:10]:
            print(f"    {name}: {before} → {after}")


if __name__ == "__main__":
    main()
