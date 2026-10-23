"""The capture file says what it is in manifest.json. The tool was made generic (#26),
so the name it writes changed - and files saved under the old name still have to open.

    python test_capture_format.py
"""

import json
import os
import shutil
import tempfile
import unittest
import zipfile

import ctxstore

LOG_LINE = json.dumps({"type": "request", "seq": 1, "ts": "2026-01-01T00:00:00",
                       "api": "/v1/chat/completions", "model": "any-model",
                       "body_file": "payloads/1.json"})
ANSWER_LINE = json.dumps({"type": "response", "seq": 1, "ts": "2026-01-01T00:00:05",
                          "status": 200, "usage": {"prompt_tokens": 10}})


class CaptureFormatTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxfmt")
        self.addCleanup(shutil.rmtree, self.dir, True)
        with open(os.path.join(self.dir, "requests.jsonl"), "w", encoding="utf-8") as f:
            f.write(LOG_LINE + "\n" + ANSWER_LINE + "\n")
        os.makedirs(os.path.join(self.dir, "payloads"))
        with open(os.path.join(self.dir, "payloads", "1.json"), "w", encoding="utf-8") as f:
            f.write('{"messages": []}')

    def save(self):
        source = ctxstore.Capture(self.dir)
        self.addCleanup(source.close)
        out = os.path.join(self.dir, "saved.ctxcap")
        ctxstore.save(source, out, [1])
        return out

    def rewrite_manifest(self, path, **changes):
        """Re-save a capture file with a different manifest, the way an old tool wrote it."""
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            members = {name: archive.read(name) for name in archive.namelist()}
        manifest.update(changes)
        members["manifest.json"] = json.dumps(manifest).encode("utf-8")
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return path

    def test_a_saved_capture_says_what_it_is_in_generic_words(self):
        with zipfile.ZipFile(self.save()) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        self.assertEqual("model-context-capture", manifest["format"])
        self.assertEqual(ctxstore.FORMAT, manifest["format"])

    def test_a_capture_saved_under_the_old_name_still_opens(self):
        path = self.rewrite_manifest(self.save(), format="strata-context-capture")
        cap = ctxstore.Capture(path)
        self.addCleanup(cap.close)
        self.assertEqual("file", cap.kind)
        self.assertIn('"seq":1', cap.events())

    def test_a_file_from_an_unrelated_tool_is_still_refused(self):
        path = self.rewrite_manifest(self.save(), format="something-else")
        with self.assertRaises(SystemExit):
            ctxstore.Capture(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
