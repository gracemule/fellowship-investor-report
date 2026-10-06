"""What the model actually supports, asked of the provider rather than assumed.

DeepSeek's `GET /models` returns each model's `context_window` and `max_output_tokens`; Anthropic's
`GET /v1/models/{id}` returns `max_input_tokens` and `max_tokens`. When the provider cannot be asked (no
network, an unknown model), a deliberately small fallback is used and reported as such, so that being
unable to look something up makes the agent more careful rather than silently wrong.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

FALLBACK_WINDOW = 128_000
FALLBACK_OUTPUT = 16_000
_TTL = 6 * 3600
_CACHE: dict[tuple[str, str], tuple[float, Limits]] = {}


@dataclass(frozen=True)
class Limits:
    context_window: int
    max_output: int
    source: str                        # "provider" or a note saying why the fallback was used


def _fallback(why: str) -> Limits:
    return Limits(FALLBACK_WINDOW, FALLBACK_OUTPUT, f"assumed (could not ask the provider: {why})")


def _deepseek(model: str, client: httpx.Client) -> Limits:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    base = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
    r = client.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    for m in r.json().get("data", []):
        if m.get("id") == model and m.get("context_window"):
            return Limits(int(m["context_window"]), int(m.get("max_output_tokens") or FALLBACK_OUTPUT), "provider")
    raise LookupError(f"{model!r} is not in the provider's model list")


def _anthropic(model: str, client: httpx.Client) -> Limits:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    r = client.get(f"https://api.anthropic.com/v1/models/{model}",
                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    d = r.json()
    if not d.get("max_input_tokens"):
        raise LookupError("the provider did not state a context size")
    return Limits(int(d["max_input_tokens"]), int(d.get("max_tokens") or FALLBACK_OUTPUT), "provider")


def get_limits(provider: str, model: str, *, client: httpx.Client | None = None, refresh: bool = False) -> Limits:
    key = (provider, model)
    hit = _CACHE.get(key)
    if hit and not refresh and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        c = client or httpx.Client(timeout=15.0)
        limits = (_anthropic if provider == "anthropic" else _deepseek)(model, c)
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown", never a crash
        return _fallback(str(exc)[:90])
    _CACHE[key] = (time.time(), limits)
    return limits
