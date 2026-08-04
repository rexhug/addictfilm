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
    """The `# ── ... ─` markers are the only navigation in the large modules."""

    def _sources(self):
        return [p for p in sorted(BACKEND.rglob("*.py")) if "__pycache__" not in p.parts]

    def _markers(self):
        seen = {}
        for path in self._sources():
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.startswith("# ── "):
                    seen.setdefault(line.rstrip("─ ").removeprefix("# ── ").strip(),
                                    []).append(f"{path.relative_to(BACKEND)}:{index}")
        return seen

    def test_no_section_title_is_used_twice(self):
        """Counted by occurrence, not by file: an earlier version counted files,
        so two markers inside one module were invisible — which is exactly how a
        pair of orphans survived the catalogue extraction.
        """
        for title, places in sorted(self._markers().items()):
            with self.subTest(marker=title):
                self.assertEqual(len(places), 1, f"{title}: {places}")

    def test_no_section_marker_is_left_without_a_section(self):
        """A marker with nothing under it is signage left behind after a move.

        Counting occurrences does not catch this: an orphan satisfies "exists
        exactly once" just as well as a marker in its right place.
        """
        for path in self._sources():
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if not line.startswith("# ── "):
                    continue
                tail = [l for l in lines[i + 1:] if l.strip()]
                with self.subTest(file=path.name, marker=line[:40]):
                    self.assertTrue(tail, "marker at end of file")
                    self.assertFalse(tail[0].startswith("# ── "),
                                     f"{path.name}:{i + 1} — section with no body")


if __name__ == "__main__":
    unittest.main()
