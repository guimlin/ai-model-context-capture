"""Tests for managing the monitor pages that are running: the list of every page's
port, URL and pid, and the button that stops one of them. Also the query-string
guards of the page's polls, the live round count, and the date-and-time stamp on the
"What just went by" window.

    python test_pages.py
"""
import json
import os
import tempfile
import unittest

import ctxstore
import ctxweb

CIM = """4242|python.exe "C:\\Local Model\\Context Monitor\\ctxweb.py" --port 8790 --proxy-port 8788
9001|python.exe C:\\other\\ctxweb.py
7777|python.exe ctxweb.py --port=8895
"""


class PageProcsTests(unittest.TestCase):
    """The process scan: one 'pid|command line' line per ctxweb.py process."""

    def setUp(self):
        real = ctxweb.subprocess.run
        self.addCleanup(setattr, ctxweb.subprocess, "run", real)
        ctxweb.subprocess.run = lambda *a, **k: type("Out", (), {"stdout": CIM})()

    def test_each_page_process_comes_back_as_pid_and_command(self):
        self.assertEqual(ctxweb.page_procs(), [
            (4242, 'python.exe "C:\\Local Model\\Context Monitor\\ctxweb.py" '
                   '--port 8790 --proxy-port 8788'),
            (9001, "python.exe C:\\other\\ctxweb.py"),
            (7777, "python.exe ctxweb.py --port=8895"),
        ])

    def test_a_scan_that_fails_is_an_empty_list_not_a_crash(self):
        def boom(*a, **k):
            raise OSError("no powershell here")
        ctxweb.subprocess.run = boom
        self.assertEqual(ctxweb.page_procs(), [])


class PagePortTests(unittest.TestCase):
    def test_the_port_the_page_was_started_with(self):
        self.assertEqual(ctxweb.page_port("python ctxweb.py --port 8895 --proxy-port 8788"),
                         8895)
        self.assertEqual(ctxweb.page_port("python ctxweb.py --port=8895"), 8895)

    def test_the_default_when_no_port_was_given(self):
        self.assertEqual(ctxweb.page_port("python ctxweb.py"), ctxweb.DEFAULT_PAGE_PORT)


class MonitorPagesTests(unittest.TestCase):
    def setUp(self):
        real = ctxweb.page_procs
        self.addCleanup(setattr, ctxweb, "page_procs", real)
        ctxweb.page_procs = lambda: [(4242, "python ctxweb.py --port 8790"),
                                     (9001, "python ctxweb.py --port 8895")]

    def test_the_list_carries_port_url_and_pid_sorted_by_port(self):
        pages = ctxweb.monitor_pages()
        self.assertEqual([p["port"] for p in pages], [8790, 8895])
        self.assertEqual([p["pid"] for p in pages], [4242, 9001])
        self.assertEqual(pages[1]["url"], "http://127.0.0.1:8895/")
        self.assertEqual([p["here"] for p in pages], [False, False])

    def test_this_page_is_marked_in_its_own_list(self):
        ctxweb.page_procs = lambda: [(os.getpid(), "python ctxweb.py --port 8790")]
        self.assertTrue(ctxweb.monitor_pages()[0]["here"])


class KillPageTests(unittest.TestCase):
    """The stop button: only a pid that is really a monitor page gets killed."""

    def setUp(self):
        self.saved_procs = ctxweb.page_procs
        self.saved_run = ctxweb.subprocess.run
        self.killed = []
        self.alive = [4242, 9001]
        ctxweb.page_procs = lambda: [(pid, "python ctxweb.py --port 8790")
                                     for pid in self.alive]
        ctxweb.subprocess.run = self._run
        ctxweb._status_log.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        ctxweb.page_procs = self.saved_procs
        ctxweb.subprocess.run = self.saved_run
        ctxweb._status_log.clear()

    def _run(self, command, **kwargs):
        if command[0] == "taskkill":
            pid = int(command[2])
            self.killed.append(pid)
            self.alive = [p for p in self.alive if p != pid]   # the kill took
        return type("Out", (), {"stdout": "", "returncode": 0})()

    def test_a_pid_that_is_not_a_monitor_page_is_refused(self):
        result = ctxweb.api_kill_page({"pid": 5555})
        self.assertFalse(result["ok"])
        self.assertIn("not a monitor page", result["error"])
        self.assertEqual(self.killed, [])

    def test_no_pid_says_so(self):
        self.assertFalse(ctxweb.api_kill_page({})["ok"])
        self.assertFalse(ctxweb.api_kill_page({"pid": "not a number"})["ok"])

    def test_a_page_that_is_running_gets_killed_and_logged(self):
        result = ctxweb.api_kill_page({"pid": 4242})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.killed, [4242])
        self.assertEqual(result["port"], 8790)
        text = "\n".join(line[1] for line in ctxweb.status_log())
        self.assertIn("stopped the monitor page on port 8790 (pid 4242)", text)

    def test_a_page_that_survives_the_kill_is_reported(self):
        ctxweb.subprocess.run = lambda command, **kw: type(
            "Out", (), {"stdout": "", "returncode": 1})()   # taskkill does nothing
        result = ctxweb.api_kill_page({"pid": 4242})
        self.assertFalse(result["ok"])
        self.assertIn("still running", result["error"])

    def test_stopping_this_page_answers_first_and_closes_after(self):
        # killing its own process mid-request would leave the asking tab with no reply,
        # so the kill is scheduled just after the answer goes back
        self.alive.append(os.getpid())
        timers = []

        def fake_timer(delay, fn):
            timers.append([delay, fn])
            return type("T", (), {"start": lambda self: None})()

        real = ctxweb.threading
        self.addCleanup(setattr, ctxweb, "threading", real)
        ctxweb.threading = type("Fake", (), {"Timer": staticmethod(fake_timer)})()
        result = ctxweb.api_kill_page({"pid": os.getpid()})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["this_page"])
        self.assertEqual(self.killed, [])           # nothing killed yet
        timers[0][1]()                              # the timer fires after the reply
        self.assertEqual(self.killed, [os.getpid()])


class QueryIntTests(unittest.TestCase):
    """Junk in a poll's query string falls back to the default instead of 500ing."""

    def test_numbers_come_back_as_numbers(self):
        self.assertEqual(ctxweb._query_int({"limit": ["20"]}, "limit", 60), 20)

    def test_a_missing_or_empty_value_takes_the_default(self):
        self.assertEqual(ctxweb._query_int({}, "limit", 60), 60)
        self.assertEqual(ctxweb._query_int({"limit": [""]}, "limit", 60), 60)

    def test_junk_takes_the_default_instead_of_a_crash(self):
        self.assertEqual(ctxweb._query_int({"limit": ["sixty"]}, "limit", 60), 60)
        self.assertEqual(ctxweb._query_int({"tail": ["5;60"]}, "tail", 14), 14)


class LogRoundsCacheTests(unittest.TestCase):
    """The page polls the live round count every few seconds; the log is only
    re-parsed when its file has actually changed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = dict(ctxweb.CFG)
        self.saved_cache = dict(ctxweb._log_rounds_cache)
        ctxweb.CFG["log_dir"] = self.tmp.name
        with open(os.path.join(self.tmp.name, "requests.jsonl"), "w"):
            pass
        self.calls = []
        real = ctxstore._events
        self.addCleanup(setattr, ctxstore, "_events", real)
        ctxstore._events = lambda cap: (self.calls.append(1) or ([], {}, []))

    def tearDown(self):
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)
        ctxweb._log_rounds_cache.clear()
        ctxweb._log_rounds_cache.update(self.saved_cache)
        self.tmp.cleanup()

    def test_the_log_is_only_re_read_when_the_file_changes(self):
        self.assertEqual(ctxweb.log_rounds(), 0)
        self.assertEqual(ctxweb.log_rounds(), 0)
        self.assertEqual(len(self.calls), 1)        # the second poll hits the cache
        with open(os.path.join(self.tmp.name, "requests.jsonl"), "a",
                  encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertEqual(ctxweb.log_rounds(), 0)
        self.assertEqual(len(self.calls), 2)        # the change forces a re-parse


class TailStampTests(unittest.TestCase):
    """The "What just went by" window stamps every line with the date as well as the
    time: mm/dd/yyyy hh:mm:ss, so a round taken before midnight is told apart from one
    taken after it (issue #39)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = dict(ctxweb.CFG)
        ctxweb.CFG["source"] = self.tmp.name
        with open(os.path.join(self.tmp.name, "requests.jsonl"), "w", encoding="utf-8"):
            pass

    def tearDown(self):
        ctxweb.drop_view()
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)
        self.tmp.cleanup()

    def test_an_iso_stamp_prints_as_date_and_time(self):
        self.assertEqual(ctxweb.stamp_text("2026-02-05T14:31:07-08:00"),
                         "02/05/2026 14:31:07")

    def test_a_stamp_without_a_zone_offset_still_gets_its_date(self):
        self.assertEqual(ctxweb.stamp_text("2026-02-05T00:07:09"), "02/05/2026 00:07:09")
        self.assertEqual(ctxweb.stamp_text("2026-12-31 23:59:59"), "12/31/2026 23:59:59")

    def test_a_missing_or_unrecognised_stamp_is_left_as_it_came(self):
        for raw in (None, "", "?", "not a stamp", "2026-02-05"):
            self.assertEqual(ctxweb.stamp_text(raw), (raw or "").strip())

    def test_the_tail_lines_carry_the_date(self):
        events = [{"type": "request", "seq": 3, "api": "chat", "summary": "hi",
                   "ts": "2026-02-05T23:59:58-08:00"},
                  {"type": "response", "seq": 3, "summary": "done",
                   "ts": "2026-02-06T00:00:04-08:00"}]
        with open(os.path.join(self.tmp.name, "requests.jsonl"), "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        lines = ctxweb.api_log({"tail": ["16"]})["lines"]
        self.assertEqual([line["ts"] for line in lines],
                         ["02/05/2026 23:59:58", "02/06/2026 00:00:04"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
