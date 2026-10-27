"""Clienți (tenants): chei API, modele permise, rate limit și buget lunar.

Bugetul se verifică de două ori:
1. **înainte** de apel, cu costul maxim posibil (input estimat + `max_tokens` la prețul de output),
   ca o singură cerere mare să nu poată depăși bugetul;
2. **după** apel, se înregistrează costul real din `usage`.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Callable

# USD / 1M tokeni (input, output)
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class AuthError(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limit; reîncearcă peste {retry_after:.1f}s")
        self.retry_after = retry_after


class BudgetExceeded(Exception):
    pass


class ModelNotAllowed(Exception):
    pass


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p_in, p_out = PRICES[model]
    return (input_tokens * p_in + cache_write * p_in * 1.25 + cache_read * p_in * 0.1
            + output_tokens * p_out) / 1_000_000


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Tenant:
    id: str
    key_hash: str
    models: tuple[str, ...]
    rpm: int = 60
    monthly_budget_usd: float = 50.0
    redact_pii: bool = True


@dataclass
class TokenBucket:
    rate_per_s: float
    capacity: float
    tokens: float
    updated: float

    def take(self, now: float) -> float:
        """0 dacă a trecut, altfel câte secunde de așteptat."""
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate_per_s)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate_per_s


@dataclass
class TenantRegistry:
    tenants: dict[str, Tenant] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    buckets: dict[str, TokenBucket] = field(default_factory=dict)

    def add(self, tenant_id: str, key: str, models: tuple[str, ...], **kw) -> Tenant:
        unknown = set(models) - set(PRICES)
        if unknown:
            raise ValueError(f"modele fără preț: {sorted(unknown)}")
        t = Tenant(tenant_id, hash_key(key), models, **kw)
        self.tenants[tenant_id] = t
        return t

    def authenticate(self, key: str | None) -> Tenant:
        if not key:
            raise AuthError("lipsește cheia API")
        digest = hash_key(key)
        for t in self.tenants.values():
            if hmac.compare_digest(t.key_hash, digest):
                return t
        raise AuthError("cheie API invalidă")

    def check_rate(self, tenant: Tenant) -> None:
        now = self.clock()
        bucket = self.buckets.get(tenant.id)
        if bucket is None:
            capacity = max(1.0, tenant.rpm / 6)  # rafale de ~10 secunde
            bucket = self.buckets[tenant.id] = TokenBucket(tenant.rpm / 60, capacity, capacity, now)
        wait = bucket.take(now)
        if wait > 0:
            raise RateLimited(wait)

    @staticmethod
    def check_model(tenant: Tenant, model: str) -> None:
        if model not in tenant.models:
            raise ModelNotAllowed(f"modelul {model} nu e permis pentru {tenant.id}")

    @staticmethod
    def check_budget(tenant: Tenant, spent_usd: float, model: str, prompt_text: str, max_tokens: int) -> float:
        worst = cost_usd(model, estimate_tokens(prompt_text), max_tokens)
        if spent_usd + worst > tenant.monthly_budget_usd:
            raise BudgetExceeded(f"buget lunar {tenant.monthly_budget_usd:.2f}$: cheltuit {spent_usd:.4f}$, "
                                 f"cererea poate costa până la {worst:.4f}$")
        return worst
