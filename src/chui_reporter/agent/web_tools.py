"""The web tools. Only subagents carry these: the main agent delegates research instead of doing it itself,
so search results and page text never enter its conversation (see agents/)."""

from __future__ import annotations

from langchain_core.tools import tool

_STORE = None


def get_store():
    from . import tools as _t

    return _t.get_store()


_ROUTER = None


def get_router():
    """The web-search router for the current store (built on first use, rebuilt if the store changes)."""
    global _ROUTER
    from ..services.search import SearchRouter

    if _ROUTER is None or _ROUTER.store is not get_store():
        _ROUTER = SearchRouter(get_store())
    return _ROUTER


def set_router(router) -> None:
    global _ROUTER
    _ROUTER = router


@tool
def web_search(query: str, max_results: int = 6, topic: str = "general", days: int = 0,
               only_sites: str = "") -> str:
    """Search the web. Returns titles, addresses, a short extract and a date where known.

    Use it to FIND pages (for example the central bank's or the national statistics office's page for a
    country's latest GDP growth, inflation, policy rate or exchange rate). A search result is a lead, not
    evidence: you cannot record a figure from it. Open the page with web_fetch and quote it there.

    topic: "general" or "news". days: only results from the last N days (0 = any).
    only_sites: comma-separated domains to restrict to, e.g. "centralbank.go.ke,knbs.or.ke".
    Credit is limited and shared: make each query specific, and do not repeat a query."""
    from ..services.search import NoSearchAvailable

    domains = [d.strip() for d in only_sites.split(",") if d.strip()]
    try:
        res = get_router().search(query, max_results=max_results, topic=topic, days=days, include_domains=domains or None)
    except NoSearchAvailable as exc:
        return ("ERROR: web search is not available right now (" + str(exc) + "). If the figures are needed, call "
                "request_sources(['macro']) so the user can supply them, and carry on without them otherwise.")
    if not res.hits:
        return f"[{res.provider}] no results for {query!r}. Try different words or a named source."
    lines = [f"[{res.provider}] {len(res.hits)} results for {query!r}" + (f"  ({'; '.join(res.notes)})" if res.notes else "")]
    for h in res.hits:
        lines.append(f"- {h.title}\n  {h.url}" + (f"  ({h.published})" if h.published else "") + f"\n  {h.snippet[:300]}")
    return "\n".join(lines)


@tool
def web_fetch(url: str, find: str = "", max_chars: int = 6000) -> str:
    """Read a web page (HTML, PDF, JSON or text) and keep it as evidence.

    This is how a web figure becomes usable: a figure can only be reported with the exact address you fetched here
    and a quote copied character for character from the text returned here (the sentence or table row that contains
    it). `find` returns only the passages around that word or number (use it on long pages).
    Pages are fetched by the server: private and internal addresses are refused. An address or site that fails is
    not worth retrying."""
    from ..services.web import FetchError, fetch, windows

    try:
        page = fetch(get_store(), url, router=get_router())
    except FetchError as exc:
        return f"ERROR: {exc}"
    text = page["text"]
    if find:
        found = windows(text, find)
        body = ("\n---\n".join(found) if found else f"{find!r} does not appear on the page.")
    else:
        body = text[: max(500, min(int(max_chars), 12000))]
    more = "" if find or len(text) <= len(body) else f"\n[... {len(text) - len(body):,} more characters; use find= to jump to a figure]"
    return f"[{page['title'] or page['url']}]  {page['url']}  (via {page['via']})\n{body}{more}"


WEB_TOOLS = [web_search, web_fetch]
