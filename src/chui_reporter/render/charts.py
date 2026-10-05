"""Charts in the brand palette. Numbers shown come only from the values given;
the percentages are computed here from those same values."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

NAVY, TANGERINE, AMBER, OCHRE = "#00223B", "#E25A00", "#E28B00", "#5B3000"
PALETTE = [NAVY, TANGERINE, AMBER, "#5B8DB8", OCHRE, "#A9B8C5", "#2F5D7C", "#F2B27A"]
INK, MUTED = "#24313D", "#8C9AAA"

_FONT_DONE = False


def _fonts() -> str:
    """Register TT Norms with matplotlib; fall back to a sans if it is absent."""
    global _FONT_DONE
    fam = "DejaVu Serif"
    for f in sorted((Path.home() / "Library/Fonts").glob("Larken Regular.ttf")):
        font_manager.fontManager.addfont(str(f))
        fam = font_manager.FontProperties(fname=str(f)).get_name()
    _FONT_DONE = True
    return fam


def render_chart(kind: str, labels: list[str], values: list[float], dest: Path, *,
                 title: str | None = None, value_format: str = "{:,.0f}",
                 show_values: bool = True, width_in: float = 6.6, height_in: float = 3.0) -> Path:
    fam = _fonts()
    plt.rcParams.update({"font.family": fam, "text.color": INK, "axes.edgecolor": MUTED})
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind in ("donut", "pie"):
        _ring(labels, values, dest, kind == "donut", value_format, show_values, width_in, height_in)
    elif kind in ("bar", "hbar"):
        _bars(labels, values, dest, kind == "hbar", value_format, show_values, width_in, height_in)
    else:
        raise ValueError(f"unknown chart kind {kind!r}; use donut, pie, bar or hbar")
    return dest


def _ring(labels, values, dest, donut, fmt, show_values, w, h):
    fig, ax = plt.subplots(figsize=(w, h), dpi=200)
    total = float(sum(values)) or 1.0
    wedges, _ = ax.pie(values, colors=PALETTE[: len(values)], startangle=90, counterclock=False,
                       wedgeprops={"width": 0.42 if donut else 1.0, "edgecolor": "white",
                                   "linewidth": 1.6})
    ax.set_aspect("equal")
    rows = [f"{lab}" for lab in labels]
    detail = [f"{fmt.format(v)}   {v / total * 100:.1f}%" if show_values else f"{v / total * 100:.1f}%"
              for v in values]
    leg = ax.legend(wedges, [f"{a}\n{b}" for a, b in zip(rows, detail)], loc="center left",
                    bbox_to_anchor=(1.02, 0.5), frameon=False, fontsize=8.5, labelspacing=1.1,
                    handlelength=1.0, handleheight=1.0)
    for t in leg.get_texts():
        t.set_color(INK)
    fig.subplots_adjust(left=0.02, right=0.60, top=0.97, bottom=0.03)
    fig.savefig(dest, transparent=False, facecolor="white")
    plt.close(fig)


def _bars(labels, values, dest, horizontal, fmt, show_values, w, h):
    fig, ax = plt.subplots(figsize=(w, h), dpi=200)
    idx = range(len(values))
    if horizontal:
        bars = ax.barh(list(idx), values, color=NAVY, height=0.62)
        ax.set_yticks(list(idx), labels, fontsize=8)
        ax.invert_yaxis()
    else:
        bars = ax.bar(list(idx), values, color=NAVY, width=0.62)
        ax.set_xticks(list(idx), labels, fontsize=7.5, rotation=45, ha="right")
    if show_values:
        for b, v in zip(bars, values):
            if horizontal:
                ax.text(b.get_width(), b.get_y() + b.get_height() / 2, " " + fmt.format(v),
                        va="center", fontsize=7.5, color=INK)
            else:
                ax.text(b.get_x() + b.get_width() / 2, b.get_height(), fmt.format(v),
                        ha="center", va="bottom", fontsize=7, color=INK)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, labelsize=8)
    ax.grid(axis="x" if horizontal else "y", color="#E5E9ED", linewidth=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(dest, facecolor="white")
    plt.close(fig)
