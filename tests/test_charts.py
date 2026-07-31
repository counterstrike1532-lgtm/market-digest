"""parse_figures на реальных строках из боевых прогонов (T9a)."""
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
