import pytest

from gateway.cache import ResponseCache, cache_key
from gateway.traces import Trace, TraceStore
from gateway.upstream import FakeUpstream, UpstreamError

MSGS = [{"role": "user", "content": "Salut"}]


def test_cache_key_isolates_tenants_and_ignores_whitespace():
    a = cache_key("t1", "m", "sys", MSGS, 100)
    assert a == cache_key("t1", "m", " sys ", [{"role": "user", "content": " Salut "}], 100)
    assert a != cache_key("t2", "m", "sys", MSGS, 100)
    assert a != cache_key("t1", "m", "sys", MSGS, 200)


def test_cache_ttl_and_lru():
    t = [0.0]
    c = ResponseCache(max_entries=2, ttl_s=10, clock=lambda: t[0])
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1          # „a” devine cel mai recent
    c.put("c", 3)                   # iese „b”
    assert c.get("b") is None and c.get("c") == 3
    t[0] = 11
    assert c.get("a") is None       # expirat
    assert c.hit_rate == pytest.approx(2 / 4)


def test_fake_upstream_fails_on_demand():
    up = FakeUpstream(failing_models={"claude-opus-5-5"})
    with pytest.raises(UpstreamError) as info:
        up.complete("claude-opus-5-5", "", MSGS, 10)
    assert info.value.retryable
    assert up.complete("claude-haiku-5-5", "", MSGS, 10).text == "Am primit: Salut"


def test_trace_summary_and_prometheus():
    store = TraceStore()
    store.record(Trace("1", "2026-10-25T10:00:00+00:00", "clinica", "claude-haiku-5-5", "claude-haiku-5-5",
                       latency_ms=120, input_tokens=100, output_tokens=20, cost_usd=0.00002, redactions=2))
    store.record(Trace("2", "2026-10-25T10:01:00+00:00", "clinica", "claude-haiku-5-5", "claude-haiku-5-5",
                       status="cache_hit", latency_ms=2, cache_hit=True, saved_usd=0.00002))
    store.record(Trace("3", "2026-10-25T10:02:00+00:00", "clinica", "claude-haiku-5-5", status="rate_limited",
                       http_status=429))
    s = store.summary()["tenants"]["clinica"]
    assert s["served"] == 2 and s["cache_hit_rate"] == 0.5 and s["blocked"] == {"rate_limited": 1}
    assert s["redactions"] == 2
    prom = store.prometheus()
    assert 'llm_gateway_requests_total{tenant="clinica",status="cache_hit"} 1' in prom
    assert 'direction="input"} 100' in prom


def test_spent_this_month_ignores_other_months():
    from datetime import datetime, timezone
    store = TraceStore()
    store.record(Trace("1", "2026-09-30T23:00:00+00:00", "x", "m", cost_usd=5.0))
    store.record(Trace("2", "2026-10-01T00:00:00+00:00", "x", "m", cost_usd=1.5))
    assert store.spent_this_month("x", datetime(2026, 10, 25, tzinfo=timezone.utc)) == 1.5
