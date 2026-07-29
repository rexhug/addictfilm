"""Отбор изображения: что имеет право называться горизонтальным кадром.

Смысл проверок — не «формула считает как написано», а продуктовое обещание:
широкий блок получает только доказанно широкую и крупную картинку, всё
остальное честно уходит в режим постера. Именно нарушение этого обещания и
выглядело на экране как поломка вёрстки.
"""
from __future__ import annotations

import unittest

import hero_media
from fanart import FanartImage


def image(width=1920, height=1080, likes=12, language="00", ident="a") -> FanartImage:
    return FanartImage(id=ident, url=f"https://assets.fanart.tv/{ident}.jpg",
                       language=language, likes=likes, width=width, height=height)


class QualificationTests(unittest.TestCase):
    def test_full_hd_textless_and_liked_qualifies(self):
        selection = hero_media.choose_fanart_background([image()])
        self.assertIsNotNone(selection)
        self.assertEqual(selection.hero_type, hero_media.HERO_BACKDROP)
        self.assertEqual(selection.source, hero_media.SOURCE_FANART)
        self.assertEqual((selection.width, selection.height), (1920, 1080))

    def test_small_image_is_rejected_however_popular(self):
        self.assertIsNone(hero_media.choose_fanart_background([image(640, 360, likes=9999)]))

    def test_vertical_image_is_rejected(self):
        self.assertIsNone(hero_media.choose_fanart_background([image(1200, 1800)]))

    def test_extremely_wide_panorama_is_rejected(self):
        # 3.2:1 обрезался бы до неузнаваемости в блоке 16:10.
        self.assertIsNone(hero_media.choose_fanart_background([image(3840, 1200)]))

    def test_high_resolution_with_unknown_language_can_still_qualify(self):
        selection = hero_media.choose_fanart_background([image(3840, 2160, likes=25, language="de")])
        self.assertIsNotNone(selection)

    def test_low_total_score_falls_back_to_the_poster(self):
        # 1280×720 не проходит порог по ширине — режим постера, а не растяжка.
        weak = image(1280, 720, likes=0)
        selection = hero_media.choose_hero(fanart_images=[weak], poster_url="https://p/1.jpg")
        self.assertEqual(selection.hero_type, hero_media.HERO_POSTER_BLUR)
        self.assertEqual(selection.source, hero_media.SOURCE_POSTER)
        self.assertEqual(selection.url, "https://p/1.jpg")

    def test_poster_fallback_declares_no_dimensions(self):
        selection = hero_media.poster_fallback("https://p/1.jpg")
        self.assertIsNone(selection.width)
        self.assertIsNone(selection.height)

    def test_no_backdrop_and_no_poster_means_no_hero(self):
        self.assertIsNone(hero_media.choose_hero(fanart_images=[], poster_url=None))
        self.assertIsNone(hero_media.choose_hero(fanart_images=[], poster_url="   "))

    def test_zero_dimensions_never_qualify(self):
        # Такое изображение уже отфильтровано клиентом, но политика обязана
        # держаться и сама: «неизвестный размер» никогда не «широкий кадр».
        self.assertFalse(hero_media.qualifies(image(0, 0)))
        self.assertEqual(hero_media._ratio_score(0, 0), 0.0)


class DeterminismTests(unittest.TestCase):
    def test_best_candidate_wins_over_a_merely_popular_one(self):
        candidates = [image(1600, 900, likes=99, ident="popular"),
                      image(3840, 2160, likes=20, ident="sharp")]
        self.assertEqual(hero_media.choose_fanart_background(candidates).url,
                         "https://assets.fanart.tv/sharp.jpg")

    def test_equal_candidates_break_the_tie_the_same_way_every_time(self):
        candidates = [image(ident="b"), image(ident="a"), image(ident="c")]
        chosen = {hero_media.choose_fanart_background(list(reversed(candidates))).url,
                  hero_media.choose_fanart_background(candidates).url}
        self.assertEqual(chosen, {"https://assets.fanart.tv/c.jpg"})

    def test_scoring_is_stable_across_calls(self):
        candidate = image(2560, 1440, likes=7, language="ru")
        self.assertEqual(hero_media.score_fanart_background(candidate),
                         hero_media.score_fanart_background(candidate))

    def test_textless_scores_above_a_localized_copy(self):
        self.assertGreater(hero_media.score_fanart_background(image(language="00")),
                           hero_media.score_fanart_background(image(language="en")))


class PayloadTests(unittest.TestCase):
    def test_stored_selection_is_returned_as_is(self):
        payload = hero_media.hero_payload({
            "hero_url": "https://assets.fanart.tv/a.jpg", "hero_type": "backdrop",
            "hero_source": "fanart", "hero_quality_score": 0.9123,
            "poster_url": "https://p/1.jpg"})
        self.assertEqual(payload["hero_type"], "backdrop")
        self.assertEqual(payload["hero_url"], "https://assets.fanart.tv/a.jpg")
        self.assertEqual(payload["hero_fit"], "contain")
        self.assertEqual(payload["hero_focus_x"], 0.5)
        self.assertEqual(payload["hero_focus_y"], 0.36)

    def test_explicit_presentation_is_preserved_and_focus_is_clamped(self):
        cover = hero_media.hero_payload({
            "hero_url": "https://assets.fanart.tv/a.jpg", "hero_type": "backdrop",
            "hero_source": "fanart", "hero_fit": "cover",
            "hero_focus_x": 1.4, "hero_focus_y": -0.2,
        })
        self.assertEqual(cover["hero_fit"], "cover")
        self.assertEqual((cover["hero_focus_x"], cover["hero_focus_y"]), (1.0, 0.0))
        contained = hero_media.hero_payload({
            "hero_url": "https://assets.fanart.tv/a.jpg", "hero_type": "backdrop",
            "hero_source": "fanart", "hero_fit": "contain",
        })
        self.assertEqual(contained["hero_fit"], "contain")

    def test_a_stored_poster_fallback_follows_the_current_poster(self):
        """Запасной режим — это решение «показывать постер», а не копия ссылки.

        Постер живёт своей жизнью: резолвер заменяет его на лучший. Замороженная
        копия означала бы, что экран подбора неделями показывает старый постер,
        пока весь остальной продукт показывает новый.
        """
        payload = hero_media.hero_payload({
            "hero_url": "https://p/old.jpg", "hero_type": "poster_blur",
            "hero_source": "poster", "hero_quality_score": 0.5,
            "poster_url": "https://p/upgraded.jpg"})
        self.assertEqual(payload["hero_url"], "https://p/upgraded.jpg")
        self.assertEqual(payload["hero_type"], "poster_blur")

    def test_a_stored_backdrop_is_still_trusted_verbatim(self):
        # У кадра обратное: он ДОКАЗАН проверкой файла, и другого такого нет.
        payload = hero_media.hero_payload({
            "hero_url": "https://b/proved.jpg", "hero_type": "backdrop",
            "hero_source": "kinopoisk", "hero_quality_score": 0.935,
            "poster_url": "https://p/1.jpg"})
        self.assertEqual(payload["hero_url"], "https://b/proved.jpg")

    def test_a_stored_fallback_disappears_with_its_poster(self):
        payload = hero_media.hero_payload({
            "hero_url": "https://p/old.jpg", "hero_type": "poster_blur",
            "hero_source": "poster", "poster_url": None})
        self.assertIsNone(payload["hero_url"])

    def test_a_row_without_hero_columns_still_gets_a_usable_mode(self):
        payload = hero_media.hero_payload({"poster_url": "https://p/1.jpg"})
        self.assertEqual(payload, {
            "hero_url": "https://p/1.jpg", "hero_type": "poster_blur",
            "hero_source": "poster", "hero_quality_score": 0.5,
            "hero_fit": None, "hero_focus_x": None, "hero_focus_y": None})

    def test_a_row_with_nothing_at_all_declares_nothing(self):
        self.assertEqual(hero_media.hero_payload({}), {
            "hero_url": None, "hero_type": None, "hero_source": None,
            "hero_quality_score": None, "hero_fit": None,
            "hero_focus_x": None, "hero_focus_y": None})

    def test_exact_rejected_poster_is_omitted_but_a_changed_url_is_usable(self):
        rejected = {
            "poster_url": "https://p/promo.jpg",
            "poster_display_state": "rejected",
            "poster_display_url": "https://p/promo.jpg",
        }
        self.assertTrue(hero_media.poster_is_rejected(rejected))
        self.assertIsNone(hero_media.hero_payload(rejected)["hero_url"])
        changed = {**rejected, "poster_url": "https://p/clean.jpg"}
        self.assertFalse(hero_media.poster_is_rejected(changed))
        self.assertEqual(hero_media.hero_payload(changed)["hero_url"], "https://p/clean.jpg")

    def test_auto_and_approved_posters_remain_usable(self):
        for state in ("auto", "approved", None):
            film = {
                "poster_url": "https://p/1.jpg",
                "poster_display_state": state,
                "poster_display_url": "https://p/1.jpg",
            }
            self.assertEqual(hero_media.hero_payload(film)["hero_type"], "poster_blur")

    def test_backdrop_survives_a_rejected_poster(self):
        payload = hero_media.hero_payload({
            "hero_url": "https://b/1.jpg", "hero_type": "backdrop",
            "hero_source": "fanart", "poster_url": "https://p/promo.jpg",
            "poster_display_state": "rejected",
            "poster_display_url": "https://p/promo.jpg",
        })
        self.assertEqual(payload["hero_url"], "https://b/1.jpg")
        self.assertEqual(payload["hero_fit"], "contain")

    def test_public_media_row_hides_moderation_audit_fields(self):
        public = hero_media.public_media_row({
            "id": 7,
            "poster_url": "https://p/promo.jpg",
            "poster_display_state": "rejected",
            "poster_display_url": "https://p/promo.jpg",
            "poster_reject_reason": "embedded_kinopoisk_promotion",
        })
        self.assertEqual(public["id"], 7)
        self.assertIsNone(public["poster_url"])
        self.assertIsNone(public["hero_url"])
        self.assertNotIn("poster_display_state", public)
        self.assertNotIn("poster_display_url", public)
        self.assertNotIn("poster_reject_reason", public)

    def test_an_unknown_stored_type_is_not_trusted(self):
        # Значение вне словаря — это либо ручная правка, либо будущая версия.
        # Показывать его как режим отрисовки нельзя.
        payload = hero_media.hero_payload({"hero_url": "https://x/1.jpg", "hero_type": "cinema",
                                           "poster_url": "https://p/1.jpg"})
        self.assertEqual(payload["hero_type"], "poster_blur")
        self.assertEqual(payload["hero_url"], "https://p/1.jpg")

    def test_kinopoisk_backdrop_is_never_promoted_automatically(self):
        # Ровно этот дефект и чинится: наличие backdrop_url ничего не доказывает.
        payload = hero_media.hero_payload({"backdrop_url": "https://kp/wide.jpg",
                                           "poster_url": "https://p/1.jpg"})
        self.assertEqual(payload["hero_url"], "https://p/1.jpg")
        self.assertEqual(payload["hero_source"], "poster")


if __name__ == "__main__":
    unittest.main()
