"""Gọi mô hình.

- kind = "openai": mọi server tương thích OpenAI Chat Completions (vLLM, TGI, LM Studio,
  Ollama /v1, các nhà cung cấp API…). base_url trỏ tới gốc /v1.
- kind = "mock": không gọi mạng; base_url dạng "mock://0.8" nghĩa là trả lời đúng ~80% mẫu.
  Dùng để thử toàn bộ luồng khi chưa có GPU hay API key.
Muốn thêm kiểu khác (SDK riêng, script…), thêm một nhánh trong `chat`.
"""

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass

import httpx

from ..config import settings
from ..models import ModelEndpoint
from .secrets import secrets


@dataclass
class ChatResult:
    text: str
    tokens: int
    latency_ms: int


class ModelCallError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _rng(*parts) -> random.Random:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def mock_quality(ep: ModelEndpoint) -> float:
    try:
        return max(0.0, min(1.0, float(ep.base_url.split("://", 1)[1])))
    except (IndexError, ValueError):
        return 0.7


async def chat(
    client: httpx.AsyncClient,
    ep: ModelEndpoint,
    messages: list[dict],
    *,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    seed: int | None = None,
    mock_reply: str | None = None,
) -> ChatResult:
    t0 = time.perf_counter()

    if ep.kind == "mock":
        lo, hi = settings.mock_latency_s
        r = _rng(ep.id, seed, messages[-1].get("content", "") if messages else "")
        await asyncio.sleep(lo + r.random() * (hi - lo))
        text = mock_reply if mock_reply is not None else "Tôi không chắc về câu trả lời."
        return ChatResult(text=text, tokens=len(text.split()), latency_ms=int((time.perf_counter() - t0) * 1000))

    if ep.kind != "openai":
        raise ModelCallError(f"Kiểu kết nối '{ep.kind}' chưa được hỗ trợ", retryable=False)

    headers = {"Content-Type": "application/json"}
    if ep.secret_name:
        key = secrets.get(ep.secret_name)
        if not key:
            raise ModelCallError(f"thiếu bí mật {ep.secret_name}", retryable=False)
        headers["Authorization"] = f"Bearer {key}"

    body: dict = {
        "model": ep.model_name or ep.id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if seed is not None:
        body["seed"] = seed

    url = ep.base_url.rstrip("/") + "/chat/completions"
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=settings.model_timeout_s)
    except httpx.TimeoutException as e:
        raise ModelCallError(f"quá thời gian chờ {int(settings.model_timeout_s)}s", retryable=True) from e
    except httpx.HTTPError as e:
        raise ModelCallError(f"lỗi mạng ({type(e).__name__})", retryable=True) from e

    if resp.status_code == 429 or resp.status_code >= 500:
        raise ModelCallError(f"HTTP {resp.status_code}", retryable=True)
    if resp.status_code >= 400:
        raise ModelCallError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False)

    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ModelCallError("phản hồi không đúng định dạng Chat Completions", retryable=False) from e
    usage = data.get("usage") or {}
    tokens = usage.get("completion_tokens") or len(text.split())
    return ChatResult(text=text, tokens=int(tokens), latency_ms=int((time.perf_counter() - t0) * 1000))


async def ping(client: httpx.AsyncClient, ep: ModelEndpoint) -> int:
    """Gửi một yêu cầu 1 token. Trả về độ trễ (ms) hoặc ném ModelCallError."""
    res = await chat(client, ep, [{"role": "user", "content": "ping"}], max_tokens=1, mock_reply="pong")
    return res.latency_ms
