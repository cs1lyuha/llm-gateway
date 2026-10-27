# 🚦 llm-gateway

Un **gateway de producție** între aplicațiile tale și API-urile LLM. Problema pe care o rezolvă e reală:
când trei echipe sau trei clienți folosesc aceeași cheie Anthropic, nu mai știi cine a cheltuit 400 $,
nu poți opri o buclă care arde bani și trimiți numerele de telefon ale clienților la un provider extern.

```
aplicație ──x-api-key──▶ llm-gateway ──▶ Claude (Opus → Sonnet → Haiku)
                          │ cheie per client · modele permise · rate limit · buget lunar
                          │ mascare PII (reversibilă) · cache exact · fallback
                          └─ trace pentru fiecare cerere → /admin/summary, /metrics (Prometheus)
```

> Pasul 6 din planul meu de studiu (production readiness): rate limiting, observability, cost, OWASP LLM.

![CI](https://github.com/cs1lyuha/llm-gateway/actions/workflows/ci.yml/badge.svg)

## Cifre

`python -m bench.traffic`: 5000 de cereri simulate (provider fals, seed fix), 3 clienți cu profiluri diferite:

| client | cereri | servite | blocate | erori | cache hit | fallback | cost $ | economisit de cache $ | date mascate |
|---|---:|---:|---|---:|---:|---:|---:|---:|---:|
| support-bot (Haiku, FAQ repetate) | 3000 | 3000 | — | 0 | 58% | 0 | 0.0619 | 0.0829 | 422 |
| agentie (Opus, pană de 300 de cereri) | 1500 | 1500 | — | **0** | 0% | 300 | 13.83 | 0 | 0 |
| clinica-mica (buget 0,02 $, 30 rpm) | 500 | 218 | rate limit: 181, buget: 101 | 0 | 0% | 0 | 0.0196 | 0 | 0 |

- **Cache-ul** înjumătățește costul botului de suport: 0,083 $ economisiți față de 0,062 $ plătiți.
- **Fallback-ul** salvează toate cele 300 de cereri din timpul penei la Opus: 0 erori pentru client.
- **Bugetul** nu e depășit niciodată (0,0196 $ din 0,02 $), pentru că verificarea se face *înainte* de apel, pe costul maxim posibil.
- **Overhead-ul gateway-ului**: p50 0,3 ms, p95 0,7 ms. E neglijabil față de secundele unui apel LLM.

Testul `tests/test_bench.py` verifică aceste relații la fiecare push.

## Controalele, în ordine

| # | Control | Răspuns la refuz | Detalii |
|---|---|---|---|
| 1 | cheie API | 401 | cheile se salvează doar ca SHA-256; comparație în timp constant |
| 2 | model permis | 403 | fiecare client are lista lui (clinica nu poate folosi Opus) |
| 3 | rate limit | 429 + `retry-after` | token bucket per client, rafale de ~10 s |
| 4 | buget lunar | 402 | `cheltuit + cost maxim posibil (input + max_tokens) > buget` → refuz **înainte** de apel |
| 5 | mascare PII | — | e-mail, telefon MD, card (Luhn), IDNP, IBAN → `[EMAIL_1]`…; restaurate în răspuns |
| 6 | cache exact | — | opțional per cerere (`"cache": true`); cheia include clientul, deci nu există scurgeri între clienți |
| 7 | fallback | 529 dacă pică tot | Opus → Sonnet → Haiku, doar printre modelele permise clientului; erorile 4xx nu fac fallback |
| 8 | trace | — | latență, tokeni, cost, cache, fallback, date mascate. **Fără prompturi** în baza de date |

### Un detaliu la mascare + cache

Cheia de cache se calculează pe textul **deja mascat**. „Sunați-mă la 069 111 222” și „Sunați-mă la 078 333 444” devin amândouă
„Sunați-mă la [PHONE_1]”, deci au același răspuns în cache. Fiecare utilizator primește însă răspunsul cu numărul **lui**,
pentru că demascarea folosește maparea cererii curente (`test_cached_answer_restores_this_requests_pii`).

## Pornire

```bash
git clone https://github.com/cs1lyuha/llm-gateway.git && cd llm-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt anthropic uvicorn

python -m gateway.app add-tenant clinica claude-haiku-5-5 --rpm 60 --budget 20   # afișează cheia o singură dată
export ANTHROPIC_API_KEY=... GATEWAY_ADMIN_KEY=$(openssl rand -hex 16)
uvicorn "gateway.app:create_app" --factory --port 8080
```

Aplicațiile folosesc gateway-ul exact ca pe API-ul Anthropic (subsetul text):

```bash
curl localhost:8080/v1/messages -H "x-api-key: gw_..." -H "content-type: application/json" -d '{
  "model": "claude-haiku-5-5", "max_tokens": 300, "cache": true,
  "messages": [{"role": "user", "content": "Pacientul 069 123 456 întreabă de un implant"}]}'

curl localhost:8080/admin/summary -H "x-admin-key: $GATEWAY_ADMIN_KEY"
curl localhost:8080/metrics       -H "x-admin-key: $GATEWAY_ADMIN_KEY"   # pentru Prometheus / Grafana
```

Fiecare răspuns are antetele `x-gateway-trace-id`, `x-gateway-cost-usd`, `x-gateway-cache` și `x-gateway-redactions`,
plus `x-gateway-fallback`, dacă a fost folosit.

## OWASP Top 10 pentru LLM: ce acoperă

| Risc | Acoperire |
|---|---|
| LLM02 Sensitive Information Disclosure | mascare PII înainte de provider; trace-urile nu conțin prompturi |
| LLM10 Unbounded Consumption | rate limit, buget lunar cu verificare pe cazul cel mai rău, limită pentru `max_tokens` și dimensiunea mesajelor |
| LLM06 Excessive Agency (parțial) | allowlist de modele per client |
| Disponibilitate | fallback între modele; 4xx nu se repetă |

## Structura

| Fișier | Rol |
|---|---|
| `gateway/core.py` | ordinea controalelor, fallback, construirea răspunsului |
| `gateway/tenants.py` | clienți, chei, token bucket, buget, prețuri |
| `gateway/redact.py` | mascare și restaurare PII |
| `gateway/cache.py` | cache LRU cu TTL, izolat pe client |
| `gateway/traces.py` | SQLite, rezumat, export Prometheus |
| `gateway/upstream.py` | provider fals și adaptor Anthropic |
| `gateway/app.py` | FastAPI + generarea cheilor |
| `bench/traffic.py` | scenariul de trafic de mai sus |

**40 de teste:** `pytest -q`.

## Ce urmează

- [ ] Streaming SSE prin gateway (cu numărarea tokenilor din stream)
- [ ] Redis pentru rate limit și cache, ca să ruleze mai multe instanțe în paralel
- [ ] Export OpenTelemetry / Langfuse în loc de SQLite
- [ ] Cache semantic (embeddings) pentru întrebări parafrazate, cu prag măsurat pe un set de evaluare
