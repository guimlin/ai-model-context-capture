"""The expand-all / collapse-all button in the round detail window.

Issue #22 asks for one button in the head of every round card, sitting left of the copy
icon and carrying an icon rather than a word: it opens every block of that round at a
click, and shuts them all on the next. The round page (ctxpage.render_html) builds the
button into each card and the page's own script does the opening and shutting, so these
tests check what the page is built with: the button and its two icons in every card, the
rules that swap the icons, and the script that reads and sets the blocks of the card the
button sits in.

    python -m unittest test_expand_round
"""
import json
import os
import re
import shutil
import tempfile
import unittest

import ctxpage

# Every block starts shut now (issue #24), so the length of this text no longer decides
# anything; it only has to be long enough to be worth opening.
ROOT = ("You are a coding agent working in C:/demo/src. Keep diffs small. "
        "Read the failing test before you touch anything. " * 30)


def _events():
    """Two rounds in one conversation, each with a system prompt, a user turn, an
    answer, and thinking, so every card holds several blocks to open and shut."""
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


def _cards(html):
    """The text of each round card, keyed by its round number."""
    return {int(seq): body for seq, body
            in re.findall(r'<section class="card[^"]*" id="r(\d+)"[^>]*>(.*?)</section>', html, re.S)}


def _button(card):
    """The whole toggle button markup of a card, or None when it has none."""
    return re.search(r'<button class="togglebtn[^"]*"[^>]*>.*?</button>', card, re.S)


def _inside(card):
    """What is between the button's tags: the icons it carries, and nothing else."""
    return re.search(r'<button class="togglebtn[^"]*"[^>]*>(.*?)</button>', card, re.S).group(1)


def _round_buttons(html):
    """The toggle buttons of the round cards. The part-wide button added by issue #24
    shares the togglebtn class, so its own expandall class is what tells them apart."""
    return re.findall(r'<button class="togglebtn(?: all)?"', html)


class ExpandRoundTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxexpand-")
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

    def test_every_round_card_carries_one_toggle_button(self):
        self.assertEqual([1, 2], sorted(self.cards))
        self.assertEqual(2, len(_round_buttons(self.html)))
        for seq, card in self.cards.items():
            self.assertEqual(1, card.count('<button class="togglebtn'),
                             "round %d does not have exactly one toggle button" % seq)

    def test_the_button_sits_left_of_the_copy_button_in_the_head(self):
        """Issue #22 asks for it beside the copy icon, on its left."""
        for seq, card in self.cards.items():
            head = card.split('<div class="kv">')[0]
            toggle = head.find('<button class="togglebtn')
            copy = head.find('<button class="copybtn"')
            self.assertGreater(toggle, -1, "round %d has no toggle button in its head" % seq)
            self.assertGreater(copy, toggle, "round %d's toggle is not left of its copy" % seq)
            self.assertIn('</button><button class="copybtn"', head,
                          "round %d's two buttons are not beside each other" % seq)

    def test_the_button_carries_icons_and_no_word(self):
        """The issue asks for an icon instead of text: the button holds the two icons of
        the pair and nothing else, so it stays as small as the copy button beside it."""
        for seq, card in self.cards.items():
            self.assertEqual(ctxpage.EXPAND_ICON + ctxpage.COLLAPSE_ICON, _inside(card),
                             "round %d's button is not made of the two icons alone" % seq)

    def test_the_icons_are_drawn_in_the_same_hand_as_the_copy_icon(self):
        """Issue #21 settled the icon language of the site: 16x16, no fill, currentColor
        stroke. The expand pair is drawn the same way, so the three buttons match."""
        frame = ('viewBox="0 0 16 16" width="13" height="13" fill="none"'
                 ' stroke="currentColor" stroke-width="1.4"')
        for icon in (ctxpage.EXPAND_ICON, ctxpage.COLLAPSE_ICON):
            self.assertIn(frame, icon)
            self.assertIn('aria-hidden="true"', icon)
        self.assertIn('viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"',
                      ctxpage.COPY_ICON)
        self.assertIn(ctxpage.EXPAND_ICON, self.html)
        self.assertIn(ctxpage.COLLAPSE_ICON, self.html)

    def test_the_icon_only_button_still_names_itself(self):
        """An icon carries no word, so the accessible name comes from title and aria-label,
        and the script keeps them in step with what the button is about to do."""
        for seq, card in self.cards.items():
            markup = _button(card).group(0)
            self.assertIn('title="expand every block in this round"', markup)
            self.assertIn('aria-label="expand every block in this round"', markup)
        self.assertIn("btn.title = label;", self.html)
        self.assertIn("btn.setAttribute('aria-label', label);", self.html)
        self.assertIn("'collapse every block in this round'", self.html)
        self.assertIn("'expand every block in this round'", self.html)

    def test_css_shows_one_icon_at_a_time(self):
        """Both icons are in the button; the .all class on the button is what swaps them,
        and it has to come after the shut-blocks rule to win."""
        shut = self.html.index('.togglebtn svg.min { display:none }')
        swap = self.html.index('.togglebtn.all svg.max { display:none }')
        shown = self.html.index('.togglebtn.all svg.min { display:block }')
        self.assertLess(shut, swap, "the swap loses to the default")
        self.assertLess(swap, shown, "the open icon is not put back on top")
        self.assertIn(".copybtn, .togglebtn { background:#1b2029", self.html)
        self.assertIn(".copybtn svg, .togglebtn svg { display:block }", self.html)

    def test_the_button_starts_out_named_for_what_it_is_about_to_do(self):
        """The page writes the button already painted: a card with a shut block offers to
        open them all, a card whose blocks are all open offers to shut them."""
        for seq, card in self.cards.items():
            shut = "<details>" in card
            label = ("expand every block in this round" if shut
                     else "collapse every block in this round")
            markup = _button(card).group(0)
            self.assertIn('title="%s"' % label, markup,
                          "round %d's button does not offer to %s" % (seq, label))
            self.assertIn('aria-label="%s"' % label, markup)
            self.assertEqual(shut, 'class="togglebtn"' in markup,
                             "round %d's icon is the wrong way round" % seq)

    def test_the_script_opens_and_shuts_every_block_of_its_own_card(self):
        for seq, card in self.cards.items():
            blocks = re.findall(r'<details( open)?>', card)
            self.assertGreater(len(blocks), 1, "round %d has nothing to expand" % seq)
            # issue #24 shut every block of every round by default, so a card always
            # starts with nothing open
            self.assertFalse(any(b for b in blocks),
                             "round %d does not start with every block shut" % seq)
        self.assertIn("function blocksOf(card){ return card.querySelectorAll('details'); }", self.html)
        self.assertIn("function everyBlockOpen(card)", self.html)
        self.assertIn("if (!list[i].open) { return false; }", self.html)
        self.assertIn("Array.prototype.forEach.call(blocksOf(card), function (d) { d.open = open; });",
                      self.html)
        # the button reads the blocks rather than remembering a state of its own, so
        # opening or shutting one by hand keeps the icon and the name honest
        self.assertIn("var open = !everyBlockOpen(card);", self.html)
        self.assertIn("btn.classList.toggle('all', open);", self.html)

    def test_the_script_catches_the_card_and_its_clone_alike(self):
        """The window below the table is a clone of a card, so the listener is on the
        document and finds the card the clicked button actually sits in."""
        self.assertIn("e.target.closest('button.togglebtn')", self.html)
        self.assertIn("var card = btn.closest('.card');", self.html)
        self.assertIn("var copy = card.cloneNode(true);", self.html)


if __name__ == "__main__":
    unittest.main()
