"""Test rate limiter + metrics."""
import ipaddress
from types import SimpleNamespace

from coordinator.middleware import Metrics, TokenBucketLimiter, _client_ip


def test_rate_limit_basic():
    lim = TokenBucketLimiter(capacity=3, refill_per_sec=1.0)
    assert lim.allow("ip1")
    assert lim.allow("ip1")
    assert lim.allow("ip1")
    assert not lim.allow("ip1")  # 4th in same instant → denied
    # different IP unaffected
    assert lim.allow("ip2")


def test_rate_limit_refills():
    import time
    lim = TokenBucketLimiter(capacity=2, refill_per_sec=10.0)  # ~10 tokens/sec
    assert lim.allow("ip")
    assert lim.allow("ip")
    assert not lim.allow("ip")
    time.sleep(0.2)  # ~2 tokens refill
    assert lim.allow("ip")


def test_metrics_basic():
    m = Metrics()
    m.inc("hits")
    m.inc("hits")
    m.inc("hits", n=3)
    m.gauge("queue_depth", 5)
    m.observe("latency", 0.1)
    m.observe("latency", 0.2)

    out = m.render_prometheus()
    assert "hits 5" in out
    assert "queue_depth 5" in out
    assert "latency_count 2" in out


def test_metrics_with_labels():
    m = Metrics()
    m.inc("requests", labels={"path": "/submit", "status": 200})
    m.inc("requests", labels={"path": "/submit", "status": 401})

    out = m.render_prometheus()
    assert 'requests{path="/submit",status="200"} 1' in out
    assert 'requests{path="/submit",status="401"} 1' in out


# ----- X-Forwarded-For handling (audit F5) ----------------------------------


def _mk_request(peer: str, xff: str | None = None):
    """Build a minimal request object the _client_ip helper can read."""
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers={k.lower(): v for k, v in headers.items()},
    )


def test_xff_ignored_when_no_trusted_proxies():
    """Without configured proxies, XFF must not influence the IP — otherwise
    any attacker can spoof their IP."""
    req = _mk_request("203.0.113.10", xff="1.2.3.4, 5.6.7.8")
    assert _client_ip(req, None) == "203.0.113.10"
    assert _client_ip(req, []) == "203.0.113.10"


def test_xff_used_when_peer_is_trusted_proxy():
    """When the immediate peer is in the trusted CIDR, the right-most non-trusted
    XFF entry is the real client."""
    proxies = [ipaddress.ip_network("10.0.0.0/8")]
    req = _mk_request("10.0.0.1", xff="203.0.113.10, 10.0.0.2")
    assert _client_ip(req, proxies) == "203.0.113.10"


def test_xff_skips_trusted_chain():
    """Multi-hop trusted-proxy chain — strip them and find the real client."""
    proxies = [ipaddress.ip_network("10.0.0.0/8")]
    req = _mk_request("10.0.0.1", xff="203.0.113.10, 10.0.0.5, 10.0.0.2")
    assert _client_ip(req, proxies) == "203.0.113.10"


def test_xff_ignored_when_peer_not_trusted():
    """Even if XFF is present, an untrusted peer can't claim a different IP."""
    proxies = [ipaddress.ip_network("10.0.0.0/8")]
    req = _mk_request("8.8.8.8", xff="1.2.3.4")
    assert _client_ip(req, proxies) == "8.8.8.8"


def test_xff_malformed_falls_back():
    proxies = [ipaddress.ip_network("10.0.0.0/8")]
    req = _mk_request("10.0.0.1", xff="not-an-ip, also-bad")
    # All XFF entries are unparseable, fall back to peer
    assert _client_ip(req, proxies) == "10.0.0.1"


def test_xff_all_entries_trusted_falls_back():
    """If literally every XFF hop is itself a trusted proxy, we have no client
    to attest to — fall back to the immediate peer (still better than picking
    one of the trusted proxies)."""
    proxies = [ipaddress.ip_network("10.0.0.0/8")]
    req = _mk_request("10.0.0.1", xff="10.0.0.5, 10.0.0.2")
    assert _client_ip(req, proxies) == "10.0.0.1"
