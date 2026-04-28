"""Rate limiting + Prometheus metrics middleware."""
from __future__ import annotations

import ipaddress
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


# ----------------------------- Rate limiter ----------------------------------


class TokenBucketLimiter:
    """Simple in-memory per-key token bucket. Persists nothing; resets on
    Coordinator restart (acceptable since real defense is KYA + bond)."""

    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = capacity
        self.refill = refill_per_sec
        self._buckets: dict[str, tuple[float, float]] = {}  # key → (tokens, last_ts)

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        # refill
        tokens = min(self.capacity, tokens + (now - last) * self.refill)
        if tokens < cost:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - cost, now)
        return True


def _client_ip(
    request: Request,
    trusted_proxies: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None,
) -> str:
    """Resolve the real client IP, handling reverse-proxy X-Forwarded-For.

    Closes audit F5: under nginx / CloudFront / ALB, request.client.host is
    the proxy address — without this, all traffic shares one bucket.

    Algorithm (matches uvicorn / starlette --proxy-headers behavior):
      1. If the immediate peer (request.client.host) is in trusted_proxies,
         use the LAST entry of X-Forwarded-For — that's the closest hop the
         trusted proxy attests to. Otherwise X-Forwarded-For is unsigned and
         attacker-spoofable, so we ignore it.
      2. If trusted_proxies is empty/None, just use the immediate peer
         (no proxy expected).
    """
    peer = request.client.host if request.client else "unknown"
    if not trusted_proxies:
        return peer
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    is_trusted = any(peer_addr in net for net in trusted_proxies)
    if not is_trusted:
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return peer
    # XFF format: "client, proxy1, proxy2". The right-most entry is the most
    # recent (trusted) proxy; the LEFT-most is the original client. We take
    # the right-most NON-trusted entry by scanning right-to-left.
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    for candidate in reversed(parts):
        try:
            cand_addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not any(cand_addr in net for net in trusted_proxies):
            return candidate
    # Everything in XFF is itself a trusted proxy — fall back to immediate peer
    return peer


def install_rate_limit(
    app,
    limiter: TokenBucketLimiter,
    forge_limiter: TokenBucketLimiter | None = None,
    trusted_proxies: list[str] | None = None,
):
    """Apply per-IP rate limit across all /v1/ endpoints.

    Two tiers:
      - `forge_limiter` (tight) for /v1/forge/* — each call drives an LLM
        request + chain reads, so per-IP burst must be small.
      - `limiter` (default) for everything else under /v1/.

    `trusted_proxies` is a list of CIDR strings (e.g. ["10.0.0.0/8", "::1/128"])
    representing the reverse proxies the Coordinator sits behind. When the
    immediate connection comes from one of these, the right-most non-trusted
    entry of X-Forwarded-For is used as the real client IP. Without this,
    every request looks like it's from the proxy and rate limiting collapses.
    """
    # Pre-compile networks for fast membership check
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in trusted_proxies or []:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            # Skip malformed CIDR entries; production deploy should validate config
            continue

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        path = request.url.path
        if path.startswith("/v1/"):
            ip = _client_ip(request, nets)
            if forge_limiter is not None and path.startswith("/v1/forge/"):
                if not forge_limiter.allow(ip):
                    raise HTTPException(
                        status_code=429,
                        detail="rate limit exceeded (forge)",
                    )
            else:
                if not limiter.allow(ip):
                    raise HTTPException(
                        status_code=429,
                        detail="rate limit exceeded",
                    )
        return await call_next(request)


# ----------------------------- Metrics ---------------------------------------


class Metrics:
    """In-process Prometheus-style counters / gauges. Exposed at /metrics."""

    def __init__(self):
        self.counters: dict[str, int] = defaultdict(int)
        self.gauges: dict[str, float] = {}
        self.histograms: dict[str, deque] = defaultdict(lambda: deque(maxlen=1024))

    def inc(self, name: str, n: int = 1, labels: dict | None = None):
        self.counters[self._key(name, labels)] += n

    def gauge(self, name: str, value: float, labels: dict | None = None):
        self.gauges[self._key(name, labels)] = value

    def observe(self, name: str, value: float, labels: dict | None = None):
        self.histograms[self._key(name, labels)].append(value)

    @staticmethod
    def _key(name: str, labels: dict | None) -> str:
        if not labels:
            return name
        labelstr = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{labelstr}}}"

    def render_prometheus(self) -> str:
        """Render in Prometheus text exposition format."""
        lines = []
        for k, v in self.counters.items():
            lines.append(f"{k} {v}")
        for k, v in self.gauges.items():
            lines.append(f"{k} {v}")
        # Simple histogram: emit count + sum + p50 + p95 + p99
        for k, vals in self.histograms.items():
            if not vals:
                continue
            arr = sorted(vals)
            n = len(arr)
            total = sum(arr)
            p50 = arr[int(n * 0.50)]
            p95 = arr[int(n * 0.95)]
            p99 = arr[int(n * 0.99)] if n > 100 else arr[-1]
            base = k.split("{")[0]
            label_suffix = ""
            if "{" in k:
                label_suffix = "{" + k.split("{", 1)[1]
            sep = "," if label_suffix else ""
            lines.append(f"{base}_count{label_suffix} {n}")
            lines.append(f"{base}_sum{label_suffix} {total}")
            lines.append(f"{base}_p50{label_suffix} {p50}")
            lines.append(f"{base}_p95{label_suffix} {p95}")
            lines.append(f"{base}_p99{label_suffix} {p99}")
        return "\n".join(lines) + "\n"


def install_metrics(app, metrics: Metrics):
    """Add request-counting middleware + /metrics endpoint."""
    from fastapi.responses import PlainTextResponse

    @app.middleware("http")
    async def _count_requests(request: Request, call_next):
        t0 = time.monotonic()
        try:
            response = await call_next(request)
            metrics.inc(
                "ardi_http_requests_total",
                labels={"path": request.url.path, "status": response.status_code},
            )
            return response
        finally:
            dt = time.monotonic() - t0
            metrics.observe(
                "ardi_http_duration_seconds",
                dt,
                labels={"path": request.url.path},
            )

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics_endpoint():
        return metrics.render_prometheus()
