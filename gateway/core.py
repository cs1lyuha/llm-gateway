"""Inima gateway-ului: ordinea controalelor pentru fiecare cerere.

    cheie API → model permis → rate limit → buget (cazul cel mai rău)
      → mascare PII → cache? → provider (cu fallback pe alt model permis) → demascare
      → cost real → trace

Fiecare refuz are statusul HTTP potrivit (401, 403, 429, 402, 502), iar fiecare
cerere, refuzată sau nu, lasă un trace.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from gateway.cache import ResponseCache, cache_key
from gateway.redact import Redaction, redact
from gateway.tenants import (AuthError, BudgetExceeded, ModelNotAllowed, RateLimited, TenantRegistry, cost_usd)
from gateway.traces import Trace, TraceStore, now_iso
from gateway.upstream import Upstream, UpstreamError

FALLBACKS = {
    "claude-opus-5-5": ["claude-sonnet-5-5", "claude-haiku-5-5"],
    "claude-sonnet-5-5": ["claude-haiku-5-5"],
    "claude-haiku-5-5": [],
}


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=200_000)


class MessagesRequest(BaseModel):
    model: str
    max_tokens: int = Field(ge=1, le=64_000)
    system: str = Field(default="", max_length=200_000)
    messages: list[Message] = Field(min_length=1, max_length=500)
    cache: bool = False  # opțiune a gateway-ului: folosește cache-ul exact


@dataclass
class Reply:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def _error(status: int, kind: str, message: str, trace_id: str, **headers: str) -> Reply:
    return Reply(status, {"type": "error", "error": {"type": kind, "message": message}},
                 {"x-gateway-trace-id": trace_id, **headers})


class Gateway:
    def __init__(self, tenants: TenantRegistry, upstream: Upstream, traces: TraceStore | None = None,
                 cache: ResponseCache | None = None) -> None:
        self.tenants = tenants
        self.upstream = upstream
        self.traces = traces or TraceStore()
        self.cache = cache or ResponseCache()

    def handle(self, api_key: str | None, body: dict[str, Any]) -> Reply:
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex[:16]
        trace = Trace(trace_id, now_iso(), "anonim", str(body.get("model", "?")))
        reply = self._handle(api_key, body, trace)
        trace.http_status = reply.status
        trace.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        self.traces.record(trace)
        reply.headers["x-gateway-trace-id"] = trace_id
        return reply

    def _handle(self, api_key: str | None, body: dict[str, Any], trace: Trace) -> Reply:
        tid = trace.trace_id
        try:
            tenant = self.tenants.authenticate(api_key)
        except AuthError as err:
            trace.status, trace.error = "forbidden", str(err)
            return _error(401, "authentication_error", str(err), tid)
        trace.tenant = tenant.id
        try:
            req = MessagesRequest.model_validate(body)
        except ValidationError as err:
            trace.status, trace.error = "invalid", "validare"
            return _error(400, "invalid_request_error", "; ".join(e["msg"] for e in err.errors()[:3]), tid)
        try:
            self.tenants.check_model(tenant, req.model)
            self.tenants.check_rate(tenant)
            prompt_text = req.system + "".join(m.content for m in req.messages)
            self.tenants.check_budget(tenant, self.traces.spent_this_month(tenant.id), req.model, prompt_text,
                                      req.max_tokens)
        except ModelNotAllowed as err:
            trace.status, trace.error = "forbidden", str(err)
            return _error(403, "permission_error", str(err), tid)
        except RateLimited as err:
            trace.status, trace.error = "rate_limited", str(err)
            return _error(429, "rate_limit_error", str(err), tid, **{"retry-after": f"{err.retry_after:.0f}"})
        except BudgetExceeded as err:
            trace.status, trace.error = "budget", str(err)
            return _error(402, "budget_exceeded", str(err), tid)
        trace.prompt_chars = len(prompt_text)

        # mascare: aceeași mapare pentru system și toate mesajele, ca placeholderele să fie coerente
        red = Redaction("")
        system = req.system
        messages = []
        if tenant.redact_pii:
            if system:
                red = redact(system, red)
                system = red.text
            for m in req.messages:
                red = redact(m.content, red)
                messages.append({"role": m.role, "content": red.text})
        else:
            messages = [m.model_dump() for m in req.messages]
        trace.redactions = red.count

        key = cache_key(tenant.id, req.model, system, messages, req.max_tokens)
        if req.cache and (hit := self.cache.get(key)) is not None:
            trace.status, trace.cache_hit, trace.served_model = "cache_hit", True, hit["model"]
            trace.saved_usd = hit["cost_usd"]
            return Reply(200, self._body(hit, red), {"x-gateway-cache": "hit", "x-gateway-cost-usd": "0"})

        chain = [req.model] + [m for m in FALLBACKS.get(req.model, []) if m in tenant.models]
        last: UpstreamError | None = None
        for i, model in enumerate(chain):
            t0 = time.perf_counter()
            try:
                up = self.upstream.complete(model, system, messages, req.max_tokens)
            except UpstreamError as err:
                trace.upstream_ms += (time.perf_counter() - t0) * 1000
                last = err
                if not err.retryable:
                    break
                continue
            trace.upstream_ms += (time.perf_counter() - t0) * 1000
            cost = cost_usd(model, up.input_tokens, up.output_tokens, up.cache_read_tokens, up.cache_write_tokens)
            trace.served_model, trace.fallback_used = model, i > 0
            trace.input_tokens, trace.output_tokens, trace.cost_usd = up.input_tokens, up.output_tokens, cost
            result = {"model": model, "text": up.text, "stop_reason": up.stop_reason,
                      "input_tokens": up.input_tokens, "output_tokens": up.output_tokens, "cost_usd": cost}
            if req.cache:
                self.cache.put(key, result)
            headers = {"x-gateway-cache": "miss" if req.cache else "off", "x-gateway-cost-usd": f"{cost:.6f}",
                       "x-gateway-redactions": str(red.count)}
            if i > 0:
                headers["x-gateway-fallback"] = f"{req.model}->{model}"
            return Reply(200, self._body(result, red), headers)
        trace.status, trace.error = "upstream_error", str(last)[:200]
        return _error(502 if last is None or last.status < 500 else last.status, "upstream_error",
                      "providerul nu a răspuns", trace.trace_id)

    @staticmethod
    def _body(result: dict[str, Any], red: Redaction) -> dict[str, Any]:
        return {
            "id": "msg_gw_" + uuid.uuid4().hex[:12],
            "type": "message",
            "role": "assistant",
            "model": result["model"],
            "content": [{"type": "text", "text": red.restore(result["text"])}],
            "stop_reason": result["stop_reason"],
            "usage": {"input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"]},
        }
