# Endpoint Monitor

A local HTTP endpoint monitor with scheduled checks, response-time tracking, recent history, a JSON API, and a live dashboard. It uses only the Python standard library.

## Run

Requires Python 3.9 or newer. From the repository root:

```powershell
py endpoint-monitor/server.py
```

Open <http://127.0.0.1:8765>. Stop the server with Ctrl+C. Monitor records are stored in `endpoint-monitor/data/monitors.json` by default. Set `ENDPOINT_MONITOR_DATA` to use another JSON file.

## API

- `GET /api/health` reports server status and monitor count.
- `GET /api/monitors` lists current status and bounded recent history.
- `POST /api/monitors` creates a monitor. JSON fields: `name`, `url`, and optional `interval_seconds`.
- `POST /api/monitors/{id}/check` runs an immediate check.
- `DELETE /api/monitors/{id}` removes a monitor and its history.

Checks run from the host machine and can reach any URL that machine can access. The server binds to loopback only; do not expose it to an untrusted network. Add only endpoints you trust. Checks time out after five seconds, intervals are limited to 5–3600 seconds, and at most 100 monitors are supported.

## Tests

```powershell
py -3 -m unittest discover -s endpoint-monitor/tests -v
```