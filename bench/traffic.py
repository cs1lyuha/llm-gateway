"""Trafic simulat prin gateway: ce face fiecare control în cifre.

    python -m bench.traffic          # tabel Markdown

Scenariu (seed fix, provider fals, fără rețea):
- `support-bot` (Haiku): 3000 de întrebări de suport; ~60% repetă un FAQ de 40 de întrebări → cache;
  15% conțin telefon/e-mail → mascare.
- `agentie` (Opus → Sonnet → Haiku): 1500 de cereri; în cererile 600–899 Opus e „overloaded” → fallback.
- `clinica-mica` (Haiku, buget 0,02 $/lună, 30 rpm): 500 de cereri în rafale → rate limit + buget.

Măsurăm și **overhead-ul gateway-ului** (timpul total minus timpul providerului).
"""

from __future__ import annotations

import random
import statistics
import time

from gateway.core import Gateway
from gateway.tenants import TenantRegistry
from gateway.upstream import FakeUpstream

FAQ = [f"Cum fac {x} pentru comanda mea?" for x in (
    "retur", "schimb", "anulare", "plată în rate", "livrare rapidă", "factură", "garanție", "reparație",
    "ridicare din magazin", "schimbarea adresei")] + [f"Ce program are magazinul din {c}?" for c in (
    "Chișinău", "Bălți", "Cahul", "Orhei", "Ungheni", "Comrat", "Soroca", "Edineț", "Hîncești", "Strășeni")] + [
    f"Aveți în stoc {p}?" for p in ("iPhone 16", "Pixel 9", "ThinkPad E14", "MacBook Air", "AirPods Pro",
                                    "monitor Dell 27", "imprimantă HP", "UPS APC", "căști Sony", "tastatură Logitech")] + [
    f"Cât costă livrarea în {c}?" for c in ("Chișinău", "Bălți", "Cahul", "Orhei", "Ungheni", "Comrat", "Soroca",
                                            "Edineț", "Hîncești", "Strășeni")]


def run(seed: int = 11) -> dict:
    rng = random.Random(seed)
    clock = [0.0]
    reg = TenantRegistry(clock=lambda: clock[0])
    reg.add("support-bot", "k-support", ("claude-haiku-5-5",), rpm=6000, monthly_budget_usd=50)
    reg.add("agentie", "k-agentie", ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"), rpm=6000,
            monthly_budget_usd=500)
    reg.add("clinica-mica", "k-clinica", ("claude-haiku-5-5",), rpm=30, monthly_budget_usd=0.02)
    up = FakeUpstream(responder=lambda q: "Răspuns detaliat: " + q + " " + "detalii " * 40)
    gw = Gateway(reg, up)
    overhead = []

    def send(key: str, model: str, text: str, max_tokens: int = 400, cache: bool = False):
        t0 = time.perf_counter()
        before = len(up.calls)
        r = gw.handle(key, {"model": model, "max_tokens": max_tokens, "cache": cache,
                            "messages": [{"role": "user", "content": text}]})
        total = (time.perf_counter() - t0) * 1000
        overhead.append(total)  # providerul fals răspunde instant → timpul măsurat e overhead-ul gateway-ului
        return r, len(up.calls) - before

    for i in range(3000):
        clock[0] += 0.02
        if rng.random() < 0.6:
            text = rng.choice(FAQ)
        else:
            text = f"Întrebare unică #{i}: " + " ".join(rng.choice(FAQ).split()[:4])
        if rng.random() < 0.15:
            text += f" Sunați-mă la 06{rng.randint(0, 9)} {rng.randint(100, 999)} {rng.randint(100, 999)}."
        send("k-support", "claude-haiku-5-5", text, cache=True)

    for i in range(1500):
        clock[0] += 0.05
        up.failing_models = {"claude-opus-5-5"} if 600 <= i < 900 else set()
        send("k-agentie", "claude-opus-5-5", f"Analizează contractul {i} " + "clauză " * 200, max_tokens=2000)
    up.failing_models = set()

    for i in range(500):
        clock[0] += rng.choice([0.05, 0.05, 0.05, 6.0])  # rafale
        send("k-clinica", "claude-haiku-5-5", f"Mesaj pacient {i}: " + "text " * 60, max_tokens=800)

    s = gw.traces.summary()
    overhead.sort()
    s["overhead_p50_ms"] = round(statistics.median(overhead), 3)
    s["overhead_p95_ms"] = round(overhead[int(0.95 * len(overhead)) - 1], 3)
    return s


def main() -> None:
    s = run()
    print("| client | cereri | servite | blocate | erori | cache hit | fallback | cost $ | economisit de cache $ | date mascate |")
    print("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|")
    for name, t in s["tenants"].items():
        blocked = ", ".join(f"{k}: {v}" for k, v in t["blocked"].items()) or "—"
        print(f"| {name} | {t['requests']} | {t['served']} | {blocked} | {t['errors']} | {t['cache_hit_rate']:.0%} "
              f"| {t['fallbacks']} | {t['cost_usd']:.4f} | {t['saved_by_cache_usd']:.4f} | {t['redactions']} |")
    print(f"\nOverhead gateway: p50 {s['overhead_p50_ms']} ms, p95 {s['overhead_p95_ms']} ms (pe {s['requests']} cereri)")


if __name__ == "__main__":
    main()
