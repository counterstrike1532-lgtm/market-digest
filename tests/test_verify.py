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
    # T9 fix 3: словом-порядком, с пробелом - "200 billion", как источник и пишет,
    # не слитно "200billion" (раньше именно так, substring-поиск не находил ничего)
    assert verify._to_search_variants("$200 billion") == ["200 billion"]


def test_search_variants_pln_thousand():
    assert verify._to_search_variants("100,000 PLN") == ["100,000 PLN", "100000 PLN"]


def test_search_variants_magnitude_words_real_run_examples():
    """T9 fix 3: реальный прогон, 4 из 5 значений не находились - все со
    словом-порядком или составной единицей после числа."""
    assert verify._to_search_variants("$45 billion") == ["45 billion"]
    assert verify._to_search_variants("$10 billion") == ["10 billion"]
    assert verify._to_search_variants("8.5 million tons") == \
        ["8.5 million tons", "8,5 million tons"]
    assert verify._to_search_variants("1.3 percentage points") == \
        ["1.3 percentage points", "1,3 percentage points"]


def test_to_float_understands_magnitude_words():
    assert verify._to_float("$45 billion") == 45e9
    assert verify._to_float("8.5 million tons") == 8.5e6
    assert verify._to_float("1.3 percentage points") == 1.3
    assert verify._to_float("100,000 PLN") == 100000.0
    # без числового ядра в начале - как и раньше, не парсится
    assert verify._to_float("approximately 45") is None


def test_to_float_handles_thousand_separator_with_decimal():
    """Реальный прогон (после T9 fix 3): "15,019.5 thousand" - разделитель тысяч
    И десятичная точка одновременно. Раньше это давало "15.019.5" (две точки,
    float() падает) и число уходило в UNPARSED незаслуженно."""
    assert verify._to_float("15,019.5 thousand") == 15_019_500.0
    assert verify._to_search_variants("15,019.5 thousand") == \
        ["15,019.5 thousand", "15019.5 thousand"]


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
    out, stats = verify.verify_drafts(raw, selected=[], data_text="")

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
    assert stats["drafted"] == 2
    assert stats["verdicts"]["SKIP"] == 2


def test_verify_drafts_no_model_header_still_gets_one():
    raw = _draft_block("digest-short", "Only body.", "none used", _ITEM_1_URL)
    out, stats = verify.verify_drafts(raw, selected=[], data_text="")
    assert out.count("DRAFT 1") == 1
    assert "SHAPE: digest-short" in out
    assert stats["drafted"] == 1


# ---------------------------------------------------------------- T9b: денаминатор

def test_denominator_is_pair_count_not_recognized_count():
    """Реальный кейс из прогона: FIGURES из трёх пар ("100,000 PLN", "2012",
    "185 billion euros"). Раньше "1/1 found" вместо честного отчёта -
    знаменатель занижался вместе с числом распознанных пар. Со значением/бодами
    без совпадения "100,000 PLN" и "185 billion euros" теперь честно
    NO_SOURCE_TEXT (T9 fix 3 научил их распознаваться как числа - см.
    test_magnitude_words_now_findable_in_source ниже про сам поиск), "2012" -
    по-прежнему YEAR и не входит в знаменатель."""
    pairs = charts.parse_figures(
        "100,000 PLN -> Statistics Poland; 2012 -> Statistics Poland; "
        "185 billion euros -> NBP")
    assert len(pairs) == 3          # структура распознана для всех трёх

    level_a = verify.verify_figures_local(pairs, bodies=[], data_text="")
    statuses = {r["value"]: r["status"] for r in level_a}
    assert statuses["100,000 PLN"] == "NO_SOURCE_TEXT"
    assert statuses["185 billion euros"] == "NO_SOURCE_TEXT"
    assert statuses["2012"] == "YEAR"

    report, downgrade, _ = verify._render(level_a, {})
    assert "2/2" not in report and "1/1" not in report   # не выдаёт себя за проверку
    assert "1 year" in report          # год не входит в знаменатель, но заметен


def test_magnitude_words_now_findable_in_source():
    """T9 fix 3: "$45 billion"/"185 billion euros" структурно распознаются
    parse_figures и ТЕПЕРЬ находятся в тексте источника словесной формой -
    раньше падали в UNPARSED и поиск даже не пытался."""
    pairs = [("185 billion euros", "NBP"), ("$45 billion", "CNBC")]
    body = ("The fund raised 185 billion euros this year. Separately, "
            "the company reported $45 billion in quarterly revenue.")
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    statuses = {r["value"]: r["status"] for r in level_a}
    assert statuses["185 billion euros"] == "FOUND"
    assert statuses["$45 billion"] == "FOUND"


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


def test_unparsed_forces_downgrade():
    """T9 fix 6: непроверенное число - это не проверенное число. Раньше
    UNPARSED-only черновик оставался чистым POST; теперь принудительно MAYBE,
    как NOT_FOUND/MISMATCH."""
    pairs = [("approximately 45 or so", "Statistics Poland")]
    level_a = verify.verify_figures_local(pairs, bodies=[], data_text="")
    assert level_a[0]["status"] == "UNPARSED"
    report, downgrade, offending = verify._render(level_a, {})
    assert downgrade is True
    assert offending == ["approximately 45 or so"]
    assert "unparsed" in report
    assert "✅" not in report


def test_unparsed_offending_lists_all_values_not_just_first():
    pairs = [("approximately 45", "A"), ("roughly 90", "B")]
    level_a = verify.verify_figures_local(pairs, bodies=[], data_text="")
    _, downgrade, offending = verify._render(level_a, {})
    assert downgrade is True
    assert set(offending) == {"approximately 45", "roughly 90"}
    assert "2 unparsed" in verify._render(level_a, {})[0]


# ---------------------------------------------------------------- T9d: метрики прогона

def test_stats_verdicts_reflect_verifier_downgrade_not_raw_verdict():
    """Черновик со своим VERDICT: POST, у которого верификатор находит
    NOT_FOUND число, должен попасть в счётчик MAYBE, а не POST - метрики
    считают эффективный вердикт, а не то, что написала модель."""
    from types import SimpleNamespace

    data_text = "- money.pl metric: 6,872 (some source)"
    draft_ok = _draft_block("digest", "All good here.", "6,872 (Money.pl)",
                            _ITEM_1_URL, verdict="POST")
    draft_bad = _draft_block("A", "Something else.", "999,999 (Money.pl)",
                             _ITEM_2_URL, verdict="POST")
    draft_skip = _draft_block("digest-short", "Skip this.", "none used",
                              "https://example.com/story3", verdict="SKIP")
    raw = "\n\n".join([draft_ok, draft_bad, draft_skip])

    # тело статьи 2 без фигурирующего числа - иначе пустое тело даёт
    # NO_SOURCE_TEXT, а не NOT_FOUND (это две разные, недвижимые статусы)
    selected = [{"item": SimpleNamespace(url=_ITEM_2_URL),
                "body": "This article talks about something unrelated entirely."}]

    _, stats = verify.verify_drafts(raw, selected=selected, data_text=data_text)

    assert stats["drafted"] == 3
    assert stats["verdicts"] == {"POST": 1, "MAYBE": 1, "SKIP": 1}
    assert stats["figures"]["found"] == 1
    assert stats["figures"]["not_found"] == 1


def test_verify_drafts_check_first_lists_all_unparsed_values():
    """T9 fix 6: unparsed-only черновик раньше оставался чистым POST. Теперь
    верификатор сам понижает и перечисляет ВСЕ непроверенные значения в своей
    аннотации, а не только первое. Значения структурно распознаются
    parse_figures (начинаются с цифры), но не приводятся к числу даже с учётом
    слов-порядков (T9 fix 3) - искажённый десятичный формат."""
    draft = _draft_block(
        "digest", "Body text.",
        "3.4.5 million -> A; 1,2,3 -> A",
        _ITEM_1_URL, verdict="POST")
    out, stats = verify.verify_drafts(draft, selected=[], data_text="")
    assert stats["verdicts"] == {"POST": 0, "MAYBE": 1, "SKIP": 0}
    assert "VERDICT эффективно MAYBE" in out
    assert '"3.4.5 million"' in out
    assert '"1,2,3"' in out


def test_stats_empty_when_format_unparseable():
    stats = verify._empty_stats()
    assert stats["drafted"] == 0
    assert sum(stats["verdicts"].values()) == 0
    assert sum(stats["figures"].values()) == 0


# ---------------------------------------------------------------- T9e: заголовки DRAFT

def test_verify_drafts_dedups_model_printed_header():
    """Модель сама напечатала "DRAFT 2" перед SHAPE: - раньше рендерер дописывал
    свой заголовок следом без разделителя ("DRAFT 2DRAFT 2 (A)"). Теперь чужой
    заголовок вырезается, остаётся ровно один - в каноническом формате."""
    raw = (
        _draft_block("digest", "First body.", "none used", _ITEM_1_URL)
        + "\n\nDRAFT 2\n"
        + _draft_block("A", "Second body.", "none used", _ITEM_2_URL)
    )
    out, _ = verify.verify_drafts(raw, selected=[], data_text="")
    assert "DRAFT 2DRAFT 2" not in out
    assert out.count("DRAFT 2") == 1
    assert "DRAFT 2 (A)" in out


def test_verify_drafts_dedups_header_with_no_blank_line_before_it():
    """Тот же кейс, но модель не оставила пустую строку перед своим "DRAFT 2" -
    самый жёсткий вариант "слипания"."""
    raw = (
        _draft_block("digest", "First body.", "none used", _ITEM_1_URL)
        + "\nDRAFT 2\n"
        + _draft_block("A", "Second body.", "none used", _ITEM_2_URL)
    )
    out, _ = verify.verify_drafts(raw, selected=[], data_text="")
    assert "DRAFT 2DRAFT 2" not in out
    assert out.count("DRAFT 2") == 1


def test_strip_model_header_leaves_non_header_content_alone():
    assert verify._strip_model_header("\n\n---\n\n") == "\n\n---"
    assert verify._strip_model_header("\n\nDRAFT 3\n") == ""
    assert verify._strip_model_header("") == ""
