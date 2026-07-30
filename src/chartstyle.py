"""Стиль карточек-графиков: тёмная и светлая тема, один акцентный цвет.
Никакого chart-junk: подписи значений у концов линий вместо легенды, тонкая
горизонтальная сетка, без рамок сверху/справа. rcParams, не rc-файл — проще
и не тянет отдельный конфиг matplotlib.
"""
from __future__ import annotations

ACCENT = "#A9082C"
NEUTRAL = "#8a8a8a"

_THEMES = {
    "dark": {"bg": "#16161a", "fg": "#f5f5f5"},
    "light": {"bg": "#fafafa", "fg": "#1a1a1a"},
}


def colors(theme: str = "dark") -> dict:
    """Цвета фона/текста темы плюс общий акцент и нейтральный серый."""
    t = _THEMES.get(theme, _THEMES["dark"])
    return {"bg": t["bg"], "fg": t["fg"], "accent": ACCENT, "neutral": NEUTRAL}


def rc_params(theme: str = "dark") -> dict:
    """1200x675 px при dpi=150 (16:9). Проверено на обеих темах: подписи значений
    читаются на bg обеих тем при цвете accent/neutral/fg."""
    c = colors(theme)
    return {
        "font.family": "DejaVu Sans",
        "font.size": 12,
        "figure.figsize": (8.0, 4.5),
        "figure.dpi": 150,
        "figure.facecolor": c["bg"],
        "axes.facecolor": c["bg"],
        "axes.edgecolor": c["fg"],
        "axes.labelcolor": c["fg"],
        "axes.titlelocation": "left",
        "axes.titlesize": 20,
        "axes.titleweight": "bold",
        "axes.titlecolor": c["fg"],
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.spines.left": False,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "grid.alpha": 0.15,
        "grid.color": c["fg"],
        "xtick.color": c["fg"],
        "ytick.color": c["fg"],
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "text.color": c["fg"],
        "legend.frameon": False,
        "savefig.facecolor": c["bg"],
        "savefig.dpi": 150,
    }
