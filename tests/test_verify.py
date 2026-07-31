"""verify.py: поиск чисел в тексте и сборка отчёта ЦИФРЫ (T9a, T9b)."""
from __future__ import annotations

from src import charts, verify


# ---------------------------------------------------------------- _to_search_variants

def test_search_variants_polish_decimal_comma():
    assert set(verify._to_search_variants("31,0")) == {"31.0", "31,0"}
    assert set(verify._to_search_variants("31.0")) == {"31.0", "31,0"}


def test_search_variants_thousand_separator_both_forms():
    assert set(verify._to_search_variants("6,872")) == {"6,872", "6872"}
    # источник без запятой-разделителя тысяч — вариант без запятой это уже сам value
    assert verify._to_search_variants("6872") == ["6872"]


def test_search_variants_dollar_billion():
    assert verify._to_search_variants("$200 billion") == ["200billion"]


def test_search_variants_pln_thousand():
    # единица измерения в значении - не голое число, поэтому вариант всего один
    # (это и есть кейс, который в T9b получает статус UNPARSED, а не NOT_FOUND)
    assert verify._to_search_variants("100,000 PLN") == ["100,000PLN"]


# ---------------------------------------------------------------- вставка ЦИФРЫ

_ITEM_1_URL = "https://example.com/story1"
_ITEM_2_URL = "https://example.com/story2"


def _draft_block(shape, body, figures, source, verdict="SKIP", why="no edge"):
    return (
        f"SHAPE: {shape}\n"
        f"BODY: {body}\n"
        f"FIGURES: {figures}\n"
        f"SOURCE: {source}\n"
        f"WHY_THIS_ONE: reason\n"
        f"VERDICT: {verdict}\n"
        f"WHY: {why}\n"
        f"CHECK_FIRST: -"
    )


def test_verify_drafts_inserts_headers_across_dashes_separator():
    """Модель часто разделяет черновики строкой "---" и не печатает свой
    заголовок "DRAFT n" - рендерер должен подписать оба блока и не потерять
    разделитель между ними."""
    raw = (
        _draft_block("digest", "First body.", "none used", _ITEM_1_URL)
        + "\n\n---\n\n"
        + _draft_block("A", "Second body.", "none used", _ITEM_2_URL)
    )
    out = verify.verify_drafts(raw, selected=[], data_text="")

    assert "DRAFT 1 (digest)" in out
    assert "DRAFT 2 (A)" in out
    assert "---" in out
    # разделитель физически между двумя блоками, не склеен ни с одним из них
    idx_sep = out.index("---")
    idx_draft2 = out.index("DRAFT 2 (A)")
    assert idx_sep < idx_draft2
    assert "ЦИФРЫ: ✅ verified (no figures used)" in out
    # аннотация первого черновика идёт до разделителя, не после
    assert out.index("ЦИФРЫ", out.index("DRAFT 1")) < idx_sep


def test_verify_drafts_no_model_header_still_gets_one():
    raw = _draft_block("digest-short", "Only body.", "none used", _ITEM_1_URL)
    out = verify.verify_drafts(raw, selected=[], data_text="")
    assert out.count("DRAFT 1") == 1
    assert "SHAPE: digest-short" in out


# ---------------------------------------------------------------- T9b: денаминатор

def test_denominator_is_pair_count_not_recognized_count():
    """Реальный кейс из прогона: FIGURES из трёх пар ("100,000 PLN", "2012",
    "185 billion euros"), ни одна из первой и третьей не приводится к числу
    (единица измерения в тексте). Раньше это тихо давало "1/1 found" вместо
    честного отчёта - знаменатель занижался вместе с числом распознанных пар."""
    pairs = charts.parse_figures(
        "100,000 PLN -> Statistics Poland; 2012 -> Statistics Poland; "
        "185 billion euros -> NBP")
    assert len(pairs) == 3          # структура распознана для всех трёх

    level_a = verify.verify_figures_local(pairs, bodies=[], data_text="")
    statuses = {r["value"]: r["status"] for r in level_a}
    assert statuses["100,000 PLN"] == "UNPARSED"
    assert statuses["185 billion euros"] == "UNPARSED"
    assert statuses["2012"] == "YEAR"

    report, downgrade, _ = verify._render(level_a, {})
    assert "2/2" not in report and "1/1" not in report   # не выдаёт себя за проверку
    assert "0/2 found" in report
    assert "2 unparsed" in report
    assert "1 year" in report          # год не входит в знаменатель, но заметен
    assert "✅" not in report          # не даёт чистый чек, когда часть непроверена


def test_denominator_all_found_is_still_clean_checkmark():
    pairs = [("6,872", "Money.pl"), ("57%", "Money.pl")]
    data_text = "- 6,872: some data point\n- 57%: another"
    level_a = verify.verify_figures_local(pairs, bodies=[], data_text=data_text)
    report, downgrade, _ = verify._render(level_a, {})
    assert report == "✅ verified (2/2 found)"
    assert downgrade is False


def test_denominator_year_only_excluded_entirely():
    pairs = [("2012", "Statistics Poland")]
    level_a = verify.verify_figures_local(pairs, bodies=[], data_text="")
    report, downgrade, _ = verify._render(level_a, {})
    assert "no checkable figures" in report
    assert "1 year" in report
    assert downgrade is False


def test_unparsed_does_not_force_downgrade_alone():
    """UNPARSED - честно непроверено, но не то же самое, что найденная ошибка
    (NOT_FOUND/MISMATCH). Verdict не понижается принудительно только из-за
    единицы измерения, которую парсер не разобрал."""
    pairs = [("100,000 PLN", "Statistics Poland")]
    level_a = verify.verify_figures_local(pairs, bodies=[], data_text="")
    _, downgrade, offending = verify._render(level_a, {})
    assert downgrade is False
    assert offending is None
