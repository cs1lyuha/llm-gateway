"""API HTTP compatibil (pe subsetul text) cu Messages API de la Anthropic.

    POST /v1/messages        cerere obișnuită; cheia în `x-api-key` sau `Authorization: Bearer`
    GET  /admin/summary      rezumat per client (cere GATEWAY_ADMIN_KEY)
    GET  /metrics            format Prometheus (cere GATEWAY_ADMIN_KEY)
    GET  /health

Clienții se configurează într-un JSON (`GATEWAY_TENANTS`, implicit tenants.json):
    [{"id": "clinica", "key_sha256": "...", "models": ["claude-haiku-5-5"], "rpm": 60, "monthly_budget_usd": 20}]
Cheia generată de `python -m gateway.app add-tenant ...` se afișează o singură dată.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from gateway.core import Gateway
from gateway.tenants import Tenant, TenantRegistry, hash_key
from gateway.traces import TraceStore


def load_registry(path: str | Path) -> TenantRegistry:
    reg = TenantRegistry()
    if Path(path).exists():
        for t in json.loads(Path(path).read_text(encoding="utf-8")):
            reg.tenants[t["id"]] = Tenant(t["id"], t["key_sha256"], tuple(t["models"]), t.get("rpm", 60),
                                          t.get("monthly_budget_usd", 50.0), t.get("redact_pii", True))
    return reg


def create_app(gateway: Gateway | None = None, *, admin_key: str | None = None) -> FastAPI:
    if gateway is None:  # pragma: no cover - configurația de producție
        from gateway.upstream import AnthropicUpstream
        gateway = Gateway(load_registry(os.environ.get("GATEWAY_TENANTS", "tenants.json")), AnthropicUpstream(),
                          TraceStore(os.environ.get("GATEWAY_DB", "gateway.db")))
    admin = admin_key if admin_key is not None else os.environ.get("GATEWAY_ADMIN_KEY", "")
    app = FastAPI(title="llm-gateway", version="0.1.0")

    def require_admin(key: str | None) -> None:
        if not admin or not key or not hmac.compare_digest(admin, key):
            raise HTTPException(401, "cheie de admin invalidă")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/v1/messages")
    async def messages(request: Request, x_api_key: str | None = Header(default=None),
                       authorization: str | None = Header(default=None)):
        key = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"type": "error", "error": {"type": "invalid_request_error", "message": "JSON invalid"}},
                                status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"type": "error", "error": {"type": "invalid_request_error",
                                                            "message": "corpul trebuie să fie un obiect"}}, status_code=400)
        reply = gateway.handle(key, body)
        return JSONResponse(reply.body, status_code=reply.status, headers=reply.headers)

    @app.get("/admin/summary")
    def summary(x_admin_key: str | None = Header(default=None)) -> dict:
        require_admin(x_admin_key)
        out = gateway.traces.summary()
        out["cache"] = {"entries": len(gateway.cache.entries), "hit_rate": round(gateway.cache.hit_rate, 3)}
        return out

    @app.get("/metrics")
    def metrics(x_admin_key: str | None = Header(default=None)) -> PlainTextResponse:
        require_admin(x_admin_key)
        return PlainTextResponse(gateway.traces.prometheus(), media_type="text/plain; version=0.0.4")

    return app


def add_tenant(path: str, tenant_id: str, models: list[str], rpm: int, budget: float) -> str:
    key = "gw_" + secrets.token_urlsafe(24)
    data = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else []
    data = [t for t in data if t["id"] != tenant_id]
    data.append({"id": tenant_id, "key_sha256": hash_key(key), "models": models, "rpm": rpm,
                 "monthly_budget_usd": budget, "redact_pii": True})
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return key


if __name__ == "__main__":  # python -m gateway.app add-tenant clinica claude-haiku-5-5 --rpm 60 --budget 20
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["add-tenant"])
    p.add_argument("tenant")
    p.add_argument("models", nargs="+")
    p.add_argument("--rpm", type=int, default=60)
    p.add_argument("--budget", type=float, default=50.0)
    p.add_argument("--file", default=os.environ.get("GATEWAY_TENANTS", "tenants.json"))
    a = p.parse_args()
    print(f"Cheia pentru {a.tenant} (se afișează o singură dată):\n{add_tenant(a.file, a.tenant, a.models, a.rpm, a.budget)}")
    sys.exit(0)
