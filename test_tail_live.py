"""The "What just went by" box on tab 1 is the live log, whatever the page is viewing.

It used to read the source being viewed, so opening a capture file left the box showing
that file's rounds forever - the newest rounds going by never appeared, and the box was
dead (issue #36).

Run:  python -m unittest test_tail_live
"""
import json
import os
import tempfile
import unittest

import ctxstore
import ctxweb


def line(seq, kind, api="openai-chat", summary=None):
    record = {"type": kind, "seq": seq,
              "ts": "2026-10-22T19:41:%02d+08:00" % (30 + seq),
              "summary": summary or "%s %d" % (kind, seq)}
    if kind == "request":
        record["api"] = api
        record["model"] = "Qwen3-Next-80B-A3B-GPT-Q8"
    return json.dumps(record, ensure_ascii=False)


class TailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = dict(ctxweb.CFG)
        self.addCleanup(self._restore_cfg)
        self.log_dir = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.log_dir)
        ctxweb.CFG["log_dir"] = self.log_dir
        ctxweb.CFG["source"] = ""
        self.write_live(6)

    def _restore_cfg(self):
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)
        ctxweb.drop_view()

    def write_live(self, rounds):
        """rounds rounds of live traffic: one request line and one answer line each."""
        with open(os.path.join(self.log_dir, "requests.jsonl"), "w",
                  encoding="utf-8") as handle:
            for seq in range(1, rounds + 1):
                handle.write(line(seq, "request") + "\n")
                handle.write(line(seq, "response") + "\n")

    def save_capture(self, rounds):
        """Save the first `rounds` rounds as a capture file, and view it."""
        path = os.path.join(self.tmp.name, "one.ctxcap")
        ctxstore.save(ctxstore.Capture(self.log_dir), path, list(range(1, rounds + 1)),
                      note="the first few")
        ctxweb.drop_view()
        ctxweb.CFG["source"] = path
        return path

    def seqs(self, tail=14):
        return [entry["seq"] for entry in ctxweb.api_log({"tail": [str(tail)]})["lines"]]

    def test_the_tail_shows_the_newest_rounds_of_the_live_log(self):
        self.assertEqual(self.seqs(), [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6])

    def test_the_tail_is_the_newest_lines_not_the_oldest(self):
        self.assertEqual(self.seqs(4), [5, 5, 6, 6])

    def test_the_tail_keeps_updating_as_the_log_grows(self):
        self.assertEqual(self.seqs(2), [6, 6])
        self.write_live(9)
        self.assertEqual(self.seqs(2), [9, 9])

    def test_a_capture_file_being_viewed_does_not_freeze_the_tail(self):
        """The bug: the box read the viewed source, so a saved capture - which never
        changes - was shown as though it were what was going by."""
        self.save_capture(3)
        self.assertEqual(ctxweb.view()[4], [1, 2, 3])   # the page still views the file
        self.assertEqual(self.seqs(4), [5, 5, 6, 6])

    def test_the_tail_survives_a_viewed_capture_file_that_is_gone(self):
        path = self.save_capture(3)
        os.remove(path)
        ctxweb.drop_view(path)
        self.assertEqual(self.seqs(2), [6, 6])

    def test_the_box_says_which_log_its_lines_came_from(self):
        self.save_capture(3)
        self.assertEqual(ctxweb.api_log({})["path"], os.path.abspath(self.log_dir))

    def test_each_line_is_stamped_with_the_date_it_went_by(self):
        """The date stamp the tail prints (#39) comes from the live log too."""
        self.assertEqual(ctxweb.api_log({"tail": ["1"]})["lines"][0]["ts"],
                         "10/22/2026 19:41:36")

    def test_the_page_labels_the_box_as_the_live_log(self):
        self.assertIn('id="tailnote"', ctxweb.PAGE)
        self.assertIn("the live log, not the capture file you are reading", ctxweb.PAGE)
        self.assertIn("nothing going by in the live log yet", ctxweb.PAGE)

    def test_the_app_s_own_page_traffic_is_left_out(self):
        with open(os.path.join(self.log_dir, "requests.jsonl"), "a",
                  encoding="utf-8") as handle:
            handle.write(line(7, "request", api=None) + "\n")
        self.assertNotIn(7, self.seqs())

    def test_a_live_log_that_does_not_exist_yet_is_an_empty_tail(self):
        ctxweb.CFG["log_dir"] = os.path.join(self.tmp.name, "not-yet")
        ctxweb.drop_view()
        self.assertEqual(ctxweb.api_log({})["lines"], [])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "not-yet",
                                                    "requests.jsonl")))

    def test_junk_lines_do_not_stop_the_tail(self):
        with open(os.path.join(self.log_dir, "requests.jsonl"), "a",
                  encoding="utf-8") as handle:
            handle.write("not json at all\n\n")
            handle.write(line(7, "request") + "\n")
        self.assertEqual(self.seqs(2), [6, 7])   # the junk line is skipped, #7 still shows


if __name__ == "__main__":
    unittest.main(verbosity=2)
