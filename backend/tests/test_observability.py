import unittest

from fastapi import Request
from observability import RequestMetrics


def _request(path: str) -> Request:
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "https", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [], "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443), "route": None,
    })


class RequestMetricsTests(unittest.TestCase):
    def test_snapshot_is_bounded_and_never_includes_query_data(self):
        metrics = RequestMetrics(max_samples=2)
        request = _request("/api/movies")
        metrics.record(request, 200, 10)
        metrics.record(request, 503, 30)
        metrics.record(request, 200, 20)

        snapshot = metrics.snapshot()["routes"]

        self.assertEqual(snapshot, [{
            "route": "GET /api/movies", "samples": 2, "avg_ms": 25.0,
            "p95_ms": 20, "errors_5xx": 1,
        }])
