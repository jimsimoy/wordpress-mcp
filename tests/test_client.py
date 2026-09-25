"""WPClient guards: REST path validation, read_only, request counting and pacing. No network."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wp_client
from wp_client import WPClient, normalise_rest_path


def make_client(**extra):
    cfg = {"wp_url": "https://example.com", "wp_user": "user", "wp_app_password": "not-a-real-password"}
    cfg.update(extra)
    return WPClient(cfg)          # app_password mode makes no requests in the constructor


class FakeResponse:
    def __init__(self, body=b'{"ok": true}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NormaliseRestPathTests(unittest.TestCase):
    def test_accepts_plain_routes_and_prefixes_wp_json(self):
        self.assertEqual(normalise_rest_path("/wcdn/v1/settings"), "/wp-json/wcdn/v1/settings")
        self.assertEqual(normalise_rest_path("wcdn/v1/settings"), "/wp-json/wcdn/v1/settings")
        self.assertEqual(normalise_rest_path("/wp-json/wp/v2/settings"), "/wp-json/wp/v2/settings")
        self.assertEqual(normalise_rest_path("/"), "/wp-json/")

    def test_rejects_anything_that_is_not_a_route_on_this_site(self):
        for bad in ("https://evil.example/x", "//evil.example/x", "/a/../b", "/a%2e%2e/b", "/a%2Fb/../c",
                    "/a?x=1", "/a#frag", "/a b", "/a\tb", "/a\\b", "", "   ", None, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalise_rest_path(bad)


class ReadOnlyTests(unittest.TestCase):
    def test_read_only_refuses_writes_before_any_network_and_without_counting(self):
        client = make_client(read_only=True)
        with mock.patch("urllib.request.urlopen") as urlopen:
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(method=method):
                    with self.assertRaises(RuntimeError):
                        client.rest(method, "/wp/v2/settings", payload={"a": 1})
            urlopen.assert_not_called()
        self.assertEqual(client.request_count, 0)

    def test_read_only_still_allows_get(self):
        client = make_client(read_only=True)
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            self.assertEqual(client.rest("GET", "/wp/v2/settings"), {"ok": True})
            self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(client.request_count, 1)

    def test_writes_are_allowed_by_default(self):
        client = make_client()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            self.assertEqual(client.rest("POST", "/wp/v2/settings", payload={"a": 1}), {"ok": True})

    def test_rest_rejects_unknown_methods(self):
        with self.assertRaises(ValueError):
            make_client().rest("TRACE", "/wp/v2/settings")


class CountingAndPacingTests(unittest.TestCase):
    def test_request_count_counts_every_request(self):
        client = make_client()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            for _ in range(3):
                client.rest("GET", "/wp/v2/settings")
        self.assertEqual(client.request_count, 3)

    def test_request_delay_spaces_requests(self):
        client = make_client(request_delay=4)
        # monotonic() readings in call order. A request that has to wait reads the clock to work out
        # the wait, sleeps, then reads it again to stamp itself; the first request only stamps.
        clock = iter([100.0,            # request 1: stamp
                      101.0, 104.0,     # request 2: 1s since request 1 -> waits 3s, then stamps at 104
                      108.5, 108.5])    # request 3: 4.5s since request 2 -> past the delay, no wait
        with mock.patch.object(wp_client.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(wp_client.time, "sleep") as sleep, \
                mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            client.rest("GET", "/a")     # first request: never waits
            client.rest("GET", "/b")     # 1s after the first: must wait the remaining 3s
            client.rest("GET", "/c")     # 4.5s after the second: already past the delay
        self.assertEqual([round(c.args[0], 2) for c in sleep.call_args_list], [3.0])

    def test_no_delay_configured_never_sleeps(self):
        client = make_client()
        with mock.patch.object(wp_client.time, "sleep") as sleep, \
                mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            client.rest("GET", "/a")
            client.rest("GET", "/b")
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
