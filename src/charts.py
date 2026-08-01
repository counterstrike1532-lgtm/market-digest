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
import textwrap
from datetime import date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (после matplotlib.use, см. ГРАБЛИ)

from . import chartstyle

log = logging.getLogger(__name__)

_OUT_DIR = pathlib.Path(tempfile.gettempdir()) / "newsbot_charts"

# value = числовое ядро ($/цифры/запятые/точки/% + необязательный слитный
# диапазон "2.8-3.2") + до 3 слов единицы измерения ("billion", "PLN",
# "billion euros") — модель часто пишет "$200 billion", а не голое число.
# Слова не в числовом ядре: жадный числовой класс без \s не даёт им проглотить
# пробел перед словом, а \s+ в расширении требует его обратно.
#
# Слитный диапазон ("2.8-3.2 million tons") - ядро ДОЛЖНО жадно захватывать
# "-\d..." как часть себя самого: живой прогон (T10g review) показал, что без
# этого общий "-" -разделитель "value - source" (см. _FIGURE_ARROW) откусывает
# половину диапазона себе - "2.8-3.2 million tons -> Source [3] text"
# разбиралось как value="2.8", source="3.2 million tons -> Source [3] text"
# (хвост с настоящей стрелкой внутри "source" же и остаётся, поле полностью
# искажено). Дефис в диапазоне всегда стоит МЕЖДУ цифрами без пробелов - у
# разделителя "value - source" пробел или буква после дефиса обязательны,
# так что жадный "-\d" в ядре не может случайно проглотить настоящий
# разделитель.
_NUM_CORE = r"\$?\d[\d,.%$]*(?:-\d[\d,.%$]*)?"
_NUM_VALUE = rf"{_NUM_CORE}(?:\s+[A-Za-z][\w.]*){{0,3}}"

# Диапазон через "to"/"and" ("80,000 to 120,000", "2.8 and 3.2 million tons")
# и дата словом-месяцем ("August 1, 2026") раньше вообще не матчились _VALUE
# (число-диапазон не начинается с "->" сразу после первого числа, дата не
# начинается с цифры) - вся строка FIGURES молча выпадала из pairs, и
# знаменатель верификатора оказывался меньше настоящего числа пар (9 пар ->
# 7 распознанных -> 6 countable вместо 8, T10c req 3). Модель пишет диапазон
# и через "to", и через "and" в разных черновиках одного прогона (T10g
# review) - оба варианта нужны. Не обязаны привестись к float (см.
# verify._to_float) - обязаны только не теряться на этапе парсинга.
_MONTH = (r"(?:January|February|March|April|May|June|July|August|"
         r"September|October|November|December)")
_DATE_VALUE = rf"{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}"
_VALUE = rf"(?:{_DATE_VALUE}|{_NUM_VALUE}(?:\s+(?:to|and)\s+{_NUM_VALUE})?)"
_FIGURE_PAREN = re.compile(rf"^(?P<value>{_VALUE})\s*\(\s*(?P<source>[^()]+?)\s*\)$")
_FIGURE_ARROW = re.compile(rf"^(?P<value>{_VALUE})\s*(?:->|—|-)\s*(?P<source>\S.*)$")


def _stretch_x(n: int, n_max: int) -> list[float]:
    """n точек, растянутых на общий диапазон [0, n_max-1] - оба конца (первый и
    последний торговый день) совпадают у рядов разной длины (см. market_overview)."""
    if n <= 1:
        return [0.0] * n
    return [i * (n_max - 1) / (n - 1) for i in range(n)]


def _save(fig, name: str) -> pathlib.Path:
    _OUT_DIR.mkdir(exist_ok=True)
    path = _OUT_DIR / name
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


_WIG_KEY = "WIG20 TR (ETF)"    # см. numbers._DISPLAY_NAME - переименовано там (T9 fix 2)


def market_overview(numbers: dict, market_source: dict | None = None,
                    theme: str = "dark") -> pathlib.Path | None:
    """WIG20 TR (ETF) и S&P 500 за ~45 дней, нормированные к 100 на первую дату.
    Карточка в духе Robinhood/Revolut/FT, не matplotlib-по-умолчанию - см.
    chartstyle.py (свечение, градиентная заливка, чипы значений, типографика).

    numbers - полный снимок из numbers.gather() (ещё с ключом "_series",
    main.py забирает его позже). Нет рядов - молча None, текстовая сводка
    уходит без картинки.

    market_source - {symbol_name: "stooq"|"yfinance"} из numbers.gather()
    (ключ "_market_source"), для честной подписи в подвале - раньше там всегда
    стояло "Data: stooq", даже когда данные реально пришли от yfinance (T9 fix 1).
    """
    market_source = market_source or {}
    series = numbers.get("_series") or {}
    wig = series.get(_WIG_KEY)
    spx = series.get("sp500")
    if not wig or not spx or len(wig.get("close", [])) < 2 or len(spx.get("close", [])) < 2:
        log.info("charts: нет рядов %s/sp500 для market_overview", _WIG_KEY)
        return None
    try:
        with plt.rc_context(chartstyle.rc_params(theme)):
            c = chartstyle.colors(theme)
            fig, ax = plt.subplots()

            wig_y = [v / wig["close"][0] * 100 for v in wig["close"]]
            spx_y = [v / spx["close"][0] * 100 for v in spx["close"]]
            # WIG20 TR (ETF) торгуется в Варшаве, S&P 500 - в Нью-Йорке: разные
            # праздничные календари легко дают разное число торговых дней в
            # одном и том же окне (живой прогон: 44 против 43). Растягиваем
            # каждый ряд на общий x-диапазон [0, n_max-1], чтобы обе линии
            # начинались и заканчивались вместе - без этого fill_between/glow
            # падают на несовпадении длин x и y.
            n_max = max(len(wig_y), len(spx_y))
            x_wig = _stretch_x(len(wig_y), n_max)
            x_spx = _stretch_x(len(spx_y), n_max)

            all_vals = wig_y + spx_y + [100.0]
            data_min, data_max = min(all_vals), max(all_vals)
            span = (data_max - data_min) or 5.0
            y_min, y_max = data_min - span * 0.22, data_max + span * 0.30
            ax.set_ylim(y_min, y_max)
            ax.set_xlim(-0.5, (n_max - 1) * 1.34)     # щедрое поле справа под чипы значений

            chartstyle.draw_grid(ax, y_min, y_max, c, n=5)
            chartstyle.draw_baseline(ax, 100.0, c, "100")

            # нейтральная линия первой (под акцентной) - без свечения и заливки
            ax.plot(x_spx, spx_y, color=c["neutral"], linewidth=2.0,
                   solid_capstyle="round", solid_joinstyle="round", zorder=2)
            chartstyle.end_marker(ax, x_spx[-1], spx_y[-1], c["neutral"], zorder=4)
            chartstyle.value_chip(ax, x_spx[-1], spx_y[-1],
                                 f"S&P 500 {spx_y[-1] - 100:+.1f}%", c, kind="neutral")

            chartstyle.gradient_fill(ax, x_wig, wig_y, y_min, c["accent"])
            chartstyle.glow_line(ax, x_wig, wig_y, c["accent"])
            chartstyle.end_marker(ax, x_wig[-1], wig_y[-1], c["accent"], zorder=5)
            chartstyle.value_chip(ax, x_wig[-1], wig_y[-1],
                                 f"WIG20 TR {wig_y[-1] - 100:+.1f}%", c, kind="accent")

            ax.set_xticks([])
            ax.set_yticks([])
            dates = wig.get("dates") or []
            chartstyle.x_edge_labels(ax, dates[0] if dates else "",
                                    dates[-1] if dates else "", c)

            chartstyle.titles(fig, ax, f"{_WIG_KEY} vs S&P 500 - rebased to 100",
                             "Markets, last 45 days", c)

            sources = sorted({market_source[k] for k in (_WIG_KEY, "sp500")
                             if k in market_source}) or ["unknown"]
            footer_text = (f"Data: {'/'.join(sources)} | {date.today().strftime('%d.%m.%Y')} | "
                          f"PL line is total return (incl. dividends), S&P 500 is price-only")
            chartstyle.footer(fig, footer_text, c)

            fig.subplots_adjust(left=0.09, right=0.99, top=0.80, bottom=0.11)
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
            bar_colors = [c["accent"] if i == len(nums) - 1 else c["neutral"]
                         for i in range(len(nums))]
            ax.margins(x=0.28)             # щедрое поле справа под чипы значений
            ax.barh(y, nums, color=bar_colors, height=0.55, zorder=2)
            for yi, (n, r) in enumerate(zip(nums, raw)):
                kind = "accent" if yi == len(nums) - 1 else "neutral"
                chartstyle.value_chip(ax, n, yi, r, c, kind=kind, xytext=(8, 0))
            ax.set_yticks(y)
            ax.set_yticklabels(labels, fontsize=10)
            ax.set_title(title[:60] if title else "", loc="left", pad=14)
            ax.set_xticks([])
            chartstyle.footer(fig, f"Data: draft FIGURES | {date.today().strftime('%d.%m.%Y')}", c)
            fig.subplots_adjust(left=0.22, right=0.98, top=0.86, bottom=0.12)
            return _save(fig, f"figures_{theme}.png")
    except Exception as exc:
        log.warning("figures_chart упал: %s", exc)
        return None


def stat_card(rows: list[tuple[str, str]], title: str, subtitle: str,
              theme: str = "dark", key: str = "0") -> pathlib.Path | None:
    """Карточка-типографика для дайджест-черновиков (T10e): до 3 строк "число +
    короткая фраза", ничего не утверждает про связь между числами (в отличие
    от figures_chart) - поэтому числа из разных сюжетов законно уживаются на
    одной карточке. Одна раскладка, без вариантов - заготовка мысли, не
    публикуемый ассет, финальный график владелец делает сам.

    rows - уже отфильтрованы вызывающим кодом (main.py) до статуса FOUND по
    верификатору: сюда попадает не более 3 пар, само значение и его форма
    здесь не проверяются заново.

    key - различает файлы разных вызовов в одном прогоне (main.py передаёт
    номер черновика). Живой прогон (T10g review) показал: без ключа стат-
    карточки черновиков 1 и 2 писали в один и тот же путь "stat_dark.png" -
    вторая перезаписывала файл раньше, чем deliver.send_photo успевал
    прочитать первую, и черновику 1 отправлялась картинка черновика 2."""
    rows = (rows or [])[:3]
    if not rows:
        return None
    try:
        with plt.rc_context(chartstyle.rc_params(theme)):
            c = chartstyle.colors(theme)
            fig, ax = plt.subplots()
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.set_xticks([])
            ax.set_yticks([])

            n = len(rows)
            band = 1.0 / n
            for i, (value, phrase) in enumerate(rows):
                y_top = 1.0 - band * i
                y_center = y_top - band / 2
                ax.text(0.0, y_center + band * 0.16, str(value), fontsize=28,
                       fontweight="semibold", color=c["accent"], ha="left", va="center")
                ax.text(0.0, y_center - band * 0.22, str(phrase)[:80], fontsize=13,
                       color=c["fg"], ha="left", va="center")
                if i < n - 1:
                    ax.axhline(y_top - band, color=c["grid"], alpha=0.10, linewidth=1)

            chartstyle.titles(fig, ax, subtitle or "", title or "", c)
            chartstyle.footer(fig, f"Data: draft FIGURES | {date.today().strftime('%d.%m.%Y')}", c)
            fig.subplots_adjust(left=0.09, right=0.96, top=0.80, bottom=0.12)
            return _save(fig, f"stat_{key}_{theme}.png")
    except Exception as exc:
        log.warning("stat_card упал: %s", exc)
        return None


def quote_card(sentence: str, source: str, theme: str = "dark",
               key: str = "0") -> pathlib.Path | None:
    """Фолбэк stat_card (T10e): одна сильная фраза черновика типографикой плюс
    источник - когда проверенных (FOUND) чисел не осталось вовсе. Пустая
    фраза - None, а не пустая карточка. key - см. stat_card (та же коллизия
    возможна между черновиками, оба падающими на этот фолбэк в одном прогоне)."""
    sentence = (sentence or "").strip()
    if not sentence:
        return None
    try:
        with plt.rc_context(chartstyle.rc_params(theme)):
            c = chartstyle.colors(theme)
            fig, ax = plt.subplots()
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.set_xticks([])
            ax.set_yticks([])

            wrapped = textwrap.fill(sentence, width=38)
            ax.text(0.5, 0.58, wrapped, fontsize=21, fontweight="semibold",
                   color=c["fg"], ha="center", va="center", linespacing=1.45)
            if source:
                ax.text(0.5, 0.14, source[:60], fontsize=12, color=c["muted"],
                       ha="center", va="center")

            fig.subplots_adjust(left=0.10, right=0.90, top=0.92, bottom=0.10)
            return _save(fig, f"quote_{key}_{theme}.png")
    except Exception as exc:
        log.warning("quote_card упал: %s", exc)
        return None
