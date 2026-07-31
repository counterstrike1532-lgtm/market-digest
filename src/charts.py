"""Картинки к сводке. Ноль запросов Gemini - все данные уже собраны в пайплайне.

Правило без исключений: figures_chart рисует ТОЛЬКО числа из FIGURES черновика,
у которых указан источник. Не распозналась структура - молча возвращает None,
ничего не досочиняем.
"""
from __future__ import annotations

import logging
import pathlib
import re
import tempfile
from datetime import date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (после matplotlib.use, см. ГРАБЛИ)

from . import chartstyle

log = logging.getLogger(__name__)

_OUT_DIR = pathlib.Path(tempfile.gettempdir()) / "newsbot_charts"

# value = числовое ядро ($/цифры/запятые/точки/%) + до 3 слов единицы измерения
# ("billion", "PLN", "billion euros") — модель часто пишет "$200 billion", а не
# голое число. Слова не в числовом ядре: жадный числовой класс без \s не даёт
# им проглотить пробел перед словом, а \s+ в расширении требует его обратно.
_VALUE = r"\$?\d[\d,.%$]*(?:\s+[A-Za-z][\w.]*){0,3}"
_FIGURE_PAREN = re.compile(rf"^(?P<value>{_VALUE})\s*\(\s*(?P<source>[^()]+?)\s*\)$")
_FIGURE_ARROW = re.compile(rf"^(?P<value>{_VALUE})\s*(?:->|—|-)\s*(?P<source>\S.*)$")


def _save(fig, name: str) -> pathlib.Path:
    _OUT_DIR.mkdir(exist_ok=True)
    path = _OUT_DIR / name
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def _footer(fig, source_label: str, c: dict) -> None:
    fig.text(0.01, 0.02, f"Data: {source_label} | {date.today().strftime('%d.%m.%Y')}",
              fontsize=8, color=c["neutral"])


def market_overview(numbers: dict, theme: str = "dark") -> pathlib.Path | None:
    """WIG20 и S&P 500 за ~45 дней, нормированные к 100 на первую дату.

    numbers - полный снимок из numbers.gather() (ещё с ключом "_series",
    main.py забирает его позже). Нет рядов - молча None, текстовая сводка
    уходит без картинки.
    """
    series = numbers.get("_series") or {}
    wig = series.get("wig20")
    spx = series.get("sp500")
    if not wig or not spx or len(wig.get("close", [])) < 2 or len(spx.get("close", [])) < 2:
        log.info("charts: нет рядов wig20/sp500 для market_overview")
        return None
    try:
        with plt.rc_context(chartstyle.rc_params(theme)):
            c = chartstyle.colors(theme)
            fig, ax = plt.subplots()
            for label, s, color in (("WIG20", wig, chartstyle.ACCENT),
                                     ("S&P 500", spx, chartstyle.NEUTRAL)):
                closes = s["close"]
                base = closes[0]
                norm = [v / base * 100 for v in closes]
                x = list(range(len(norm)))
                ax.plot(x, norm, color=color, linewidth=2.4, solid_capstyle="round")
                pct = norm[-1] - 100
                ax.annotate(f"{label} {pct:+.1f}%", xy=(x[-1], norm[-1]),
                            xytext=(6, 0), textcoords="offset points",
                            color=color, fontsize=11, fontweight="bold", va="center")
            ax.set_title("Markets, last 45 days (rebased to 100)")
            ax.set_xticks([])
            ax.margins(x=0.14)
            _footer(fig, "stooq", c)
            return _save(fig, f"market_{theme}.png")
    except Exception as exc:
        log.warning("market_overview упал: %s", exc)
        return None


def parse_figures(text: str) -> list[tuple[str, str]] | None:
    """Парсит FIGURES-поле черновика в пары (значение, источник).

    Модель в реальности пишет и "значение -> источник" по одной паре на строку,
    и "значение (источник); значение (источник)" все пары в одну строку через
    точку с запятой - оба формата поддержаны. Разделитель между парами строго
    ";" или перевод строки, никогда "," - у чисел запятая бывает разделителем
    тысяч ("6,872").

    Возвращает None ТОЛЬКО если поле пустое или буквально "none used" - это
    единственный случай, когда "цифр нет" законно. Если текст непустой, но
    ни одна пара не распозналась, возвращает [] (пусто, но не None) - вызывающий
    код обязан отличать "нечего рисовать/проверять" от "не смогли разобрать".
    """
    text = (text or "").strip()
    if not text or text.lower().startswith("none used"):
        return None
    pairs = []
    for entry in re.split(r"[;\n]", text):
        entry = entry.strip().lstrip("-*").strip()
        if not entry:
            continue
        m = _FIGURE_PAREN.match(entry) or _FIGURE_ARROW.match(entry)
        if not m:
            continue
        value, source = m.group("value").strip(), m.group("source").strip()
        if value and source:
            pairs.append((value, source))
    return pairs


def _to_float(value: str) -> float | None:
    v = value.replace("$", "").replace("%", "").strip().replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def _kind(value: str) -> str:
    """% и $ - разные единицы. Бар длиной "2.6%" рядом с баром "58,600" врёт
    о масштабе, поэтому смешанные единицы = нераспознанная структура."""
    if "%" in value:
        return "pct"
    if "$" in value:
        return "usd"
    return "num"


# Подпись бара должна называть ПОКАЗАТЕЛЬ ("occupancy rate"), а не то, кто его
# сообщил ("BIG InfoMonitor report", "CNBC article", "Source [3]"). Реальный
# случай (T9f): 7 чисел из FIGURES трёх РАЗНЫХ сюжетов слились в один график,
# и подписями стали как раз имена источников, повторённые по 2-3 раза.
_CITATION_LABEL = re.compile(
    r"\b(report|article|press release|filing|study|statement|according to)\b"
    r"|^source\s*(\[\s*\d+\s*\])?\.?$", re.IGNORECASE)


def _looks_like_citation(label: str) -> bool:
    return bool(_CITATION_LABEL.search(label.strip()))


def figures_chart(figures: list[tuple[str, str]] | None, title: str,
                  theme: str = "light") -> pathlib.Path | None:
    """2-5 именованных значений ОДНОЙ единицы измерения с источником -> горизонтальные бары.

    Всё остальное (меньше 2, больше 5, число не распарсилось, смешанные единицы,
    подпись выглядит как имя источника, а не показателя, один и тот же источник
    процитирован больше одного раза - явный признак, что слиты числа из разных
    сюжетов) -> None молча. Сомнение = None (T8a, ужесточено в T9f): лучше прогон
    без графика, чем график, который врёт о структуре данных.
    """
    if not figures or not (2 <= len(figures) <= 5):
        return None
    sources = [source for _, source in figures]
    if len(set(sources)) < len(sources):
        return None
    if any(_looks_like_citation(source) for source in sources):
        return None
    parsed = []
    for value, source in figures:
        f = _to_float(value)
        if f is None:
            return None
        parsed.append((source, f, value))
    if len({_kind(value) for value, _ in figures}) > 1:
        return None
    try:
        with plt.rc_context(chartstyle.rc_params(theme)):
            c = chartstyle.colors(theme)
            fig, ax = plt.subplots()
            parsed = parsed[::-1]          # первый пункт FIGURES - верхний бар
            labels = [p[0][:40] for p in parsed]
            nums = [p[1] for p in parsed]
            raw = [p[2] for p in parsed]
            y = list(range(len(nums)))
            bar_colors = [chartstyle.ACCENT if i == len(nums) - 1 else chartstyle.NEUTRAL
                         for i in range(len(nums))]
            ax.barh(y, nums, color=bar_colors, height=0.55)
            for yi, (n, r) in enumerate(zip(nums, raw)):
                ax.annotate(r, xy=(n, yi), xytext=(6, 0), textcoords="offset points",
                            va="center", color=c["fg"], fontsize=11, fontweight="bold")
            ax.set_yticks(y)
            ax.set_yticklabels(labels, fontsize=10)
            ax.set_title(title[:60] if title else "")
            ax.set_xticks([])
            _footer(fig, "draft FIGURES", c)
            return _save(fig, f"figures_{theme}.png")
    except Exception as exc:
        log.warning("figures_chart упал: %s", exc)
        return None
