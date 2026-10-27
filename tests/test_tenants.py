import pytest

from gateway.tenants import (AuthError, BudgetExceeded, ModelNotAllowed, RateLimited, TenantRegistry, cost_usd)


def registry(clock=None):
    t = [0.0]
    reg = TenantRegistry(clock=clock or (lambda: t[0]))
    reg.add("clinica", "sk-clinica", ("claude-haiku-5-5",), rpm=60, monthly_budget_usd=1.0)
    reg.add("agentie", "sk-agentie", ("claude-opus-5-5", "claude-haiku-5-5"), rpm=6)
    return reg, t


def test_authenticate_with_hashed_keys():
    reg, _ = registry()
    assert reg.authenticate("sk-clinica").id == "clinica"
    assert "sk-clinica" not in str(reg.tenants)
    for bad in (None, "", "sk-ghicit"):
        with pytest.raises(AuthError):
            reg.authenticate(bad)


def test_model_allowlist():
    reg, _ = registry()
    with pytest.raises(ModelNotAllowed):
        reg.check_model(reg.tenants["clinica"], "claude-opus-5-5")


def test_rate_limit_burst_then_refill():
    reg, t = registry()
    agentie = reg.tenants["agentie"]  # 6 rpm → rafală de 1, apoi 1 la 10 s
    reg.check_rate(agentie)
    with pytest.raises(RateLimited) as info:
        reg.check_rate(agentie)
    assert info.value.retry_after == pytest.approx(10.0)
    t[0] = 10.0
    reg.check_rate(agentie)


def test_budget_precheck_uses_worst_case():
    reg, _ = registry()
    clinica = reg.tenants["clinica"]
    assert reg.check_budget(clinica, 0.0, "claude-haiku-5-5", "salut", 1000) < 0.001
    with pytest.raises(BudgetExceeded):
        reg.check_budget(clinica, 0.9999, "claude-haiku-5-5", "x" * 4000, 64000)


def test_cost_with_cache():
    assert cost_usd("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert cost_usd("claude-opus-5-5", 0, 0, cache_read=1_000_000) == pytest.approx(0.4)
    assert cost_usd("claude-opus-5-5", 0, 0, cache_write=1_000_000) == pytest.approx(5.0)


def test_unknown_model_price_rejected():
    with pytest.raises(ValueError):
        TenantRegistry().add("x", "k", ("gpt-fantezie",))
