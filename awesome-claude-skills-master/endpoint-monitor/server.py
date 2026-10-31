import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_DIR = Path(__file__).parent
MAX_BODY_BYTES = 16_384
MAX_MONITORS = 100
MAX_HISTORY = 30
CHECK_TIMEOUT_SECONDS = 5
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def utc_timestamp(epoch=None):
    value = datetime.fromtimestamp(epoch or time.time(), timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("URL must be a non-empty string of at most 2048 characters")
    if any(character.isspace() for character in value):
        raise ValueError("URL must not contain whitespace")
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise ValueError("URL is malformed") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials must not be embedded in the URL")
    if parsed.fragment:
        raise ValueError("URL must not include a fragment")
    return value


class MonitorStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.monitors = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return
            for monitor in data:
                if not isinstance(monitor, dict) or not ID_PATTERN.fullmatch(str(monitor.get("id", ""))):
                    continue
                try:
                    validate_url(monitor.get("url"))
                    if monitor.get("status") not in {"unknown", "up", "down"}:
                        continue
                    self.monitors[monitor["id"]] = monitor
                except (TypeError, ValueError):
                    continue
        except (OSError, json.JSONDecodeError):
            return

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(list(self.monitors.values()), temporary_file)
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def add(self, name, url, interval_seconds):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Name must contain 1 to 80 characters")
        validate_url(url)
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
            raise ValueError("Interval must be an integer")
        if not 5 <= interval_seconds <= 3600:
            raise ValueError("Interval must be between 5 and 3600 seconds")
        with self.lock:
            if len(self.monitors) >= MAX_MONITORS:
                raise ValueError(f"A maximum of {MAX_MONITORS} monitors is supported")
            if any(item["url"] == url for item in self.monitors.values()):
                raise ValueError("A monitor for this URL already exists")
            monitor_id = uuid.uuid4().hex
            self.monitors[monitor_id] = {
                "id": monitor_id,
                "name": name.strip(),
                "url": url,
                "interval_seconds": interval_seconds,
                "status": "unknown",
                "status_code": None,
                "latency_ms": None,
                "last_checked": None,
                "error": None,
                "history": [],
                "next_check_at": time.time(),
            }
            self._save()
            return self.public_record(self.monitors[monitor_id])

    def remove(self, monitor_id):
        with self.lock:
            if monitor_id not in self.monitors:
                return False
            del self.monitors[monitor_id]
            self._save()
            return True

    def get(self, monitor_id):
        with self.lock:
            monitor = self.monitors.get(monitor_id)
            return self.public_record(monitor) if monitor else None

    def list(self):
        with self.lock:
            return [self.public_record(item) for item in self.monitors.values()]

    def due(self, now=None):
        now = now or time.time()
        with self.lock:
            return [
                (monitor["id"], monitor["url"], monitor["interval_seconds"])
                for monitor in self.monitors.values()
                if monitor.get("next_check_at", 0) <= now
            ]

    def record_result(self, monitor_id, status_code, latency_ms, error, interval_seconds):
        with self.lock:
            monitor = self.monitors.get(monitor_id)
            if monitor is None:
                return None
            checked_at = utc_timestamp()
            status = "up" if status_code is not None and 200 <= status_code < 400 else "down"
            monitor.update(
                status=status,
                status_code=status_code,
                latency_ms=round(latency_ms, 1),
                last_checked=checked_at,
                error=error,
                next_check_at=time.time() + interval_seconds,
            )
            monitor["history"].append({
                "checked_at": checked_at,
                "status": status,
                "latency_ms": monitor["latency_ms"],
                "status_code": status_code,
            })
            monitor["history"] = monitor["history"][-MAX_HISTORY:]
            self._save()
            return self.public_record(monitor)

    @staticmethod
    def public_record(monitor):
        return {key: value for key, value in monitor.items() if key != "next_check_at"}


def check_endpoint(url, timeout=CHECK_TIMEOUT_SECONDS):
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "EndpointMonitor/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.status
    except urllib.error.HTTPError as error:
        status_code = error.code
        error.close()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        reason = getattr(error, "reason", error)
        return None, (time.monotonic() - started) * 1000, str(reason)[:240]
    latency_ms = (time.monotonic() - started) * 1000
    error_message = None if 200 <= status_code < 400 else f"HTTP {status_code}"
    return status_code, latency_ms, error_message


class MonitorService:
    def __init__(self, store):
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="endpoint-check")
        self.stop_event = threading.Event()
        self.in_flight = set()
        self.lock = threading.Lock()
        self.scheduler = threading.Thread(target=self._schedule, daemon=True)

    def start(self):
        self.scheduler.start()

    def stop(self):
        self.stop_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _schedule(self):
        while not self.stop_event.wait(1):
            for monitor_id, url, interval in self.store.due():
                with self.lock:
                    if monitor_id in self.in_flight:
                        continue
                    self.in_flight.add(monitor_id)
                self.executor.submit(self._run_check, monitor_id, url, interval)

    def _run_check(self, monitor_id, url, interval):
        try:
            result = check_endpoint(url)
            self.store.record_result(monitor_id, *result, interval)
        finally:
            with self.lock:
                self.in_flight.discard(monitor_id)

    def check_now(self, monitor_id):
        monitor = self.store.get(monitor_id)
        if monitor is None:
            return None
        status_code, latency_ms, error = check_endpoint(monitor["url"])
        return self.store.record_result(
            monitor_id, status_code, latency_ms, error, monitor["interval_seconds"]
        )


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server_version = "EndpointMonitor/1.0"

    def log_message(self, _format, *_args):
        return

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length must be an integer") from error
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            raise ValueError(f"Request body must be between 1 and {MAX_BODY_BYTES} bytes")
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def do_GET(self):
        if self.path == "/api/health":
            self._send_json(200, {"status": "ok", "monitors": len(self.server.store.list())})
            return
        if self.path == "/api/monitors":
            self._send_json(200, {"monitors": self.server.store.list()})
            return
        static_assets = {
            "/app.js": ("application/javascript; charset=utf-8", "app.js"),
            "/styles.css": ("text/css; charset=utf-8", "styles.css"),
        }
        if self.path in static_assets:
            content_type, file_name = static_assets[self.path]
            body = (APP_DIR / file_name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/" or self.path == "/index.html":
            body = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/monitors":
            try:
                payload = self._read_json()
                monitor = self.server.store.add(
                    payload.get("name"),
                    payload.get("url"),
                    payload.get("interval_seconds", 60),
                )
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            self._send_json(201, {"monitor": monitor})
            return
        match = re.fullmatch(r"/api/monitors/([0-9a-f]{32})/check", self.path)
        if match:
            monitor = self.server.service.check_now(match.group(1))
            if monitor is None:
                self._send_json(404, {"error": "Monitor not found"})
            else:
                self._send_json(200, {"monitor": monitor})
            return
        self._send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        match = re.fullmatch(r"/api/monitors/([0-9a-f]{32})", self.path)
        if not match:
            self._send_json(404, {"error": "Not found"})
            return
        if not self.server.store.remove(match.group(1)):
            self._send_json(404, {"error": "Monitor not found"})
            return
        self._send_json(200, {"deleted": True})


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        super().__init__(address, MonitorRequestHandler)
        self.store = store
        self.service = MonitorService(store)

    def server_close(self):
        self.service.stop()
        super().server_close()


def main():
    data_path = Path(os.environ.get("ENDPOINT_MONITOR_DATA", APP_DIR / "data" / "monitors.json"))
    store = MonitorStore(data_path)
    server = MonitorHTTPServer(("127.0.0.1", 8765), store)
    server.service.start()
    print("Endpoint Monitor is running at http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()