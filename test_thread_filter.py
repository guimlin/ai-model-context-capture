"""The thread filter in the Context page.

The page is built by ctxpage.render_html; the filter itself runs in the page's own
script. These tests check what the page is built with - the filter control, the
thread each round row and picker option carries, and the script that wires them -
against a small log holding two conversations.

    python -m unittest test_thread_filter
"""
import json
import os
import shutil
import tempfile
import unittest

import ctxpage

ROOT_A = "You are a coding agent working in C:/demo/src. Keep diffs small."
ROOT_B = "You are an agent in a different repository, answering in one line."


def _events():
    """Five rounds: the build conversation (1-3, each resending the whole history) and
    the dry-run one (4-5, a different root prompt). They are called by what they ask."""
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


class ThreadFilterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxfilter-")
        os.makedirs(os.path.join(self.dir, "payloads"))
        lines, bodies = _events()
        with open(os.path.join(self.dir, "requests.jsonl"), "w", encoding="utf-8") as handle:
            for entry in lines:
                handle.write(json.dumps(entry) + "\n")
        for seq, body in bodies.items():
            with open(os.path.join(self.dir, "payloads", "req-%06d.json" % seq),
                      "w", encoding="utf-8") as handle:
                json.dump(body, handle)
        ctxpage._cap(self.dir)
        reqs, resps, order = ctxpage.load()
        self.html = ctxpage.render_html(reqs, resps, order, 4000, 0, 204800, "the test window", 10)

    def tearDown(self):
        ctxpage.CAP = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_filter_lists_every_thread(self):
        self.assertIn('<select id="tfilter">', self.html)
        self.assertIn('<option value="">all 2 threads</option>', self.html)
        self.assertIn('<option value="build-red">thread build-red', self.html)
        self.assertIn('<option value="add-dry-run-flag">thread add-dry-run-flag', self.html)

    def test_each_thread_option_says_which_rounds_it_holds(self):
        self.assertIn("thread build-red", self.html)
        self.assertIn("3 rounds", self.html)
        self.assertIn("2 rounds", self.html)

    def test_every_round_row_carries_its_thread(self):
        build, dry = "build-red", "add-dry-run-flag"
        for seq, thread in ((1, build), (2, build), (3, build), (4, dry), (5, dry)):
            self.assertIn('<tr data-seq="%d" data-thread="%s">' % (seq, thread), self.html)

    def test_picker_options_carry_their_thread(self):
        self.assertIn('<option value="1" data-thread="build-red">', self.html)
        self.assertIn('<option value="4" data-thread="add-dry-run-flag">', self.html)

    def test_every_round_card_carries_its_thread(self):
        """Issue #33: the cards of part 4 carry the same data-thread as the rows of
        part 2, which is what lets the filter of the page's own script hide them."""
        build, dry = "build-red", "add-dry-run-flag"
        for seq, thread in ((1, build), (2, build), (3, build), (4, dry), (5, dry)):
            self.assertRegex(self.html, r'<section class="card[^"]*" id="r%d" data-thread="%s">'
                             % (seq, thread))

    def test_the_page_script_wires_the_filter_to_table_picker_and_paging(self):
        self.assertIn("getElementById('tfilter')", self.html)
        self.assertIn("function filterThread()", self.html)
        self.assertIn("tfilter.addEventListener('change', filterThread)", self.html)
        # the rows and the picker options both follow the filter
        self.assertIn("r.getAttribute('data-thread') === want", self.html)
        self.assertIn("o.getAttribute('data-thread') !== want", self.html)
        # paging restarts at page one, and a filtered-away round closes its card
        self.assertIn("if(keep && order.indexOf(keep) < 0)", self.html)
        # the table, picker and paging are filled at load, not only on the first filter move
        self.assertIn("\nfilterThread();", self.html)

    def test_the_filter_hides_the_round_cards_of_the_other_threads(self):
        """Issue #33: the all-rounds part used to show every round whatever the filter
        said. Its cards are collected once, like the rows, so a thread hides the cards of
        the other threads and "all threads" puts every one of them back."""
        self.assertIn("document.querySelectorAll('#part-all .card[data-thread]')", self.html)
        self.assertIn("allCards.forEach(function(c){", self.html)
        self.assertIn("c.getAttribute('data-thread') === want){ c.style.display = ''; left++; }",
                      self.html)
        self.assertIn("else { c.style.display = 'none'; }", self.html)

    def test_the_cards_are_put_back_before_the_picked_round_is_redrawn(self):
        """The round-details window is a clone of a card of part 4: cloning a card still
        marked display:none would leave that window blank, so the cards are shown again
        before show() takes its clone."""
        cards = self.html.index("allCards.forEach(function(c){")
        redraw = self.html.index("if(keep){ show(keep); }")
        self.assertLess(cards, redraw)

    def test_the_all_rounds_heading_names_the_thread_it_is_showing(self):
        self.assertIn("function paintAllHead(want, left){", self.html)
        self.assertIn("paintAllHead(want, left);", self.html)
        self.assertIn("document.querySelector('#part-all h2 .what')", self.html)
        self.assertIn("' of thread '", self.html)
        # with no filter the heading keeps the line the page was written with
        self.assertIn("every round on this page, oldest to newest", self.html)

    def test_the_part_wide_expand_button_only_reaches_the_rounds_on_show(self):
        """The expand-all button of part 4 speaks for the rounds you can see: a card the
        filter hid is neither opened by it nor counted when it decides whether everything
        here is already open."""
        self.assertIn("function shown(card){ return card.style.display !== 'none'; }", self.html)
        self.assertIn("if (!shown(c)) { return; }", self.html)

    def test_the_filter_only_offers_threads_that_are_in_the_table(self):
        """--last can cut a whole conversation out of the table; the filter must not
        offer it, or picking it would leave an empty table."""
        reqs, resps, order = ctxpage.load()
        html = ctxpage.render_html(reqs, resps, order, 4000, 2, 204800, "the test window", 10)
        self.assertIn('<option value="">all 1 thread</option>', html)
        self.assertIn('<option value="add-dry-run-flag">thread add-dry-run-flag', html)
        self.assertIn("2 rounds", html)
        self.assertNotIn('<option value="build-red">', html)
        self.assertNotIn('<tr data-seq="1"', html)

    def test_row_clicks_are_taken_from_the_rounds_table(self):
        self.assertIn('<table id="rounds">', self.html)
        self.assertIn("getElementById('rounds').addEventListener('click'", self.html)

    def test_a_picked_round_keeps_its_hint_when_the_filter_changes(self):
        """A picked round can survive the filter but land off the new first page, and its
        hint would still name the neighbours it had before the filter, so the page shows
        it again instead of leaving the old text up."""
        self.assertIn("var keep = pick.value;", self.html)
        self.assertIn("if(keep && order.indexOf(keep) < 0)", self.html)
        self.assertIn("if(keep){ show(keep); }", self.html)

    def test_shift_arrow_steps_one_round_before_arrows_page(self):
        """e.key is still ArrowLeft / ArrowRight with shift held, so the shift branch has
        to be tested first or the paging branch below swallows it."""
        shift = self.html.index("if(e.shiftKey && (e.key === 'ArrowLeft'")
        paging = self.html.index("if(e.key === 'ArrowLeft'  || e.key === 'PageUp')")
        self.assertLess(shift, paging)


if __name__ == "__main__":
    unittest.main()
