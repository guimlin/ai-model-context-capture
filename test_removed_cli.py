"""The terminal front ends (ctxview.py, ctxsave.py) are gone: the page is the tool.

Run:  python -m unittest test_removed_cli
"""

import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DELETED = ["ctxview.py", "ctxsave.py"]
SUFFIXES = (".py", ".ps1", ".bat", ".cmd", ".md", ".json", ".yml", ".yaml", ".toml",
            ".cfg", ".ini", ".txt")
SKIP_DIRS = {".git", "__pycache__", "logs", "web", "captures", "docs", "session-state"}


def source_files():
    for folder, names, files in os.walk(HERE):
        names[:] = [n for n in names if n not in SKIP_DIRS and not n.startswith(".")]
        for name in files:
            if name.endswith(SUFFIXES) and name != os.path.basename(__file__):
                yield os.path.join(folder, name)


class RemovedCliTest(unittest.TestCase):
    def test_scripts_are_gone(self):
        for name in DELETED:
            self.assertFalse(os.path.exists(os.path.join(HERE, name)),
                             "%s is still here" % name)

    def test_nothing_points_at_them(self):
        for path in source_files():
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
            for name in DELETED:
                self.assertNotIn(name[:-3], text,
                                 "%s still mentions %s" % (os.path.relpath(path, HERE), name))

    def test_the_page_still_drives_the_shared_modules(self):
        """ctxweb is the only entry point left, and it still reads captures."""
        import ctxstore
        import ctxweb  # noqa: F401 - importing it is the point

        self.assertEqual("model-context-capture", ctxstore.FORMAT)
        self.assertTrue(callable(ctxstore.choose) and callable(ctxstore.save)
                        and callable(ctxstore.extract))


if __name__ == "__main__":
    unittest.main()
