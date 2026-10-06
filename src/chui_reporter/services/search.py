"""Web search with automatic fallback between providers.

Tavily is the default; Brave takes over when Tavily has used its credit (and the other way round).
A provider that has run out is skipped for a cooldown and then tried again first, so Tavily comes back
as the default by itself once its allowance renews. Nothing here decides what the agent does with the
results; it only finds pages. Figures are never taken from a search result: they must be read from a
fetched page (see web.py), because a snippet is not something that can be verified later.

Failure handling, by what the provider tells us:

  401 / 403            the key is wrong: skip this provider for a short while, say so
  Tavily 432 / 433     plan or pay-as-you-go limit reached: skip for hours, use the other provider
  Brave 402            credit used: same
  429                  rate limited: wait (Retry-After, capped) and retry once, then use the other provider
                       (Brave answers 429 for a spent monthly allowance too; its RateLimit headers say which)
  5xx / network        transient: use the other provider, do not mark this one
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx

from ..agent.store import Store
from . import usage

COOLDOWN_EXHAUSTED_H = 6.0
COOLDOWN_AUTH_H = 0.25
TIMEOUT = 25.0


@dataclass
class Hit:
    title: str
    url: str
    snippet: str
    published: str | None = None
    score: float | None = None


class ProviderError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind                 # exhausted | rate_limited | auth | transient | bad_request
        self.retry_after = retry_after


class NoSearchAvailable(Exception):
    """Every provider is unset, spent or failing. `reasons` says why, per provider."""

    def __init__(self, reasons: dict[str, str]):
        self.reasons = reasons
        super().__init__("; ".join(f"{k}: {v}" for k, v in reasons.items()) or "no search provider is configured")


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    try:
        return min(float(v), 10.0) if v else None
    except ValueError:
        return None


class Tavily:
    name = "tavily"
    key_env = "TAVILY_API_KEY"
    base = "https://api.tavily.com"

    def __init__(self, key: str | None = None, client: httpx.Client | None = None):
        self.key = key or os.environ.get(self.key_env, "").strip()
        self.client = client or httpx.Client(timeout=TIMEOUT)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self.client.post(f"{self.base}{path}", json=body, headers={"Authorization": f"Bearer {self.key}"})
        except httpx.HTTPError as exc:
            raise ProviderError("transient", f"Tavily unreachable: {type(exc).__name__}") from exc
        if r.status_code == 200:
            return r.json()
        if r.status_code in (401, 403):
            raise ProviderError("auth", "Tavily rejected the API key")
        if r.status_code in (432, 433):
            raise ProviderError("exhausted", "Tavily credit is used up" + (" (pay-as-you-go cap reached)" if r.status_code == 433 else ""))
        if r.status_code == 429:
            raise ProviderError("rate_limited", "Tavily is rate-limiting", _retry_after(r))
        if r.status_code >= 500:
            raise ProviderError("transient", f"Tavily error {r.status_code}")
        raise ProviderError("bad_request", f"Tavily rejected the request ({r.status_code})")

    def search(self, query: str, *, max_results: int = 6, topic: str = "general", days: int = 0,
               include_domains: list[str] | None = None) -> list[Hit]:
        body: dict = {"query": query, "max_results": max(1, min(max_results, 10)), "search_depth": "basic",
                      "topic": topic if topic in ("general", "news") else "general", "include_raw_content": False}
        if days:
            body["time_range"] = "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year"
        if include_domains:
            body["include_domains"] = include_domains[:20]
        data = self._post("/search", body)
        return [Hit(r.get("title") or r["url"], r["url"], (r.get("content") or "").strip(),
                    r.get("published_date"), r.get("score")) for r in data.get("results", []) if r.get("url")]

    def extract(self, urls: list[str]) -> dict[str, str]:
        data = self._post("/extract", {"urls": urls[:5], "extract_depth": "basic", "format": "text"})
        return {r["url"]: r.get("raw_content") or "" for r in data.get("results", [])}


class Brave:
    name = "brave"
    key_env = "BRAVE_API_KEY"
    base = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, key: str | None = None, client: httpx.Client | None = None):
        self.key = key or os.environ.get(self.key_env, "").strip()
        self.client = client or httpx.Client(timeout=TIMEOUT)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    @staticmethod
    def _monthly_spent(resp: httpx.Response) -> bool:
        """Brave sends 'limit, limit' and 'remaining, remaining' as comma lists: per-second then per-month."""
        rem = resp.headers.get("x-ratelimit-remaining", "")
        parts = [p.strip() for p in rem.split(",") if p.strip()]
        return len(parts) >= 2 and parts[-1] == "0"

    def search(self, query: str, *, max_results: int = 6, topic: str = "general", days: int = 0,
               include_domains: list[str] | None = None) -> list[Hit]:
        q = query
        if include_domains:
            q += " (" + " OR ".join(f"site:{d}" for d in include_domains[:5]) + ")"
        params = {"q": q, "count": max(1, min(max_results, 20))}
        if days:
            params["freshness"] = "pd" if days <= 1 else "pw" if days <= 7 else "pm" if days <= 31 else "py"
        try:
            r = self.client.get(self.base, params=params,
                                headers={"X-Subscription-Token": self.key, "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise ProviderError("transient", f"Brave unreachable: {type(exc).__name__}") from exc
        if r.status_code == 200:
            results = (r.json().get("web") or {}).get("results", [])
            return [Hit(x.get("title") or x["url"], x["url"], (x.get("description") or "").strip(), x.get("age"))
                    for x in results if x.get("url")]
        if r.status_code in (401, 403):
            raise ProviderError("auth", "Brave rejected the API key")
        if r.status_code == 402:
            raise ProviderError("exhausted", "Brave credit is used up")
        if r.status_code == 429:
            if self._monthly_spent(r):
                raise ProviderError("exhausted", "Brave's monthly allowance is used up")
            raise ProviderError("rate_limited", "Brave is rate-limiting", _retry_after(r) or 1.1)
        if r.status_code >= 500:
            raise ProviderError("transient", f"Brave error {r.status_code}")
        raise ProviderError("bad_request", f"Brave rejected the request ({r.status_code})")


@dataclass
class SearchResult:
    provider: str
    hits: list[Hit]
    notes: list[str] = field(default_factory=list)      # e.g. "Tavily is out of credit; used Brave"


class SearchRouter:
    """Tries providers in order (default tavily, brave), remembering which have run out."""

    def __init__(self, store: Store, providers: dict | None = None, order: list[str] | None = None,
                 sleep=time.sleep):
        self.store = store
        self.providers = providers or {"tavily": Tavily(), "brave": Brave()}
        self.order = order or [x.strip().lower() for x in os.environ.get("CHUI_SEARCH_ORDER", "tavily,brave").split(",")
                               if x.strip().lower() in self.providers] or ["tavily", "brave"]
        self._sleep = sleep

    def status(self) -> list[dict]:
        out = []
        for name in self.order:
            p = self.providers[name]
            st = usage.get_state(self.store, name)
            out.append({"name": name, "configured": p.configured, "state": st["state"] if p.configured else "unset",
                        "until": st["until"], "detail": st["detail"], "used": usage.used_this_month(self.store, name)})
        return out

    def _attempt(self, name: str, fn, notes: list[str]):
        """Run `fn(provider)`; returns (ok, value). Records usage, cooldowns and a note on failure."""
        p = self.providers[name]
        for attempt in (0, 1):
            try:
                value = fn(p)
                usage.record(self.store, name)
                if usage.get_state(self.store, name)["state"] != "ok":
                    usage.clear_state(self.store, name)
                return True, value
            except ProviderError as exc:
                if exc.kind == "rate_limited" and attempt == 0:
                    self._sleep(exc.retry_after or 1.0)
                    continue
                if exc.kind == "exhausted":
                    usage.set_state(self.store, name, "exhausted", hours=COOLDOWN_EXHAUSTED_H, detail=str(exc))
                elif exc.kind == "auth":
                    usage.set_state(self.store, name, "auth", hours=COOLDOWN_AUTH_H, detail=str(exc))
                elif exc.kind == "rate_limited":
                    usage.set_state(self.store, name, "exhausted", hours=1.0, detail="still rate-limited after a retry")
                notes.append(f"{name}: {exc}")
                return False, exc
        return False, None

    def _run(self, fn):
        notes: list[str] = []
        reasons: dict[str, str] = {}
        for name in self.order:
            p = self.providers[name]
            if not p.configured:
                reasons[name] = "no API key set"
                continue
            st = usage.get_state(self.store, name)
            if st["state"] != "ok":
                reasons[name] = f"{st['detail'] or st['state']} (will retry automatically)"
                continue
            ok, value = self._attempt(name, fn, notes)
            if ok:
                return name, value, notes
            reasons[name] = notes[-1].split(": ", 1)[-1] if notes else "failed"
        raise NoSearchAvailable(reasons)

    def search(self, query: str, **kw) -> SearchResult:
        name, hits, notes = self._run(lambda p: p.search(query, **kw))
        return SearchResult(name, hits, notes)

    def extract(self, urls: list[str]) -> tuple[str, dict[str, str]]:
        """Page text from a provider that can render pages (only Tavily today)."""
        capable = {n: p for n, p in self.providers.items() if hasattr(p, "extract")}
        saved = self.order
        self.order = [n for n in saved if n in capable]
        try:
            name, value, _ = self._run(lambda p: p.extract(urls))
            return name, value
        finally:
            self.order = saved
