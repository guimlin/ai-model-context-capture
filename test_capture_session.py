"""Tests for issue #38, "No new capture are saved": a capture session gets its own log
file when it starts, the rounds the capture before it recorded are saved away rather than
buried under the new ones, and stopping a capture writes down what it saved.

    python test_capture_session.py
"""
import json
import os
import tempfile
import unittest
import warnings

import context_proxy
import ctxstore
import ctxweb


class FakeProc:
    """Stand in for the capture process: alive until something terminates it."""

    def __init__(self):
        self.pid = 4242
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        self.dead = True


def write_round(log_dir, seq, ts="2025-03-04T09:00:00+01:00"):
    """One round in a log folder, the way the capture writes it: a request line, an
    answer line, and the prompt text in payloads."""
    payloads = os.path.join(log_dir, "payloads")
    os.makedirs(payloads, exist_ok=True)
    with open(os.path.join(log_dir, "requests.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "request", "seq": seq, "ts": ts, "api": True,
                                 "method": "POST", "path": "/v1/chat/completions",
                                 "summary": "POST /v1/chat/completions"}) + "\n")
        handle.write(json.dumps({"type": "response", "seq": seq, "ts": ts, "api": True,
                                 "summary": "200 OK"}) + "\n")
    with open(os.path.join(payloads, "req-%06d.json" % seq), "w", encoding="utf-8") as handle:
        handle.write('{"messages": [{"role": "user", "content": "round %d"}]}' % seq)


def log_lines(log_dir):
    with open(os.path.join(log_dir, "requests.jsonl"), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_manifest(folder):
    with open(os.path.join(folder, "session.json"), encoding="utf-8") as handle:
        return json.load(handle)


class SessionCase(unittest.TestCase):
    """The page with its logs in a temp folder and no real capture process."""

    def setUp(self):
        self.saved = dict(ctxweb.CFG)
        self.fns = {name: getattr(ctxweb, name)
                    for name in ("proxy_state", "listening_ports", "find_upstream")}
        self.saved_popen = ctxweb.subprocess.Popen
        self.tmp = tempfile.TemporaryDirectory()
        ctxweb.CFG["log_dir"] = os.path.join(self.tmp.name, "logs")
        ctxweb.CFG["source"] = ""
        ctxweb.CFG["proxy_port"] = 8788
        ctxweb.CFG["upstream"] = ""
        ctxweb.find_upstream = lambda again=False: []
        ctxweb.listening_ports = lambda any_address=False: []
        ctxweb._proc = None
        ctxweb._session.clear()
        ctxweb._session_cache["key"] = None
        ctxweb._session_cache["info"] = None
        ctxweb._status_log.clear()
        self._log_target = context_proxy._state["dir"]
        context_proxy._state["dir"] = ctxweb.CFG["log_dir"]
        self.started = []
        self.capturing = set()
        self.procs = []
        ctxweb.proxy_state = self._proxy_state
        ctxweb.subprocess.Popen = self._popen
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)

    def tearDown(self):
        for name, fn in self.fns.items():
            setattr(ctxweb, name, fn)
        ctxweb.subprocess.Popen = self.saved_popen
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)
        ctxweb._proc = None
        ctxweb._session.clear()
        ctxweb._session_cache["key"] = None
        ctxweb._status_log.clear()
        context_proxy._state["dir"] = self._log_target
        self.tmp.cleanup()

    def _proxy_state(self, port=None):
        port = port or ctxweb.CFG["proxy_port"]
        if port in self.capturing and any(not proc.dead for proc in self.procs):
            return {"port": port, "ours": ctxweb._proc is not None, "upstream": "http://x/v1"}
        return None

    def _popen(self, command, **kwargs):
        self.started.append(command)
        self.capturing.add(int(command[command.index("--port") + 1]))
        proc = FakeProc()
        self.procs.append(proc)
        return proc

    @property
    def log_dir(self):
        return ctxweb.CFG["log_dir"]

    def sessions(self):
        try:
            return sorted(os.listdir(ctxweb.sessions_dir()))
        except OSError:
            return []


class NewLogFilePerCaptureTests(SessionCase):
    """Issue #38 ask 1: start a new capture file when capturing starts."""

    def test_a_started_capture_records_in_a_new_log_file(self):
        write_round(self.log_dir, 1)
        write_round(self.log_dir, 2)
        result = ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(log_lines(self.log_dir), [])

    def test_the_rounds_before_it_are_saved_away_not_lost(self):
        write_round(self.log_dir, 1)
        write_round(self.log_dir, 2)
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        folder = os.path.join(ctxweb.sessions_dir(), self.sessions()[0])
        self.assertEqual(len(log_lines(folder)), 4)
        self.assertTrue(os.path.isfile(os.path.join(folder, "payloads", "req-000001.json")))
        _reqs, _resps, order = ctxstore._events(ctxstore.Capture(folder))
        self.assertEqual(order, [1, 2])
        self.assertEqual(read_manifest(folder)["rounds"], 2)

    def test_the_page_says_where_the_earlier_rounds_went(self):
        write_round(self.log_dir, 1)
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        notes = [line[1] for line in ctxweb.status_log()]
        self.assertTrue(any("new capture file" in note for note in notes), notes)
        self.assertTrue(any("saved in" in note for note in notes), notes)

    def test_a_new_capture_starts_its_rounds_at_1(self):
        write_round(self.log_dir, 7)
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        self.assertEqual(ctxweb.api_state()["session"]["rounds"], 0)
        self.assertEqual(ctxweb.api_state()["session"]["first_seq"], None)

    def test_an_empty_log_starts_no_new_file(self):
        ctxweb.ensure_live_log()
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        self.assertEqual(self.sessions(), [])

    def test_a_log_folder_that_does_not_exist_yet_starts_no_new_file(self):
        self.assertEqual(ctxweb.rotate_live_log(), None)
        self.assertEqual(self.sessions(), [])

    def test_a_capture_that_is_already_running_starts_no_new_file(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        write_round(self.log_dir, 1)
        again = ctxweb.start_capture({})
        self.assertTrue(again.get("already"), again)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(len(log_lines(self.log_dir)), 2)

    def test_a_log_of_only_a_start_line_is_left_where_it_is(self):
        ctxweb.ensure_live_log()
        ctxweb.append_log_event({"type": "session", "start": True, "ts": ctxweb._now(),
                                 "summary": "capture started -> http://x/v1"})
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        self.assertEqual(self.sessions(), [])
        self.assertEqual(len(log_lines(self.log_dir)), 1)

    def test_two_captures_started_in_the_same_second_each_get_their_own_folder(self):
        write_round(self.log_dir, 1)
        ctxweb.rotate_live_log()
        write_round(self.log_dir, 1)
        ctxweb.rotate_live_log()
        self.assertEqual(len(self.sessions()), 2)
        for name in self.sessions():
            self.assertEqual(len(log_lines(os.path.join(ctxweb.sessions_dir(), name))), 2)

    def test_the_live_log_folder_is_never_left_without_a_log(self):
        write_round(self.log_dir, 1)
        ctxweb.rotate_live_log()
        self.assertTrue(os.path.isfile(os.path.join(self.log_dir, "requests.jsonl")))
        self.assertEqual(ctxweb.api_state()["source"]["rounds"], 0)


class StoppingSavesTheSessionTests(SessionCase):
    """Issue #38 ask 2: save what was captured when capturing stops."""

    def test_stopping_writes_down_in_the_log_what_it_saved(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        write_round(self.log_dir, 1)
        write_round(self.log_dir, 2)
        result = ctxweb.stop_capture()
        self.assertTrue(result["ok"], result)
        end = log_lines(self.log_dir)[-1]
        self.assertEqual(end["type"], "session")
        self.assertTrue(end["end"])
        self.assertIn("2 round(s) saved", end["summary"])

    def test_stopping_writes_a_session_file_beside_the_log(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        write_round(self.log_dir, 1)
        ctxweb.stop_capture()
        manifest = read_manifest(self.log_dir)
        self.assertEqual(manifest["rounds"], 1)
        self.assertEqual(manifest["first_seq"], 1)
        self.assertEqual(manifest["last_seq"], 1)
        self.assertTrue(manifest["started"])
        self.assertTrue(manifest["stopped"])
        self.assertEqual(manifest["upstream"], "http://127.0.0.1:47831/v1")

    def test_stopping_a_capture_that_recorded_nothing_adds_nothing_to_the_log(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        ctxweb.stop_capture()
        self.assertEqual(log_lines(self.log_dir), [])

    def test_a_capture_that_was_never_started_saves_nothing(self):
        ctxweb.ensure_live_log()
        result = ctxweb.stop_capture()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["saved"], None)
        self.assertFalse(os.path.exists(os.path.join(self.log_dir, "session.json")))

    def test_the_next_capture_saves_away_what_the_stopped_one_recorded(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        write_round(self.log_dir, 1)
        ctxweb.stop_capture()
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        self.assertEqual(len(self.sessions()), 1)
        folder = os.path.join(ctxweb.sessions_dir(), self.sessions()[0])
        self.assertEqual(read_manifest(folder)["rounds"], 1)
        self.assertEqual(log_lines(self.log_dir), [])

    def test_the_session_file_of_a_stopped_capture_travels_with_its_log(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        write_round(self.log_dir, 1)
        stopped = ctxweb.stop_capture()["saved"]["stopped"]
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        self.assertFalse(os.path.exists(os.path.join(self.log_dir, "session.json")))
        folder = os.path.join(ctxweb.sessions_dir(), self.sessions()[0])
        manifest = read_manifest(folder)
        self.assertEqual(manifest["rounds"], 1)
        self.assertEqual(manifest["stopped"], stopped)

    def test_the_page_answers_how_many_rounds_the_session_saved(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        write_round(self.log_dir, 1)
        write_round(self.log_dir, 2)
        session = ctxweb.api_state()["session"]
        self.assertEqual(session["rounds"], 2)
        self.assertEqual(session["first_seq"], 1)
        self.assertEqual(session["last_seq"], 2)
        self.assertTrue(session["started"])
        self.assertEqual(session["upstream"], "http://127.0.0.1:47831/v1")
        self.assertEqual(session["log"], os.path.join(self.log_dir, "requests.jsonl"))
        self.assertEqual(session["saved"], 0)


class SessionLinesTests(SessionCase):
    """The start and stop lines live in the log itself, and are never rounds."""

    def test_the_capture_writes_a_start_line_into_its_own_log(self):
        ctxweb.ensure_live_log()
        entry = context_proxy.log_session_start("http://127.0.0.1:47831/v1",
                                                "http://127.0.0.1:8788", 0)
        self.assertEqual(log_lines(self.log_dir), [entry])
        self.assertIn("new log: rounds start at 1", entry["summary"])

    def test_a_log_that_carries_on_says_where_the_numbering_continues(self):
        ctxweb.ensure_live_log()
        entry = context_proxy.log_session_start("http://x/v1", "http://127.0.0.1:8788", 7)
        self.assertIn("log continues at seq 7", entry["summary"])

    def test_the_tail_box_shows_the_capture_coming_and_going(self):
        ctxweb.ensure_live_log()
        context_proxy.log_session_start("http://x/v1", "http://127.0.0.1:8788", 0)
        write_round(self.log_dir, 1)
        ctxweb.append_log_event({"type": "session", "end": True, "ts": ctxweb._now(),
                                 "rounds": 1, "summary": "capture stopped - 1 round saved"})
        summaries = [line["summary"] for line in ctxweb.api_log({"tail": 20})["lines"]]
        self.assertIn("capture started -> http://x/v1, new log: rounds start at 1", summaries)
        self.assertIn("capture stopped - 1 round saved", summaries)

    def test_a_session_line_is_never_counted_as_a_round(self):
        ctxweb.ensure_live_log()
        ctxweb.append_log_event({"type": "session", "end": True, "ts": ctxweb._now(),
                                 "rounds": 0, "summary": "capture stopped - 0 rounds saved"})
        self.assertEqual(ctxweb.api_state()["source"]["rounds"], 0)
        self.assertEqual(ctxweb.api_state()["live_rounds"], 0)


class AdoptedCaptureTests(SessionCase):
    """The page sometimes finds a capture already running that it did not start itself.
    The capture's own start line in the log still says when that session ran."""

    def test_the_start_line_the_capture_wrote_says_when_the_session_ran(self):
        ctxweb.ensure_live_log()
        entry = context_proxy.log_session_start("http://127.0.0.1:47831/v1",
                                                "http://127.0.0.1:8788", 0)
        write_round(self.log_dir, 1)
        session = ctxweb.api_state()["session"]
        self.assertEqual(session["started"], entry["ts"])
        self.assertEqual(session["upstream"], "http://127.0.0.1:47831/v1")
        self.assertEqual(session["rounds"], 1)

    def test_its_rounds_are_saved_with_that_start_and_that_model_server(self):
        ctxweb.ensure_live_log()
        entry = context_proxy.log_session_start("http://127.0.0.1:47831/v1",
                                                "http://127.0.0.1:8788", 0)
        write_round(self.log_dir, 1)
        ctxweb.rotate_live_log()
        manifest = read_manifest(os.path.join(ctxweb.sessions_dir(), self.sessions()[0]))
        self.assertEqual(manifest["started"], entry["ts"])
        self.assertEqual(manifest["upstream"], "http://127.0.0.1:47831/v1")
        self.assertEqual(manifest["rounds"], 1)


class SavedSessionsTests(SessionCase):
    """The saved sessions are listed, newest first, and open like any other source."""

    def save_three_sessions(self):
        for seq in (1, 2, 3):
            write_round(self.log_dir, seq)
            ctxweb.rotate_live_log()

    def test_the_list_is_newest_first_and_says_what_each_saved(self):
        self.save_three_sessions()
        listed = ctxweb.api_sessions()
        self.assertEqual(len(listed["sessions"]), 3)
        self.assertEqual(listed["folder"], ctxweb.sessions_dir())
        self.assertEqual([s["manifest"]["rounds"] for s in listed["sessions"]], [1, 1, 1])
        names = [s["name"] for s in listed["sessions"]]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_nothing_is_listed_before_the_first_capture_ends(self):
        self.assertEqual(ctxweb.api_sessions(), {"ok": True, "sessions": [],
                                                "folder": ctxweb.sessions_dir()})

    def test_a_saved_session_folder_opens_like_a_capture_file(self):
        write_round(self.log_dir, 1)
        write_round(self.log_dir, 2)
        ctxweb.rotate_live_log()
        folder = os.path.join(ctxweb.sessions_dir(), self.sessions()[0])
        opened = ctxweb.api_open({"path": folder})
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["kind"], "dir")
        _cap, _reqs, _resps, order, shown, _hidden, _links = ctxweb.view()
        self.assertEqual(order, [1, 2])
        self.assertEqual(len(shown), 2)

    def test_opening_a_session_says_when_that_capture_ran(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        write_round(self.log_dir, 1)
        ctxweb.stop_capture()
        folder = self.log_dir
        cap, reqs, resps, order, shown, hidden, links = ctxweb.view()
        state = ctxweb.describe_source(cap, reqs, resps, order, shown, hidden, links)
        self.assertEqual(state["session"]["rounds"], 1)
        self.assertEqual(state["session"]["upstream"], "http://127.0.0.1:47831/v1")
        self.assertTrue(state["session"]["stopped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
