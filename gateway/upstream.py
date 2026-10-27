"""Providerii din spatele gateway-ului. Interfața e minimă: `complete(request) -> UpstreamResponse`."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class UpstreamError(Exception):
    def __init__(self, message: str, *, retryable: bool, status: int = 502):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class UpstreamResponse:
    text: str
    model: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class Upstream(Protocol):
    def complete(self, model: str, system: str, messages: list[dict[str, Any]], max_tokens: int) -> UpstreamResponse: ...


@dataclass
class FakeUpstream:
    """Provider simulat: răspunde cu ecoul ultimului mesaj. Poate pica la comandă, pe model."""

    latency_s: float = 0.0
    failing_models: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)
    responder: Callable[[str], str] | None = None
    sleep: Callable[[float], None] = time.sleep

    def complete(self, model, system, messages, max_tokens):
        self.calls.append({"model": model, "system": system, "messages": messages, "max_tokens": max_tokens})
        if self.latency_s:
            self.sleep(self.latency_s)
        if model in self.failing_models:
            raise UpstreamError(f"{model}: 529 overloaded", retryable=True, status=529)
        last = messages[-1]["content"] if messages else ""
        text = self.responder(last) if self.responder else f"Am primit: {last}"
        prompt = system + " ".join(m["content"] for m in messages)
        return UpstreamResponse(text, model, "end_turn", max(1, len(prompt) // 4), max(1, len(text) // 4))


class AnthropicUpstream:  # pragma: no cover - cere cheie API
    def __init__(self, client: Any = None) -> None:
        import anthropic

        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic(max_retries=1, timeout=60.0)

    def complete(self, model, system, messages, max_tokens):
        a = self.anthropic
        try:
            kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
            if system:
                kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            msg = self.client.messages.create(**kwargs)
        except a.RateLimitError as err:
            raise UpstreamError(str(err), retryable=True, status=429) from err
        except a.APIStatusError as err:
            raise UpstreamError(str(err), retryable=err.status_code >= 500, status=err.status_code) from err
        except a.APIConnectionError as err:
            raise UpstreamError(str(err), retryable=True, status=504) from err
        text = "".join(b.text for b in msg.content if b.type == "text")
        u = msg.usage
        return UpstreamResponse(text, msg.model, msg.stop_reason, u.input_tokens, u.output_tokens,
                                getattr(u, "cache_read_input_tokens", 0) or 0,
                                getattr(u, "cache_creation_input_tokens", 0) or 0)
