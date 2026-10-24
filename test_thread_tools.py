"""The thread tools of the Context page, which the monitor page's Threads tab used to hold.

Issue #35: the Threads tab was a second copy of the "1 Threads" part of the Context page, so
the tab is gone and its tools - pick a conversation, hide it, show hidden rounds again,
delete it from the capture file, save the threads you picked - live on the thread rows
themselves, above a table that scrolls at five conversations. These tests check the tab is
out of the monitor page, the tools are in the Context page the monitor page serves (and out
of an offline report.html), and that hiding a thread's rounds takes the conversation - and
any conversation made only of those rounds - off the rebuilt page.

    python -m unittest test_thread_tools
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import ctxpage
import ctxstore
import ctxweb

ROOT_A = "You are a coding agent working in C:/demo/src. Keep diffs small."
ROOT_B = "You are an agent in a different repository, answering in one line."


def _events():
    """Five rounds in two conversations: the build one (1-3) and the dry-run one (4-5).
    Hiding the dry-run rounds has to take that conversation off the page."""
    lines, bodies = [], {}
    histories = {
        "A": [{"role": "system", "content": ROOT_A}, {"role": "user", "content": "why is the build red?"}],
        "B": [{"role": "system", "content": ROOT_B}, {"role": "user", "content": "add a dry-run flag."}],
    }
    seq = 0
    for thread, turns in (("A", 3), ("B", 2)):
        for _ in range(turns):
            seq += 1
            bodies[seq] = {"model": "iq3_s", "messages": list(histories[thread])}
            lines.append({"type": "request", "seq": seq, "ts": "2026-10-20T10:%02d:00" % seq,
                          "method": "POST", "path": "/v1/chat/completions", "api": "openai-chat",
                          "model": "iq3_s", "stream": False, "headers": {}, "body_bytes": 400,
                          "est_prompt_tokens": 100 * seq, "payload_file": "payloads/req-%06d.json" % seq})
            lines.append({"type": "response", "seq": seq, "ts": "2026-10-20T10:%02d:05" % seq,
                          "status": 200, "finish_reason": "stop",
                          "usage": {"prompt_tokens": 90 * seq, "completion_tokens": 12,
                                    "prompt_tokens_details": {"cached_tokens": 60 * seq}},
                          "completion_preview": "answer %d" % seq})
            histories[thread] += [{"role": "assistant", "content": "done: round %d" % seq},
                                  {"role": "user", "content": "next step?"}]
    return lines, bodies


def _write_source(dir_):
    """The five rounds as a log folder, which is a source the page can be built from."""
    os.makedirs(os.path.join(dir_, "payloads"), exist_ok=True)
    lines, bodies = _events()
    with open(os.path.join(dir_, "requests.jsonl"), "w", encoding="utf-8") as handle:
        for entry in lines:
            handle.write(json.dumps(entry) + "\n")
    for seq, body in bodies.items():
        with open(os.path.join(dir_, "payloads", "req-%06d.json" % seq),
                  "w", encoding="utf-8") as handle:
            json.dump(body, handle)
    return dir_


class ThreadsTabIsGone(unittest.TestCase):
    """The tab duplicated part 1 of the Context page, so it is out: no tab button, no
    section, and none of the script that filled its two lists."""

    def test_the_threads_tab_is_out_of_the_monitor_page(self):
        for gone in ('data-tab="threads"', '<section id="threads">', 'id="selall"',
                     'id="clearsel"', 'id="openall"', 'id="delfile"', 'id="savepick"',
                     'id="hiddenbox"', 'id="roundlist"', 'id="threadlist"',
                     "loadThreads", "loadRounds", "promptLine", "newest rounds of"):
            self.assertNotIn(gone, ctxweb.PAGE, "%s is still in the monitor page" % gone)

    def test_the_rounds_of_a_thread_are_not_listed_twice(self):
        """Item 1 and item 2 of the issue: the tab's own rounds table and its "open every
        thread list" button went with the tab."""
        self.assertNotIn("function bars(", ctxweb.PAGE)
        self.assertNotIn(".bars {", ctxweb.PAGE)
        self.assertNotIn("data-jump", ctxweb.PAGE)

    def test_the_context_page_is_the_third_tab_now(self):
        tabs = re.findall(r'data-tab="([^"]+)">([^<]*)', ctxweb.PAGE)
        self.assertEqual([("monitor", "1 &middot; Monitor"), ("capture", "2 &middot; Capture files"),
                          ("context", "3 &middot; Context"),
                          ("analysis", "4 &middot; Capture Analysis")], tabs)

    def test_the_monitor_page_picks_up_what_the_frame_changed(self):
        """The tools live inside the frame, so the frame tells this page to re-read its
        figures after a hide, a delete or a save."""
        self.assertIn("ev.data.ctxChanged", ctxweb.PAGE)
        self.assertIn("postMessage({ ctxChanged: true }", ctxpage.JS)


class ThreadToolsOnTheContextPage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxtools-")
        _write_source(self.dir)
        ctxpage._cap(self.dir)
        reqs, resps, order = ctxpage.load()
        self.html = ctxpage.render_html(reqs, resps, order, 4000, 0, 204800,
                                        "the test window", 10, served=True)

    def tearDown(self):
        ctxpage.CAP = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_every_thread_row_can_be_picked(self):
        """One checkbox per conversation, and it stands in for every round that thread is
        made of, which is what hiding or deleting it takes away."""
        picked = dict(re.findall(r'data-threadpick="([^"]+)" data-rounds="([\d,]+)"', self.html))
        self.assertEqual(2, len(picked))
        self.assertIn("1,2,3", picked.values())
        self.assertIn("4,5", picked.values())
        self.assertIn('<th class="l" title="tick a thread to hide, delete or save it"></th>',
                      self.html)

    def test_the_tools_sit_above_the_threads_table(self):
        tools = self.html.index('id="threadtools"')
        self.assertLess(tools, self.html.index('<table class="threads">'))
        for button in ('id="selthreads"', 'id="unselthreads"', 'id="hidebtn"', 'id="unhidebtn"',
                       'id="delbtn"', 'id="savebtn"'):
            self.assertIn(button, self.html)

    def test_the_threads_table_scrolls_at_five_conversations(self):
        """Item 3 of the issue: five threads in view, the rest behind a scrollbar, with the
        header held at the top while you scroll."""
        self.assertIn("--thread-rows:5", self.html)
        self.assertIn(".threadbox { max-height:calc(var(--thread-rows) * 34px); overflow:auto;",
                      self.html)
        self.assertIn(".threadbox thead th { position:sticky;", self.html)
        box = self.html[self.html.index('<div class="threadbox">'):]
        self.assertIn('<table class="threads">', box)
        self.assertIn('</table></div>', box)

    def test_the_tools_talk_to_the_monitor_page(self):
        for call in ("'/api/hide'", "'/api/delete'", "'/api/save'", "'/api/report/build'"):
            self.assertIn(call, ctxpage.JS)

    def test_the_live_log_cannot_be_edited_while_it_is_being_written(self):
        """The source here is a log folder, so deleting rounds out of it is shut, exactly as
        the old tab used to shut it."""
        self.assertIn('id="delbtn" type="button" class="bad" disabled', self.html)

    def test_an_offline_page_leaves_the_tools_out(self):
        """A report.html copied off this machine has no API to answer the buttons, so it
        shows the table without the checkboxes rather than dead controls. The scrollbar
        stays: it needs nothing but CSS."""
        reqs, resps, order = ctxpage.load()
        offline = ctxpage.render_html(reqs, resps, order, 4000, 0, 204800, "the test window", 10)
        self.assertNotIn('<div class="parttools" id="threadtools">', offline)
        self.assertNotIn('<input type="checkbox" data-threadpick=', offline)
        self.assertIn('<div class="threadbox"><table class="threads">', offline)


class HidingTakesTheThreadAway(unittest.TestCase):
    """Item 5 of the issue: hide a conversation and its rounds leave the page, so the
    conversation leaves with them - and showing them again brings it back."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxhide-")
        self.web = os.path.join(self.dir, "web")
        _write_source(self.dir)
        self.hidden = {}
        self._source, self._web = ctxweb.CFG["source"], ctxweb.CFG["web_dir"]
        ctxweb.CFG["source"], ctxweb.CFG["web_dir"] = self.dir, self.web
        self._patches = [
            mock.patch.object(ctxstore, "load_hidden", lambda here: dict(self.hidden)),
            mock.patch.object(ctxstore, "set_hidden",
                              lambda here, path, seqs: self.hidden.__setitem__(str(path), list(seqs))),
        ]
        for patch in self._patches:
            patch.start()
        ctxweb.build_report()      # the page the tools live on, built the way the server builds it

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        ctxweb.CFG["source"], ctxweb.CFG["web_dir"] = self._source, self._web
        ctxweb.drop_view()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _threads_on_the_page(self):
        with open(os.path.join(self.web, "report.html"), encoding="utf-8") as handle:
            page = handle.read()
        return dict(re.findall(r'data-threadpick="([^"]+)" data-rounds="([\d,]+)"', page)), page

    def test_hiding_a_threads_rounds_takes_it_off_the_page(self):
        before, _ = self._threads_on_the_page()
        dry = [name for name, rounds in before.items() if rounds == "4,5"]
        self.assertEqual(1, len(dry), "the dry-run conversation is not on the page to start with")

        result = ctxweb.api_hide({"seqs": [4, 5]})
        self.assertTrue(result["ok"])
        self.assertEqual(3, result["report"]["rounds"])
        after, page = self._threads_on_the_page()
        self.assertEqual([name for name, _ in before.items() if name != dry[0]],
                         list(after.keys()))
        self.assertNotIn('<tr data-seq="4"', page)
        self.assertNotIn('<tr data-seq="5"', page)

        back = ctxweb.api_hide({"clear": True})
        self.assertEqual(5, back["report"]["rounds"])
        self.assertEqual(2, len(self._threads_on_the_page()[0]))

    def test_hiding_every_round_says_so_instead_of_blanking_the_page(self):
        result = ctxweb.api_hide({"seqs": [1, 2, 3, 4, 5]})
        self.assertTrue(result["ok"])
        self.assertFalse(result["report"]["ok"])
        self.assertIn("show hidden again", result["report"]["error"])

    def test_the_page_the_monitor_page_builds_carries_the_tools(self):
        ctxweb.build_report()
        page = self._threads_on_the_page()[1]
        self.assertIn('id="threadtools"', page)
        self.assertIn('<div class="threadbox">', page)


if __name__ == "__main__":
    unittest.main()
