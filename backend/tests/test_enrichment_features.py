"""Таксономия, извлечение признаков, семантика и слияние доказательств.

Проверяем СВОЙСТВА правил, а не конкретные числа: значения будут
пересчитываться, а «семейный мультфильм не должен становиться чёрной комедией»
обязано выполняться всегда.
"""
import unittest

from enrichment import extraction, mappings, merge, semantic, validation
from enrichment.models import FeatureEvidence, NormalizedMovieSource
from enrichment.taxonomy import (AudienceFlag, CanonicalGenre, CanonicalTone, ContentType,
                                 EvidenceSource, MOVIE_TAXONOMY_VERSION, precedence_rank)


def _source(**kwargs) -> NormalizedMovieSource:
    payload = {"film_id": 1, "provider": "kinopoisk", "provider_id": "1",
               "content_type": ContentType.MOVIE, "title": "Фильм"}
    payload.update(kwargs)
    return NormalizedMovieSource(**payload)


class TaxonomyTests(unittest.TestCase):
    def test_versions_are_explicit(self):
        self.assertTrue(MOVIE_TAXONOMY_VERSION.startswith("movie-taxonomy-"))

    def test_evidence_precedence_puts_provider_above_semantics(self):
        self.assertLess(precedence_rank(EvidenceSource.PROVIDER_CONTENT_TYPE),
                        precedence_rank(EvidenceSource.SEMANTIC))
        self.assertLess(precedence_rank(EvidenceSource.MANUAL_OVERRIDE),
                        precedence_rank(EvidenceSource.REVIEWED_KEYWORD))
        self.assertLess(precedence_rank(EvidenceSource.REVIEWED_KEYWORD),
                        precedence_rank(EvidenceSource.SEMANTIC))
        self.assertLess(precedence_rank(EvidenceSource.SEMANTIC),
                        precedence_rank(EvidenceSource.GENRE_BASELINE))

    def test_provider_genres_map_to_canonical_ids(self):
        canonical, unknown = mappings.canonical_genres(["комедия", "Comedy", "Crime"])
        self.assertEqual(canonical, {str(CanonicalGenre.COMEDY), str(CanonicalGenre.CRIME)})
        self.assertEqual(unknown, ())

    def test_unknown_provider_genre_never_becomes_a_tag(self):
        canonical, unknown = mappings.canonical_genres(["реальное ТВ", "комедия"])
        self.assertEqual(canonical, {str(CanonicalGenre.COMEDY)})
        self.assertIn("реальное тв", unknown)

    def test_format_genre_is_not_a_content_genre(self):
        canonical, _ = mappings.canonical_genres(["короткометражка", "драма"])
        self.assertEqual(canonical, {str(CanonicalGenre.DRAMA)})

    def test_markers_match_on_word_boundary(self):
        self.assertNotIn(str(CanonicalTone.DARK_COMEDY),
                         mappings.match_reviewed_tones("Черника и письмо Деду Морозу"))
        self.assertIn(str(CanonicalTone.DARK_COMEDY),
                      mappings.match_reviewed_tones("Чёрная комедия о похоронах"))

    def test_certification_drives_audience_flags(self):
        self.assertIn(str(AudienceFlag.CHILD_ORIENTED),
                      mappings.audience_from_certification("G", None))
        self.assertIn(str(AudienceFlag.MATURE_THEMES),
                      mappings.audience_from_certification("R", None))
        self.assertIn(str(AudienceFlag.MATURE_THEMES),
                      mappings.audience_from_certification(None, 18))


class ExtractionTests(unittest.TestCase):
    def test_all_dimensions_stay_in_range(self):
        for genres in (("комедия",), ("ужасы", "триллер"), (), ("несуществующий",)):
            features = extraction.extract(_source(genre_names=genres,
                                                  overview="месть, война, смерть, философия"))
            for dimension, value in features.mood.items():
                with self.subTest(genres=genres, dimension=dimension):
                    self.assertGreaterEqual(value, 0.0)
                    self.assertLessEqual(value, 1.0)

    def test_signature_genre_survives_mixing(self):
        mixed = extraction.extract(_source(genre_names=("мультфильм", "комедия", "приключения")))
        self.assertGreater(mixed.mood["humor"], 0.45)

    def test_peak_darkness_keeps_the_darkest_component(self):
        """Усреднённый тон чёрной комедии всегда низкий из-за жанра «комедия»."""
        features = extraction.extract(_source(genre_names=("комедия", "военный")))
        self.assertGreater(features.mood["peak_darkness"], features.mood["darkness"])

    def test_marker_deltas_are_bounded(self):
        loaded = extraction.extract(_source(
            genre_names=("драма",),
            overview="месть смерть война выживание антиутопия призрак утрата психологич"))
        baseline = extraction.extract(_source(genre_names=("драма",)))
        self.assertLessEqual(loaded.mood["darkness"] - baseline.mood["darkness"],
                             extraction.MAX_TOTAL_DELTA + 1e-6)

    def test_animation_alone_is_not_child_oriented(self):
        features = extraction.extract(_source(genre_names=("мультфильм",)))
        self.assertNotIn(str(AudienceFlag.CHILD_ORIENTED), features.audience_flags)

    def test_adult_animation_is_not_family_friendly(self):
        features = extraction.extract(_source(genre_names=("мультфильм", "ужасы"),
                                              certification="R"))
        self.assertIn(str(AudienceFlag.ADULT_ANIMATION), features.audience_flags)
        self.assertNotIn(str(AudienceFlag.FAMILY_FRIENDLY), features.audience_flags)

    def test_extraction_is_deterministic(self):
        source = _source(genre_names=("триллер",), overview="Расследование убийства")
        self.assertEqual(extraction.extract(source).mood, extraction.extract(source).mood)


class SemanticTests(unittest.TestCase):
    def test_disabled_classifier_returns_nothing(self):
        self.assertEqual(semantic.DisabledSemanticClassifier().classify(_source()), ())

    def test_label_profiles_are_precomputed_once(self):
        classifier = semantic.LexicalSemanticClassifier()
        self.assertEqual(set(classifier._label_profiles), set(semantic.SEMANTIC_LABEL_DESCRIPTIONS))

    def test_short_text_produces_no_labels(self):
        classifier = semantic.LexicalSemanticClassifier()
        self.assertEqual(classifier.classify(_source(overview="Коротко.")), ())

    def test_low_score_is_rejected(self):
        threshold = semantic.SemanticThreshold(0.5, 0.05)
        self.assertFalse(semantic.accept_semantic_label(score=0.3, second_best_score=0.1,
                                                        threshold=threshold))

    def test_insufficient_margin_is_rejected(self):
        threshold = semantic.SemanticThreshold(0.3, 0.10)
        self.assertFalse(semantic.accept_semantic_label(score=0.55, second_best_score=0.50,
                                                        threshold=threshold))

    def test_strong_and_distinct_score_is_accepted(self):
        threshold = semantic.SemanticThreshold(0.3, 0.05)
        self.assertTrue(semantic.accept_semantic_label(score=0.62, second_best_score=0.40,
                                                       threshold=threshold))

    def test_default_classifier_is_disabled_by_measurement(self):
        self.assertIsInstance(semantic.build_classifier(), semantic.DisabledSemanticClassifier)

    def test_unavailable_model_falls_back_instead_of_failing(self):
        classifier = semantic.build_classifier("sentence_transformers", "несуществующая/модель")
        self.assertIsInstance(classifier, semantic.LexicalSemanticClassifier)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.merger = merge.MovieFeatureMerger()

    def test_semantic_cannot_add_a_tone_contradicting_the_dimensions(self):
        """Светлая комедия не становится чёрной от одного семантического ярлыка."""
        deterministic = extraction.extract(_source(genre_names=("комедия", "семейный")))
        evidence = (FeatureEvidence(EvidenceSource.SEMANTIC, str(CanonicalTone.DARK_COMEDY),
                                    0.9, 0.9),)
        merged, contradictions = self.merger.merge(deterministic=deterministic, semantic=evidence)
        self.assertNotIn(str(CanonicalTone.DARK_COMEDY), merged.tones)
        self.assertGreater(contradictions, 0)

    def test_semantic_refines_what_rules_did_not_know(self):
        deterministic = extraction.extract(_source(genre_names=("драма", "детектив"),
                                                   overview="Долгое расследование в маленьком городе"))
        evidence = (FeatureEvidence(EvidenceSource.SEMANTIC, str(CanonicalTone.SLOW_BURN), 0.7, 0.7),)
        merged, _ = self.merger.merge(deterministic=deterministic, semantic=evidence)
        self.assertIn(str(CanonicalTone.SLOW_BURN), merged.tones)

    def test_semantic_cannot_change_content_type(self):
        deterministic = extraction.extract(_source(content_type=ContentType.TV,
                                                   genre_names=("комедия",)))
        evidence = (FeatureEvidence(EvidenceSource.SEMANTIC, str(CanonicalTone.COZY), 0.9, 0.9),)
        merged, _ = self.merger.merge(deterministic=deterministic, semantic=evidence)
        self.assertIs(merged.content_type, ContentType.TV)

    def test_child_audience_blocks_dark_comedy(self):
        deterministic = extraction.extract(_source(
            genre_names=("мультфильм", "комедия", "криминал"), certification="G",
            overview="Чёрная комедия про ограбление"))
        merged, contradictions = self.merger.merge(deterministic=deterministic)
        self.assertNotIn(str(CanonicalTone.DARK_COMEDY), merged.tones)
        self.assertGreater(contradictions, 0)

    def test_manual_override_wins_and_is_recorded(self):
        deterministic = extraction.extract(_source(genre_names=("комедия",)))
        merged, _ = self.merger.merge(deterministic=deterministic,
                                      overrides={"mood": {"humor": 0.1}})
        self.assertAlmostEqual(merged.mood["humor"], 0.1)
        self.assertTrue(any(item.source == EvidenceSource.MANUAL_OVERRIDE
                            for item in merged.evidence))

    def test_override_ignores_fields_outside_the_allowlist(self):
        deterministic = extraction.extract(_source(genre_names=("комедия",)))
        merged, _ = self.merger.merge(deterministic=deterministic,
                                      overrides={"confidence": 1.0, "evil": "x"})
        self.assertLess(merged.confidence, 1.0)

    def test_confidence_stays_in_range_and_drops_on_contradictions(self):
        clean = merge.calculate_profile_confidence(
            has_valid_content_type=True, genre_count=3, reviewed_marker_count=4,
            has_overview=True, semantic_confidence=0.8, agreement_score=1.0,
            contradiction_count=0)
        conflicted = merge.calculate_profile_confidence(
            has_valid_content_type=True, genre_count=3, reviewed_marker_count=4,
            has_overview=True, semantic_confidence=0.8, agreement_score=1.0,
            contradiction_count=3)
        self.assertLessEqual(clean, 1.0)
        self.assertGreaterEqual(conflicted, 0.0)
        self.assertGreater(clean, conflicted)

    def test_status_follows_calibrated_thresholds(self):
        self.assertEqual(merge.resolve_status(0.9, invalid=False), "ready")
        self.assertEqual(merge.resolve_status(0.40, invalid=False), "low_confidence")
        self.assertEqual(merge.resolve_status(0.9, invalid=True), "failed")


class ValidationTests(unittest.TestCase):
    def _features(self, **overrides):
        base = extraction.extract(_source(genre_names=("драма",)))
        mood = {**base.mood, **overrides.pop("mood", {})}
        return type(base)(content_type=overrides.pop("content_type", base.content_type),
                          genres=frozenset(overrides.pop("genres", base.genres)),
                          themes=base.themes,
                          tones=frozenset(overrides.pop("tones", base.tones)),
                          audience_flags=frozenset(overrides.pop("audience_flags",
                                                                 base.audience_flags)),
                          mood=mood, confidence=0.5, evidence=())

    def test_clean_profile_is_valid(self):
        self.assertEqual(validation.validate(self._features()).level, "valid")

    def test_family_friendly_but_dark_is_invalid(self):
        outcome = validation.validate(self._features(
            audience_flags={str(AudienceFlag.FAMILY_FRIENDLY)}, mood={"darkness": 0.9}))
        self.assertEqual(outcome.level, "invalid")
        self.assertTrue(outcome.blocks_recommendations)

    def test_cozy_but_tense_is_invalid(self):
        outcome = validation.validate(self._features(
            tones={str(CanonicalTone.COZY)}, mood={"tension": 0.9}))
        self.assertEqual(outcome.level, "invalid")

    def test_dark_comedy_without_humor_is_invalid(self):
        outcome = validation.validate(self._features(
            tones={str(CanonicalTone.DARK_COMEDY)}, mood={"humor": 0.1, "peak_darkness": 0.9}))
        self.assertEqual(outcome.level, "invalid")

    def test_comedy_without_humor_is_only_a_warning(self):
        outcome = validation.validate(self._features(
            genres={str(CanonicalGenre.COMEDY)}, mood={"humor": 0.1}))
        self.assertEqual(outcome.level, "warning")
        self.assertFalse(outcome.blocks_recommendations)

    def test_out_of_range_value_is_invalid(self):
        self.assertEqual(validation.validate(self._features(mood={"humor": 1.4})).level, "invalid")


if __name__ == "__main__":
    unittest.main()
