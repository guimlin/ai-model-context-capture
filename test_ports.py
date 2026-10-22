"""Tests for the capture's port: it is the one the page was started with, no port box.

    python test_ports.py
"""
import os
import tempfile
import unittest
import warnings

import ctxweb

NETSTAT = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:8788         0.0.0.0:0              LISTENING       111
  TCP    0.0.0.0:9100           0.0.0.0:0              LISTENING       222
  TCP    [::]:9101              [::]:0                 LISTENING       333
  TCP    10.0.0.5:9102          0.0.0.0:0              LISTENING       555
  TCP    127.0.0.1:52000        127.0.0.1:52001        ESTABLISHED     444
"""


class FakeProc:
    """Stand in for the capture process: alive until something terminates it."""

    def __init__(self):
        self.pid = 4242
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        self.dead = True


class ListeningPortsTests(unittest.TestCase):
    def setUp(self):
        real = ctxweb.subprocess.run
        self.addCleanup(setattr, ctxweb.subprocess, "run", real)
        ctxweb.subprocess.run = lambda *a, **k: type("Out", (), {"stdout": NETSTAT})()

    def test_the_page_only_probes_loopback_when_looking_for_strata(self):
        self.assertEqual(ctxweb.listening_ports(), [(8788, 111)])

    def test_a_wildcard_listener_holds_a_port_when_the_conflict_is_checked(self):
        # 0.0.0.0 and [::] sit in the way of a loopback bind on Windows, but a listener
        # on some other interface (10.0.0.5) does not
        ports = {port for port, _ in ctxweb.listening_ports(any_address=True)}
        self.assertEqual(ports, {8788, 9100, 9101})


class StartCaptureTests(unittest.TestCase):
    """start_capture with the guards around the launch port, no real process."""

    def setUp(self):
        self.saved = dict(ctxweb.CFG)
        self.fns = {name: getattr(ctxweb, name)
                    for name in ("proxy_state", "listening_ports", "find_upstream",
                                 "probe_upstream_port")}
        self.saved_popen = ctxweb.subprocess.Popen
        self.tmp = tempfile.TemporaryDirectory()
        ctxweb.CFG["log_dir"] = os.path.join(self.tmp.name, "logs")
        ctxweb.CFG["proxy_port"] = 8788
        ctxweb.CFG["upstream"] = "http://127.0.0.1:9090/v1"
        # these tests stand in for a page started with --upstream, a pinned URL
        self._manual = ctxweb._manual_upstream
        ctxweb._manual_upstream = True
        ctxweb.find_upstream = lambda again=False: []
        ctxweb.probe_upstream_port = lambda port: None
        ctxweb.listening_ports = lambda any_address=False: []
        ctxweb._proc = None
        self.started = []
        self.capturing = set()          # ports with a capture answering /ctx-stats
        ctxweb.proxy_state = self._proxy_state
        ctxweb.subprocess.Popen = self._popen
        # unittest switches warnings back on after this module is imported, and a fake
        # process leaves the capture's log handle to the garbage collector
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
        ctxweb._manual_upstream = self._manual
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

    def _launched_port(self):
        return self.started[0][self.started[0].index("--port") + 1]

    def _fast_polls(self):
        """Keep the wait for the capture to come up, lose the ten seconds of waiting."""
        real = ctxweb.time.sleep
        self.addCleanup(setattr, ctxweb.time, "sleep", real)
        ctxweb.time.sleep = lambda seconds: None

    def test_starting_on_the_launch_port_when_nothing_is_asked(self):
        result = ctxweb.start_capture()
        self.assertTrue(result["ok"])
        self.assertEqual(self._launched_port(), "8788")

    def test_a_port_in_the_payload_is_ignored(self):
        # the page has no port box; an old bookmark or a hand-made request cannot move the
        # capture, so the status box and the agent settings keep pointing at one port
        result = ctxweb.start_capture({"port": "9100"})
        self.assertTrue(result["ok"])
        self.assertEqual(self._launched_port(), "8788")
        self.assertEqual(ctxweb.CFG["proxy_port"], 8788)

    def test_a_capture_answering_on_the_launch_port_is_adopted(self):
        self.capturing.add(8788)                        # started outside this page
        result = ctxweb.start_capture()
        self.assertTrue(result["ok"])
        self.assertTrue(result["already"])
        self.assertEqual(self.started, [])
        self.assertIsNotNone(ctxweb.proxy_state())

    def test_a_capture_adopted_on_a_wildcard_address_is_stopped_too(self):
        # whoever holds the port is the capture the page just took over, even when it
        # listens on every address rather than loopback
        ctxweb.CFG["proxy_port"] = 9141
        self.capturing.add(9141)
        ctxweb.listening_ports = lambda any_address=False: (
            [(9141, 777)] if any_address else [])
        real_run = ctxweb.subprocess.run
        self.addCleanup(setattr, ctxweb.subprocess, "run", real_run)

        killed = []

        def kill(command, **kwargs):
            killed.append(command)
            self.capturing.discard(9141)

        ctxweb.subprocess.run = kill
        result = ctxweb.stop_capture()
        self.assertTrue(result["ok"], result)
        self.assertEqual(killed[0][:3], ["taskkill", "/PID", "777"])

    def test_a_slow_start_is_still_this_pages_to_stop(self):
        self._fast_polls()
        # the process is started but never answers /ctx-stats
        ctxweb.subprocess.Popen = lambda command, **kw: (self.started.append(command),
                                                         FakeProc())[1]
        result = ctxweb.start_capture()
        self.assertFalse(result["ok"])
        self.assertIn("not answering yet", result["error"])
        self.assertEqual(ctxweb.CFG["proxy_port"], 8788)   # the launch port, always
        self.assertTrue(ctxweb.stop_capture()["ok"])       # the page can still clean it up
        self.assertIsNone(ctxweb._proc)

    def test_starting_again_while_one_is_running_says_already(self):
        ctxweb._proc = FakeProc()                         # this page started it
        self.capturing.add(8788)
        result = ctxweb.start_capture()
        self.assertTrue(result["ok"])
        self.assertTrue(result["already"])
        self.assertEqual(self.started, [])

    def test_a_port_taken_by_something_else_is_refused(self):
        ctxweb.listening_ports = lambda any_address=False: [(8788, 5555)]
        result = ctxweb.start_capture()
        self.assertFalse(result["ok"])
        self.assertIn("taken by something else", result["error"])
        self.assertIn("--proxy-port", result["error"])    # where to go instead
        self.assertEqual(self.started, [])

    def test_a_port_held_on_a_wildcard_address_is_refused_too(self):
        asked = {}

        def ports(any_address=False):
            asked["any_address"] = any_address
            return [(8788, 777)] if any_address else []

        ctxweb.listening_ports = ports
        result = ctxweb.start_capture()
        self.assertFalse(result["ok"])
        self.assertIn("taken by something else", result["error"])
        self.assertTrue(asked["any_address"])             # the wider check is the one used
        self.assertEqual(self.started, [])

    def test_no_strata_to_forward_to_still_reports_that(self):
        ctxweb.CFG["upstream"] = ""
        result = ctxweb.start_capture()
        self.assertFalse(result["ok"])
        self.assertIn("Strata is not answering", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
