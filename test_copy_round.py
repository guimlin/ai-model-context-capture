"""The copy button in the round detail window.

The round page (ctxpage.render_html) puts a "copy" button in the head of every round
card, and the window below the table is a clone of the picked card, so the
button is in that detail window too. The page's own script turns the card the button
sits in into plain text and puts it on the clipboard. These tests check what the page
is built with: the button in every card, the script that does the copying, and that
every piece of markup the script reads is really in the cards it reads them from.

    python -m unittest test_copy_round
"""
import json
import os
import re
import shutil
import tempfile
import unittest

import ctxpage

ROOT_A = "You are a coding agent working in C:/demo/src. Keep diffs small."
ROOT_B = "You are an agent in a different repository, answering in one line."


def _events():
    """Four rounds in two conversations, each with Strata's counts and an answer file."""
    lines = []
    histories = {
        "A": [{"role": "system", "content": ROOT_A},
              {"role": "user", "content": "why is the build red?"}],
        "B": [{"role": "system", "content": ROOT_B},
              {"role": "user", "content": "add a dry-run flag."}],
    }
    senders = {
        "A": {"user-agent": "claude-cli/1.0.28 (external, cli)", "x-app": "cli"},
        "B": {"user-agent": "codex_cli_rs/0.46.0 (Mac OS 14.0.0)"},
    }
    bodies, answers = {}, {}
    seq = 0
    for thread, turns in (("A", 2), ("B", 2)):
        for _ in range(turns):
            seq += 1
            bodies[seq] = {"model": "iq3_s", "messages": list(histories[thread])}
            answers[seq] = ("=== thinking ===\nlooking at the test output\n\n"
                            "=== text ===\nfixed: build.js line 14 uses fs.cpSync")
            lines.append({"type": "request", "seq": seq, "ts": "2026-10-20T10:%02d:00" % seq,
                          "method": "POST", "path": "/v1/chat/completions", "api": "openai-chat",
                          "model": "iq3_s", "stream": False, "headers": senders[thread],
                          "body_bytes": 900,
                          "est_prompt_tokens": 100 * seq,
                          "payload_file": "payloads/req-%06d.json" % seq})
            lines.append({"type": "response", "seq": seq, "ts": "2026-10-20T10:%02d:05" % seq,
                          "status": 200, "finish_reason": "stop",
                          "usage": {"prompt_tokens": 900 + 100 * seq, "completion_tokens": 12,
                                    "prompt_tokens_details": {"cached_tokens": 600 + 50 * seq}},
                          "completion_preview": "fixed: build.js line 14",
                          "response_file": "payloads/resp-%06d.txt" % seq})
            histories[thread] += [{"role": "assistant", "content": "done: round %d" % seq},
                                  {"role": "user", "content": "next step?"}]
    return lines, bodies, answers


def _cards(html):
    """The text of each round card, keyed by its round number."""
    return {int(seq): body for seq, body
            in re.findall(r'<section class="card" id="r(\d+)">(.*?)</section>', html, re.S)}


def _fields(card):
    """The hidden field list a card carries: {label: value}, in the order it is written."""
    block = re.search(r'<pre class="roundfields" hidden>(.*?)</pre>', card, re.S)
    if not block:
        return {}
    rows = {}
    for line in block.group(1).split("\n"):
        label, _, value = line.partition(": ")
        rows[label] = value
    return rows


class CopyRoundTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxcopy-")
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

    def test_every_round_card_carries_a_copy_button(self):
        self.assertEqual([1, 2, 3, 4], sorted(self.cards))
        self.assertEqual(4, self.html.count('<button class="copybtn" type="button"'))
        for seq, card in self.cards.items():
            self.assertIn('title="Copy every field of this round as plain text">copy</button>',
                          card, "round %d has no copy button" % seq)

    def test_the_button_sits_in_the_head_of_the_card(self):
        for seq, card in self.cards.items():
            head = card.split('<div class="kv">')[0]
            self.assertIn('</span><button class="copybtn"', head,
                          "round %d's button is not beside its title" % seq)

    def test_the_detail_window_is_a_clone_of_the_card_so_it_carries_the_button(self):
        """The window below the table copies the whole card, button and all."""
        self.assertIn("var card = document.getElementById('r' + v);", self.html)
        self.assertIn("var copy = card.cloneNode(true);", self.html)
        self.assertIn("focus.appendChild(copy);", self.html)

    def test_the_script_turns_a_card_into_text_and_copies_it(self):
        self.assertIn("function roundText(card)", self.html)
        self.assertIn("function copyText(text, done)", self.html)
        self.assertIn("card.querySelector('pre.roundfields')", self.html)
        self.assertIn("navigator.clipboard.writeText(text)", self.html)
        # a file:// page or an unfocused frame can refuse the clipboard API
        self.assertIn("document.execCommand('copy')", self.html)
        # one listener on the document, so it catches the card and its clone alike
        self.assertIn("e.target.closest('button.copybtn')", self.html)
        self.assertIn("btn.closest('.card')", self.html)
        self.assertIn("copyText(roundText(card)", self.html)
        # the button says what it did, then goes back to its own name
        self.assertIn("btn.textContent = ok ? 'copied' : 'copy failed'", self.html)
        self.assertIn("btn.textContent = 'copy'", self.html)

    def test_the_copy_text_carries_every_field_of_the_round(self):
        """The card holds a hidden list of its own fields, which is what the copy button
        writes out first: the thread, the counts, the window, who sent it, and where the
        uncut text lives."""
        fields = _fields(self.cards[2])
        self.assertEqual("#2", fields["round"])
        self.assertEqual("A", fields["thread"])
        self.assertEqual("continues #1: 4 messages, 2 shared, 2 new", fields["in the conversation"])
        self.assertEqual("200", fields["http result"])
        self.assertEqual("stop", fields["finish"])
        self.assertEqual("1,100 tokens", fields["context this round"])
        self.assertEqual("204,800 tokens (the test window)", fields["context window"])
        self.assertEqual("700 tokens", fields["cache reused"])
        self.assertEqual("400 tokens", fields["read new"])
        self.assertEqual("12", fields["answer tokens"])
        self.assertIn("room left in window", fields)
        self.assertIn("growth vs previous round", fields)
        self.assertIn("percent of window", fields)
        self.assertIn("request size", fields)
        self.assertIn("messages sent", fields)
        self.assertIn("root prompt", fields)
        self.assertIn("tool definitions", fields)
        self.assertIn("who sent it", fields)
        self.assertIn("claude-cli/1.0.28", fields["who sent it"])
        self.assertEqual("next step?", fields["asked this round"])
        self.assertEqual("payloads/req-000002.json", fields["request payload file"])
        self.assertEqual("payloads/resp-000002.txt", fields["answer payload file"])

    def test_the_first_round_of_a_thread_says_so_in_its_fields(self):
        fields = _fields(self.cards[1])
        self.assertEqual("thread A starts here, 2 messages", fields["in the conversation"])
        self.assertEqual("why is the build red?", fields["asked this round"])
        self.assertNotIn("growth vs previous round", fields)
        self.assertEqual("none", fields["tool calls back"])

    def test_the_field_list_is_hidden_on_the_page(self):
        for seq, card in self.cards.items():
            self.assertIn('<pre class="roundfields" hidden>', card,
                          "round %d has no field list" % seq)
        self.assertIn(".roundfields { display:none }", self.html)

    def test_the_copy_text_reads_the_field_list_and_the_two_columns(self):
        """roundText writes the field list, then the blocks of each column; a card that
        stops carrying one of them would copy a round with a piece missing."""
        for seq, card in self.cards.items():
            for piece in ('<pre class="roundfields" hidden>', '<div class="cols">', '<h3>',
                          '<summary><span class="role">', '<span class="size">', '<pre>'):
                self.assertIn(piece, card, "round %d's card has no %s" % (seq, piece))
            self.assertIn('<span class="role">system prompt</span>', card)
            self.assertIn('<span class="role">#0 user</span>', card)
            self.assertIn('<span class="role">thinking</span>', card)
            self.assertIn("fixed: build.js line 14 uses fs.cpSync", card)

    def test_a_round_without_counts_says_so_rather_than_guessing(self):
        """A round whose answer never came back still gets a full field list; the
        fields that have no value say why instead of carrying a made-up number."""
        req = {"seq": 9, "ts": "2026-10-20T10:09:00", "method": "POST",
               "path": "/v1/chat/completions", "model": "iq3_s", "body_bytes": 500,
               "payload_file": "payloads/req-000009.json"}
        view = {"prompt": None, "cached": 0, "out": None, "est": 300, "api": "openai-chat",
                "pieces": [], "chars": 900, "cplit": 0.0, "kb": 0.5, "finish": None}
        link = {"thread": "C", "parent": None, "n_msgs": 2, "shared": 0, "added": None,
                "client": "-", "n_tools": 0, "root_chars": 0, "root_fp": "",
                "forked": [], "same_history": []}
        rows = dict(ctxpage.round_fields(9, req, None, view, link, 0, "", 300, None, 0))
        self.assertEqual("thread C starts here, 2 messages", rows["in the conversation"])
        self.assertEqual("no answer recorded", rows["http result"])
        self.assertEqual("300 tokens (estimated: Strata sent no count back)",
                         rows["context this round"])
        self.assertEqual("not known: the page scaled this round on the biggest round",
                         rows["context window"])
        self.assertEqual("no token count came back for this round", rows["cache reused"])
        self.assertEqual("not counted", rows["answer tokens"])
        self.assertNotIn("room left in window", rows)
        self.assertNotIn("growth vs previous round", rows)
        self.assertNotIn("characters per token", rows)
        self.assertNotIn("who sent it", rows)
        self.assertNotIn("asked this round", rows)

    def test_the_button_is_styled_with_the_rest_of_the_card(self):
        self.assertIn(".copybtn { margin-left:auto", self.html)
        self.assertIn(".copybtn.done", self.html)
        self.assertIn(".copybtn.bad", self.html)


if __name__ == "__main__":
    unittest.main()
