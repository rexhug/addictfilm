"""Bounded request observability shared by the public FastAPI application.

This intentionally stays dependency-free: Sentry receives sampled traces, while
the protected admin endpoint exposes a short per-process latency window for
operational debugging.  It never stores a user id, query string, or request body.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response


def append_vary(response: Response, field: str) -> None:
    """Add a Vary value without overwriting one installed by another middleware."""
    existing = [value.strip() for value in response.headers.get("Vary", "").split(",") if value.strip()]
    if field.lower() not in {value.lower() for value in existing}:
        response.headers["Vary"] = ", ".join([*existing, field])


class RequestMetrics:
    """A bounded, privacy-preserving timing window for route-level operations."""

    def __init__(self, max_samples: int) -> None:
        self._samples: dict[str, deque[tuple[float, int]]] = defaultdict(
            lambda: deque(maxlen=max_samples)
        )

    def record(self, request: Request, status: int, elapsed_ms: float) -> None:
        route = request.scope.get("route")
        path = getattr(route, "path", None) or request.url.path
        self._samples[f"{request.method} {path}"].append((elapsed_ms, status))

    def snapshot(self) -> dict:
        routes = []
        for route, samples in self._samples.items():
            if not samples:
                continue
            timings = sorted(sample[0] for sample in samples)
            p95_index = min(len(timings) - 1, max(0, int(len(timings) * 0.95) - 1))
            routes.append({
                "route": route,
                "samples": len(samples),
                "avg_ms": round(sum(timings) / len(timings), 1),
                "p95_ms": round(timings[p95_index], 1),
                "errors_5xx": sum(1 for _elapsed, status in samples if status >= 500),
            })
        return {"routes": sorted(routes, key=lambda item: item["p95_ms"], reverse=True)}


async def observe_request(
    metrics: RequestMetrics,
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    logger: logging.Logger,
) -> Response:
    """Add safe response headers and record latency for one request."""
    started = time.perf_counter()
    response: Response | None = None
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics.record(request, status, elapsed_ms)
    assert response is not None
    if elapsed_ms >= 750:
        logger.warning("Slow request: %s %s -> %s in %.0fms",
                       request.method, request.url.path, response.status_code, elapsed_ms)
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.0f}"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=()")
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/avatar/"):
        response.headers.setdefault("Cache-Control", "private, no-store")
        append_vary(response, "X-Init-Data")
    return response
