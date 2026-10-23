"""How fast the model went, per round and per thread (issue #46).

Only the proxy can know how long a round took, so it measures the round with a clock that
does not jump when the machine's clock is corrected and writes `duration_ms` and `ttft_ms`
on the response line of the log. ctxstore turns those two and the model's own token counts
into the two figures the issue asks for: how fast the model read its prompt (prefill, the
prompt tokens up to the first answer token) and how fast it wrote the answer (decode, the
answer tokens after it). The Context page then shows them in the rounds table, on the round
card, in the threads table, in the fields the copy button writes out and in the Markdown
report. These tests cover all three links: the maths, what the proxy really writes down for
a streamed answer, a one-piece answer and an unreachable upstream, and what the page shows -
including a dash rather than an invented number for a round logged before timing existed.

    python -m unittest test_round_metrics
"""
import http.client
import io
import json
import os
import re
import shutil
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

import context_proxy
import ctxpage
import ctxstore


def _round(ms=None, ttft=None, prompt=1000, out=200, req_ts=None, resp_ts=None):
    """One round as the log holds it: a request line, a response line, the model's own
    token counts, and whichever of the two timings the proxy managed to write down."""
    req = {"type": "request", "seq": 1}
    resp = {"type": "response", "seq": 1}
    if req_ts:
        req["ts"] = req_ts
    if resp_ts:
        resp["ts"] = resp_ts
    if ms is not None:
        resp["duration_ms"] = ms
    if ttft is not None:
        resp["ttft_ms"] = ttft
    resp["usage"] = {"prompt_tokens": prompt, "completion_tokens": out}
    return req, resp


class RoundMathsTests(unittest.TestCase):
    """The two figures of one round, worked out of the log."""

    def test_a_streamed_round_splits_reading_from_writing(self):
        req, resp = _round(ms=4200, ttft=200, prompt=1000, out=200)
        m = ctxstore.round_metrics(req, resp)
        self.assertEqual(4200.0, m["ms"])
        self.assertEqual(200.0, m["ttft"])
        self.assertEqual(1000 / 0.2, m["prefill"])        # 5,000 tok/s of prompt read
        self.assertEqual(200 / 4.0, m["decode"])          # 50 tok/s of answer written
        self.assertTrue(m["split"])
        self.assertIsNone(m["whole"])

    def test_an_answer_that_arrives_in_one_piece_gives_only_a_floor(self):
        """No first token means no split: the overall rate counts the reading too, so it
        is a floor on the writing rather than the writing itself."""
        req, resp = _round(ms=2000, prompt=1100, out=100)
        m = ctxstore.round_metrics(req, resp)
        self.assertIsNone(m["prefill"])
        self.assertIsNone(m["decode"])
        self.assertEqual(100 / 2.0, m["whole"])
        self.assertFalse(m["split"])

    def test_a_round_the_log_never_timed_measures_nothing(self):
        req, resp = _round(prompt=1200, out=60)
        m = ctxstore.round_metrics(req, resp)
        self.assertIsNone(m["ms"])
        self.assertIsNone(m["prefill"])
        self.assertIsNone(m["decode"])
        self.assertIsNone(m["whole"])

    def test_an_older_log_with_only_whole_second_stamps_still_gives_the_length(self):
        """A log written before the proxy timed rounds carries the request and response
        `ts` stamps, which say how long the round took but never when the first token came
        out, so only the floor is available."""
        req, resp = _round(prompt=1000, out=150,
                           req_ts="2026-10-20T10:01:00", resp_ts="2026-10-20T10:01:03")
        m = ctxstore.round_metrics(req, resp)
        self.assertEqual(3000.0, m["ms"])
        self.assertIsNone(m["ttft"])
        self.assertEqual(150 / 3.0, m["whole"])

    def test_a_round_inside_one_second_of_the_old_log_is_left_unmeasured(self):
        """Whole-second stamps make a quick round look like it took no time at all, and a
        rate built on that would be nonsense."""
        req, resp = _round(prompt=1000, out=150,
                           req_ts="2026-10-20T10:01:00", resp_ts="2026-10-20T10:01:00")
        self.assertEqual((None, None), ctxstore.round_times(req, resp))

    def test_a_log_that_disagrees_with_itself_measures_nothing(self):
        """A first token after the answer finished cannot have happened; the split goes
        away and the round falls back to what its length alone can say."""
        req, resp = _round(ms=1000, ttft=1500, prompt=1000, out=100)
        m = ctxstore.round_metrics(req, resp)
        self.assertIsNone(m["ttft"])
        self.assertIsNone(m["prefill"])
        self.assertEqual(100 / 1.0, m["whole"])

    def test_junk_in_the_timing_fields_measures_nothing(self):
        req, resp = _round(prompt=1000, out=100)
        resp["duration_ms"] = "4s"
        resp["ttft_ms"] = True
        self.assertEqual((None, None), ctxstore.round_times(req, resp))

    def test_a_round_with_no_answer_tokens_has_a_reading_rate_but_no_writing_rate(self):
        req, resp = _round(ms=800, ttft=300, prompt=900, out=0)
        m = ctxstore.round_metrics(req, resp)
        self.assertEqual(900 / 0.3, m["prefill"])
        self.assertIsNone(m["decode"])

    def test_a_round_whose_answer_is_still_missing_has_no_response_line_at_all(self):
        req, _resp = _round(prompt=1000, out=100)
        m = ctxstore.round_metrics(req, None)
        self.assertIsNone(m["ms"])
        self.assertEqual(0, m["out"])


class ThreadMathsTests(unittest.TestCase):
    """The speed of a whole conversation, from the rounds of it the log can time."""

    def test_the_thread_rate_weighs_the_rounds_by_the_time_they_took(self):
        """Not an average of the two rates: the tokens of the rounds over the seconds they
        took, so the round that spent nine tenths of a second reading counts for that."""
        fast = _round(ms=1000, ttft=100, prompt=1000, out=100)
        slow = _round(ms=1000, ttft=900, prompt=1000, out=10)
        reqs = {1: fast[0], 2: slow[0]}
        resps = {1: fast[1], 2: slow[1]}
        m = ctxstore.thread_metrics([1, 2], reqs, resps)
        self.assertEqual(2, m["rounds"])
        self.assertEqual(2000.0, m["ms"])
        self.assertEqual(2000 / 1.0, m["prefill"])        # not (10000 + 1111) / 2
        self.assertEqual(110 / 1.0, m["decode"])          # 110 answer tokens, 1.0 s writing

    def test_rounds_the_log_cannot_time_are_left_out(self):
        timed = _round(ms=1000, ttft=200, prompt=1000, out=100)
        blind = _round(prompt=1000, out=100)
        reqs = {1: timed[0], 2: blind[0], 3: timed[0]}
        resps = {1: timed[1], 3: timed[1]}
        m = ctxstore.thread_metrics([1, 2, 3], reqs, resps)
        self.assertEqual(2, m["rounds"])
        self.assertEqual(2000.0, m["ms"])
        self.assertEqual(2000 / 0.4, m["prefill"])

    def test_a_thread_of_both_kinds_shows_both_figures(self):
        """A streamed round can be split; a one-piece answer cannot. The thread reports
        the split rounds as prefill and decode and gathers the rest as a floor."""
        split = _round(ms=1000, ttft=200, prompt=1000, out=100)
        whole = _round(ms=2000, prompt=1100, out=100)
        reqs = {1: split[0], 2: whole[0]}
        resps = {1: split[1], 2: whole[1]}
        m = ctxstore.thread_metrics([1, 2], reqs, resps)
        self.assertEqual(1000 / 0.2, m["prefill"])
        self.assertEqual(100 / 0.8, m["decode"])
        self.assertEqual(100 / 2.0, m["whole"])
        self.assertEqual(1, m["whole_rounds"])

    def test_a_thread_of_nothing_measurable_says_so(self):
        blind = _round(prompt=1000, out=100)
        m = ctxstore.thread_metrics([1], {1: blind[0]}, {1: blind[1]})
        self.assertEqual(0, m["rounds"])
        self.assertIsNone(m["prefill"])
        self.assertIsNone(m["decode"])
        self.assertIsNone(m["whole"])


class _Upstream(socketserver.BaseRequestHandler):
    """A model server slow enough for the proxy to see the first token arrive: a role
    delta that carries no answer, then the answer in two pieces, then the usage. The
    one-piece variant answers with a plain JSON body instead, which has no first token
    for the proxy to notice."""
    STREAM = True

    def handle(self):
        conn = self.request
        rfile = conn.makefile("rb")
        rfile.readline()                       # the request line the proxy sent on
        headers = {}
        while True:
            line = rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        rfile.read(int(headers.get("content-length") or 0))
        if self.STREAM:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Connection: close\r\n\r\n")
            for piece, nap in ((None, 0.25), ("Hel", 0.15), ("lo", 0.0)):
                if nap:
                    time.sleep(nap)
                delta = {"role": "assistant"} if piece is None else {"content": piece}
                conn.sendall(b'data: {"id":"c1","choices":[{"delta":'
                             + json.dumps(delta).encode() + b"}]}\r\n\r\n")
            time.sleep(0.15)                   # the usage rides in after the answer
            conn.sendall(b'data: {"usage":{"prompt_tokens":1000,"completion_tokens":200}}\r\n\r\n'
                         b"data: [DONE]\r\n\r\n")
        else:
            time.sleep(0.2)
            body = json.dumps({"choices": [{"message": {"content": "hello"}}],
                               "usage": {"prompt_tokens": 1000,
                                         "completion_tokens": 200}}).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        conn.close()


class ProxyWritesTimingsTests(unittest.TestCase):
    """What the proxy puts in the log for a round it watched go by."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxtiming-")
        os.makedirs(os.path.join(self.dir, "payloads"))
        self._state = dict(context_proxy._state)
        self._counter = context_proxy._counter
        context_proxy._state.update(dir=self.dir, upstream=None, proxy=None,
                                    store_max_bytes=0, log_all=False)
        context_proxy._counter = 0
        self.up = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Upstream)
        self.up.daemon_threads = True
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.proxy = context_proxy.Server(("127.0.0.1", 0), context_proxy.ProxyHandler)
        context_proxy._state["upstream"] = "http://127.0.0.1:%d" % self.up.server_address[1]
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        for srv in (self.proxy, self.up):
            srv.shutdown()
            srv.server_close()
        context_proxy._state.clear()
        context_proxy._state.update(self._state)
        context_proxy._counter = self._counter
        shutil.rmtree(self.dir, ignore_errors=True)

    def _round_trip(self, stream=True):
        """One round through the proxy, and the two log lines it left behind."""
        body = json.dumps({"model": "iq3_s", "stream": stream,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_address[1], timeout=20)
        try:
            conn.request("POST", "/v1/chat/completions", body=body,
                         headers={"content-type": "application/json"})
            conn.getresponse().read()
        finally:
            conn.close()
        with open(os.path.join(self.dir, "requests.jsonl"), encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        return ([e for e in lines if e.get("type") == "request"][0],
                [e for e in lines if e.get("type") == "response"][0])

    def test_a_streamed_round_writes_down_its_length_and_its_first_token(self):
        with redirect_stdout(io.StringIO()):
            req, resp = self._round_trip(stream=True)
        self.assertEqual(200, resp["status"])
        took, first = resp["duration_ms"], resp["ttft_ms"]
        self.assertIsInstance(took, int)
        self.assertIsInstance(first, int)
        self.assertGreaterEqual(first, 150, "the first token was not waited for")
        self.assertGreaterEqual(took, 400, "the round ended before its last piece")
        self.assertLess(first, took, "the first token came after the answer finished")
        self.assertIn("first token after", resp["summary"])

        m = ctxstore.round_metrics(req, resp)
        self.assertTrue(m["split"])
        self.assertAlmostEqual(1000 / (first / 1000.0), m["prefill"], places=1)
        self.assertAlmostEqual(200 / ((took - first) / 1000.0), m["decode"], places=1)

    def test_an_answer_that_arrives_in_one_piece_leaves_the_first_token_out(self):
        _Upstream.STREAM = False
        try:
            with redirect_stdout(io.StringIO()):
                req, resp = self._round_trip(stream=False)
        finally:
            _Upstream.STREAM = True
        self.assertIsNone(resp["ttft_ms"])
        self.assertGreaterEqual(resp["duration_ms"], 150)
        self.assertNotIn("first token after", resp["summary"])
        m = ctxstore.round_metrics(req, resp)
        self.assertFalse(m["split"])
        self.assertIsNone(m["prefill"])
        self.assertTrue(m["whole"])

    def test_a_round_the_upstream_never_answered_still_says_how_long_it_waited(self):
        """A round that never reached a model is a round too: the log says how long the
        wait for nothing was."""
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        context_proxy._state["upstream"] = "http://127.0.0.1:%d" % port
        with redirect_stdout(io.StringIO()):
            _req, resp = self._round_trip(stream=True)
        self.assertEqual(502, resp["status"])
        self.assertIsInstance(resp["duration_ms"], int)
        self.assertIn("502 upstream unreachable after", resp["summary"])


ROOT = ("You are a coding agent working in C:/demo/src. Keep diffs small. "
        "Read the failing test before you touch anything. " * 20)


def _events():
    """Three rounds of one conversation, answered three different ways: the first in
    pieces, so its reading and its writing were both timed; the second in one piece, so
    only a floor on its writing is known; the third went by before the proxy wrote any
    timing down at all."""
    lines, bodies, answers = [], {}, {}
    history = [{"role": "system", "content": ROOT},
               {"role": "user", "content": "why is the build red?"}]
    timings = [
        {"duration_ms": 4200, "ttft_ms": 200, "prompt": 1000, "out": 200, "cached": 600},
        {"duration_ms": 2000, "prompt": 1100, "out": 100, "cached": 700},
        # the third round finished inside the second its request went out, which is all an
        # old log could say about it even if it were one, and it carries no timings either
        {"prompt": 1200, "out": 60, "cached": 800, "resp_ts": "2026-10-20T10:03:00"},
    ]
    for seq, t in enumerate(timings, start=1):
        bodies[seq] = {"model": "iq3_s", "messages": list(history)}
        answers[seq] = "=== text ===\nfixed: build.js line 14 uses fs.cpSync"
        lines.append({"type": "request", "seq": seq, "ts": "2026-10-20T10:0%d:00" % seq,
                      "method": "POST", "path": "/v1/chat/completions", "api": "openai-chat",
                      "model": "iq3_s", "stream": seq == 1,
                      "headers": {"user-agent": "claude-cli/1.0.28 (external, cli)"},
                      "body_bytes": 900, "est_prompt_tokens": 900 + 100 * seq,
                      "payload_file": "payloads/req-%06d.json" % seq})
        entry = {"type": "response", "seq": seq,
                 "ts": t.get("resp_ts") or "2026-10-20T10:0%d:05" % seq,
                 "status": 200, "finish_reason": "stop",
                 "usage": {"prompt_tokens": t["prompt"], "completion_tokens": t["out"],
                           "prompt_tokens_details": {"cached_tokens": t["cached"]}},
                 "completion_preview": "fixed: build.js line 14",
                 "response_file": "payloads/resp-%06d.txt" % seq}
        for key in ("duration_ms", "ttft_ms"):
            if key in t:
                entry[key] = t[key]
        lines.append(entry)
        history += [{"role": "assistant", "content": "done: round %d" % seq},
                    {"role": "user", "content": "next step?"}]
    return lines, bodies, answers


def _part(html, part_id):
    """The markup of one part of the page, from its opening tag to the next part."""
    start = html.index('<section class="part" id="%s"' % part_id)
    nxt = html.find('<section class="part"', start + 10)
    return html[start:nxt if nxt > -1 else html.index('</main>')]


def _cards(html):
    """The text of each round card, keyed by its round number."""
    return {int(seq): body for seq, body
            in re.findall(r'<section class="card[^"]*" id="r(\d+)"[^>]*>(.*?)</section>',
                          html, re.S)}


def _row(html, seq):
    """One row of the rounds table."""
    return re.search(r'<tr data-seq="%d"[^>]*>(.*?)</tr>' % seq, html, re.S).group(1)


def _fields(card):
    """The hidden field list a card carries: {label: value}."""
    block = re.search(r'<pre class="roundfields" hidden>(.*?)</pre>', card, re.S)
    if not block:
        return {}
    rows = {}
    for line in block.group(1).split("\n"):
        label, _, value = line.partition(": ")
        rows[label] = value
    return rows


class PageShowsSpeedTests(unittest.TestCase):
    """The figures the issue asked for, where the page shows them."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxspeed-")
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
        self.reqs, self.resps, self.order = ctxpage.load()
        self.html = ctxpage.render_html(self.reqs, self.resps, self.order,
                                        4000, 0, 204800, "the test window", 10)
        self.cards = _cards(self.html)
        self.md = ctxpage.render_md(self.reqs, self.resps, self.order, 4000, 204800)

    def tearDown(self):
        ctxpage.CAP = None
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------------ the rounds table
    def test_the_rounds_table_has_a_column_for_the_two_rates(self):
        rounds = _part(self.html, "part-rounds")
        self.assertIn(">tokens/s</th>", rounds, "the rounds table has no tokens/s column")
        self.assertIn("prefill", rounds, "the legend does not say what prefill means")
        self.assertIn("decode", rounds, "the legend does not say what decode means")

    def test_a_streamed_round_shows_its_reading_rate_and_its_writing_rate(self):
        row = _row(self.html, 1)
        self.assertIn('<span class="tps">', row)
        self.assertIn('class="tps-p"', row)
        self.assertIn("5,000", row, "prefill of 1,000 prompt tokens in 0.2 s is not 5,000")
        self.assertIn('class="tps-d"', row)
        self.assertIn("50.0", row, "decode of 200 answer tokens in 4.0 s is not 50.0")
        self.assertIn("prefill: 1,000 prompt tokens in 0.2 s", row,
                      "the figure does not say what it was worked out of")

    def test_a_one_piece_answer_shows_a_floor_on_its_writing(self):
        """The reading time is counted inside that rate, so the page says at least, not
        equal."""
        row = _row(self.html, 2)
        self.assertIn("&ge;50.0", row)
        self.assertNotIn("tps-p", row, "a round with no first token cannot show a prefill")

    def test_a_round_that_was_never_timed_shows_a_dash_not_a_number(self):
        row = _row(self.html, 3)
        self.assertIn('<span class="dim">&ndash;</span>', row)
        self.assertNotIn("tps-p", row)
        self.assertNotIn("tps-d", row)

    # --------------------------------------------------------------------- the round card
    def test_the_round_card_holds_the_length_and_both_rates_as_figures_of_their_own(self):
        card = self.cards[1]
        self.assertIn("<span>round took</span><b title=", card)
        self.assertIn(">4.2 s</b>", card)
        self.assertIn('<span>prefill tok/s</span><b><span title="prefill: 1,000 prompt tokens'
                      ' in 0.2 s">5,000</span></b>', card)
        self.assertIn('<span>decode tok/s</span><b><span title="decode: 200 answer tokens'
                      ' in 4.0 s">50.0</span></b>', card)
        self.assertIn("took <b>4.2 s</b>", card, "the folded round does not show its length")
        self.assertIn('tok/s <span class="tps">', card, "the folded round does not show rates")

    def test_a_card_of_a_one_piece_answer_marks_its_rate_a_floor(self):
        self.assertIn('<span>decode tok/s</span><b><span title="the answer came in one piece'
                      ', so reading and writing cannot be told apart: 100 answer tokens over'
                      ' the whole 2.0 s of the round">&ge;50.0</span></b>', self.cards[2])
        self.assertIn('<span>prefill tok/s</span><b><span class="dim">&ndash;</span></b>',
                      self.cards[2])

    def test_a_card_of_an_untimed_round_says_when_it_was_logged(self):
        card = self.cards[3]
        self.assertIn("<span>round took</span><b title=", card)
        self.assertIn(">not timed</b>", card)
        self.assertIn("this round went by before the proxy started timing rounds", card)
        self.assertIn('<span>prefill tok/s</span><b><span class="dim">&ndash;</span></b>', card)

    # ----------------------------------------------------------------- the threads table
    def test_the_threads_table_shows_the_speed_of_a_whole_conversation(self):
        threads = _part(self.html, "part-threads")
        self.assertIn(">tokens/s</th>", threads, "the threads table has no tokens/s column")
        self.assertIn("5,000", threads, "the thread's prefill is not its prompt tokens over"
                                        " the time to their first answer tokens")
        self.assertIn("50.0", threads)
        self.assertIn("over 2 rounds", threads,
                      "the figure does not say how many of the thread's rounds it covers")

    # ------------------------------------------------------------------- the copy fields
    def test_the_copy_fields_carry_the_length_and_both_rates(self):
        fields = _fields(self.cards[1])
        self.assertEqual("4.2 s", fields["round took"])
        self.assertEqual("5,000 (1,000 prompt tokens in 0.2 s, up to the first answer token)",
                         fields["prefill tokens per second"])
        self.assertEqual("50.0 (200 answer tokens in 4.0 s, after the first answer token)",
                         fields["decode tokens per second"])

    def test_a_field_with_no_rate_says_why_instead_of_leaving_it_out(self):
        one_piece = _fields(self.cards[2])
        self.assertEqual("2.0 s", one_piece["round took"])
        self.assertIn("cannot be told apart", one_piece["prefill tokens per second"])
        self.assertEqual("at least 50.0 (100 answer tokens over the whole 2.0 s of the round)",
                         one_piece["decode tokens per second"])
        untimed = _fields(self.cards[3])
        for label in ("round took", "prefill tokens per second", "decode tokens per second"):
            self.assertIn("before the proxy wrote timings down", untimed[label],
                          "%s says nothing about why it is missing" % label)

    # --------------------------------------------------------------------- the Markdown
    def test_the_markdown_report_carries_the_same_figures(self):
        self.assertIn("- speed: prefill 5,000, decode 50.0 tok/s, round took 4.2 s", self.md)
        self.assertIn("- speed: at least 50.0 tok/s, round took 2.0 s", self.md)
        self.assertIn("| thread | rounds | first - last | who sent it | root prompt | tools"
                      " | tokens/s | what changed |", self.md)
        self.assertIn("prefill 5,000, decode 50.0", self.md)

    def test_the_markdown_leaves_the_speed_line_out_of_a_round_it_cannot_time(self):
        self.assertEqual(2, self.md.count("- speed:"),
                         "the untimed round has no length to work a rate out from")

    # ---------------------------------------------------------------------------- the CSS
    def test_the_two_rates_are_tinted_apart_from_each_other(self):
        self.assertIn(".tps-p { color:var(--accent) }", self.html)
        self.assertIn(".tps-d { color:var(--reuse) }", self.html)


if __name__ == "__main__":
    unittest.main()
