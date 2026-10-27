"""Tracing: un rând per cerere, cu tot ce trebuie pentru cost, latență și depanare.

Prompturile NU se salvează (doar lungimea și numărul de date mascate). Pentru
depanare există `trace_id`, întors clientului în antetul `x-gateway-trace-id`.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    tenant TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    served_model TEXT,
    status TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    upstream_ms REAL NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    saved_usd REAL NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    redactions INTEGER NOT NULL DEFAULT 0,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    prompt_chars INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS traces_tenant_at ON traces(tenant, at);
"""


@dataclass
class Trace:
    trace_id: str
    at: str
    tenant: str
    requested_model: str
    served_model: str | None = None
    status: str = "ok"            # ok | cache_hit | rate_limited | budget | forbidden | upstream_error | invalid
    http_status: int = 200
    latency_ms: float = 0.0
    upstream_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    cache_hit: bool = False
    redactions: int = 0
    fallback_used: bool = False
    prompt_chars: int = 0
    error: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def record(self, trace: Trace) -> None:
        row = asdict(trace)
        row["cache_hit"], row["fallback_used"] = int(trace.cache_hit), int(trace.fallback_used)
        cols = ", ".join(row)
        with self.conn:
            self.conn.execute(f"INSERT INTO traces ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    def spent_this_month(self, tenant: str, now: datetime | None = None) -> float:
        month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
        row = self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM traces WHERE tenant = ? AND substr(at,1,7) = ?",
                                (tenant, month)).fetchone()
        return float(row[0])

    def summary(self) -> dict:
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM traces")]
        out: dict = {"requests": len(rows), "tenants": {}}
        by_tenant: dict[str, list[dict]] = {}
        for r in rows:
            by_tenant.setdefault(r["tenant"], []).append(r)
        for tenant, rs in sorted(by_tenant.items()):
            served = [r for r in rs if r["status"] in ("ok", "cache_hit")]
            lat = sorted(r["latency_ms"] for r in served)
            out["tenants"][tenant] = {
                "requests": len(rs),
                "served": len(served),
                "blocked": {s: sum(r["status"] == s for r in rs)
                            for s in ("rate_limited", "budget", "forbidden", "invalid") if any(r["status"] == s for r in rs)},
                "errors": sum(r["status"] == "upstream_error" for r in rs),
                "cache_hit_rate": round(sum(r["cache_hit"] for r in served) / len(served), 3) if served else 0.0,
                "fallbacks": sum(r["fallback_used"] for r in rs),
                "cost_usd": round(sum(r["cost_usd"] for r in rs), 6),
                "saved_by_cache_usd": round(sum(r["saved_usd"] for r in rs), 6),
                "redactions": sum(r["redactions"] for r in rs),
                "p50_ms": round(statistics.median(lat), 1) if lat else None,
                "p95_ms": round(lat[max(0, int(0.95 * len(lat)) - 1)], 1) if lat else None,
            }
        return out

    def prometheus(self) -> str:
        """Metrici în formatul text Prometheus, pentru /metrics."""
        lines = ["# HELP llm_gateway_requests_total Cereri după client și status",
                 "# TYPE llm_gateway_requests_total counter"]
        for r in self.conn.execute("SELECT tenant, status, COUNT(*) n FROM traces GROUP BY tenant, status ORDER BY 1, 2"):
            lines.append(f'llm_gateway_requests_total{{tenant="{r["tenant"]}",status="{r["status"]}"}} {r["n"]}')
        lines += ["# HELP llm_gateway_cost_usd_total Cost cumulat în USD", "# TYPE llm_gateway_cost_usd_total counter"]
        for r in self.conn.execute("SELECT tenant, SUM(cost_usd) c FROM traces GROUP BY tenant ORDER BY 1"):
            lines.append(f'llm_gateway_cost_usd_total{{tenant="{r["tenant"]}"}} {r["c"]:.6f}')
        lines += ["# HELP llm_gateway_tokens_total Tokeni procesați", "# TYPE llm_gateway_tokens_total counter"]
        for r in self.conn.execute("SELECT tenant, SUM(input_tokens) i, SUM(output_tokens) o FROM traces GROUP BY tenant ORDER BY 1"):
            lines.append(f'llm_gateway_tokens_total{{tenant="{r["tenant"]}",direction="input"}} {r["i"]}')
            lines.append(f'llm_gateway_tokens_total{{tenant="{r["tenant"]}",direction="output"}} {r["o"]}')
        return "\n".join(lines) + "\n"
