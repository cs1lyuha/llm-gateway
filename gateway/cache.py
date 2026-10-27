"""Cache exact pentru răspunsuri: aceeași cerere (după mascare) → același răspuns, fără apel la provider.

Cheia include clientul (tenant): doi clienți nu-și văd niciodată răspunsurile unul altuia,
chiar dacă trimit exact același prompt. Cache-ul e LRU cu TTL și ține minte economiile.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable


def cache_key(tenant_id: str, model: str, system: str, messages: list[dict[str, Any]], max_tokens: int) -> str:
    payload = json.dumps({"t": tenant_id, "m": model, "s": system.strip(),
                          "msgs": [{"role": m["role"], "content": m["content"].strip()} for m in messages],
                          "max": max_tokens}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class ResponseCache:
    max_entries: int = 1000
    ttl_s: float = 3600.0
    clock: Callable[[], float] = time.monotonic
    entries: "OrderedDict[str, tuple[float, Any]]" = field(default_factory=OrderedDict)
    hits: int = 0
    misses: int = 0

    def get(self, key: str) -> Any | None:
        item = self.entries.get(key)
        if item is None or self.clock() - item[0] > self.ttl_s:
            self.entries.pop(key, None)
            self.misses += 1
            return None
        self.entries.move_to_end(key)
        self.hits += 1
        return item[1]

    def put(self, key: str, value: Any) -> None:
        self.entries[key] = (self.clock(), value)
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
