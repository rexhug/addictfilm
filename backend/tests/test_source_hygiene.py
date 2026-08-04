"""Shape of the source files, not their behaviour.

Extracting a block by AST line ranges leaves a hole where the block used to be.
Eighty-nine consecutive blank lines once survived a full lint run: ruff's
blank-line rules (E301-E303) are preview-only in the pinned version, and turning
preview on adds seventy-odd unrelated findings across the codebase. A single
assertion buys the same guarantee without that trade.
"""
import pathlib
import re
import unittest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
MAX_CONSECUTIVE_BLANK_LINES = 2


class BlankLineHygieneTests(unittest.TestCase):
    def test_no_source_file_carries_an_extraction_hole(self):
        offenders = []
        for path in sorted(BACKEND.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(rf"\n{{{MAX_CONSECUTIVE_BLANK_LINES + 2},}}", text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(BACKEND)}:{line} "
                                 f"({match.group().count(chr(10)) - 1} blank lines)")
        self.assertEqual(offenders, [], "\n".join(offenders))


class SectionMarkerTests(unittest.TestCase):
    """The `# ── ... ─` markers are the only navigation in the large modules.

    One was silently removed together with the migrations block; losing another
    would make the next split start blind.
    """

    def test_the_query_layer_keeps_its_section_markers(self):
        text = (BACKEND / "database.py").read_text(encoding="utf-8")
        for marker in ("Инициализация", "Каталог фильмов", "Список пользователя",
                       "Подбор", "Центр уведомлений"):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
