"""Import order must not matter — and that has to be guarded.

The split rests on the facade depending on the repositories, never the reverse.
One module-level `import database` inside a repository makes the graph cyclic,
and it does not fail here: it fails in production, on the first import that
happens to come from the other side.
"""
import pathlib
import subprocess
import sys
import unittest

ENTRY_POINTS = (
    "database",
    "migrations",
    "db_session",
    "db_helpers",
    "repositories.audit",
    "repositories.catalog",
)


class ImportOrderTests(unittest.TestCase):
    def test_every_entry_point_imports_standalone(self):
        for module in ENTRY_POINTS:
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-c", f"import {module}"],
                                        capture_output=True, text=True,
                                        cwd=pathlib.Path(__file__).resolve().parent.parent)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_repository_imports_the_facade(self):
        """A repository reaching back for `database` is the cycle being avoided."""
        root = pathlib.Path(__file__).resolve().parent.parent / "repositories"
        for path in sorted(root.glob("*.py")):
            with self.subTest(module=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("import database", text)
                self.assertNotIn("from database import", text)


if __name__ == "__main__":
    unittest.main()
