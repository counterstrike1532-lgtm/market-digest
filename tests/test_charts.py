"""parse_figures и figures_chart на реальных строках из боевых прогонов (T9a, T9f)."""
from __future__ import annotations

from src import charts, chartstyle


def test_parse_figures_paren_format():
    text = "0.4% (Statistics Poland); 6,872 (Money.pl); 57% (Money.pl)"
    assert charts.parse_figures(text) == [
        ("0.4%", "Statistics Poland"),
        ("6,872", "Money.pl"),
        ("57%", "Money.pl"),
    ]


def test_parse_figures_arrow_format_with_units():
    text = "$200 billion -> Source [3]; $49.3 billion -> Source [3]."
    assert charts.parse_figures(text) == [
        ("$200 billion", "Source [3]"),
        ("$49.3 billion", "Source [3]."),
    ]


def test_parse_figures_none_used():
    assert charts.parse_figures("none used") is None
    assert charts.parse_figures("None used.") is None


def test_parse_figures_empty_block():
    assert charts.parse_figures("") is None
    assert charts.parse_figures("   ") is None


def test_parse_figures_garbage_is_empty_list_not_none():
    result = charts.parse_figures("this is just garbage text with no structure at all")
    assert result == []
    assert result is not None


def test_parse_figures_multiline_with_units():
    """T9b real draft: три пары, одна из них - голый год без единицы."""
    text = "100,000 PLN -> Statistics Poland; 2012 -> Statistics Poland; 185 billion euros -> NBP"
    assert charts.parse_figures(text) == [
        ("100,000 PLN", "Statistics Poland"),
        ("2012", "Statistics Poland"),
        ("185 billion euros", "NBP"),
    ]


def test_parse_figures_multiline_bullet_format():
    """T9f: модель иногда пишет FIGURES построчно, "- значение -> источник" на
    каждой строке, а не через ";"."""
    text = (
        "- 100,000 PLN -> Statistics Poland\n"
        "- 2012 -> Statistics Poland\n"
        "- 185 billion euros -> NBP"
    )
    assert charts.parse_figures(text) == [
        ("100,000 PLN", "Statistics Poland"),
        ("2012", "Statistics Poland"),
        ("185 billion euros", "NBP"),
    ]


# ---------------------------------------------------------------- T9f: figures_chart

def test_figures_chart_refuses_seven_numbers_from_three_stories(tmp_path, monkeypatch):
    """Реальный кейс: 7 чисел из FIGURES трёх РАЗНЫХ сюжетов (долги отелей,
    Micron, налог) слились в один график; подписями стали названия источников,
    повторённые по 2-3 раза. Такое не должно рисоваться вообще - ни как один
    график (>5 значений), ни как усечённый (источники - не показатели)."""
    figures = [
        ("12.5%", "BIG InfoMonitor report on hotel debt"),
        ("340 million PLN", "BIG InfoMonitor report on hotel debt"),
        ("$200 billion", "CNBC article on Micron"),
        ("$49.3 billion", "CNBC article on Micron"),
        ("15%", "tax filing"),
        ("21%", "tax filing"),
        ("8.5%", "tax filing"),
    ]
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    assert charts.figures_chart(figures, title="Draft 1") is None


def test_figures_chart_refuses_duplicate_source_even_within_limit(monkeypatch, tmp_path):
    """Тот же сигнал (источник процитирован дважды), но в пределах 2-5 значений -
    само по себе размера графика недостаточно, чтобы считать структуру верной."""
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    figures = [("12.5%", "CNBC article on Micron"), ("21%", "CNBC article on Micron")]
    assert charts.figures_chart(figures, title="Draft 1") is None


def test_figures_chart_refuses_citation_style_labels(monkeypatch, tmp_path):
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    figures = [("12.5%", "BIG InfoMonitor report"), ("21%", "Statistics Poland study")]
    assert charts.figures_chart(figures, title="Draft 1") is None
    assert charts.figures_chart(
        [("12.5%", "Source [3]"), ("21%", "Source [4]")], title="Draft 1") is None


def test_figures_chart_draws_for_clean_single_story_metrics(monkeypatch, tmp_path):
    """Позитивный кейс не должен пострадать от ужесточения: разные показатели,
    уникальные подписи, один сюжет, одна единица измерения."""
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    figures = [("12.5%", "occupancy rate"), ("21%", "delinquency rate")]
    path = charts.figures_chart(figures, title="Draft 1")
    assert path is not None
    assert path.exists()


# ---------------------------------------------------------------- T9 fix 1/2: market_overview

_WIG_SERIES = {"close": [80.0, 81.0, 82.0, 83.0, 84.0]}
_SPX_SERIES = {"close": [5000.0, 5010.0, 5020.0, 5030.0, 5040.0]}


def test_market_overview_requires_renamed_wig_key(monkeypatch, tmp_path):
    """numbers.market_series() теперь отдаёт ключ "WIG20 TR (ETF)", не "wig20"
    (T9 fix 2) - старый ключ график найти не должен, это признак несинхронной
    подмены, а не повод рисовать неизвестно что."""
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    numbers = {"_series": {"wig20": _WIG_SERIES, "sp500": _SPX_SERIES}}
    assert charts.market_overview(numbers, {}, theme="dark") is None


def test_market_overview_draws_with_renamed_key(monkeypatch, tmp_path):
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    numbers = {"_series": {"WIG20 TR (ETF)": _WIG_SERIES, "sp500": _SPX_SERIES}}
    path = charts.market_overview(numbers, {}, theme="dark")
    assert path is not None
    assert path.exists()


def test_market_overview_handles_mismatched_series_lengths(monkeypatch, tmp_path):
    """Живой прогон: WIG20 TR (ETF) торгуется в Варшаве, S&P 500 - в Нью-Йорке,
    разные праздничные календари дают РАЗНОЕ число торговых дней в одном и том
    же окне (44 против 43) - раньше это падало на fill_between/glow (x и y
    разной длины)."""
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    wig = {"close": [80.0 + i * 0.1 for i in range(44)],
          "dates": [f"2026-06-{(i % 28) + 1:02d}" for i in range(44)]}
    spx = {"close": [5000.0 + i for i in range(43)],
          "dates": [f"2026-06-{(i % 28) + 1:02d}" for i in range(43)]}
    numbers = {"_series": {"WIG20 TR (ETF)": wig, "sp500": spx}}
    path = charts.market_overview(numbers, {}, theme="dark")
    assert path is not None
    assert path.exists()


def test_stretch_x_aligns_endpoints():
    assert charts._stretch_x(5, 5) == [0.0, 1.0, 2.0, 3.0, 4.0]
    stretched = charts._stretch_x(3, 5)
    assert stretched[0] == 0.0
    assert stretched[-1] == 4.0
    assert len(stretched) == 3
    assert charts._stretch_x(1, 5) == [0.0]


def test_market_overview_line_label_is_wig_tr_not_bare_wig20(monkeypatch, tmp_path):
    """Подпись чипа - "WIG20 TR +X%" (T9 redesign - короткая форма, полное имя
    "WIG20 TR (ETF)" остаётся в kicker-заголовке и подвале), не голый "WIG20"
    без "TR" (T9 fix 2): читатель не должен принять цену пая за уровень индекса."""
    import matplotlib.axes

    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    captured = []
    orig = matplotlib.axes.Axes.annotate

    def fake_annotate(self, text, *a, **kw):
        captured.append(text)
        return orig(self, text, *a, **kw)

    monkeypatch.setattr(matplotlib.axes.Axes, "annotate", fake_annotate)
    numbers = {"_series": {"WIG20 TR (ETF)": _WIG_SERIES, "sp500": _SPX_SERIES}}
    charts.market_overview(numbers, {}, theme="dark")
    assert any(t.startswith("WIG20 TR ") for t in captured)
    assert not any(t.startswith("WIG20 ") and "TR" not in t for t in captured)


def test_market_overview_kicker_carries_full_wig_name(monkeypatch, tmp_path):
    """Полное "WIG20 TR (ETF)" - в надзаголовке (kicker), не в самом чипе."""
    captured = {}

    def fake_titles(fig, ax, kicker, title, c):
        captured["kicker"] = kicker

    monkeypatch.setattr(chartstyle, "titles", fake_titles)
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    numbers = {"_series": {"WIG20 TR (ETF)": _WIG_SERIES, "sp500": _SPX_SERIES}}
    charts.market_overview(numbers, {}, theme="dark")
    assert "WIG20 TR (ETF)" in captured["kicker"]


def test_market_overview_footer_shows_actual_source(monkeypatch, tmp_path):
    """Раньше подвал всегда писал "Data: stooq", даже когда данные пришли от
    yfinance (T9 fix 1) - источник теперь берётся из того же market_source,
    что и лог "рынки: yfinance (...)" в numbers.py."""
    captured = {}

    def fake_footer(fig, text, c):
        captured["text"] = text

    monkeypatch.setattr(chartstyle, "footer", fake_footer)
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    numbers = {"_series": {"WIG20 TR (ETF)": _WIG_SERIES, "sp500": _SPX_SERIES}}
    market_source = {"WIG20 TR (ETF)": "yfinance", "sp500": "stooq"}
    path = charts.market_overview(numbers, market_source, theme="dark")
    assert path is not None
    assert "yfinance" in captured["text"]
    assert "stooq" in captured["text"]
    assert "total return" in captured["text"]


def test_market_overview_footer_falls_back_when_source_unknown(monkeypatch, tmp_path):
    captured = {}

    def fake_footer(fig, text, c):
        captured["text"] = text

    monkeypatch.setattr(chartstyle, "footer", fake_footer)
    monkeypatch.setattr(charts, "_OUT_DIR", tmp_path)
    numbers = {"_series": {"WIG20 TR (ETF)": _WIG_SERIES, "sp500": _SPX_SERIES}}
    charts.market_overview(numbers, None, theme="dark")
    assert "unknown" in captured["text"]
