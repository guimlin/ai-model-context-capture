"""Tests for the headers the proxy forwards upstream: no compression is asked for,
so the answer stays plain JSON the usage scan can actually read; the agent's API key
rides through but is masked in what gets written down; and an https upstream - a
cloud API the agent calls with a key - is reached over TLS.

    python test_proxy_headers.py
"""
import unittest

import context_proxy


class StripHopByHopTests(unittest.TestCase):
    def test_the_usual_hop_by_hop_headers_go(self):
        headers = {"Host": "x", "Connection": "keep-alive", "Content-Length": "5",
                   "Transfer-Encoding": "chunked", "Authorization": "Bearer t",
                   "Content-Type": "application/json"}
        fwd = context_proxy._strip_hop_by_hop(headers)
        self.assertEqual(fwd, {"Authorization": "Bearer t",
                               "Content-Type": "application/json"})

    def test_the_proxy_never_asks_for_a_compressed_answer(self):
        # a gzipped body is unreadable to the scan that records usage and finish
        fwd = context_proxy._strip_hop_by_hop({"Accept-Encoding": "gzip", "X-Keep": "1"})
        self.assertEqual(fwd, {"X-Keep": "1"})


class UpstreamTargetTests(unittest.TestCase):
    """Where the capture forwards: an https upstream is wrapped in TLS, so an agent
    pointed at the capture can talk to a cloud API with its own key."""

    def test_a_plain_upstream_is_reached_over_tcp_on_its_port(self):
        self.assertEqual(context_proxy._upstream_target("http://127.0.0.1:8080"),
                         ("127.0.0.1", 8080, False))

    def test_an_https_upstream_is_reached_over_tls_on_443(self):
        self.assertEqual(context_proxy._upstream_target("https://api.openai.com/v1"),
                         ("api.openai.com", 443, True))
        self.assertEqual(context_proxy._upstream_target("https://api.anthropic.com"),
                         ("api.anthropic.com", 443, True))

    def test_an_explicit_port_wins_over_the_scheme_default(self):
        self.assertEqual(context_proxy._upstream_target("https://127.0.0.1:8443/v1"),
                         ("127.0.0.1", 8443, True))


class HostHeaderTests(unittest.TestCase):
    def test_the_default_port_of_the_scheme_is_not_spelled_out(self):
        self.assertEqual(context_proxy._host_header("api.openai.com", 443, True),
                         "api.openai.com")
        self.assertEqual(context_proxy._host_header("127.0.0.1", 80, False),
                         "127.0.0.1")

    def test_any_other_port_is_spelled_out(self):
        self.assertEqual(context_proxy._host_header("127.0.0.1", 8080, False),
                         "127.0.0.1:8080")
        self.assertEqual(context_proxy._host_header("127.0.0.1", 8443, True),
                         "127.0.0.1:8443")


class MaskTests(unittest.TestCase):
    """The key headers go upstream untouched so the call works, and only a mask ever
    lands in the log - which is what makes key-authenticated traffic safe to capture."""

    def test_the_key_headers_are_masked_and_the_rest_ride_through(self):
        masked = context_proxy._mask({"Authorization": "Bearer test-key-1",
                                      "X-Api-Key": "test-key-2", "Api-Key": "test-key-3",
                                      "Proxy-Authorization": "Bearer test-key-4",
                                      "Content-Type": "application/json"})
        for name in ("Authorization", "X-Api-Key", "Api-Key", "Proxy-Authorization"):
            self.assertEqual(masked[name], "<masked>")
        self.assertEqual(masked["Content-Type"], "application/json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
