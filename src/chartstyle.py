"""Стиль карточек-графиков: тёмная и светлая тема, один акцентный цвет.
Ориентир — карточки Robinhood/Revolut, графики FT/Bloomberg: никакого
matplotlib-по-умолчанию. rcParams, не rc-файл — проще и не тянет отдельный
конфиг matplotlib.

Шрифт Inter (SIL OFL 1.1, LICENSE лежит рядом в assets/fonts/) регистрируется
здесь один раз при импорте. Файла нет или регистрация упала - молча откатываемся
на DejaVu Sans, прогон это не должно ронять ни при каких обстоятельствах.
"""
from __future__ import annotations

import logging
import pathlib

log = logging.getLogger(__name__)

ACCENT = "#A9082C"
NEUTRAL = "#8a8a8a"       # обратная совместимость: старое имя, см. colors()["neutral"]

_FONTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "fonts"
_FONT_FILES = ("Inter-Regular.ttf", "Inter-SemiBold.ttf")


def _register_inter() -> str:
    """Возвращает имя семейства для rcParams: "Inter", если хотя бы Regular
    зарегистрировался, иначе "DejaVu Sans". Никогда не бросает исключение —
    отрисовка графика не должна падать из-за шрифта."""
    try:
        from matplotlib import font_manager

        regular = _FONTS_DIR / _FONT_FILES[0]
        if not regular.exists():
            return "DejaVu Sans"
        for name in _FONT_FILES:
            path = _FONTS_DIR / name
            if path.exists():
                font_manager.fontManager.addfont(str(path))
        return "Inter"
    except Exception as exc:
        log.warning("chartstyle: Inter не загрузился, использую DejaVu Sans: %s", exc)
        return "DejaVu Sans"


FONT_FAMILY = _register_inter()

_THEMES = {
    "dark": {
        "bg": "#16161a",
        "fg": "#f5f5f5",
        "muted": "#9a9aa3",
        "grid": "#f5f5f5",
        "neutral_line": "#b8b8c0",     # светлее NEUTRAL — иначе тонет на тёмном фоне
        "chip_neutral_bg": "#f5f5f5",
        "chip_neutral_alpha": 0.16,
    },
    "light": {
        "bg": "#fafafa",
        "fg": "#1a1a1a",
        "muted": "#6b6b70",
        "grid": "#1a1a1a",
        "neutral_line": "#8a8a8a",
        "chip_neutral_bg": "#1a1a1a",
        "chip_neutral_alpha": 0.10,
    },
}


def colors(theme: str = "dark") -> dict:
    """Цвета темы плюс общий акцент. chip_accent_* — сплошной бейдж под цифру
    акцентной линии (белый текст на акценте — единственное сочетание, которое
    читается на обеих темах без подгонки под каждую по отдельности)."""
    t = _THEMES.get(theme, _THEMES["dark"])
    return {
        "bg": t["bg"], "fg": t["fg"], "muted": t["muted"], "grid": t["grid"],
        "accent": ACCENT, "neutral": t["neutral_line"],
        "chip_accent_bg": ACCENT, "chip_accent_fg": "#ffffff",
        "chip_neutral_bg": t["chip_neutral_bg"], "chip_neutral_fg": t["fg"],
        "chip_neutral_alpha": t["chip_neutral_alpha"],
    }


def draw_grid(ax, y_min: float, y_max: float, c: dict, n: int = 5) -> None:
    """n горизонтальных линий, ровно распределённых по видимому диапазону,
    едва заметных (alpha 0.08) и строго за данными (zorder ниже линий/заливки)."""
    if y_max <= y_min:
        return
    step = (y_max - y_min) / (n + 1)
    for i in range(1, n + 1):
        ax.axhline(y_min + step * i, color=c["grid"], alpha=0.08, linewidth=1,
                  zorder=0)


def draw_baseline(ax, y: float, c: dict, label: str) -> None:
    """Пунктирная база нормировки (обычно 100) — заметнее обычной сетки, с
    мелкой подписью у левого края."""
    ax.axhline(y, color=c["muted"], alpha=0.3, linewidth=1, linestyle="--",
              zorder=1)
    ax.text(0.005, y, label, transform=ax.get_yaxis_transform(),
           ha="left", va="bottom", fontsize=8, color=c["muted"])


def glow_line(ax, x, y, color: str, base_lw: float = 2.6, zorder: int = 3) -> None:
    """Основная линия + 3 более широких и прозрачных прохода под ней —
    имитация свечения без растровых эффектов (недоступны в чистом matplotlib
    без дополнительных фильтров/зависимостей)."""
    for lw, alpha in ((12, 0.02), (8, 0.04), (5, 0.06)):
        ax.plot(x, y, color=color, linewidth=lw, alpha=alpha,
               solid_capstyle="round", solid_joinstyle="round", zorder=zorder - 1)
    ax.plot(x, y, color=color, linewidth=base_lw, alpha=1.0,
           solid_capstyle="round", solid_joinstyle="round", zorder=zorder)


def gradient_fill(ax, x, y_line: list[float], y_bottom: float, color: str,
                  n: int = 14, max_alpha: float = 0.18, zorder: int = 1) -> None:
    """Заливка под линией без единого градиента (matplotlib fill_between не
    умеет вертикальный градиент нативно): n смежных горизонтальных полос от
    линии до низа области, alpha убывает линейно от max_alpha у линии до ~0
    у дна — глазом читается как непрерывный градиент."""
    for i in range(n):
        frac_top = i / n
        frac_bottom = (i + 1) / n
        y_top = [v - frac_top * (v - y_bottom) for v in y_line]
        y_low = [v - frac_bottom * (v - y_bottom) for v in y_line]
        alpha = max_alpha * (1 - i / n)
        ax.fill_between(x, y_low, y_top, color=color, alpha=alpha,
                        linewidth=0, zorder=zorder)


def end_marker(ax, x, y, color: str, zorder: int = 4) -> None:
    ax.scatter([x], [y], color=color, s=32, zorder=zorder, edgecolors="none")


def value_chip(ax, x, y, text: str, c: dict, kind: str = "accent",
               xytext: tuple = (10, 0)) -> None:
    """Подпись значения как скруглённый бейдж, не голый текст. accent — сплошной
    фон акцентного цвета с белым текстом (единственное сочетание, читаемое на
    обеих темах без калибровки под каждую отдельно); neutral — полупрозрачный
    фон в цвете темы со своим текстом."""
    if kind == "accent":
        face, alpha, fg = c["chip_accent_bg"], 1.0, c["chip_accent_fg"]
    else:
        face, alpha, fg = c["chip_neutral_bg"], c["chip_neutral_alpha"], c["chip_neutral_fg"]
    ax.annotate(text, xy=(x, y), xytext=xytext, textcoords="offset points",
               va="center", ha="left", color=fg, fontsize=11, fontweight="semibold",
               bbox=dict(boxstyle="round,pad=0.45", facecolor=face,
                        edgecolor="none", alpha=alpha),
               zorder=5)


def titles(fig, ax, kicker: str, title: str, c: dict) -> None:
    """Три уровня типографики заголовка: надзаголовок капсом, заголовок,
    основной ax.set_title заголовком matplotlib не пользуемся — нужен
    отдельный текстовый блок над ним с letterspacing-подобным разрежением."""
    spaced = " ".join(kicker.upper())            # имитация letter-spacing
    fig.text(0.09, 0.93, spaced, fontsize=9.5, color=c["muted"],
             fontweight="normal", ha="left", va="bottom")
    ax.set_title(title, loc="left", pad=18)


def footer(fig, text: str, c: dict) -> None:
    fig.text(0.09, 0.02, text, fontsize=8, color=c["muted"], ha="left", va="bottom")


def x_edge_labels(ax, first: str, last: str, c: dict) -> None:
    """Первая/последняя дата окна — единственная подпись оси X, в нижних
    углах области графика, тики убраны полностью (см. rc_params)."""
    ax.text(0.01, 0.02, first, transform=ax.transAxes, fontsize=8,
           color=c["muted"], ha="left", va="bottom")
    ax.text(0.99, 0.02, last, transform=ax.transAxes, fontsize=8,
           color=c["muted"], ha="right", va="bottom")


def rc_params(theme: str = "dark") -> dict:
    """1200x675 px при dpi=150 (16:9) — не менять, размер зафиксирован снаружи.

    Никакого chart-junk: спайны все выключены (включая нижний — раньше был
    виден белой полосой), тики обеих осей выключены полностью. Сетка и
    x-подписи первой/последней даты рисуются вручную в charts.py — нужен
    точный контроль alpha/z-order/количества линий, которого rcParams не даёт.
    """
    c = colors(theme)
    return {
        "font.family": FONT_FAMILY,
        "font.sans-serif": [FONT_FAMILY, "DejaVu Sans"],
        "font.size": 12,
        "figure.figsize": (8.0, 4.5),
        "figure.dpi": 150,
        "figure.facecolor": c["bg"],
        "axes.facecolor": c["bg"],
        "axes.edgecolor": c["fg"],
        "axes.labelcolor": c["fg"],
        "axes.titlelocation": "left",
        "axes.titlesize": 22,
        "axes.titleweight": "semibold",
        "axes.titlecolor": c["fg"],
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.spines.left": False,
        "axes.spines.bottom": False,
        "axes.grid": False,          # сетку рисуем вручную — см. charts._draw_grid
        "axes.axisbelow": True,
        "xtick.bottom": False,
        "xtick.top": False,
        "ytick.left": False,
        "ytick.right": False,
        "xtick.color": c["muted"],
        "ytick.color": c["muted"],
        "text.color": c["fg"],
        "legend.frameon": False,
        "savefig.facecolor": c["bg"],
        "savefig.dpi": 150,
    }
