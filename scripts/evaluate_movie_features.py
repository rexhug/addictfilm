"""Ручной обзор профилей: смотреть надо на НАЗВАНИЯ, а не на «тесты зелёные».

Никакой unit-тест не подтвердит, что «Марс атакует!» — чёрная комедия, а
«К-9: Собачья работа» — нет. Этот отчёт печатает то, по чему такое решение
принимает человек.

    python scripts/evaluate_movie_features.py --sample 50
    python scripts/evaluate_movie_features.py --tone dark_comedy
    python scripts/evaluate_movie_features.py --warnings
"""
import argparse
import asyncio
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import aiosqlite  # noqa: E402
import database as db  # noqa: E402
import db_runtime  # noqa: E402
from config import DATABASE_URL  # noqa: E402
from enrichment import repository  # noqa: E402

# Категории для представительной выборки: по одной строке на каждую, чтобы
# обзор покрывал не только то, чего в каталоге больше всего.
REVIEW_BUCKETS: dict[str, dict] = {
    "чёрная комедия":       {"tone": "dark_comedy"},
    "сатира":               {"tone": "satirical"},
    "уютное":               {"tone": "cozy"},
    "заставляет думать":    {"tone": "thought_provoking"},
    "психологическое":      {"tone": "psychological"},
    "медленное":            {"tone": "slow_burn"},
    "комедия":              {"genre": "comedy"},
    "семейная анимация":    {"genre": "animation", "audience": "family_friendly"},
    "взрослая анимация":    {"audience": "adult_animation"},
    "драма":                {"genre": "drama"},
    "психологический триллер": {"genre": "thriller"},
    "ужасы":                {"genre": "horror"},
    "фантастика":           {"genre": "science_fiction"},
    "фэнтези":              {"genre": "fantasy"},
    "документальное":       {"genre": "documentary"},
    "криминал":             {"genre": "crime"},
}


async def _rows() -> list[dict]:
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT f.id, f.title, f.genres, f.runtime FROM films f ORDER BY f.id")).fetchall()
    films = {row["id"]: dict(row) for row in rows}
    profiles = await repository.get_profiles(list(films))
    return [{**films[film_id], "profile": profile} for film_id, profile in profiles.items()]


def _matches(item: dict, rule: dict) -> bool:
    profile = item["profile"]
    if "tone" in rule and rule["tone"] not in profile["tones"]:
        return False
    if "genre" in rule and rule["genre"] not in profile["genres"]:
        return False
    if "audience" in rule and rule["audience"] not in profile["audience_flags"]:
        return False
    return True


def _print(item: dict) -> None:
    profile = item["profile"]
    mood = profile["mood"]
    print(f"  {str(item['title'])[:40]:40} | {profile['content_type']:7} | "
          f"уверенность {profile['confidence']:.2f} | {profile['status']}")
    print(f"     жанры провайдера: {str(item['genres'])[:64]}")
    print(f"     канон: {','.join(profile['genres']) or '—'}")
    print(f"     темы:  {','.join(profile['themes']) or '—'}")
    print(f"     тон:   {','.join(profile['tones']) or '—'}   аудитория: "
          f"{','.join(profile['audience_flags']) or '—'}")
    print(f"     настроение: юмор {mood['humor']:.2f} мрак {mood['darkness']:.2f} "
          f"(пик {mood['peak_darkness']:.2f}) напряж {mood['tension']:.2f} "
          f"сложн {mood['complexity']:.2f} эмоц {mood['emotionality']:.2f} "
          f"темп {mood['pace']:.2f} реализм {mood['realism']:.2f}")
    if profile["warnings"]:
        print(f"     предупреждения: {','.join(profile['warnings'])}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=0, help="по N на категорию")
    parser.add_argument("--tone", default=None)
    parser.add_argument("--genre", default=None)
    parser.add_argument("--warnings", action="store_true")
    parser.add_argument("--low-confidence", action="store_true")
    args = parser.parse_args()

    await db.init_db()
    items = await _rows()
    confidences = sorted(item["profile"]["confidence"] for item in items)

    if args.tone or args.genre:
        rule = {k: v for k, v in (("tone", args.tone), ("genre", args.genre)) if v}
        selected = [item for item in items if _matches(item, rule)]
        print(f"=== {rule}: {len(selected)}")
        for item in selected:
            _print(item)
    elif args.warnings:
        selected = [item for item in items if item["profile"]["warnings"]]
        print(f"=== с предупреждениями: {len(selected)}")
        for item in selected:
            _print(item)
    elif args.low_confidence:
        selected = [item for item in items if item["profile"]["status"] == "low_confidence"]
        print(f"=== низкая уверенность: {len(selected)}")
        for item in selected[:40]:
            _print(item)
    else:
        per_bucket = max(1, args.sample // max(1, len(REVIEW_BUCKETS))) if args.sample else 3
        for name, rule in REVIEW_BUCKETS.items():
            selected = [item for item in items if _matches(item, rule)]
            print(f"\n=== {name}: всего {len(selected)}")
            for item in selected[:per_bucket]:
                _print(item)

    distribution = await repository.distribution()
    print(f"\nкаталог: {distribution['catalog']} | профили: {distribution['by_status']}")
    print(f"типы записей: {distribution['by_content_type']}")
    if confidences:
        print(f"уверенность: сред {statistics.mean(confidences):.3f} "
              f"p25 {confidences[len(confidences)//4]:.3f} "
              f"медиана {statistics.median(confidences):.3f} "
              f"p75 {confidences[3*len(confidences)//4]:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
