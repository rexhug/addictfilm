"""Рулетка по списку «Хочу посмотреть»: равномерно и без повторов внутри круга.

Прежняя реализация выбирала равномерно, но без памяти — один и тот же фильм мог
выпасть подряд несколько раз, и для человека это выглядит как поломка.
"""
import asyncio
import collections
import tempfile
import unittest
from pathlib import Path

import database as db


class WishlistRouletteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "w.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})
        await db.upsert_user({"id": 2, "first_name": "Two", "username": "two"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _add(self, user_id: int, imdb_id: str) -> int:
        film_id = await db.get_or_create_film(imdb_id=imdb_id, title=f"Фильм {imdb_id}",
                                              genres="драма", media_type="movie",
                                              poster_url="https://example.test/p.jpg")
        await db.add_to_list(user_id, film_id, "want_to_watch", None)
        return film_id

    async def _draw(self, user_id: int, times: int) -> list[int]:
        picks = []
        for _ in range(times):
            movie = await db.pick_random_wishlist_film(user_id)
            picks.append(movie["id"] if movie else None)
        return picks

    async def test_empty_wishlist_returns_nothing(self):
        self.assertIsNone(await db.pick_random_wishlist_film(1))

    async def test_single_film_always_returns_that_film(self):
        film_id = await self._add(1, "tt1")
        self.assertEqual(await self._draw(1, 4), [film_id] * 4)

    async def test_two_films_alternate_and_never_repeat_inside_a_cycle(self):
        first, second = await self._add(1, "tt1"), await self._add(1, "tt2")
        picks = await self._draw(1, 6)
        for start in range(0, 6, 2):
            with self.subTest(cycle=start // 2):
                self.assertEqual(set(picks[start:start + 2]), {first, second})

    async def test_no_repeat_until_the_whole_list_was_shown(self):
        ids = {await self._add(1, f"tt{index}") for index in range(6)}
        picks = await self._draw(1, 6)
        self.assertEqual(set(picks), ids)
        self.assertEqual(len(set(picks)), 6, "фильм повторился до конца круга")

    async def test_new_cycle_starts_after_exhaustion(self):
        ids = {await self._add(1, f"tt{index}") for index in range(3)}
        await self._draw(1, 3)
        self.assertEqual((await db.wishlist_roulette_state(1))["cycle"], 1)
        second_cycle = await self._draw(1, 3)
        self.assertEqual(set(second_cycle), ids)
        self.assertEqual((await db.wishlist_roulette_state(1))["cycle"], 2)

    async def test_film_added_mid_cycle_becomes_reachable_without_a_reset(self):
        """Новый фильм входит в ТЕКУЩИЙ круг, а не ждёт следующего."""
        first, second = await self._add(1, "tt1"), await self._add(1, "tt2")
        shown_first = (await db.pick_random_wishlist_film(1))["id"]
        third = await self._add(1, "tt3")
        rest = set(await self._draw(1, 2))
        self.assertEqual(rest, {first, second, third} - {shown_first})
        self.assertIn(third, rest)

    async def test_watched_film_leaves_the_roulette(self):
        keep = await self._add(1, "tt1")
        watched = await self._add(1, "tt2")
        await db.set_status(1, watched, "watched")
        self.assertEqual(await self._draw(1, 3), [keep] * 3)

    async def test_users_have_independent_cycles(self):
        mine = await self._add(1, "tt1")
        theirs = await self._add(2, "tt2")
        self.assertEqual((await db.pick_random_wishlist_film(1))["id"], mine)
        self.assertEqual((await db.pick_random_wishlist_film(2))["id"], theirs)

    async def test_concurrent_draws_do_not_return_the_same_film(self):
        """Два запроса подряд не должны выдать один фильм, пока есть выбор."""
        ids = {await self._add(1, f"tt{index}") for index in range(4)}
        results = await asyncio.gather(*[db.pick_random_wishlist_film(1) for _ in range(4)])
        picked = [movie["id"] for movie in results if movie]
        self.assertEqual(len(picked), 4)
        self.assertEqual(set(picked), ids)
        self.assertEqual(len(set(picked)), 4)

    async def test_distribution_is_approximately_uniform_over_many_cycles(self):
        ids = [await self._add(1, f"tt{index}") for index in range(4)]
        counts = collections.Counter(await self._draw(1, 4 * 40))
        for film_id in ids:
            with self.subTest(film=film_id):
                # Полные круги => ровно по 40 показов на фильм.
                self.assertEqual(counts[film_id], 40)

    async def test_history_is_not_deleted_when_a_cycle_restarts(self):
        await self._add(1, "tt1")
        await self._draw(1, 3)
        import db_runtime
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT COUNT(*) FROM wishlist_random_picks WHERE user_id=1")).fetchone()
        self.assertEqual(int(row[0]), 3)      # по одной записи на каждый круг

    async def test_prepare_is_read_only_until_consumed(self):
        film_id = await self._add(1, "tt1")
        prepared = await db.prepare_random_wishlist_film(1)
        self.assertEqual(prepared["item"]["id"], film_id)
        self.assertEqual(prepared["cycle_number"], 1)
        state = await db.wishlist_roulette_state(1)
        self.assertEqual(state["shown_in_cycle"], 0)

        consumed = await db.consume_prepared_wishlist_film(
            1, film_id, prepared["cycle_number"])
        self.assertEqual(consumed["id"], film_id)
        state = await db.wishlist_roulette_state(1)
        self.assertEqual(state["shown_in_cycle"], 1)

    async def test_abandoned_prepare_never_changes_cycle_or_history(self):
        await self._add(1, "tt1")
        first = await db.prepare_random_wishlist_film(1)
        second = await db.prepare_random_wishlist_film(1)
        self.assertEqual(first["cycle_number"], 1)
        self.assertEqual(second["cycle_number"], 1)
        state = await db.wishlist_roulette_state(1)
        self.assertEqual(state["cycle"], 1)
        self.assertEqual(state["shown_in_cycle"], 0)

    async def test_prepared_next_cycle_is_rejected_when_list_changes(self):
        first = await self._add(1, "tt1")
        prepared = await db.prepare_random_wishlist_film(1)
        self.assertIsNotNone(await db.consume_prepared_wishlist_film(
            1, first, prepared["cycle_number"]))
        next_cycle = await db.prepare_random_wishlist_film(1)
        self.assertEqual(next_cycle["cycle_number"], 2)

        # A newly added film belongs to the still-current cycle.  Consuming the
        # old next-cycle token would skip it, so the server must reject it.
        await self._add(1, "tt2")
        self.assertIsNone(await db.consume_prepared_wishlist_film(
            1, next_cycle["item"]["id"], next_cycle["cycle_number"]))
        self.assertEqual((await db.wishlist_roulette_state(1))["cycle"], 1)

    async def test_prepared_pick_can_only_be_consumed_once(self):
        film_id = await self._add(1, "tt1")
        prepared = await db.prepare_random_wishlist_film(1)
        results = await asyncio.gather(*[
            db.consume_prepared_wishlist_film(
                1, film_id, prepared["cycle_number"]) for _ in range(2)
        ])
        self.assertEqual(sum(result is not None for result in results), 1)

    async def test_legacy_helper_still_works(self):
        film_id = await self._add(1, "tt1")
        movie = await db.get_random_want(1)
        self.assertEqual(movie["id"], film_id)


if __name__ == "__main__":
    unittest.main()
