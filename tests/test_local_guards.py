#!/usr/bin/env python3
"""Acceptance tests for the local-service guards (H1-H3).

These are the tests for the three findings that block distribution:

  H1  mapai-server (8765) accepted cross-origin POSTs. A `text/plain`
      body carrying JSON is a CORS "simple request" — no preflight — so
      any page the owner had open could overwrite profile.json or drive
      the photo tools. DNS rebinding reached it the same way.
  H2  m-bridge (8787) answered `Access-Control-Allow-Origin: *`, so any
      page could spend the owner's Claude subscription and read the reply.
  H3  comms (8788) defaulted to 0.0.0.0 with an empty token, publishing
      the message/file relay to whatever LAN the machine was on.

Each test drives a REAL server over a real socket rather than calling the
handler directly, because the thing being verified is what the socket
answers to a hostile request — not what a method returns.

Run:  python3 apps/lifeos/tests/test_local_guards.py
Stdlib only, no network, no fixtures beyond a temp directory.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(path, name):
    """Import a LifeOS module that has no .py extension / a dashed name."""
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ServerHarness:
    """Runs a handler on an ephemeral loopback port for the test's lifetime."""

    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, headers=None, body=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            method=method,
            data=body,
            headers=headers or {},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestServerLocalGuard(unittest.TestCase):
    """H1 — mapai-server refuses foreign Origins and preflight-free POSTs."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lifeos-test-")
        os.environ["MAPAI_WEB_ROOT"] = cls.tmp
        cls.mod = load(os.path.join(ROOT, "bin", "mapai-server.py"), "mapai_server")

    def setUp(self):
        self.server = ServerHarness(self.mod.Handler)
        # The handler validates Host against the port it was configured with,
        # so align the module's allow-lists with this harness's ephemeral port.
        # Mirror the module's own host set, IPv6 loopback included, so the
        # test exercises the same shape production builds.
        hosts = ("localhost", "127.0.0.1", "[::1]")
        self.mod.ALLOWED_HOSTS = frozenset(
            "%s:%d" % (h, self.server.port) for h in hosts
        )
        self.mod.ALLOWED_ORIGINS = frozenset(
            "http://%s:%d" % (h, self.server.port) for h in hosts
        )

    def tearDown(self):
        self.server.close()

    def _json_headers(self, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        return headers

    def test_dashboard_origin_is_allowed(self):
        own = "http://127.0.0.1:%d" % self.server.port
        status, _ = self.server.request(
            "POST", "/api/fleet/probe", self._json_headers(own), b"{}"
        )
        self.assertNotEqual(status, 403, "the dashboard's own origin must work")

    def test_no_origin_is_allowed(self):
        """curl / cron / the launcher have no Origin and must keep working."""
        status, _ = self.server.request(
            "POST", "/api/fleet/probe", self._json_headers(), b"{}"
        )
        self.assertNotEqual(status, 403)

    def test_foreign_origin_is_refused(self):
        status, body = self.server.request(
            "POST",
            "/api/profile",
            self._json_headers("https://evil.example"),
            json.dumps({"name": "pwned"}).encode(),
        )
        self.assertEqual(status, 403)
        self.assertIn(b"local-only", body)

    def test_preflight_free_text_plain_post_is_refused(self):
        """The exact bypass: a 'simple request' that skips the preflight."""
        status, _ = self.server.request(
            "POST",
            "/api/profile",
            {"Content-Type": "text/plain"},
            json.dumps({"name": "pwned"}).encode(),
        )
        self.assertEqual(status, 415)

    def test_form_encoded_post_is_refused(self):
        """The other two preflight-free content types are refused as well."""
        for ctype in (
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=x",
        ):
            with self.subTest(ctype=ctype):
                status, _ = self.server.request(
                    "POST", "/api/profile", {"Content-Type": ctype}, b"{}"
                )
                self.assertEqual(status, 415)

    def test_rebound_host_is_refused(self):
        """DNS rebinding: the page's own hostname arrives in the Host header."""
        status, body = self.server.request(
            "GET", "/api/profile", {"Host": "attacker.example:%d" % self.server.port}
        )
        self.assertEqual(status, 403)
        self.assertIn(b"local-only", body)

    def test_foreign_origin_refused_on_get_too(self):
        status, _ = self.server.request(
            "GET", "/api/profile", {"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)


class TestBridgeOriginGuard(unittest.TestCase):
    """H2 — m-bridge no longer answers `*` to every caller."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load(os.path.join(ROOT, "app", "m-bridge.py"), "m_bridge")

    def setUp(self):
        self.server = ServerHarness(self.mod.H)
        self.mod.ALLOWED_ORIGINS = frozenset(
            ["http://127.0.0.1:8765", "http://localhost:8765"]
        )

    def tearDown(self):
        self.server.close()

    def test_wildcard_is_never_sent(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/" % self.server.port,
            method="OPTIONS",
            headers={"Origin": "https://evil.example"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertNotEqual(
                resp.headers.get("Access-Control-Allow-Origin"),
                "*",
                "a wildcard hands the owner's subscription to any page",
            )

    def test_dashboard_origin_is_echoed(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/" % self.server.port,
            method="OPTIONS",
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(
                resp.headers.get("Access-Control-Allow-Origin"),
                "http://127.0.0.1:8765",
            )
            self.assertEqual(resp.headers.get("Vary"), "Origin")

    def test_foreign_origin_cannot_spend_the_subscription(self):
        status, body = self.server.request(
            "POST",
            "/v1/chat",
            {"Content-Type": "application/json", "Origin": "https://evil.example"},
            json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
        )
        self.assertEqual(status, 403)
        self.assertIn(b"forbidden", body)

    def test_local_caller_without_origin_still_works(self):
        status, _ = self.server.request("GET", "/")
        self.assertEqual(status, 200)


class TestCommsOriginGuard(unittest.TestCase):
    """H3b — the relay refuses foreign browser origins.

    Found in review of the first H1-H3 pass: comms still answered
    `Access-Control-Allow-Origin: *`, and its token — the other guard — is
    EMPTY in the default loopback configuration. So any page the owner had
    open could POST /send into the relay and read /events, which streams
    message text and file names. Same shape as H1/H2, on the third port.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = load(os.path.join(ROOT, "app", "comms.py"), "comms_origin")

    def setUp(self):
        self.server = ServerHarness(self.mod.H)
        self.mod.ALLOWED_ORIGINS = frozenset(
            ["http://127.0.0.1:8765", "http://localhost:8765"]
        )
        self.mod.TOKEN = ""  # the default: no token to fall back on

    def tearDown(self):
        self.server.close()

    def test_foreign_page_cannot_read_the_stream(self):
        status, body = self.server.request(
            "GET", "/health", {"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertIn(b"forbidden", body)

    def test_foreign_page_cannot_send_into_the_relay(self):
        status, _ = self.server.request(
            "POST",
            "/send",
            {"Content-Type": "application/json", "Origin": "https://evil.example"},
            json.dumps({"peer": "mac", "text": "injected"}).encode(),
        )
        self.assertEqual(status, 403)

    def test_wildcard_is_never_sent(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/health" % self.server.port,
            method="OPTIONS",
            headers={"Origin": "https://evil.example"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertNotEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

    def test_dashboard_origin_still_works(self):
        status, _ = self.server.request(
            "GET", "/health", {"Origin": "http://127.0.0.1:8765"}
        )
        self.assertEqual(status, 200)

    def test_local_caller_without_origin_still_works(self):
        status, _ = self.server.request("GET", "/health")
        self.assertEqual(status, 200)


class TestCommsBindDefaults(unittest.TestCase):
    """H3 — comms binds loopback and refuses a tokenless public bind."""

    def _run(self, env_extra):
        env = dict(os.environ)
        env.update(env_extra)
        env["M_COMMS_PORT"] = "0"
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "app", "comms.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_default_bind_is_loopback(self):
        mod = load(os.path.join(ROOT, "app", "comms.py"), "comms_defaults")
        self.assertEqual(
            mod.BIND, "127.0.0.1", "the relay must not publish itself by default"
        )

    def test_public_bind_without_token_refuses_to_start(self):
        result = self._run({"M_COMMS_BIND": "0.0.0.0", "M_COMMS_TOKEN": ""})
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to start", result.stderr)
        self.assertIn("M_COMMS_TOKEN", result.stderr)

    def test_loopback_without_token_is_allowed(self):
        """No token needed on loopback — nothing off-machine can reach it."""
        mod = load(os.path.join(ROOT, "app", "comms.py"), "comms_loopback")
        mod.BIND, mod.TOKEN = "127.0.0.1", ""
        mod._require_token_for_public_bind()  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
