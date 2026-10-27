import os

import pytest
from fastapi.testclient import TestClient

from gateway.app import add_tenant, create_app, load_registry
from gateway.core import Gateway
from gateway.upstream import FakeUpstream


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / "tenants.json")
    key = add_tenant(path, "clinica", ["claude-haiku-5-5"], 60, 5.0)
    gw = Gateway(load_registry(path), FakeUpstream())
    return TestClient(create_app(gw, admin_key="adm")), key, path


BODY = {"model": "claude-haiku-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "Salut"}]}


def test_tenant_file_stores_only_hash(setup):
    _, key, path = setup
    assert key not in open(path).read()
    assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_messages_with_x_api_key_and_bearer(setup):
    client, key, _ = setup
    r1 = client.post("/v1/messages", json=BODY, headers={"x-api-key": key})
    r2 = client.post("/v1/messages", json=BODY, headers={"Authorization": f"Bearer {key}"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["content"][0]["text"] == "Am primit: Salut"
    assert "x-gateway-trace-id" in r1.headers


def test_bad_json_and_bad_shapes(setup):
    client, key, _ = setup
    h = {"x-api-key": key, "content-type": "application/json"}
    assert client.post("/v1/messages", content=b"{nu e json", headers=h).status_code == 400
    assert client.post("/v1/messages", json=[1, 2], headers=h).status_code == 400
    assert client.post("/v1/messages", json=BODY).status_code == 401


def test_admin_endpoints(setup):
    client, key, _ = setup
    client.post("/v1/messages", json=BODY, headers={"x-api-key": key})
    assert client.get("/admin/summary").status_code == 401
    s = client.get("/admin/summary", headers={"x-admin-key": "adm"}).json()
    assert s["tenants"]["clinica"]["served"] == 1
    m = client.get("/metrics", headers={"x-admin-key": "adm"}).text
    assert 'llm_gateway_requests_total{tenant="clinica",status="ok"} 1' in m
