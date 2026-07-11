import importlib.util
import http.client
import io
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path


os.environ.pop("AGENT_PASSWORD", None)
MODULE_PATH = Path(__file__).resolve().parents[1] / "api" / "index.py"
SPEC = importlib.util.spec_from_file_location("agent_api", MODULE_PATH)
agent_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_api)


class SecurityHelpersTest(unittest.TestCase):
    def make_handler(self, headers=None, body=b""):
        instance = object.__new__(agent_api.handler)
        instance.headers = headers or {}
        instance.rfile = io.BytesIO(body)
        instance.client_address = ("203.0.113.10", 12345)
        return instance

    def test_requested_default_password_is_preserved(self):
        self.assertEqual(agent_api.ACCESS_PASSWORD, "123456")

    def test_constant_time_authorization_and_failure_tracking(self):
        instance = self.make_handler({"X-Forwarded-For": "203.0.113.99"})
        agent_api.auth_failure_limiter.clear(instance.client_key())
        self.assertEqual(instance.authorize({"password": "123456"}), (True, 200))
        self.assertEqual(instance.authorize({"password": "wrong"}), (False, 403))
        self.assertEqual(instance.authorize({"password": "错误密码"}), (False, 403))

    def test_session_id_validation(self):
        self.assertEqual(
            agent_api.normalize_session_id("123e4567-e89b-12d3-a456-426614174000"),
            "123e4567-e89b-12d3-a456-426614174000",
        )
        with self.assertRaises(agent_api.RequestError):
            agent_api.normalize_session_id("../../other-session")

    def test_message_size_limit(self):
        with self.assertRaises(agent_api.RequestError) as context:
            agent_api.validated_messages({"message": "x" * (agent_api.MAX_MESSAGE_CHARS + 1)})
        self.assertEqual(context.exception.status, 413)

    def test_sliding_window_limiter(self):
        limiter = agent_api.SlidingWindowLimiter(limit=2, window_seconds=60)
        self.assertTrue(limiter.allow("client"))
        self.assertTrue(limiter.allow("client"))
        self.assertFalse(limiter.allow("client"))
        limiter.clear("client")
        self.assertTrue(limiter.allow("client"))

    def test_cross_origin_is_rejected_but_same_origin_is_allowed(self):
        same_origin = self.make_handler(
            {"Origin": "https://agent.example.com", "Host": "agent.example.com"}
        )
        cross_origin = self.make_handler(
            {"Origin": "https://evil.example", "Host": "agent.example.com"}
        )
        self.assertTrue(same_origin.origin_is_allowed())
        self.assertFalse(cross_origin.origin_is_allowed())

    def test_json_body_must_be_an_object(self):
        body = b"[]"
        instance = self.make_handler(
            {"Content-Length": str(len(body)), "Content-Type": "application/json"}, body
        )
        with self.assertRaises(agent_api.RequestError):
            instance.read_json()

    def test_oversized_body_is_rejected_before_read(self):
        instance = self.make_handler(
            {
                "Content-Length": str(agent_api.MAX_REQUEST_BYTES + 1),
                "Content-Type": "application/json",
            }
        )
        with self.assertRaises(agent_api.RequestError) as context:
            instance.read_json()
        self.assertEqual(context.exception.status, 413)


class SecurityHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent_api.handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        agent_api.general_limiter.clear("127.0.0.1")
        agent_api.chat_limiter.clear("127.0.0.1")
        agent_api.auth_failure_limiter.clear("127.0.0.1")

    def request(self, path, payload, origin=None):
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if origin:
            headers["Origin"] = origin
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        result = response.status, dict(response.getheaders()), response_body
        connection.close()
        return result

    def test_status_endpoint_and_security_headers(self):
        status, headers, _ = self.request("/api/status", {"password": "123456"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_cross_origin_request_is_rejected(self):
        status, _, _ = self.request(
            "/api/status", {"password": "123456"}, origin="https://evil.example"
        )
        self.assertEqual(status, 403)

    def test_repeated_bad_password_is_rate_limited(self):
        for _ in range(agent_api.AUTH_FAILURE_LIMIT):
            status, _, _ = self.request("/api/status", {"password": "wrong"})
            self.assertEqual(status, 403)
        status, headers, _ = self.request("/api/status", {"password": "wrong"})
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)


if __name__ == "__main__":
    unittest.main()
