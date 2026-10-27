import pytest

from gateway.core import Gateway
from gateway.tenants import TenantRegistry
from gateway.upstream import FakeUpstream, UpstreamError

KEY = "sk-agentie"


@pytest.fixture
def gw():
    t = [0.0]
    reg = TenantRegistry(clock=lambda: t[0])
    reg.add("agentie", KEY, ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"), rpm=600,
            monthly_budget_usd=5.0)
    reg.add("mic", "sk-mic", ("claude-haiku-5-5",), rpm=6, monthly_budget_usd=0.001, redact_pii=False)
    g = Gateway(reg, FakeUpstream())
    g.clock = t
    return g


def req(text="Salut", model="claude-opus-5-5", **kw):
    return {"model": model, "max_tokens": 256, "messages": [{"role": "user", "content": text}], **kw}


def test_happy_path_headers_and_trace(gw):
    r = gw.handle(KEY, req())
    assert r.status == 200 and r.body["content"][0]["text"] == "Am primit: Salut"
    assert float(r.headers["x-gateway-cost-usd"]) > 0
    t = gw.traces.summary()["tenants"]["agentie"]
    assert t["served"] == 1 and t["cost_usd"] > 0


def test_pii_never_reaches_provider_but_user_gets_it_back(gw):
    r = gw.handle(KEY, req("Sunați-mă la 069 123 456 sau scrieți la ana@mail.md"))
    sent = gw.upstream.calls[-1]["messages"][0]["content"]
    assert "069 123 456" not in sent and "ana@mail.md" not in sent
    assert "[PHONE_1]" in sent
    assert r.body["content"][0]["text"] == "Am primit: Sunați-mă la 069 123 456 sau scrieți la ana@mail.md"
    assert r.headers["x-gateway-redactions"] == "2"


def test_redaction_can_be_disabled_per_tenant(gw):
    gw.tenants.tenants["mic"].monthly_budget_usd = 10
    gw.handle("sk-mic", req("ana@mail.md", model="claude-haiku-5-5"))
    assert "ana@mail.md" in gw.upstream.calls[-1]["messages"][0]["content"]


@pytest.mark.parametrize("key,body,status", [
    (None, req(), 401),
    ("sk-gresit", req(), 401),
    ("sk-mic", req(), 403),                                  # opus nu e permis
    (KEY, {"model": "claude-opus-5-5", "messages": []}, 400),
    (KEY, req(max_tokens=0), 400),
])
def test_rejections(gw, key, body, status):
    assert gw.handle(key, body).status == status


def test_rate_limit_returns_429_with_retry_after(gw):
    gw.tenants.tenants["mic"].monthly_budget_usd = 10
    first = gw.handle("sk-mic", req(model="claude-haiku-5-5"))
    second = gw.handle("sk-mic", req(model="claude-haiku-5-5"))
    assert first.status == 200 and second.status == 429 and second.headers["retry-after"] == "10"


def test_budget_blocks_before_calling_provider(gw):
    r = gw.handle("sk-mic", {**req(model="claude-haiku-5-5"), "max_tokens": 50_000})
    assert r.status == 402 and gw.upstream.calls == []


def test_fallback_to_next_allowed_model(gw):
    gw.upstream.failing_models = {"claude-opus-5-5"}
    r = gw.handle(KEY, req())
    assert r.status == 200 and r.body["model"] == "claude-sonnet-5-5"
    assert r.headers["x-gateway-fallback"] == "claude-opus-5-5->claude-sonnet-5-5"
    assert gw.traces.summary()["tenants"]["agentie"]["fallbacks"] == 1


def test_all_models_down_is_529(gw):
    gw.upstream.failing_models = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
    r = gw.handle(KEY, req())
    assert r.status == 529 and r.body["error"]["type"] == "upstream_error"


def test_non_retryable_error_does_not_fall_back(gw):
    class BadRequest(FakeUpstream):
        def complete(self, model, system, messages, max_tokens):
            self.calls.append(model)
            raise UpstreamError("400 prompt prea lung", retryable=False, status=400)
    gw.upstream = BadRequest()
    r = gw.handle(KEY, req())
    assert r.status == 502 and gw.upstream.calls == ["claude-opus-5-5"]


def test_cache_hit_is_free_and_isolated(gw):
    a = gw.handle(KEY, req(cache=True))
    b = gw.handle(KEY, req(cache=True))
    assert a.headers["x-gateway-cache"] == "miss" and b.headers["x-gateway-cache"] == "hit"
    assert b.headers["x-gateway-cost-usd"] == "0" and len(gw.upstream.calls) == 1
    s = gw.traces.summary()["tenants"]["agentie"]
    assert s["cache_hit_rate"] == 0.5 and s["saved_by_cache_usd"] > 0


def test_cached_answer_restores_this_requests_pii(gw):
    gw.handle(KEY, req("Contact: ion@x.md", cache=True))
    b = gw.handle(KEY, req("Contact: ion@x.md", cache=True))
    assert b.body["content"][0]["text"] == "Am primit: Contact: ion@x.md"
