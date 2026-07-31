"""parse_figures и figures_chart на реальных строках из боевых прогонов (T9a, T9f)."""
from __future__ import annotations

from src import charts


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
