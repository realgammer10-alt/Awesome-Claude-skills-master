import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class MonitorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = server.MonitorStore(Path(self.temporary_directory.name) / "monitors.json")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_add_persists_and_reload_hides_scheduler_fields(self):
        monitor = self.store.add(" API ", "https://example.com/health", 30)
        reloaded = server.MonitorStore(self.store.path)

        self.assertEqual(monitor["name"], "API")
        self.assertEqual(reloaded.get(monitor["id"])["url"], "https://example.com/health")
        self.assertNotIn("next_check_at", reloaded.get(monitor["id"]))

    def test_rejects_bad_url_name_interval_and_duplicates(self):
        invalid_values = [
            ("API", "ftp://example.com", 30),
            ("API", "https://user:pass@example.com", 30),
            (" " * 2, "https://example.com", 30),
            ("API", "https://example.com", True),
            ("API", "https://example.com", 4),
        ]
        for name, url, interval in invalid_values:
            with self.subTest(name=name, url=url, interval=interval):
                with self.assertRaises(ValueError):
                    self.store.add(name, url, interval)
        self.store.add("API", "https://example.com", 30)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.add("Duplicate", "https://example.com", 30)

    def test_record_result_tracks_status_and_bounds_history(self):
        monitor = self.store.add("API", "http://localhost/health", 5)
        for _ in range(server.MAX_HISTORY + 2):
            result = self.store.record_result(monitor["id"], 503, 12.34, "HTTP 503", 5)

        self.assertEqual(result["status"], "down")
        self.assertEqual(result["status_code"], 503)
        self.assertEqual(len(result["history"]), server.MAX_HISTORY)


class MonitorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        store = server.MonitorStore(Path(self.temporary_directory.name) / "monitors.json")
        self.httpd = server.MonitorHTTPServer(("127.0.0.1", 0), store)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request(self, path, method="GET", payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())
        with response:
            return response.code, json.loads(response.read())

    def test_health_and_monitor_crud(self):
        status, health = self.request("/api/health")
        self.assertEqual((status, health["status"]), (200, "ok"))

        status, created = self.request(
            "/api/monitors",
            "POST",
            {"name": "Test API", "url": "http://127.0.0.1/health", "interval_seconds": 10},
        )
        monitor_id = created["monitor"]["id"]
        self.assertEqual(status, 201)

        status, listing = self.request("/api/monitors")
        self.assertEqual((status, len(listing["monitors"])), (200, 1))
        status, deleted = self.request(f"/api/monitors/{monitor_id}", "DELETE")
        self.assertEqual((status, deleted["deleted"]), (200, True))

    def test_dashboard_assets_are_served_with_security_headers(self):
        response = urllib.request.urlopen(self.base_url, timeout=2)
        with response:
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertIn("Endpoint Monitor", response.read().decode("utf-8"))
        for path, content_type in (("/app.js", "application/javascript"), ("/styles.css", "text/css")):
            response = urllib.request.urlopen(self.base_url + path, timeout=2)
            with response:
                self.assertIn(content_type, response.headers["Content-Type"])
                self.assertGreater(int(response.headers["Content-Length"]), 0)
        status, error = self.request("/server.py")
        self.assertEqual((status, error["error"]), (404, "Not found"))

    def test_invalid_payload_and_unknown_routes_return_json_errors(self):
        status, error = self.request("/api/monitors", "POST", {"name": "bad", "url": "file:///etc/passwd"})
        self.assertEqual(status, 400)
        self.assertIn("http or https", error["error"])
        status, error = self.request("/missing")
        self.assertEqual((status, error["error"]), (404, "Not found"))

    def test_http_failure_is_reported_as_down(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class FailingEndpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        endpoint = ThreadingHTTPServer(("127.0.0.1", 0), FailingEndpoint)
        thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        thread.start()
        try:
            code, latency, error = server.check_endpoint(
                f"http://127.0.0.1:{endpoint.server_port}/health", timeout=2
            )
        finally:
            endpoint.shutdown()
            endpoint.server_close()
            thread.join(timeout=2)

        self.assertEqual(code, 503)
        self.assertGreaterEqual(latency, 0)
        self.assertEqual(error, "HTTP 503")

    @patch("server.check_endpoint", return_value=(200, 4.2, None))
    def test_manual_check_updates_status(self, check_endpoint):
        _, created = self.request(
            "/api/monitors",
            "POST",
            {"name": "Test API", "url": "http://127.0.0.1/health", "interval_seconds": 10},
        )
        monitor_id = created["monitor"]["id"]
        status, result = self.request(f"/api/monitors/{monitor_id}/check", "POST")
        self.assertEqual((status, result["monitor"]["status"]), (200, "up"))
        check_endpoint.assert_called_once()


if __name__ == "__main__":
    unittest.main()