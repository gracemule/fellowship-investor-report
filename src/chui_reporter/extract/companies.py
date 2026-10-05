"""Entity resolution.

Twenty entities appear across five sources, spelled differently in each:
`WiASSUR` / `WiAssur ` / `Waissur`, `MightyFin` / `MighyFin` / `MightFin`,
`AgriLogiq` / `AgriLogiQ` / `AgriLogic `, `Shop Zetu` / `ShopZetu`,
`Pricepally` / `PricePally.com`, `PaidHR` / `PaidHr` / `Pade HCM`. Trailing
spaces are pervasive. Nothing joins across sources without this map.

Aliases are matched on a folded key (case, spacing and punctuation removed) so
the table only needs to carry genuinely different spellings, not every
whitespace variant.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def fold(name: str | None) -> str:
    """Aggressive fold for identity matching: letters and digits only."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.casefold())


@dataclass(frozen=True)
class Company:
    canonical: str
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def keys(self) -> set[str]:
        return {fold(self.canonical)} | {fold(a) for a in self.aliases}


# Canonical names follow the Fund Model's "Portfolio Descriptions" sheet, which
# is the cleanest company dimension in the corpus.
COMPANIES: tuple[Company, ...] = (
    Company("Fingo", ("Fingo Africa",)),
    Company("Leta",),
    Company("ShopZetu", ("Shop Zetu",)),
    Company("Pricepally", ("PricePally.com", "Pricepally Inc")),
    Company("Tappi", ("Tappi Holdings Limited",)),
    Company("PaidHR", ("PaidHr", "Pade HCM", "Paid HR")),
    Company("Regxta",),
    Company("Uncover", ("Uncover Skincare",)),
    Company("Socium",),
    Company("Craydel",),
    Company("Lami", ("LAMI", "Lami Incorporated", "LAMI Incorporated")),
    Company("OneHealth", ("One Health", "One Health and Body")),
    Company("Tuteria", ("Tuteria Education",)),
    Company("Flex Finance", ("Flex Financial Technologies",)),
    Company("Ando Foods", ("Ando", "AndoFoods", "Ando Foods Inc")),
    Company("Agrilogiq", ("AgriLogiQ", "AgriLogic", "Agrilogiq Systems PTY",
                          "Agrilogiq Technical Systems")),
    Company("Emmerce", ("Emmerce Inc",)),
    Company("WiASSUR", ("WiAssur", "Waissur", "WiAssur Holdings")),
    Company("MightyFin", ("MighyFin", "MightFin", "Mighty Finance",
                          "Mighty Finance Solution")),
    # Present in the investment schedule but not the live portfolio.
    Company("MarketForce",),
    Company("Passpoint",),
)

_INDEX: dict[str, str] = {}
for _c in COMPANIES:
    for _k in _c.keys:
        if _k:
            _INDEX[_k] = _c.canonical


class UnknownCompany(KeyError):
    pass


def resolve(name: str | None, *, strict: bool = True) -> str | None:
    """Map any spelling to its canonical name.

    Falls back to prefix matching so that unseen suffixes ("Inc", "Limited",
    "Holdings") resolve without needing a new alias entry -- but only when the
    prefix is long enough to be unambiguous.
    """
    key = fold(name)
    if not key:
        if strict:
            raise UnknownCompany("empty company name")
        return None
    if key in _INDEX:
        return _INDEX[key]
    matches = {v for k, v in _INDEX.items() if len(k) >= 5 and (key.startswith(k) or k.startswith(key))}
    if len(matches) == 1:
        return matches.pop()
    if strict:
        raise UnknownCompany(
            f"{name!r} (folded {key!r}) matched {len(matches)} companies: {sorted(matches)}"
        )
    return None


def canonical_names() -> list[str]:
    return [c.canonical for c in COMPANIES]
