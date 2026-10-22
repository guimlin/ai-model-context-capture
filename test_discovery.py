"""Tests for finding Strata and pointing the capture at it: the ports it is looked for
on when the page starts, the capture that starts itself when one answers, the URL box,
and Auto Discover.

    python test_discovery.py
"""
import os
import shutil
import tempfile
import unittest
import warnings

import ctxweb

MODELS_BODY = ('{"object": "list", "data": [{"id": "strata-iq3_s", "object": "model"}]}')
GENERIC_MODELS = '{"object":"list","data":[{"id":"some-model","object":"model"}]}'


class FakeProc:
    """Stand in for the capture process: alive until something terminates it."""

    def __init__(self):
        self.pid = 4242
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        self.dead = True


class AnswersModelsTests(unittest.TestCase):
    def test_a_strata_models_list_answers(self):
        self.assertTrue(ctxweb.answers_models(MODELS_BODY))

    def test_any_openai_shaped_models_list_answers(self):
        # the reverted #17 matched the word "strata" and missed a real server; the
        # shape is what counts
        self.assertTrue(ctxweb.answers_models(GENERIC_MODELS))

    def test_other_answers_do_not(self):
        for text in ("", None, "<html>404</html>", '{"error": "unauthorized"}',
                     '{"proxy": "http://127.0.0.1:8788", "upstream": "x"}'):
            self.assertFalse(ctxweb.answers_models(text))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.probed = []
        self.routes = {}
        real = ctxweb._get
        self.addCleanup(setattr, ctxweb, "_get", real)
        ctxweb._get = self._get

    def _get(self, url, timeout=2.5):
        self.probed.append(url)
        body = self.routes.get(url)
        return (200, body) if body is not None else (None, None)

    def test_a_models_server_is_a_candidate(self):
        self.routes["http://127.0.0.1:47831/v1/models"] = MODELS_BODY
        hit = ctxweb.probe_upstream_port(47831)
        self.assertEqual(hit, {"port": 47831, "model": "strata-iq3_s",
                               "url": "http://127.0.0.1:47831/v1"})

    def test_the_capture_is_never_mistaken_for_strata(self):
        self.routes["http://127.0.0.1:8788/ctx-stats"] = (
            '{"proxy": "http://127.0.0.1:8788", "upstream": "http://x/v1"}')
        self.routes["http://127.0.0.1:8788/v1/models"] = MODELS_BODY
        self.assertIsNone(ctxweb.probe_upstream_port(8788))

    def test_a_port_that_answers_nothing_is_not_a_candidate(self):
        self.assertIsNone(ctxweb.probe_upstream_port(12345))


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(ctxweb.CFG)
        self.probed = []
        real = ctxweb._get
        self.addCleanup(setattr, ctxweb, "_get", real)
        ctxweb._get = self._get
        real_lp = ctxweb.listening_ports
        self.addCleanup(setattr, ctxweb, "listening_ports", real_lp)
        ctxweb.listening_ports = lambda any_address=False: []
        ctxweb._upstream_cache.update(at=0.0, list=[])
        ctxweb._status_log.clear()

    def tearDown(self):
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)
        ctxweb._upstream_cache.update(at=0.0, list=[])
        ctxweb._status_log.clear()

    def _get(self, url, timeout=2.5):
        self.probed.append(url)
        if url == "http://127.0.0.1:47831/v1/models":
            return 200, MODELS_BODY
        return None, None

    def test_the_usual_ports_are_tried_first(self):
        found = ctxweb.discover_upstream()
        self.assertEqual([h["port"] for h in found], [47831])
        self.assertIn("http://127.0.0.1:47831/ctx-stats", self.probed)
        self.assertIn("http://127.0.0.1:8080/v1/models", self.probed)
        # a hit on the usual ports means no sweep of every listener
        self.assertNotIn("http://127.0.0.1:52000/v1/models", self.probed)

    def test_the_sweep_of_other_listeners_is_the_fallback(self):
        ctxweb.listening_ports = lambda any_address=False: [(52000, 1), (52001, 2)]

        def get(url, timeout=2.5):
            self.probed.append(url)
            if url == "http://127.0.0.1:52001/v1/models":
                return 200, GENERIC_MODELS
            return None, None

        ctxweb._get = get
        found = ctxweb.discover_upstream()
        self.assertEqual([h["port"] for h in found], [52001])

    def test_what_it_tried_lands_in_the_status_log(self):
        ctxweb.discover_upstream()
        text = "\n".join(line[1] for line in ctxweb.status_log())
        self.assertIn("found Strata on port 47831", text)
        self.assertIn("nothing answering like Strata on port 8080", text)

    def test_quiet_discovery_logs_nothing_and_still_caches(self):
        found = ctxweb.find_upstream(again=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(ctxweb.status_log(), [])
        self.assertEqual(ctxweb.find_upstream(), found)   # the cached answer


class UpstreamUrlTests(unittest.TestCase):
    def test_a_bare_host_and_port_gets_v1(self):
        self.assertEqual(ctxweb.resolve_upstream_url("http://127.0.0.1:47831"),
                         "http://127.0.0.1:47831/v1")
        self.assertEqual(ctxweb.resolve_upstream_url(" http://127.0.0.1:47831/v1 "),
                         "http://127.0.0.1:47831/v1")
        self.assertEqual(ctxweb.resolve_upstream_url("http://127.0.0.1:8080/v1/"),
                         "http://127.0.0.1:8080/v1")

    def test_a_path_that_is_not_v1_is_left_alone(self):
        self.assertEqual(ctxweb.resolve_upstream_url("http://127.0.0.1:8080/api"),
                         "http://127.0.0.1:8080/api")

    def test_anything_that_is_not_a_plain_http_url_is_refused(self):
        for raw in ("", "8080", "strata", "https://127.0.0.1:8080/v1",
                    "http://user:pw@127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1?x=1",
                    "http://127.0.0.1:8080/v1#f", "http://127.0.0.1:99999/v1",
                    "http://127.0.0.1:notaport/v1"):
            with self.assertRaises(ValueError):
                ctxweb.resolve_upstream_url(raw)


class PickUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(ctxweb.CFG)
        self.fns = {name: getattr(ctxweb, name) for name in ("find_upstream",)}
        ctxweb.CFG["upstream"] = ""
        ctxweb.find_upstream = lambda again=False: []
        self._manual = ctxweb._manual_upstream
        ctxweb._manual_upstream = False
        ctxweb._status_log.clear()
        self.addCleanup(self._restore)

    def tearDown(self):
        self._restore()

    def _restore(self):
        for name, fn in self.fns.items():
            setattr(ctxweb, name, fn)
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)
        ctxweb._manual_upstream = self._manual
        ctxweb._status_log.clear()

    def test_the_url_in_the_box_wins_and_is_normalised(self):
        got = ctxweb.pick_upstream({"upstream": "http://127.0.0.1:47831"})
        self.assertEqual(got, "http://127.0.0.1:47831/v1")

    def test_a_bad_url_in_the_box_is_refused(self):
        with self.assertRaises(ValueError):
            ctxweb.pick_upstream({"upstream": "47831"})

    def test_the_launch_upstream_is_next(self):
        ctxweb._manual_upstream = True
        ctxweb.CFG["upstream"] = "http://127.0.0.1:9090/v1"
        self.assertEqual(ctxweb.pick_upstream({}), "http://127.0.0.1:9090/v1")

    def test_a_url_found_earlier_does_not_beat_a_fresh_probe(self):
        # Strata moves ports when it restarts, so a URL this page found for the last
        # capture is not pinned: look again.
        ctxweb.CFG["upstream"] = "http://127.0.0.1:9090/v1"
        ctxweb.find_upstream = lambda again=False: [
            {"port": 47831, "model": "m", "url": "http://127.0.0.1:47831/v1"}]
        self.assertEqual(ctxweb.pick_upstream({}), "http://127.0.0.1:47831/v1")

    def test_a_discovered_strata_is_next(self):
        ctxweb.find_upstream = lambda again=False: [{"port": 47831, "model": "m",
                                                     "url": "http://127.0.0.1:47831/v1"}]
        self.assertEqual(ctxweb.pick_upstream({}), "http://127.0.0.1:47831/v1")

    def test_nothing_to_forward_to_says_so(self):
        with self.assertRaises(ValueError) as caught:
            ctxweb.pick_upstream({})
        self.assertIn("Strata is not answering", str(caught.exception))
        text = "\n".join(line[1] for line in ctxweb.status_log())
        self.assertIn("looking for Strata", text)
        self.assertIn("not answering", text)


class StartUpstreamTests(unittest.TestCase):
    """start_capture with the URL box: the upstream it launches with, no real process."""

    def setUp(self):
        self.saved = dict(ctxweb.CFG)
        self.fns = {name: getattr(ctxweb, name)
                    for name in ("proxy_state", "listening_ports", "find_upstream")}
        self.saved_popen = ctxweb.subprocess.Popen
        self.tmp = tempfile.TemporaryDirectory()
        ctxweb.CFG["log_dir"] = os.path.join(self.tmp.name, "logs")
        ctxweb.CFG["proxy_port"] = 8788
        ctxweb.CFG["upstream"] = ""
        ctxweb.find_upstream = lambda again=False: []
        ctxweb.listening_ports = lambda any_address=False: []
        ctxweb._proc = None
        ctxweb._status_log.clear()
        self.started = []
        self.capturing = set()
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
        ctxweb._status_log.clear()
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

    def test_the_url_in_the_box_is_what_the_capture_forwards_to(self):
        result = ctxweb.start_capture({"upstream": "http://127.0.0.1:47831"})
        self.assertTrue(result["ok"], result)
        command = self.started[0]
        self.assertEqual(command[command.index("--upstream") + 1], "http://127.0.0.1:47831/v1")
        self.assertEqual(ctxweb.CFG["upstream"], "http://127.0.0.1:47831/v1")

    def test_a_bad_url_never_starts_anything(self):
        result = ctxweb.start_capture({"upstream": "not a url"})
        self.assertFalse(result["ok"])
        self.assertIn("plain http Strata URL", result["error"])
        self.assertEqual(self.started, [])

    def test_starting_without_a_url_discovers_first(self):
        ctxweb.find_upstream = lambda again=False: [{"port": 47831, "model": "m",
                                                     "url": "http://127.0.0.1:47831/v1"}]
        result = ctxweb.start_capture({})
        self.assertTrue(result["ok"], result)
        command = self.started[0]
        self.assertEqual(command[command.index("--upstream") + 1], "http://127.0.0.1:47831/v1")

    def test_the_start_lands_in_the_status_log(self):
        ctxweb.start_capture({"upstream": "http://127.0.0.1:47831/v1"})
        text = "\n".join(line[1] for line in ctxweb.status_log())
        self.assertIn("capturing on port 8788 -> http://127.0.0.1:47831/v1", text)


class StartupDiscoveryTests(unittest.TestCase):
    """What the page does the moment it starts: find Strata, capture it."""

    def setUp(self):
        self.saved = dict(ctxweb.CFG)
        self.fns = {name: getattr(ctxweb, name)
                    for name in ("discover_upstream", "start_capture", "proxy_state")}
        ctxweb.CFG["upstream"] = ""
        ctxweb.CFG["proxy_port"] = 8788
        ctxweb.proxy_state = lambda port=None: None
        ctxweb._status_log.clear()
        self.starts = []

    def tearDown(self):
        for name, fn in self.fns.items():
            setattr(ctxweb, name, fn)
        ctxweb.CFG.clear()
        ctxweb.CFG.update(self.saved)
        ctxweb._status_log.clear()

    def test_a_found_strata_starts_the_capture_at_once(self):
        ctxweb.discover_upstream = lambda quiet=False: [
            {"port": 47831, "model": "strata-iq3_s", "url": "http://127.0.0.1:47831/v1"}]
        ctxweb.start_capture = lambda payload=None: (self.starts.append(payload),
                                                     {"ok": True,
                                                      "state": {"port": 8788}})[1]
        ctxweb.startup_discovery()
        self.assertEqual(self.starts, [{"upstream": "http://127.0.0.1:47831/v1"}])
        text = "\n".join(line[1] for line in ctxweb.status_log())
        self.assertIn("looking for Strata on ports 47831 and 8080", text)
        self.assertIn("starting the capture", text)

    def test_nothing_found_leaves_the_url_at_the_default_and_starts_nothing(self):
        ctxweb.discover_upstream = lambda quiet=False: []
        ctxweb.start_capture = lambda payload=None: self.starts.append(payload)
        ctxweb.startup_discovery()
        self.assertEqual(self.starts, [])
        text = "\n".join(line[1] for line in ctxweb.status_log())
        self.assertIn(ctxweb.DEFAULT_UPSTREAM, text)

    def test_a_capture_already_running_is_left_alone(self):
        ctxweb.discover_upstream = lambda quiet=False: [
            {"port": 47831, "model": "m", "url": "http://127.0.0.1:47831/v1"}]
        ctxweb.proxy_state = lambda port=None: {"port": 8788, "ours": False}
        ctxweb.start_capture = lambda payload=None: self.starts.append(payload)
        ctxweb.startup_discovery()
        self.assertEqual(self.starts, [])
        text = "\n".join(line[1] for line in ctxweb.status_log())
        self.assertIn("already answering on port 8788", text)

    def test_a_failed_auto_start_is_reported_in_the_log(self):
        ctxweb.discover_upstream = lambda quiet=False: [
            {"port": 47831, "model": "m", "url": "http://127.0.0.1:47831/v1"}]
        ctxweb.start_capture = lambda payload=None: {"ok": False, "error": "port taken"}
        ctxweb.startup_discovery()
        text = "\n".join(line[1] for line in ctxweb.status_log())
        self.assertIn("did not start: port taken", text)


class FreshLaunchTests(unittest.TestCase):
    """The status and discovery window has to render before any capture has run,
    which on a fresh checkout means the live log folder does not exist yet."""

    def setUp(self):
        self._upstream, self._log_dir = ctxweb.CFG["upstream"], ctxweb.CFG["log_dir"]
        self._find = ctxweb.find_upstream
        self._manual = ctxweb._manual_upstream
        ctxweb._manual_upstream = False
        ctxweb.find_upstream = lambda again=False: []
        ctxweb.CFG["upstream"] = ""
        self.dir = tempfile.mkdtemp(prefix="freshlog")
        ctxweb.CFG["log_dir"] = os.path.join(self.dir, "logs")
        ctxweb._status_log[:] = []

    def tearDown(self):
        ctxweb.CFG["upstream"], ctxweb.CFG["log_dir"] = self._upstream, self._log_dir
        ctxweb.find_upstream = self._find
        ctxweb._manual_upstream = self._manual
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_an_empty_log_folder_becomes_a_log_with_zero_rounds(self):
        ctxweb.ensure_live_log()
        state = ctxweb.api_state()
        self.assertTrue(state["ok"])
        self.assertEqual(state["rounds"], 0)

    def test_the_state_carries_the_default_url_and_the_status_log(self):
        ctxweb.log_note("looking for Strata")
        state = ctxweb.api_state()
        self.assertEqual(state["upstream"]["url"], ctxweb.DEFAULT_UPSTREAM)
        self.assertEqual(state["upstream"]["default"], ctxweb.DEFAULT_UPSTREAM)
        self.assertFalse(state["upstream"]["manual"])
        self.assertIn("looking for Strata", [line[1] for line in state["status_log"]])

    def test_an_existing_log_is_never_touched(self):
        os.makedirs(ctxweb.CFG["log_dir"])
        live = os.path.join(ctxweb.CFG["log_dir"], "requests.jsonl")
        with open(live, "w", encoding="utf-8") as handle:
            handle.write('{"type": "request", "seq": 1}\n')
        ctxweb.ensure_live_log()
        self.assertEqual(len(open(live, encoding="utf-8").readlines()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
