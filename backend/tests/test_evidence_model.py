"""UNKNOWN никогда не выполняет обязательный признак.

Иначе V2 формально покажет ноль невыполненных требований, а фактически
продолжит выдумывать подтверждение: показать фильм как соответствие признаку,
которого система не подтвердила, — это и есть выдумка.
"""
import unittest

import recommendations as rec
from recommendation import evidence
from recommendation.evidence import EvidenceLevel
from recommendation.intent import build_intent


def _candidate(*, status="ready", tones=(), genres=("drama",), audience=(), mood=None,
               confidence=0.8, validation="valid"):
    values = {"humor": 0.5, "darkness": 0.5, "peak_darkness": 0.5, "tension": 0.5,
              "complexity": 0.5, "realism": 0.5, "energy": 0.5, "pace": 0.5,
              "emotionality": 0.5}
    values.update(mood or {})
    return {"id": 1, "title": "Фильм", "_profile": {
        "status": status, "content_type": "movie", "genres": list(genres),
        "themes": [], "tones": list(tones), "audience_flags": list(audience),
        "mood": values, "confidence": confidence, "validation_level": validation,
        "feature_version": "test", "source_hash": "h"}}


class EvidenceLevelTests(unittest.TestCase):
    def test_feature_present_in_a_ready_profile_is_direct(self):
        self.assertIs(evidence.evaluate("dark_comedy", _candidate(tones=("dark_comedy",))),
                      EvidenceLevel.DIRECT)

    def test_missing_profile_is_unknown_not_absent(self):
        self.assertIs(evidence.evaluate("dark_comedy", {"id": 1}), EvidenceLevel.UNKNOWN)

    def test_pending_profile_is_unknown(self):
        self.assertIs(evidence.evaluate("dark_comedy", _candidate(status="pending")),
                      EvidenceLevel.UNKNOWN)

    def test_invalid_profile_is_unknown(self):
        self.assertIs(evidence.evaluate("dark_comedy", _candidate(tones=("dark_comedy",),
                                                                  validation="invalid")),
                      EvidenceLevel.UNKNOWN)

    def test_allowed_proxy_is_supported_not_direct(self):
        candidate = _candidate(tones=("satirical",), mood={"humor": 0.6, "peak_darkness": 0.8})
        self.assertIs(evidence.evaluate("dark_comedy", candidate), EvidenceLevel.SUPPORTED)

    def test_proxy_without_the_full_condition_stays_unknown(self):
        """Сатиры мало: нужен и настоящий юмор, и настоящая мрачность."""
        candidate = _candidate(tones=("satirical",), mood={"humor": 0.6, "peak_darkness": 0.4})
        self.assertIs(evidence.evaluate("dark_comedy", candidate), EvidenceLevel.UNKNOWN)

    def test_profile_can_say_the_opposite(self):
        candidate = _candidate(tones=("dark_comedy",), audience=("child_oriented",))
        self.assertIs(evidence.evaluate("dark_comedy", candidate), EvidenceLevel.CONTRADICTED)


class RequiredSatisfactionTests(unittest.TestCase):
    def test_direct_satisfies(self):
        self.assertTrue(evidence.satisfies_required("dark_comedy",
                                                    _candidate(tones=("dark_comedy",))))

    def test_unknown_never_satisfies(self):
        self.assertFalse(evidence.satisfies_required("dark_comedy", {"id": 1}))
        self.assertFalse(evidence.satisfies_required("dark_comedy", _candidate(status="pending")))

    def test_contradicted_never_satisfies(self):
        self.assertFalse(evidence.satisfies_required(
            "dark_comedy", _candidate(tones=("dark_comedy",), audience=("child_oriented",))))

    def test_supported_satisfies_only_where_the_policy_allows_it(self):
        candidate = _candidate(tones=("satirical",), mood={"humor": 0.6, "peak_darkness": 0.8})
        self.assertEqual(evidence.policy_for("dark_comedy").evidence_policy, "direct_or_supported")
        self.assertTrue(evidence.satisfies_required("dark_comedy", candidate))

    def test_default_policy_is_direct_only(self):
        """Вектор настроения не докажет открытый финал или исчезновение."""
        self.assertEqual(evidence.DEFAULT_POLICY.evidence_policy, "direct_only")
        self.assertEqual(evidence.policy_for("mystery").evidence_policy, "direct_only")

    def test_low_confidence_profile_does_not_satisfy_a_confident_requirement(self):
        candidate = _candidate(tones=("dark_comedy",), confidence=0.2)
        self.assertFalse(evidence.satisfies_required("dark_comedy", candidate))

    def test_forbidden_needs_known_confirmation(self):
        """Незнание — не улика: без профиля ничего не отсекаем."""
        self.assertFalse(evidence.violates_forbidden("horror", {"id": 1}))
        self.assertTrue(evidence.violates_forbidden("horror", _candidate(genres=("horror",),
                                                                         mood={"tension": 0.9})))


class CanonicalSelectionTests(unittest.TestCase):
    def _with_mood(self, candidate):
        features = rec.profile_mood_features(candidate)
        return {**candidate, "_mood_features": features}

    def test_unknown_candidate_is_excluded_from_a_required_request(self):
        intent = build_intent({"q1": "humor", "h1": "dark"})
        candidate = self._with_mood(_candidate(genres=("comedy",)))
        self.assertEqual(rec.canonical_eligible([candidate], intent), [])

    def test_confirmed_candidate_passes(self):
        intent = build_intent({"q1": "humor", "h1": "dark"})
        candidate = self._with_mood(_candidate(genres=("comedy",), tones=("dark_comedy",),
                                               mood={"humor": 0.9, "peak_darkness": 0.8}))
        self.assertEqual(len(rec.canonical_eligible([candidate], intent)), 1)

    def test_forbidden_feature_excludes(self):
        intent = build_intent({"q1": "relax", "r3": "comfort"})
        candidate = self._with_mood(_candidate(genres=("drama",), tones=("disturbing",)))
        self.assertEqual(rec.canonical_eligible([candidate], intent), [])

    def test_mood_floor_excludes_what_a_feature_cannot_express(self):
        """«Максимально напряжённое» — это порог, а не категориальный признак."""
        intent = build_intent({"q1": "tension", "t1": "horror"})
        weak = self._with_mood(_candidate(genres=("horror",), mood={"tension": 0.40}))
        strong = self._with_mood(_candidate(genres=("horror",), mood={"tension": 0.90}))
        self.assertEqual(rec.canonical_eligible([weak], intent), [])
        self.assertEqual(len(rec.canonical_eligible([strong], intent)), 1)

    def test_required_features_stay_rare_and_reliable(self):
        """Требование имеет смысл только там, где каталог умеет его подтвердить."""
        from recommendation.intent import INTENT_MAPPINGS
        required = {feature for c in INTENT_MAPPINGS.values() for feature in c.required_features}
        self.assertLessEqual(len(required), 6)
        self.assertTrue(required <= {"comedy", "mystery", "horror", "dark_comedy"})


if __name__ == "__main__":
    unittest.main()
