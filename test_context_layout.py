"""The four parts of the Context page, and the collapse-all button of the last one.

Issue #24 asks the Context page to read as four parts - threads, rounds, round details
(the selected round) and all rounds - and asks that the all-rounds part start with every
block shut, with one button at its top that opens or shuts them all at once while each
round keeps the button it already had (issue #22). The page is built by
ctxpage.render_html and opened or shut by its own script, so these tests check what the
page is built with: the four labelled parts and what sits in each, the round details part
the script opens on the first round of the table as soon as the page loads (issue #32),
the shut blocks, the
folded round - a shut round of the last part is one row of its key columns - the
part-wide button and the script that drives it, and the KPI cards of the header.

    python -m unittest test_context_layout
"""
import json
import os
import re
import shutil
import tempfile
import unittest

import ctxpage

ROOT = ("You are a coding agent working in C:/demo/src. Keep diffs small. "
        "Read the failing test before you touch anything. " * 30)

# the four parts, in the order the page reads down, with the heading each one carries
PARTS = [("part-threads", "1", "Threads"),
         ("part-rounds", "2", "Rounds"),
         ("detail", "3", "Round details"),
         ("part-all", "4", "All rounds")]


def _events():
    """Two rounds in one conversation, each with a system prompt, a user turn, an answer
    and thinking, so every card holds several blocks to open and shut."""
    lines, bodies, answers = [], {}, {}
    history = [{"role": "system", "content": ROOT}, {"role": "user", "content": "why is the build red?"}]
    for seq in (1, 2):
        bodies[seq] = {"model": "iq3_s", "messages": list(history)}
        answers[seq] = ("=== thinking ===\nlooking at the test output\n\n"
                        "=== text ===\nfixed: build.js line 14 uses fs.cpSync")
        lines.append({"type": "request", "seq": seq, "ts": "2026-10-20T10:0%d:00" % seq,
                      "method": "POST", "path": "/v1/chat/completions", "api": "openai-chat",
                      "model": "iq3_s", "stream": False,
                      "headers": {"user-agent": "claude-cli/1.0.28 (external, cli)"},
                      "body_bytes": 900, "est_prompt_tokens": 100 * seq,
                      "payload_file": "payloads/req-%06d.json" % seq})
        lines.append({"type": "response", "seq": seq, "ts": "2026-10-20T10:0%d:05" % seq,
                      "status": 200, "finish_reason": "stop",
                      "usage": {"prompt_tokens": 900 + 100 * seq, "completion_tokens": 12,
                                "prompt_tokens_details": {"cached_tokens": 600 + 50 * seq}},
                      "completion_preview": "fixed: build.js line 14",
                      "response_file": "payloads/resp-%06d.txt" % seq})
        history += [{"role": "assistant", "content": "done: round %d" % seq},
                    {"role": "user", "content": "next step?"}]
    return lines, bodies, answers


def _part(html, part_id):
    """The markup of one part, from its opening tag up to the opening tag of the next
    part (or to </main> for the last one). The round cards are sections too, so a part
    ends where the next one starts rather than at its own closing tag."""
    start = html.index('<section class="part" id="%s"' % part_id)
    nxt = html.find('<section class="part"', start + 10)
    return html[start:nxt if nxt > -1 else html.index('</main>')]


def _cards(html):
    """The text of each round card, keyed by its round number."""
    return {int(seq): body for seq, body
            in re.findall(r'<section class="card[^"]*" id="r(\d+)"[^>]*>(.*?)</section>', html, re.S)}


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxlayout-")
        os.makedirs(os.path.join(self.dir, "payloads"))
        lines, bodies, answers = _events()
        with open(os.path.join(self.dir, "requests.jsonl"), "w", encoding="utf-8") as handle:
            for entry in lines:
                handle.write(json.dumps(entry) + "\n")
        for seq, body in bodies.items():
            with open(os.path.join(self.dir, "payloads", "req-%06d.json" % seq),
                      "w", encoding="utf-8") as handle:
                json.dump(body, handle)
        for seq, text in answers.items():
            with open(os.path.join(self.dir, "payloads", "resp-%06d.txt" % seq),
                      "w", encoding="utf-8") as handle:
                handle.write(text)
        ctxpage._cap(self.dir)
        reqs, resps, order = ctxpage.load()
        self.html = ctxpage.render_html(reqs, resps, order, 4000, 0, 204800, "the test window", 10)
        self.cards = _cards(self.html)

    def tearDown(self):
        ctxpage.CAP = None
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------- the header KPI cards
    def test_the_header_figures_are_kpi_cards_not_one_long_sentence(self):
        """The header used to spell every figure out in one sentence to read through. Each
        figure is a card of its own now: a label, the number, and a short note."""
        header = self.html[self.html.index("<header>"):self.html.index("</header>")]
        self.assertIn('<div class="kpis">', header)
        cards = re.findall(r'<div class="kpi([^"]*)"><span>([^<]+)</span><b>([^<]*)</b>'
                           r'(?:<i>([^<]*)</i>)?</div>', header)
        figures = {label: value for _, label, value, _ in cards}
        for want in ("rounds logged", "context window", "prompt tokens",
                     "reused from cache", "read new", "answer tokens"):
            self.assertIn(want, figures, "no KPI card for %s" % want)
        self.assertEqual("2", figures["rounds logged"])
        self.assertEqual("204,800", figures["context window"])
        self.assertEqual("2,100", figures["prompt tokens"])
        self.assertEqual("1,350", figures["reused from cache"])
        self.assertEqual("750", figures["read new"])
        self.assertEqual("24", figures["answer tokens"])
        self.assertNotIn("rounds logged &middot;", header, "the sentence is still there")
        self.assertTrue(all(note and note.strip() for _, _, _, note in cards),
                        "a KPI card carries no note saying what the figure means")

    def test_the_kpi_cards_colour_the_two_halves_of_the_context_apart(self):
        """Reused and read new are the two halves the bars are drawn in, so the cards use
        the same two colours - under their own names, since .reuse/.new are the bars'."""
        header = self.html[self.html.index("<header>"):self.html.index("</header>")]
        self.assertIn('<div class="kpi grn"><span>reused from cache</span>', header)
        self.assertIn('<div class="kpi amb"><span>read new</span>', header)
        self.assertIn(".kpi.grn b { color:var(--reuse) } .kpi.amb b { color:var(--new) }", self.html)

    def test_the_bar_legend_sits_with_the_bars_rather_than_in_the_header(self):
        """The header is cards now, so the one line that explains the bar colours moved
        down to the part whose table draws them."""
        header = self.html[self.html.index("<header>"):self.html.index("</header>")]
        rounds = _part(self.html, "part-rounds")
        self.assertNotIn("bars are drawn against", header)
        self.assertIn("bars are drawn against", rounds)
        self.assertLess(rounds.index("bars are drawn against"), rounds.index('<table id="rounds">'),
                        "the legend is not above the table it explains")

    def test_the_threads_part_carries_no_wall_of_explaining(self):
        """The paragraph that explained how conversations are grouped, hashed and named is
        out; the heading says what the part is, and the table speaks for itself."""
        threads = _part(self.html, "part-threads")
        self.assertNotIn("rounds are grouped by what they sent", self.html)
        self.assertNotIn('<div class="sub legend">', threads)
        self.assertIn('<table class="threads">', threads)

    # ------------------------------------------------------------------ the four parts
    def test_the_page_is_split_into_four_labelled_parts(self):
        """Issue #24 asks for four parts: threads, rounds, round details and all rounds."""
        ids = re.findall(r'<section class="part" id="([^"]+)"', self.html)
        self.assertEqual([part for part, _, _ in PARTS], ids)
        for part_id, number, title in PARTS:
            head = re.search(r'<section class="part" id="%s"[^>]*>\s*<h2[^>]*>'
                             r'<span class="pn">%s</span>([^<]*)<span class="what">([^<]*)</span></h2>'
                             % (part_id, number), self.html)
            self.assertIsNotNone(head, "part %s has no numbered heading" % part_id)
            self.assertEqual(title, head.group(1), "part %s is not called %s" % (part_id, title))
            self.assertTrue(head.group(2).strip(), "part %s has no line saying what it holds" % part_id)

    def test_the_four_parts_hold_the_thing_their_name_promises(self):
        threads = _part(self.html, "part-threads")
        rounds = _part(self.html, "part-rounds")
        detail = _part(self.html, "detail")
        every = _part(self.html, "part-all")
        self.assertIn('<table class="threads">', threads)
        self.assertIn('<table id="rounds">', rounds)
        self.assertIn('<div class="pager">', rounds)
        self.assertIn('<div id="focus">', detail)
        self.assertEqual([], re.findall(r'<table', detail), "the details part holds a table")
        self.assertEqual([1, 2], sorted(_cards(every)), "the all-rounds part does not hold the cards")
        self.assertNotIn('<section class="card', threads + rounds + detail)

    def test_the_parts_stay_in_the_order_the_page_already_had(self):
        """The issue keeps the order: conversations, then the round table, then the
        selected round below it, then every round."""
        places = [self.html.index('<section class="part" id="%s"' % part) for part, _, _ in PARTS]
        self.assertEqual(sorted(places), places)
        self.assertLess(self.html.index('<table class="threads">'),
                        self.html.index('<div class="picker">'),
                        "the picker no longer sits between the threads and the rounds")
        self.assertLess(self.html.index('<table id="rounds">'), places[2],
                        "the round details part no longer follows the round table")

    def test_the_round_details_part_is_hidden_only_until_the_script_runs(self):
        """Part 3 is the whole part now, not just its heading, so an empty part leaves no
        empty box in the page - and the script brings the part back with a round in it."""
        self.assertIn('<section class="part" id="detail" style="display:none">', self.html)
        self.assertIn("detail = document.getElementById('detail')", self.html)
        self.assertIn("detail.style.display = v ? '' : 'none';", self.html)
        self.assertNotIn("ftitle.style.display", self.html)
        self.assertIn("ftitle.scrollIntoView({ behavior: 'smooth', block: 'start' });", self.html)

    def test_the_page_opens_on_the_first_round_of_the_table(self):
        """Issue #32: the round details window is not left invisible until the reader picks
        a round. The page opens it on the first round the table lists - the same round the
        picker starts on - and it does so without dragging the view down to part 3."""
        self.assertIn("function show(v, jump){", self.html)
        self.assertIn("show(order.length ? order[0] : '', false);", self.html)
        # order is what the table, the picker and the paging are filled from, so the page
        # can only open a round out of it once those are filled
        self.assertLess(self.html.index("\nfilterThread();"),
                        self.html.index("show(order.length ? order[0] : '', false);"),
                        "the page opens a round before the table and the picker are filled")
        # the first round of the table is the first round of the picker too, which is what
        # order[0] means to both of them
        self.assertEqual(re.search(r'<tr data-seq="(\d+)"', self.html).group(1),
                         re.search(r'<option value="(\d+)"', self.html).group(1),
                         "the table and the picker do not start on the same round")
        # only that load-time show stays put; every round the reader picks still jumps
        # the view to the heading of part 3
        self.assertIn("if(jump !== false){ ftitle.scrollIntoView", self.html)
        self.assertIn("pick.addEventListener('change', function(){ show(pick.value); });",
                      self.html)
        self.assertIn("if(row) show(row.getAttribute('data-seq'));", self.html)

    # ---------------------------------------------------------------- the shut blocks
    def test_every_block_of_every_round_starts_shut(self):
        """Issue #24: the all-rounds window defaults to collapsed, so nothing on the page
        is open before the reader asks for it."""
        self.assertNotIn("<details open>", self.html)
        for seq, card in self.cards.items():
            blocks = re.findall(r"<details( open)?>", card)
            self.assertGreater(len(blocks), 1, "round %d has nothing to open" % seq)
            self.assertFalse(any(b for b in blocks), "round %d starts open" % seq)

    def test_the_blocks_are_still_the_same_blocks_the_copy_button_reads(self):
        """Shutting them by default changes what is open, not what is there: every card
        still carries both columns and their summaries."""
        for seq, card in self.cards.items():
            self.assertIn('<h3>the exact context that was sent', card)
            self.assertIn('<h3>what the model answered', card)
            self.assertIn('<summary><span class="role">', card)

    # ------------------------------------------ a shut round is one row of key figures
    def test_every_round_is_written_folded_into_its_key_row_to_begin_with(self):
        """Issue #24 asks the collapsed round to be a single row of key columns, not only a
        stack of shut summaries: the card carries that row and is written folded, so the
        page reads as a list of rounds before any script runs."""
        self.assertEqual(len(self.cards), self.html.count('<section class="card shut" id="r'),
                         "a round does not start folded into its row")
        for seq, card in self.cards.items():
            self.assertIn('<div class="keyrow">', card, "round %d has no key row" % seq)

    def test_the_key_row_holds_the_columns_that_matter(self):
        """The row carries the figures the table row holds: the thread, what the round cost,
        its share of the window, the two halves of it, the answer, the bar and the result."""
        for seq, card in self.cards.items():
            row = re.search(r'<div class="keyrow">(.*?)<div class="cardtools">', card, re.S).group(1)
            self.assertIn('<span class="k">thread <b>', row, "round %d's row has no thread" % seq)
            self.assertIn('<div class="bar"', row, "round %d's row has no bar" % seq)
            self.assertIn('<span class="badge ok">200</span>', row,
                          "round %d's row has no HTTP result" % seq)
            # the row never disagrees with the figures the opened card shows
            kv = dict(re.findall(r'<div><span>([^<]+)</span><b[^>]*>([^<]*)</b></div>', card))
            keys = dict(re.findall(r'<span class="k[^"]*">([a-z ]+) <b>([^<]*)</b></span>', row))
            for column, label in {
                    "context": "context this round", "of window": "of the window",
                    "reused": "cache reused", "read new": "read new",
                    "answer": "answer tokens"}.items():
                self.assertIn(column, keys, "round %d's row has no %s column" % (seq, column))
                self.assertEqual(kv[label], keys[column],
                                 "round %d's row disagrees with its own card on %s" % (seq, label))

    def test_the_key_row_sits_in_the_head_of_the_card(self):
        """One row means one line: the row shares the card head with the round number and
        the two buttons, so a folded card is a single line rather than a heading over a row."""
        for seq, card in self.cards.items():
            head = card[:card.index('<p class="line">')]
            self.assertLess(head.index('class="seq"'), head.index('class="keyrow"'),
                            "round %d's key row is not after its number" % seq)
            self.assertLess(head.index('class="keyrow"'), head.index('class="cardtools"'),
                            "round %d's key row is not before its buttons" % seq)

    def test_the_card_head_still_names_its_round(self):
        """The card tag gained a data-thread for the filter (issue #33); the number in the
        head of the card is still the round, not the thread that shares the tag."""
        for seq, card in self.cards.items():
            self.assertIn('<span class="seq">#%d</span>' % seq, card)

    def test_the_script_folds_a_shut_round_back_into_its_row(self):
        """Shutting a round - with its own button or with the part-wide one - shrinks the
        card, and the same click in reverse brings the card back."""
        self.assertIn("function paintCard(card){", self.html)
        self.assertIn("card.classList.toggle('shut', list.length > 0 && !everyOpen(list));", self.html)
        self.assertIn("paintCard(btn.closest ? btn.closest('.card') : null);", self.html)
        self.assertIn("part.querySelectorAll('.card'), paintCard);", self.html)

    def test_only_the_rounds_of_the_all_rounds_part_ever_fold(self):
        """Part 3 is the round you picked, and it is there to be read in full: it keeps its
        height with every block shut, and its clone is written without the folded class."""
        for rule in re.findall(r"([^{}]*\.card\.shut[^{}]*)\{", self.html):
            self.assertIn("#part-all", rule, "a fold rule reaches outside the all-rounds part")
        self.assertIn(".keyrow { display:none;", self.html)
        self.assertIn("#part-all .card.shut .keyrow { display:flex }", self.html)
        self.assertIn("copy.classList.remove('shut');", self.html)

    # ------------------------------------------------- the collapse / expand-all button
    def test_the_all_rounds_part_carries_one_button_at_its_top(self):
        """Issue #24 asks for the button at the top of the window, above the rounds it
        speaks for, and for that button only once."""
        every = _part(self.html, "part-all")
        self.assertEqual(1, every.count('class="togglebtn expandall wide"'))
        self.assertEqual(1, self.html.count('class="togglebtn expandall wide"'))
        self.assertLess(every.index('expandall'), every.index('<section class="card'),
                        "the button is not above the first round")
        self.assertLess(every.index('<h2'), every.index('expandall'),
                        "the button is not under the part's heading")
        self.assertIn('<div class="parttools">', every)

    def test_the_other_parts_carry_no_button_of_their_own(self):
        for part_id in ("part-threads", "part-rounds", "detail"):
            self.assertNotIn("expandall", _part(self.html, part_id))

    def test_the_button_carries_the_icon_pair_of_the_round_buttons_and_a_word(self):
        """Same icon language as issue #22, plus the word, because it speaks for every
        round in the part rather than for one card."""
        button = re.search(r'<button class="togglebtn expandall wide"[^>]*>(.*?)</button>',
                           self.html, re.S).group(1)
        self.assertIn(ctxpage.EXPAND_ICON, button)
        self.assertIn(ctxpage.COLLAPSE_ICON, button)
        self.assertEqual('<span class="word">expand all</span>',
                         re.search(r'<span class="word">.*?</span>', button).group(0))

    def test_the_button_names_itself_by_what_the_click_is_about_to_do(self):
        button = re.search(r'<button class="togglebtn expandall wide"[^>]*>', self.html).group(0)
        self.assertIn('title="expand every block of every round in this part"', button)
        self.assertIn('aria-label="expand every block of every round in this part"', button)
        self.assertNotIn(' class="togglebtn expandall wide all"', button,
                         "a part whose rounds are all shut does not offer to collapse them")
        self.assertIn("'collapse every round in this part back to one row of its key figures'",
                      self.html)

    def test_each_round_keeps_its_own_expand_button(self):
        """Issue #24 says to keep the per-round button: the part-wide one is in addition
        to it, not instead of it."""
        for seq, card in self.cards.items():
            self.assertEqual(1, len(re.findall(r'<button class="togglebtn(?: all)?"', card)),
                             "round %d lost its own expand button" % seq)
        self.assertIn("e.target.closest('button.togglebtn')", self.html)
        self.assertIn("e.target.closest('button.expandall')", self.html)

    # ---------------------------------------------------------------------- the script
    def test_the_script_opens_and_shuts_every_block_of_its_own_part(self):
        self.assertIn("function blocksIn(part){", self.html)
        self.assertIn("part.querySelectorAll('.card')", self.html)
        self.assertIn("function everyOpen(list){", self.html)
        self.assertIn("if (!list[i].open) { return false; }", self.html)
        self.assertIn("var part = btn.closest('.part');", self.html)
        self.assertIn("var open = !everyOpen(list);", self.html)
        self.assertIn("Array.prototype.forEach.call(list, function (d) { d.open = open; });",
                      self.html)
        # it reads the blocks rather than remembering a state, so opening a round by hand
        # keeps the part-wide button honest
        self.assertIn("paintPartButtons(part);", self.html)
        self.assertIn("paintAllBtn(b, everyOpen(blocksIn(part)));", self.html)

    def test_the_button_only_ever_touches_the_part_it_sits_in(self):
        """The selected round above is a clone of a card, and it keeps its own pair of
        buttons; the part-wide button reaches only the cards of its own part."""
        self.assertIn("var part = btn.closest('.part');", self.html)
        self.assertIn("blocksIn(part)", self.html)
        self.assertNotIn("document.querySelectorAll('.card')", self.html)
        self.assertIn("if (!btn || btn.classList.contains('expandall')) { return; }", self.html,
                      "the round handler would answer for the part-wide button too")

    def test_the_script_repaints_the_button_after_opening_or_shutting(self):
        self.assertIn("btn.classList.toggle('all', open);", self.html)
        self.assertIn("word.textContent = open ? 'collapse all' : 'expand all';", self.html)
        self.assertIn("btn.setAttribute('aria-label', label);", self.html)

    def test_css_paints_one_icon_at_a_time_for_both_buttons(self):
        """The part-wide button shares the icon swap of the round buttons, and takes its
        extra room from the wide class."""
        self.assertIn(".togglebtn svg.min { display:none }", self.html)
        self.assertIn(".togglebtn.all svg.max { display:none }", self.html)
        self.assertIn(".togglebtn.all svg.min { display:block }", self.html)
        self.assertIn(".togglebtn.wide { gap:7px; padding:6px 11px }", self.html)
        self.assertIn(".part > h2 .pn {", self.html)
        self.assertIn(".parttools {", self.html)


if __name__ == "__main__":
    unittest.main()
