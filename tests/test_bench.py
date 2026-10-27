from bench.traffic import run


def test_traffic_scenario_shows_each_control_working():
    s = run()["tenants"]
    assert s["support-bot"]["cache_hit_rate"] > 0.5 and s["support-bot"]["saved_by_cache_usd"] > 0
    assert s["support-bot"]["redactions"] > 300
    assert s["agentie"]["fallbacks"] == 300 and s["agentie"]["errors"] == 0
    blocked = s["clinica-mica"]["blocked"]
    assert blocked["rate_limited"] > 0 and blocked["budget"] > 0
    assert s["clinica-mica"]["cost_usd"] <= 0.02  # bugetul nu e depășit niciodată
