"""Try the web-search keys locally, before anything is deployed.

    python -m chui_reporter.services.check_search                 # default order: Tavily, then Brave
    python -m chui_reporter.services.check_search --only brave    # exercise one provider on its own
    python -m chui_reporter.services.check_search --fetch URL     # also read a page and keep it as evidence

Each search spends one credit on the provider that answers. The key is never printed."""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    ap = argparse.ArgumentParser(prog="check_search")
    ap.add_argument("--query", default="Central Bank of Kenya policy rate")
    ap.add_argument("--only", choices=["tavily", "brave"], help="use just this provider")
    ap.add_argument("--fetch", metavar="URL", help="also fetch this page and store it as evidence")
    args = ap.parse_args(argv)
    if args.only:
        os.environ["CHUI_SEARCH_ORDER"] = args.only

    from ..agent.store import Store
    from .search import NoSearchAvailable, SearchRouter

    store = Store()
    router = SearchRouter(store)
    print("providers:", ", ".join(f"{s['name']} ({'key set' if s['configured'] else 'no key'}, {s['state']})" for s in router.status()))
    try:
        res = router.search(args.query, max_results=3)
    except NoSearchAvailable as exc:
        print(f"no search available: {exc}", file=sys.stderr)
        return 1
    print(f"answered by {res.provider}" + (f" after: {'; '.join(res.notes)}" if res.notes else "") + f" -- {len(res.hits)} results")
    for h in res.hits:
        print(f"  - {h.title[:80]}\n    {h.url}")
    if args.fetch:
        from .web import FetchError, fetch

        try:
            page = fetch(store, args.fetch, router=router)
            print(f"fetched {page['url']} via {page['via']}: {len(page['text']):,} characters kept as evidence")
        except FetchError as exc:
            print(f"fetch failed: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
