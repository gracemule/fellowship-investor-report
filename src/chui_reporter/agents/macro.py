"""The macro snapshot, researched by subagents and laid out by code.

One researcher per country, run in parallel, each in a context of its own. The main agent receives a page of
verified figures; the search results and page text stay in the researchers' conversations. The table is built
deterministically from what verified, so the figures in it cannot be retyped wrongly."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .. import period as pr
from ..agent.store import Store
from . import records
from .engine import Context
from .research import research_one

INDICATORS = {
    "gdp_growth": ("GDP growth", "real GDP growth, year on year, for the latest quarter published (unit: percent)"),
    "inflation": ("Inflation", "headline consumer price inflation, year on year, for the latest month published (unit: percent)"),
    "policy_rate": ("Policy rate", "the central bank's headline policy rate in force at the end of the period (unit: percent)"),
    "fx_usd": ("Exchange rate (local currency per US$)", "the exchange rate to the US dollar at the end of the period, in local "
                                                         "currency units per one US dollar (say in as_of whether it is a period-end rate or an average)"),
}
COLUMNS = ["Country"] + [v[0] for v in INDICATORS.values()]

# Where to start looking. Hints only: nothing is restricted to these.
OFFICIAL = {
    "kenya": "Central Bank of Kenya (centralbank.go.ke), Kenya National Bureau of Statistics (knbs.or.ke)",
    "nigeria": "Central Bank of Nigeria (cbn.gov.ng), National Bureau of Statistics (nigerianstat.gov.ng)",
    "south africa": "South African Reserve Bank (resbank.co.za), Statistics South Africa (statssa.gov.za)",
    "zambia": "Bank of Zambia (boz.zm), Zambia Statistics Agency (zamstats.gov.zm)",
    "ghana": "Bank of Ghana (bog.gov.gh), Ghana Statistical Service (statsghana.gov.gh)",
    "uganda": "Bank of Uganda (bou.or.ug), Uganda Bureau of Statistics (ubos.org)",
    "tanzania": "Bank of Tanzania (bot.go.tz), National Bureau of Statistics (nbs.go.tz)",
    "egypt": "Central Bank of Egypt (cbe.org.eg), CAPMAS (capmas.gov.eg)",
    "rwanda": "National Bank of Rwanda (bnr.rw), National Institute of Statistics of Rwanda (statistics.gov.rw)",
}
UEMOA = {"senegal": "ANSD (ansd.sn)", "côte d'ivoire": "INS (ins.ci)", "cote d'ivoire": "INS (ins.ci)",
         "mali": "INSTAT", "burkina faso": "INSD", "benin": "INStaD", "togo": "INSEED", "niger": "INS", "guinea-bissau": "INE"}


# Members of a monetary union share one central bank and one currency, so a verified policy rate or CFA franc rate for any
# member is the figure for all of them.
SHARED_IN_UNION = ("policy_rate", "fx_usd")


def _in_union(country: str) -> bool:
    return country.strip().casefold() in UEMOA


def task_for(country: str, period: pr.Period, indicators: list[str] | None = None) -> str:
    key = country.strip().casefold()
    wanted = [k for k in INDICATORS if not indicators or k in indicators]
    lines = [f"Find these figures for {country} as at the end of {period.label} (the quarter ended {period.end_label}). Use only "
             f"figures for periods ending on or before {period.end_label}: do not use a later month or quarter even if it has been "
             f"published since. Use the keys exactly as written and label each claim '{country} <indicator>'."]
    lines += [f"- {k}: {INDICATORS[k][1]}" for k in wanted]
    if key in UEMOA:
        lines.append(f"{country} is in the West African Economic and Monetary Union (UEMOA): the policy rate is set by the BCEAO "
                     f"(bceao.int) and the currency is the CFA franc (XOF), so use the BCEAO's publications for policy_rate and "
                     f"fx_usd, and the national statistics office ({UEMOA[key]}) for gdp_growth and inflation.")
    elif key in OFFICIAL:
        lines.append(f"Good places to start: {OFFICIAL[key]}.")
    else:
        lines.append("Find the central bank and the national statistics office first.")
    lines.append("Give the latest figure published for a period within that limit. Report each as_of as a short period only "
                 "(for example Jun 2026, Q1 2026) and put qualifications in basis. If a figure cannot be found and verified, put it "
                 "under gaps.")
    return "\n".join(lines)


def fmt(value: float, unit: str) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0" if unit == "percent" else ""
    return f"{text}%" if unit == "percent" else text


def research_macro(ctx: Context, countries: list[str], period: pr.Period, indicators: list[str] | None = None) -> str:
    """Research every country in parallel; return the compact account the main agent works from.

    `indicators` sends researchers back for only the figures that are still missing. Their results are recorded under
    the same country, so the table (built from every finished run) and the prose draw on one set of verified figures."""
    countries = [c.strip() for c in countries if c.strip()][:12]
    if not countries:
        return "ERROR: name at least one country."
    unknown = [i for i in (indicators or []) if i not in INDICATORS]
    if unknown:
        return f"ERROR: unknown indicator {', '.join(unknown)}; use any of: {', '.join(INDICATORS)}."
    workers = max(1, min(ctx.parallel, len(countries)))
    group = f"macro {period.label}"

    def one(country: str):
        return research_one(ctx, task_for(country, period, indicators), label=country, group=group, quarter_end=period.end)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="researcher") as pool:
        results = list(pool.map(one, countries))
    tokens = sum((o.usage.get("input", 0) + o.usage.get("output", 0)) for o, _ in results)
    lines = [f"Researched {len(countries)} countries in parallel for {period.label}. The researchers used {tokens:,} tokens; none "
             f"of it is in this conversation. Verified figures are recorded in the fact ledger; lay them out with "
             f"build_macro_table, then write the prose from the figures below only."]
    for (out, rec), country in zip(results, countries):
        if out.status != "done":
            lines.append(f"{country}: NOT RESEARCHED ({out.error or out.status}). Leave it out or try again.")
            continue
        got = [f"{INDICATORS[k][0].split(' (')[0].lower()} {fmt(v['value'], v['unit'])}{'' if v['unit'] == 'percent' else ' ' + v['unit']} ({v['as_of']})"
               for k, v in rec["verified"].items() if k in INDICATORS]
        miss = [f"{INDICATORS[k][0].split(' (')[0].lower() if k in INDICATORS else k}: {why[:110]}" for k, why in rec["gaps"].items()]
        lines.append(f"{country}: " + ("; ".join(got) if got else "no verified figures")
                     + (" | gaps: " + "; ".join(miss) if miss else ""))
    return "\n".join(lines)


def _short(as_of: str) -> str:
    """The period alone, for a table cell: 'Q2 2026 average (period average, not period end)' -> 'Q2 2026 average'."""
    return re.split(r"[;(,]", as_of)[0].strip()[:32]


def _best_verified(runs: list[dict], quarter_end) -> dict[str, dict]:
    """For each indicator, the most recent verified figure across this country's finished runs (newest first), so a
    later, thinner run never discards a figure an earlier one verified."""
    from .research import after

    best: dict[str, dict] = {}
    for r in runs:
        for k, v in (((r.get("result") or {}).get("recorded") or {}).get("verified") or {}).items():
            if k not in best and not after(v.get("as_of", ""), quarter_end):
                best[k] = v
    return best


def _gap_reason(runs: list[dict], key: str) -> str:
    """Why an indicator is missing: what the newest run that explained it said (its own key, or a variant like fx_usd_period_end)."""
    for r in runs:
        for k, why in (((r.get("result") or {}).get("recorded") or {}).get("gaps") or {}).items():
            if k == key or k.startswith(key):
                return re.sub(r"\s+", " ", str(why)).strip()[:260]
    return "no figure could be found and verified"


def build_table(store: Store, report_id: str, countries: list[str], section_key: str = "3.1") -> str:
    runs = records.all_done(store, report_id, "web_researcher", countries)
    quarter_end = pr.current().end
    rows, missing, gaps = [], [], []
    found = {c: _best_verified(runs.get(c, []), quarter_end) for c in countries}
    for country in countries:
        mine = runs.get(country, [])
        if not mine:
            missing.append(country)
        ver = dict(found[country])
        if _in_union(country):
            for k in SHARED_IN_UNION:
                if k not in ver:
                    peer = next((found[c][k] for c in countries if c != country and _in_union(c) and k in found[c]), None)
                    if peer:
                        ver[k] = peer
        rows.append([country] + [f"{fmt(ver[k]['value'], ver[k]['unit'])} ({_short(ver[k]['as_of'])})" if k in ver else "—" for k in INDICATORS])
        for k, (name, _) in INDICATORS.items():
            if k not in ver and mine:
                gaps.append((f"{country}: {name.split(' (')[0].lower()} is a dash in the table. {_gap_reason(mine, k)}", "info"))
    if not rows:
        return "ERROR: name the countries to include."
    store.ensure_report("Chui Ventures Fund I", pr.current().label)
    store.set_table("t_macro", f"Macroeconomic indicators — {pr.current().label}", COLUMNS, rows, section_key,
                    {"align": ["l", "r", "r", "r", "r"], "widths": [1.15, 1, 1, 1, 1.15]})
    # The reviewer's notes about these gaps are derived from the same ledger, so they always match the table. They replace
    # earlier macro notes (and earlier complaints about this table's layout, which a rebuilt table makes stale).
    store.replace_review_notes("Macro snapshot", gaps, area_like="macro", also_text_like=("macroeconomic indicators table", "macro table"))
    filled = sum(1 for r in rows for c in r[1:] if c != "—")
    msg = (f"table t_macro saved in section {section_key}: {len(rows)} countries, {filled} of {len(rows) * len(INDICATORS)} "
           f"figures present ('—' where nothing verified). Write the prose from exactly these cells:\n"
           + "\n".join(f"{r[0]}: " + "; ".join(f"{INDICATORS[k][0].split(' (')[0].lower()} {c}" for k, c in zip(INDICATORS, r[1:]) if c != "—")
                       for r in rows))
    if missing:
        msg += f" Not researched yet: {', '.join(missing)}."
    return msg
