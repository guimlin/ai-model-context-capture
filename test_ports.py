"""Tests for the capture-port option: the port box beside Start capture.

    python test_ports.py
"""
import os
import tempfile
import unittest
import warnings

import ctxweb

# start_capture hands a log-file handle to the capture process; the fake process in
# these tests leaves it for the garbage collector, which is noise, not a finding.
warnings.filterwarnings("ignore", category=ResourceWarning)


class FakeProc:
    pid = 4242

    def poll(self):
        return None


class ResolvePortTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(ctxweb.CFG)

    def tearDown(self):
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)

    def test_nothing_asked_keeps_the_launch_port(self):
        for payload in (None, {}, {"port": ""}, {"port": "   "}):
            self.assertEqual(ctxweb.resolve_capture_port(payload), ctxweb.CFG["proxy_port"])

    def test_the_page_can_ask_for_any_port(self):
        self.assertEqual(ctxweb.resolve_capture_port({"port": "9100"}), 9100)
        self.assertEqual(ctxweb.resolve_capture_port({"port": 9100}), 9100)

    def test_a_port_outside_the_range_is_refused(self):
        for raw in ("0", "-5", "65536", 70000):
            with self.assertRaises(ValueError):
                ctxweb.resolve_capture_port({"port": raw})

    def test_a_port_that_is_not_a_number_is_refused(self):
        for raw in ("abc", "87a", "8 7 8 8"):
            with self.assertRaises(ValueError):
                ctxweb.resolve_capture_port({"port": raw})


class StartCaptureTests(unittest.TestCase):
    """start_capture with the guards around a requested port, no real process."""

    def setUp(self):
        self.saved = dict(ctxweb.CFG)
        self.fns = {name: getattr(ctxweb, name)
                    for name in ("proxy_state", "listening_ports", "find_upstream")}
        self.tmp = tempfile.TemporaryDirectory()
        ctxweb.CFG["log_dir"] = os.path.join(self.tmp.name, "logs")
        ctxweb.CFG["proxy_port"] = 8788
        ctxweb.CFG["upstream"] = "http://127.0.0.1:9090/v1"
        ctxweb.find_upstream = lambda again=False: []
        ctxweb.listening_ports = lambda: []
        ctxweb._proc = None
        self.started = []
        self.capturing = set()          # ports with a capture answering /ctx-stats
        ctxweb.proxy_state = self._proxy_state
        ctxweb.subprocess.Popen = self._popen

    def tearDown(self):
        for name, fn in self.fns.items():
            setattr(ctxweb, name, fn)
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)
        ctxweb._proc = None
        self.tmp.cleanup()

    def _proxy_state(self, port=None):
        port = port or ctxweb.CFG["proxy_port"]
        if port in self.capturing:
            return {"port": port, "ours": ctxweb._proc is not None, "upstream": "http://x/v1"}
        return None

    def _popen(self, command, **kwargs):
        self.started.append(command)
        self.capturing.add(int(command[command.index("--port") + 1]))
        return FakeProc()

    def test_a_bad_port_never_starts_anything(self):
        result = ctxweb.start_capture({"port": "not a port"})
        self.assertFalse(result["ok"])
        self.assertIn("not a port number", result["error"])
        self.assertEqual(self.started, [])

    def test_starting_on_the_launch_port_when_nothing_is_asked(self):
        result = ctxweb.start_capture()
        self.assertTrue(result["ok"])
        self.assertEqual(self.started[0][self.started[0].index("--port") + 1], "8788")

    def test_starting_on_a_port_the_page_asked_for(self):
        result = ctxweb.start_capture({"port": "9100"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["port"], 9100)
        self.assertEqual(self.started[0][self.started[0].index("--port") + 1], "9100")
        self.assertEqual(ctxweb.CFG["proxy_port"], 9100)   # the rest of the page follows

    def test_a_port_that_is_already_capturing_says_so(self):
        self.capturing.add(9100)
        result = ctxweb.start_capture({"port": 9100})
        self.assertTrue(result["ok"])
        self.assertTrue(result["already"])
        self.assertEqual(self.started, [])

    def test_moving_a_running_capture_needs_a_stop_first(self):
        self.capturing.add(8788)
        result = ctxweb.start_capture({"port": 9100})
        self.assertFalse(result["ok"])
        self.assertIn("already running on port 8788", result["error"])
        self.assertEqual(self.started, [])
        self.assertEqual(ctxweb.CFG["proxy_port"], 8788)

    def test_a_port_taken_by_something_else_is_refused(self):
        ctxweb.listening_ports = lambda: [(9100, 5555)]
        result = ctxweb.start_capture({"port": 9100})
        self.assertFalse(result["ok"])
        self.assertIn("taken by something else", result["error"])
        self.assertEqual(self.started, [])

    def test_no_strata_to_forward_to_still_reports_that(self):
        ctxweb.CFG["upstream"] = ""
        result = ctxweb.start_capture({"port": 9100})
        self.assertFalse(result["ok"])
        self.assertIn("Strata is not answering", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
