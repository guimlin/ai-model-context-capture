"""Tests for the headers the proxy forwards upstream: no compression is asked for,
so the answer stays plain JSON the usage scan can actually read.

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
