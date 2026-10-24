"""The Capture Analysis tab: the source being viewed, read out conversation by conversation.

Tab 3 shows the rounds; tab 4 answers what they add up to. A capture is usually several
conversations from several clients, and the aggregate figures hide the interesting part: one
client carries a much bigger standing prompt than another, one round in the middle of a
conversation lost the prompt cache because the client changed its tool set, and a thread's
average reading speed is dragged down by the cold rounds at its start. So the analysis cuts
the source by thread and client and compares them. These tests cover the figures it works
out, the words it draws from them, what the page shows - a dash rather than an invented
number for a log written before the proxy measured time - and the route the monitor page
builds it through.

    python -m unittest test_capture_analysis
"""
import inspect
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import ctxanalysis
import ctxlink
import ctxstore
import ctxweb

ROOT_A = "You are a coding agent working in C:/demo/src. Keep diffs small."
ROOT_B = "You are an agent in a second repository, answering in one line."

VS = {"User-Agent": "GitHubCopilotChat/0.71.0", "x-stainless-runtime": "vscode/1.95"}
APP = {"User-Agent": "OpenAI/JS 5.20.1", "x-stainless-runtime": "node/22.9"}


def _tools(names):
    return [{"type": "function", "function": {"name": name, "description": "the %s tool" % name,
                                              "parameters": {"type": "object"}}}
            for name in names]


def _events(timed=True):
    """Five rounds in two conversations. The VS Code one (1-3) grows a big prompt, loses its
    cache at round 3 and sends one fewer tool from that round on; the app one (4-5) is small
    and steady. `timed=False` is what a log written before the proxy measured time looks like:
    no `duration_ms`/`ttft_ms`, and the two `ts` stamps a second apart are the only hint -
    which is how a round under a second ends up unmeasured."""
    lines, bodies = [], {}
    histories = {
        "A": [{"role": "system", "content": ROOT_A}, {"role": "user", "content": "why is the build red?"}],
        "B": [{"role": "system", "content": ROOT_B}, {"role": "user", "content": "add a dry-run flag."}],
    }
    # seq: (thread, tools, prompt, cached, out, ms, ttft, system, tool defs, history)
    plan = [
        ("A", ["read", "run"], 1000, 0, 100, 4000, 1000, 400, 600, 0),
        ("A", ["read", "run"], 2000, 1900, 120, 3000, 100, 400, 600, 1500),
        ("A", ["read"], 3000, 0, 80, 6000, 3000, 400, 300, 2400),
        ("B", ["read", "run"], 500, 0, 50, 2000, 500, 300, 200, 0),
        ("B", ["read", "run"], 1000, 950, 60, 1500, 100, 300, 200, 1000),
    ]
    for seq, (thread, tools, prompt, cached, out, ms, ttft, sys_t, tool_t, hist) in enumerate(plan, 1):
        body = {"model": "iq3_s", "messages": list(histories[thread]), "tools": _tools(tools)}
        bodies[seq] = body
        request = {"type": "request", "seq": seq, "ts": "2026-10-20T10:%02d:00" % seq,
                   "method": "POST", "path": "/v1/chat/completions", "api": "openai-chat",
                   "model": "iq3_s", "stream": True, "headers": dict(VS if thread == "A" else APP),
                   "body_bytes": 900, "est_prompt_tokens": sys_t + tool_t + hist,
                   "system_tokens": sys_t, "tool_def_tokens": tool_t, "history_tokens": hist,
                   "payload_file": "payloads/req-%06d.json" % seq}
        response = {"type": "response", "seq": seq,
                    "ts": "2026-10-20T10:%02d:%s" % (seq, "05" if timed else "00"),
                    "status": 200, "finish_reason": "stop",
                    "usage": {"prompt_tokens": prompt, "completion_tokens": out,
                              "prompt_tokens_details": {"cached_tokens": cached}},
                    "completion_preview": "answer %d" % seq, "completion_chars": out * 3}
        if timed:
            response["duration_ms"] = ms
            response["ttft_ms"] = ttft
        lines.append(request)
        lines.append(response)
        histories[thread] += [{"role": "assistant", "content": "done: round %d" % seq},
                              {"role": "user", "content": "next step?"}]
    return lines, bodies


def _write_source(dir_, timed=True):
    """The six rounds as a log folder, which is a source the page can be built from."""
    os.makedirs(os.path.join(dir_, "payloads"), exist_ok=True)
    lines, bodies = _events(timed)
    with open(os.path.join(dir_, "requests.jsonl"), "w", encoding="utf-8") as handle:
        for entry in lines:
            handle.write(json.dumps(entry) + "\n")
    for seq, body in bodies.items():
        with open(os.path.join(dir_, "payloads", "req-%06d.json" % seq),
                  "w", encoding="utf-8") as handle:
            json.dump(body, handle)
    return dir_


def _analyse(source, shown=None):
    """The figures of a source, the way the monitor page hands them over: everything the
    view shows, unless a caller names the rounds that are left in."""
    cap = ctxstore.Capture(source)
    reqs, resps, order = ctxstore._events(cap)
    links = ctxlink.attach([reqs[s] for s in order], cap, resps)
    return ctxanalysis.analyze(cap, reqs, resps, order, shown if shown is not None else order,
                               links, 204800, "the test window")


class SourceFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxanalyse-")
        _write_source(self.dir)
        self.data = _analyse(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _thread(self, label):
        return next(b for b in self.data["threads"] if b["label"] == label)


class FiguresOfTwoClients(SourceFixture):
    """The two conversations cut apart, and what each one is made of."""

    def test_the_two_conversations_stay_apart(self):
        overall = self.data["overall"]
        self.assertEqual(5, overall["rounds"])
        self.assertEqual(2, overall["threads"])
        self.assertEqual(7500, overall["prompt"])
        self.assertEqual(2850, overall["cached"])
        self.assertEqual(4650, overall["new"])
        self.assertAlmostEqual(38.0, overall["cache_pct"])
        self.assertEqual(1, overall["cold"])          # one reset: round 3 of the VS Code thread

    def test_the_bigger_conversation_is_the_one_first(self):
        vs, app = self._thread("VS Code Copilot Chat"), self._thread("OpenAI JS client")
        self.assertEqual([1, 2, 3], vs["seqs"])
        self.assertEqual([4, 5], app["seqs"])
        self.assertEqual(6000, vs["prompt"])
        self.assertEqual(1500, app["prompt"])
        self.assertEqual(1000, vs["standing"])        # 400 of system text + 600 of tool defs
        self.assertEqual(500, app["standing"])
        self.assertEqual(1950, vs["history_avg"])

    def test_a_cold_first_round_is_not_reported_as_a_reset(self):
        """The first round of a conversation has nothing to reuse yet, so it is cold without
        saying anything went wrong. Only the cold rounds after it are resets."""
        vs, app = self._thread("VS Code Copilot Chat"), self._thread("OpenAI JS client")
        self.assertEqual([1, 3], vs["cold"])
        self.assertEqual([3], vs["resets"])
        self.assertEqual([4], app["cold"])
        self.assertEqual([], app["resets"])
        self.assertEqual(1, self.data["overall"]["cold"])

    def test_a_tool_set_change_says_what_it_changed_from_and_to(self):
        vs = self._thread("VS Code Copilot Chat")
        self.assertEqual([{"seq": 3, "from": 2, "to": 1}], vs["tool_changes"])
        self.assertEqual([], self._thread("OpenAI JS client")["tool_changes"])
        self.assertEqual(1, self.data["overall"]["tool_changes"])

    def test_the_average_reading_speed_hides_the_warm_rounds(self):
        """The point of the whole tab: a thread's overall prefill is dragged down by its cold
        rounds, so the page reports the warm rounds beside it."""
        vs = self._thread("VS Code Copilot Chat")
        self.assertAlmostEqual(6000 / 4.1, vs["prefill"])      # 1,463 tok/s over all three
        self.assertEqual(1, vs["warm_prefill"]["n"])
        self.assertEqual(20000.0, vs["warm_prefill"]["med"])   # 20,000 tok/s once cached
        self.assertLess(vs["prefill"], vs["warm_prefill"]["med"])


class WhatTheFiguresSay(SourceFixture):
    """The findings: the same figures, in sentences."""

    def _leads(self):
        return [item["lead"] for item in self.data["findings"]]

    def test_two_conversations_get_compared_with_each_other(self):
        comparison = [lead for lead in self._leads() if "times the prompt tokens" in lead]
        self.assertEqual(1, len(comparison))
        self.assertIn("4 times", comparison[0])
        self.assertIn("VS Code Copilot Chat", comparison[0])
        self.assertIn("OpenAI JS client", comparison[0])

    def test_the_smaller_standing_prompt_is_named_against_the_bigger_one(self):
        standing = [item for item in self.data["findings"] if "smaller standing prompt" in item["lead"]]
        self.assertEqual(1, len(standing))
        self.assertIn("OpenAI JS client", standing[0]["lead"])
        self.assertIn("500 estimated tokens", standing[0]["text"])
        self.assertIn("against 1,000 estimated tokens", standing[0]["text"])

    def test_a_lost_cache_and_a_changed_tool_list_each_get_their_own_sentence(self):
        self.assertTrue([lead for lead in self._leads() if "lost its prompt cache" in lead])
        changed = [item for item in self.data["findings"] if "changed its tool list" in item["lead"]]
        self.assertEqual(1, len(changed))
        self.assertIn("2 tool definitions became 1", changed[0]["text"])

    def test_a_thread_that_reads_faster_when_cached_says_so(self):
        self.assertTrue([lead for lead in self._leads() if "reads much faster once" in lead])

    def test_one_conversation_is_read_on_its_own(self):
        """With a single client there is nobody to compare it with, so the comparison and the
        standing-prompt sentences drop out rather than turning into filler."""
        only = _analyse(self.dir, shown=[1, 2, 3])
        self.assertEqual(1, only["overall"]["threads"])
        self.assertFalse([lead for lead in self._leads_of(only) if "times the prompt tokens" in lead])
        self.assertFalse([item for item in only["findings"] if "smaller standing prompt" in item["lead"]])
        self.assertTrue([lead for lead in self._leads_of(only) if "lost its prompt cache" in lead])

    def _leads_of(self, data):
        return [item["lead"] for item in data["findings"]]


class HiddenRoundsLeaveTheAnalysis(SourceFixture):
    """The analysis follows the view: rounds hidden on tab 3 leave its figures too."""

    def test_hiding_the_round_that_changed_the_tools_takes_the_change_with_it(self):
        cut = _analyse(self.dir, shown=[1, 2, 4, 5])
        vs = next(b for b in cut["threads"] if b["label"] == "VS Code Copilot Chat")
        self.assertEqual([1, 2], vs["seqs"])
        self.assertEqual([], vs["tool_changes"])
        self.assertEqual([], vs["resets"])
        self.assertEqual(0, cut["overall"]["tool_changes"])
        self.assertEqual(0, cut["overall"]["cold"])
        self.assertEqual(4500, cut["overall"]["prompt"])


class ALogWithoutTimings(unittest.TestCase):
    """A capture taken before the proxy measured time has no duration and no first token."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxuntimed-")
        _write_source(self.dir, timed=False)
        self.data = _analyse(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_no_number_is_invented_for_a_round_that_was_never_timed(self):
        overall = self.data["overall"]
        self.assertEqual(0, overall["timed"])
        self.assertIsNone(overall["slowest"])
        self.assertEqual({}, overall["ttft"])
        for block in self.data["threads"]:
            self.assertIsNone(block["wall"])
            self.assertIsNone(block["prefill"])
            self.assertIsNone(block["decode"])
            self.assertEqual({}, block["ttft"])

    def test_the_page_says_the_log_carries_no_timings(self):
        page = ctxanalysis.render_html(self.data)
        self.assertIn("&mdash;", page)
        self.assertTrue([item for item in self.data["findings"] if "no timings" in item["lead"]])


class ThePageItself(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxanalysepage-")
        _write_source(self.dir)
        self.page = ctxanalysis.render_html(_analyse(self.dir))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_every_conversation_gets_its_own_block_and_charts(self):
        for wanted in ("VS Code Copilot Chat", "OpenAI JS client", "Side by side",
                       'class="index"', 'class="say"'):
            self.assertIn(wanted, self.page)
        self.assertGreaterEqual(self.page.count("<svg"), 8)   # four charts each, plus the pair

    def test_the_threads_are_listed_under_part_one_and_jump_to_their_blocks(self):
        """The list is the map of the page: each row carries the number of the part it
        jumps to, and that part is really on the page."""
        rows = re.findall(r'<li><a href="#thread-(\d+)"><span class="pn">\1</span>([^<]*)',
                          self.page)
        self.assertEqual([("3", "build-red"), ("4", "add-dry-run-flag")], rows)
        for num, _ in rows:
            self.assertIn('id="thread-%s"' % num, self.page)
        self.assertIn('<div class="card" id="threads">', self.page)
        self.assertIn('<a class="jump" href="#threads">', self.page)
        self.assertIn("3 rounds &middot; 6,000 tok", self.page)
        self.assertIn("1 cache reset", self.page)
        self.assertIn("1 tool change", self.page)

    def test_the_part_numbers_run_once_from_one_to_the_end(self):
        """The thread blocks are numbered on, so the numbers in the list and the numbers
        on the page are the same thing."""
        numbers = re.findall(r'<h2><span class="pn">(\d+)</span>', self.page)
        self.assertEqual(["1", "2", "3", "4", "5"], numbers)

    def test_the_page_is_self_contained(self):
        """It has to open from a file as well as from the monitor page: no script, no CDN."""
        self.assertNotIn("<script", self.page)
        self.assertNotIn("http://", self.page.replace("http://127.0.0.1", ""))
        self.assertIn("<style>", self.page)

    def test_the_header_names_the_file_the_analysis_came_from(self):
        self.assertIn("Capture analysis", self.page)
        self.assertIn(os.path.basename(self.dir), self.page)

    def test_the_client_names_are_readable_not_raw_headers(self):
        self.assertEqual("VS Code Copilot Chat", ctxanalysis.client_label(
            "GitHubCopilotChat/0.71.0 vscode/1.95"))
        self.assertEqual("OpenAI JS client", ctxanalysis.client_label("OpenAI/JS 5.20.1 node/22.9"))
        self.assertEqual("client not shown", ctxanalysis.client_label("-"))


class TheMonitorPageBuildsIt(unittest.TestCase):
    """Tab 4 of the monitor page: the route, the button, and the page it writes."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxanalysisweb-")
        _write_source(self.dir)
        self.web = os.path.join(self.dir, "web")
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
        ctxweb.drop_view()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        ctxweb.CFG["source"], ctxweb.CFG["web_dir"] = self._source, self._web
        ctxweb.drop_view()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_build_writes_the_page_the_tab_shows(self):
        result = ctxweb.build_analysis()
        self.assertTrue(result["ok"])
        self.assertEqual(5, result["rounds"])
        self.assertEqual(2, result["threads"])
        self.assertEqual(os.path.join(self.web, "analysis.html"), result["out"])
        with open(result["out"], encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn("Capture analysis", page)
        self.assertIn("VS Code Copilot Chat", page)
        self.assertEqual(result["bytes"], os.path.getsize(result["out"]))

    def test_the_state_api_reports_when_the_page_was_last_built(self):
        self.assertIsNone(ctxweb.api_state()["analysis"]["built"])
        ctxweb.build_analysis()
        state = ctxweb.api_state()["analysis"]
        self.assertIsNotNone(state["built"])
        self.assertEqual(5, state["rounds"])

    def test_a_source_with_every_round_hidden_says_so_rather_than_writing_an_empty_page(self):
        order = ctxweb.view()[3]
        self.assertTrue(ctxweb.api_hide({"seqs": list(order)})["ok"])
        self.assertFalse(ctxweb.view()[4])
        result = ctxweb.build_analysis()
        self.assertFalse(result["ok"])
        self.assertIn("show hidden again", result["error"])
        self.assertFalse(os.path.exists(os.path.join(self.web, "analysis.html")))

    def test_the_analysis_is_the_fourth_tab_and_reaches_the_page_by_its_own_route(self):
        tabs = re.findall(r'data-tab="([^"]+)">([^<]*)', ctxweb.PAGE)
        self.assertEqual(("analysis", "4 &middot; Capture Analysis"), tabs[-1])
        self.assertIn('<section id="analysis">', ctxweb.PAGE)
        self.assertIn('<iframe id="analysispage" src="/analysis">', ctxweb.PAGE)
        self.assertIn('id="buildanalysis"', ctxweb.PAGE)
        self.assertIn("post('/api/analysis/build')", ctxweb.PAGE)
        # the route itself is server code, not part of the page the browser gets
        self.assertIn('if parts.path == "/analysis"', inspect.getsource(ctxweb.Handler.do_GET))
        self.assertIn('"/api/analysis/build": build_analysis',
                      inspect.getsource(ctxweb.Handler.do_POST))


if __name__ == "__main__":
    unittest.main()
