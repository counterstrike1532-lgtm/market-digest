"""verify.py: поиск чисел в тексте и сборка отчёта ЦИФРЫ (T9a, T9b)."""
from __future__ import annotations

from src import charts, verify


# ---------------------------------------------------------------- _to_search_variants

def test_search_variants_polish_decimal_comma():
    assert set(verify._to_search_variants("31,0")) == {"31.0", "31,0"}
    assert set(verify._to_search_variants("31.0")) == {"31.0", "31,0"}


def test_search_variants_thousand_separator_both_forms():
    # T10c req 2 добавил польские формы разрядов (пробел/НБСП) поверх этих
    # двух базовых - assertOf на подмножество, не на точное совпадение списка
    assert set(verify._to_search_variants("6,872")) >= {"6,872", "6872"}
    # источник без запятой-разделителя тысяч — вариант без запятой это уже сам value
    assert "6872" in verify._to_search_variants("6872")


def test_search_variants_dollar_billion():
    # T9 fix 3: словом-порядком, с пробелом - "200 billion", как источник и пишет,
    # не слитно "200billion" (раньше именно так, substring-поиск не находил ничего)
    assert verify._to_search_variants("$200 billion") == ["200 billion"]


def test_search_variants_pln_thousand():
    assert set(verify._to_search_variants("100,000 PLN")) >= {"100,000 PLN", "100000 PLN"}


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


def test_search_variants_thousand_separator_with_decimal_polish_form():
    """Боевой прогон 01.08.2026: "15,019.5 thousand" помечено NOT_FOUND, хотя
    в источнике то же число польской записью - "15019,5" (запятая = десятичный
    разделитель, разряды слитно, без разделителя). Раньше для комбинации
    "разделитель тысяч + десятичная точка" польский вариант не генерировался
    вовсе - только английская форма (с запятой-тысяч и без неё)."""
    assert set(verify._to_search_variants("15,019.5 thousand")) >= \
        {"15,019.5 thousand", "15019.5 thousand", "15019,5 thousand"}

    pairs = [("15,019.5 thousand", "GUS")]
    body = "Population declined by 15019,5 thousand people over the year, GUS said."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "FOUND"


# ---------------------------------------------------------------- уровень b: пейлоад для LLM

def test_sentence_containing_keeps_full_decimal_value():
    """Боевой прогон 01.08.2026: ответы модели вида "The rate was 4.60%, not 4"
    во всех прогонах - признак того, что в пейлоад уровня b уходит обрубленное
    число. Причина: поиск конца предложения начинался с начала needle, а первая
    "." в "4.60" - это её собственная десятичная точка, не конец предложения.
    Урезанный до "...4." текст уходил в LLM вместо целой фразы с "4.60%"."""
    text = "The central bank said the rate was 4.60%, unchanged from last quarter."
    variants = verify._to_search_variants("4.60%")
    sentence = verify._sentence_containing(text, variants)
    assert "4.60%" in sentence
    assert sentence == text

    # тот же класс бага на других значениях из боевого прогона
    text2 = "GDP fell by 19.9% year over year, the statistics office reported."
    assert "19.9%" in verify._sentence_containing(text2, verify._to_search_variants("19.9%"))

    text3 = "Inflation rose 0.2% in July compared with the prior month."
    assert "0.2%" in verify._sentence_containing(text3, verify._to_search_variants("0.2%"))


def test_verify_drafts_level_b_payload_preserves_full_figures_value():
    """Пейлоад уровня b строится из draft_sentence/source_fragment, извлечённых
    посимвольным поиском по value из FIGURES - value само по себе не искажается
    промежуточной нормализацией нигде на этом пути. Проверяем на моке brain._call,
    что фраза, отправленная в LLM, содержит исходное значение "4.60%" целиком,
    а не обрезанное "4"."""
    from unittest.mock import patch
    from types import SimpleNamespace

    body = "The central bank said the rate was 4.60%, unchanged from last quarter."
    draft = _draft_block("digest", "Central bank held rates at 4.60% again.",
                         "4.60% -> NBP", _ITEM_1_URL, verdict="POST")
    selected = [{"item": SimpleNamespace(url=_ITEM_1_URL), "body": body}]

    captured = {}

    def fake_call(prompt, **kwargs):
        captured["prompt"] = prompt
        return '[{"id": 0, "verdict": "MATCH", "why": "ok"}]'

    with patch("src.brain._call", side_effect=fake_call):
        verify.verify_drafts(draft, selected=selected, data_text="")

    assert "prompt" in captured, "уровень b не был вызван - нечего проверять"
    draft_line = next(l for l in captured["prompt"].splitlines() if l.startswith("draft:"))
    assert "4.60%" in draft_line
    assert 'draft: "Central bank held rates at 4.60%' in draft_line


def test_verify_drafts_skips_level_b_candidate_when_value_absent_from_draft_body():
    """T10c req 6: если значения из FIGURES нет дословно в BODY черновика,
    _sentence_containing раньше тихо откатывалась на первые 200 символов -
    случайное предложение уходило в LLM как "то самое" и получало MISMATCH не
    по делу (боевой прогон: претензия про год приклеилась к "0.50%" - пара
    "предложение черновика / фрагмент источника" разъехалась). Теперь такой
    кандидат просто не строится - лучше не проверить уровнем b, чем проверить
    не то."""
    from unittest.mock import patch
    from types import SimpleNamespace

    # источник (combined_body) содержит и число, и польскую цитату - matched_in
    # будет "body". Но BODY черновика ссылается на цифру ОКРУГЛЁННО ("half a
    # percent"), не дословно "0.50%" - _sentence_containing не нашла бы её.
    source_body = "Bank obnizyl oprocentowanie do 0,50 proc. od sierpnia."
    draft = _draft_block(
        "digest", "The bank quietly cut its savings rate by about half a percent.",
        '0.50% -> Story [1] source text ("0,50 proc.")', _ITEM_1_URL, verdict="POST")
    selected = [{"item": SimpleNamespace(url=_ITEM_1_URL), "body": source_body}]

    captured = {}

    def fake_call(prompt, **kwargs):
        captured["prompt"] = prompt
        return '[{"id": 0, "verdict": "MATCH", "why": "ok"}]'

    with patch("src.brain._call", side_effect=fake_call):
        verify.verify_drafts(draft, selected=selected, data_text="")

    assert "prompt" not in captured, (
        "уровень b не должен вызываться - значение не найдено дословно в BODY, "
        "пара 'предложение черновика / фрагмент источника' была бы разъехавшейся")


def test_verify_drafts_level_b_fragment_uses_quote_when_only_quote_matches():
    """Значение найдено в статье ТОЛЬКО через цитату из FIGURES (числовая форма
    "406,000" в польском тексте не встречается вовсе, только "406 tys.") -
    fragment_around для уровня b раньше искала теми же вариантами БЕЗ цитаты и
    ничего не находила, кандидат тихо терялся. Теперь цитата используется и
    здесь тоже, кандидат строится (T10c)."""
    from unittest.mock import patch
    from types import SimpleNamespace

    source_body = "W 2025 roku zmarlo 406 tys. osob w calej Polsce, podal GUS."
    draft = _draft_block(
        "digest", "In 2025, Poland recorded 406,000 deaths, GUS reported.",
        '406,000 -> Story [1] source text ("406 tys.")', _ITEM_1_URL, verdict="POST")
    selected = [{"item": SimpleNamespace(url=_ITEM_1_URL), "body": source_body}]

    captured = {}

    def fake_call(prompt, **kwargs):
        captured["prompt"] = prompt
        return '[{"id": 0, "verdict": "MATCH", "why": "ok"}]'

    with patch("src.brain._call", side_effect=fake_call):
        verify.verify_drafts(draft, selected=selected, data_text="")

    assert "prompt" in captured, "кандидат должен был построиться через цитату"
    assert "406 tys." in captured["prompt"]


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


# ---------------------------------------------------------------- T13a: parse_drafts (сырой фолбэк)
#
# parse_drafts() сама по себе долгое время была мёртвым кодом (ноль вызовов) -
# T13a завела ей первого настоящего вызывающего: main.py использует её на
# аварийном пути (verify.verify_drafts() падает целиком), чтобы всё равно
# разобрать черновики на поля и отправить их без верификации чисел. Раз этот
# путь несущий на аварии, ему нужно прямое покрытие, не только транзитивное
# через один main()-тест.

def test_parse_drafts_returns_bare_fields_for_two_blocks():
    raw = (
        _draft_block("digest", "First body.", "none used", _ITEM_1_URL, verdict="SKIP")
        + "\n\n---\n\n"
        + _draft_block("A", "Second body.", "1 -> x", _ITEM_2_URL, verdict="POST")
    )
    blocks = verify.parse_drafts(raw)
    assert len(blocks) == 2
    assert blocks[0]["shape"] == "digest"
    assert blocks[0]["body"] == "First body."
    assert blocks[0]["figures"] == "none used"
    assert blocks[0]["verdict"] == "SKIP"
    assert blocks[1]["shape"] == "A"
    assert blocks[1]["figures"] == "1 -> x"
    assert blocks[1]["verdict"] == "POST"
    # сырой парсинг формата, никакой верификации в этих полях нет
    assert "_level_a" not in blocks[0]
    assert "_report" not in blocks[0]


def test_parse_drafts_drops_truncated_block_missing_trailing_fields():
    """Модель/сеть оборвались посреди второго черновика - блок без
    VERDICT/WHY/CHECK_FIRST не матчится форматом целиком и просто выпадает,
    первый черновик разбирается независимо от второго."""
    raw = (
        _draft_block("digest", "First body.", "none used", _ITEM_1_URL, verdict="SKIP")
        + "\n\nSHAPE: A\nBODY: Second body, cut off here"
    )
    blocks = verify.parse_drafts(raw)
    assert len(blocks) == 1
    assert blocks[0]["shape"] == "digest"


def test_parse_drafts_drops_block_missing_figures_field_entirely():
    """FIGURES выпало из формата целиком (не пустая строка - отсутствующая
    строка). Весь блок не матчится regex'ом и не всплывает как наполовину
    валидный - лучше честный ноль черновиков, чем блок с оторванными полями."""
    raw = (
        "SHAPE: digest\n"
        "BODY: Body without a figures line at all.\n"
        "SOURCE: https://example.com/story1\n"
        "WHY_THIS_ONE: reason\n"
        "VERDICT: SKIP\n"
        "WHY: no edge\n"
        "CHECK_FIRST: -"
    )
    assert verify.parse_drafts(raw) == []


def test_parse_drafts_ignores_leading_model_header_noise():
    """Модель напечатала свой заголовок "DRAFT 1" перед SHAPE: - finditer не
    заботится о том, что стоит до начала SHAPE:, блок разбирается как обычно."""
    raw = "DRAFT 1\n" + _draft_block("digest", "Body.", "none used", _ITEM_1_URL)
    blocks = verify.parse_drafts(raw)
    assert len(blocks) == 1
    assert blocks[0]["body"] == "Body."


def test_parse_drafts_drops_block_with_empty_body():
    raw = _draft_block("digest", "", "none used", _ITEM_1_URL)
    assert verify.parse_drafts(raw) == []


def test_verify_drafts_inserts_headers_across_dashes_separator():
    """Модель часто разделяет черновики строкой "---" и не печатает свой
    заголовок "DRAFT n" - рендерер должен подписать оба блока и не потерять
    разделитель между ними."""
    raw = (
        _draft_block("digest", "First body.", "none used", _ITEM_1_URL)
        + "\n\n---\n\n"
        + _draft_block("A", "Second body.", "none used", _ITEM_2_URL)
    )
    out, stats, _ = verify.verify_drafts(raw, selected=[], data_text="")

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
    out, stats, _ = verify.verify_drafts(raw, selected=[], data_text="")
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


# ---------------------------------------------------------------- T10c: цитата, знаменатель, граница
#
# Боевой прогон 01.08.2026: FIGURES из девяти пар давало "1/6 found", хотя все
# числа реально есть в источнике - модель сама указала их польскую форму в
# скобках ("406 tys.", "0,50 proc." и т.д.), верификатор искал только
# английскую. Причины по коду (не по гипотезе): (1) поиск не использовал
# цитату из FIGURES вообще - только реконструкцию английских форм; (2) знамен-
# атель занижался, потому что charts.parse_figures молча ронял диапазоны
# ("80,000 to 120,000") и даты словом-месяцем ("August 1, 2026") ещё на этапе
# регулярки, до того как verify.py вообще их увидел.

def test_quote_from_figures_found_first_polish_thousand_word():
    pairs = [("406,000", 'Story [2] source text ("406 tys.")')]
    body = "W 2025 roku zmarlo 406 tys. osob w calej Polsce."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "FOUND"


def test_quote_from_figures_found_percent_polish_form():
    pairs = [("0.50%", 'Story [3] source text ("0,50 proc.")')]
    body = "Bank obnizyl oprocentowanie konta do 0,50 proc. od sierpnia."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "FOUND"


def test_quote_from_figures_found_decimal_unit():
    pairs = [("213.5 TWh", 'Story [5] source text ("213,5 TWh")')]
    body = "Import gazu do Polski osiagnal 213,5 TWh w 2025 roku."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "FOUND"


def test_quote_from_figures_found_percent_proc():
    pairs = [("23%", 'Story [5] source text ("23 proc.")')]
    body = "Import gazu wzrosl o 23 proc. w porownaniu z rokiem poprzednim."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "FOUND"


def test_denominator_counts_all_nine_figures_pairs_not_dropped_ones():
    """Девять пар FIGURES из настоящего DRAFT 1 (боевой прогон 01.08.2026) -
    знаменатель обязан быть 9 минус год ("2025"), то есть 8, а не 6. Раньше
    charts.parse_figures молча ронял диапазон ("80,000 to 120,000") и дату
    словом-месяцем ("August 1, 2026") на этапе регулярки - они не долетали
    даже до verify.py, и знаменатель занижался без следа (T10c req 3)."""
    figures_text = (
        '- 2025 -> Story [2] and [5] source text\n'
        '- 406,000 -> Story [2] source text ("406 tys.")\n'
        '- 134,000 -> Story [2] source text ("134 tys.")\n'
        '- 80,000 to 120,000 -> Story [2] source text ("80-120 tys.")\n'
        '- 0.50% -> Story [3] source text ("0,50 proc.")\n'
        '- August 1, 2026 -> Story [3] source text ("1 sierpnia 2026 r.")\n'
        '- 23% -> Story [5] source text ("23 proc.")\n'
        '- 213.5 TWh -> Story [5] source text ("213,5 TWh")\n'
        '- 42.9% -> Story [5] source text ("42,9 proc.")'
    )
    pairs = charts.parse_figures(figures_text)
    assert len(pairs) == 9              # ни одна пара не потерялась в парсинге

    level_a = verify.verify_figures_local(pairs, bodies=[], data_text="")
    countable = [r for r in level_a if r["status"] != "YEAR"]
    assert len(countable) == 8          # 9 пар минус 1 год, не минус диапазон/дату тоже


def test_sentence_start_boundary_symmetric_with_end():
    """T9 починил конец границы предложения (десятичная точка САМОГО needle не
    обрывает поиск), начало оставалось кривым: text.rfind(".", 0, idx) цеплялся
    за точку ПРЕДЫДУЩЕГО числа в том же тексте. "GDP grew 3.5 percent and
    inflation hit 4.60%." при поиске "4.60%" раньше давало фрагмент "5 percent
    and inflation hit 4.60%." - обрывок с середины предыдущего числа, а не
    начало предложения (T10c req 4)."""
    text = "GDP grew 3.5 percent and inflation hit 4.60%."
    sentence = verify._sentence_containing(text, verify._to_search_variants("4.60%"))
    assert sentence == text
    assert not sentence.startswith("5 percent")


def test_closest_not_shown_when_no_similar_magnitude():
    """Число не в источнике и ничего похожего по порядку величины рядом -
    NOT_FOUND без строки "closest": предлагать 2026 как "ближайшее" к 406000 -
    шум, не подсказка (T10c req 5)."""
    pairs = [("406,000", "Story [2] source text")]
    body = "The report was published in 2026 and covers the prior fiscal year."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "NOT_FOUND"
    assert level_a[0].get("closest") is None

    report, _, _ = verify._render(level_a, {})
    assert "closest" not in report


def test_closest_still_shown_when_same_order_of_magnitude():
    """Гейт по порядку величины не должен полностью убить полезные подсказки -
    число того же порядка (в пределах x10) всё ещё предлагается."""
    pairs = [("406,000", "Story [2] source text")]
    body = "The report counted 410,000 people in the same category."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "NOT_FOUND"
    assert level_a[0]["closest"] == "410,000"


# ---------------------------------------------------------------- T11b: единицы измерения
#
# Живой прогон 02.08.2026: 0/8 FOUND во всех трёх черновиках, все сюжеты
# польские. _to_search_variants переводит ФОРМАТ числа, но не слово-единицу
# рядом с ним - "4.1 million tons" никогда не совпадёт с "4,1 mln ton"
# посимвольно. Пять значений ниже - реальные пары из этого прогона.

def test_unit_synonyms_tons_and_pln_and_hectares_real_run_values():
    pairs = [
        ("4.1 million tons", "Story [1] source text"),
        ("2.8 million tons", "Story [1] source text"),
        ("3.2 million tons", "Story [1] source text"),
        ("37 million zl", "Story [2] source text"),
        ("93,000 hectares", "Story [3] source text"),
    ]
    body = (
        "Firma wyprodukowala 4,1 mln ton stali w 2025 roku, wobec 2,8 mln ton "
        "rok wczesniej i 3,2 mln ton dwa lata wczesniej. Inwestycja pochlonela "
        "37 mln zl, a pod uprawy przeznaczono 93 tys. ha gruntow ornych."
    )
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    statuses = {r["value"]: r["status"] for r in level_a}
    assert statuses["4.1 million tons"] == "FOUND"
    assert statuses["2.8 million tons"] == "FOUND"
    assert statuses["3.2 million tons"] == "FOUND"
    assert statuses["37 million zl"] == "FOUND"
    assert statuses["93,000 hectares"] == "FOUND"


def test_unit_mismatch_core_found_unit_absent_stays_not_found():
    """Планка не ослаблена: числовое ядро совпало, но единицы рядом нет -
    статус остаётся NOT_FOUND, не FOUND (T11b req 3)."""
    pairs = [("4.1 million tons", "Story [1] source text")]
    body = "Firma odnotowala wynik 4,1 w nowym rankingu branzowym."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "NOT_FOUND"


def test_unit_match_must_be_in_same_sentence_not_anywhere_in_body():
    """Единица есть в тексте, но в другом предложении - окно ограничено
    предложением с числом, не всем телом статьи."""
    pairs = [("4.1 million tons", "Story [1] source text")]
    body = ("Firma odnotowala wynik 4,1 w nowym rankingu branzowym. "
            "Osobno, konkurencja sprzedala w tym roku 2 mln ton stali.")
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "NOT_FOUND"


def test_unit_synonyms_unrecognized_suffix_never_forces_found():
    """Хвост value не попал ни в одно слово таблицы синонимов - единица не
    подтверждена никогда, даже если числовое ядро совпало в тексте."""
    pairs = [("4.1 whatsits", "Story [1] source text")]
    body = "W 2025 roku odnotowano wynik 4,1 w nowej kategorii nazwanej inaczej."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "NOT_FOUND"
    assert verify._unit_groups("4.1 whatsits") == []


def test_unit_groups_recognizes_known_suffix_words():
    assert verify._unit_groups("4.1 million tons") == [["mln"],
        ["ton", "tony", "tona", "tys. ton", "mln ton"]]
    assert verify._unit_groups("23%") == [["proc.", "%"]]
    assert verify._unit_groups("93,000 hectares") == \
        [["ha", "hektar", "hektara", "hektarów", "hektarow"]]


def test_not_found_carries_search_window_for_digest_log():
    """T11b, шаг перед реализацией: окно вокруг ближайшего кандидата уходит в
    digest.log (через _render -> build_message), не догадка о причине
    NOT_FOUND, а реальный фрагмент текста источника."""
    pairs = [("406,000", "Story [2] source text")]
    body = "The report counted 410,000 people in the same category, officials said."
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    assert level_a[0]["status"] == "NOT_FOUND"
    assert "410,000" in level_a[0]["search_window"]

    report, _, _ = verify._render(level_a, {})
    assert "window" in report
    assert "410,000" in report


def test_real_run_regression_draft1_finds_at_least_8_of_9():
    """Регрессия целиком: FIGURES и реалистичный текст источника (польские формы
    цифр, как их реально пишут bankier.pl/GUS) черновика 1 прогона 01.08.2026
    дают минимум 8 FOUND из 9 пар (T10c acceptance)."""
    figures_text = (
        '- 2025 -> Story [2] and [5] source text\n'
        '- 406,000 -> Story [2] source text ("406 tys.")\n'
        '- 134,000 -> Story [2] source text ("134 tys.")\n'
        '- 80,000 to 120,000 -> Story [2] source text ("80-120 tys.")\n'
        '- 0.50% -> Story [3] source text ("0,50 proc.")\n'
        '- August 1, 2026 -> Story [3] source text ("1 sierpnia 2026 r.")\n'
        '- 23% -> Story [5] source text ("23 proc.")\n'
        '- 213.5 TWh -> Story [5] source text ("213,5 TWh")\n'
        '- 42.9% -> Story [5] source text ("42,9 proc.")'
    )
    pairs = charts.parse_figures(figures_text)
    body = (
        "W 2025 roku zmarlo 406 tys. osob, a deweloperzy oddali do uzytku tylko "
        "134 tys. mieszkan. Nawet 80-120 tys. mieszkan moze trafic na rynek wtorny "
        "rocznie. Bank Millennium obnizyl oprocentowanie standardowego konta do "
        "0,50 proc. od 1 sierpnia 2026 r. Import gazu do Polski wzrosl o 23 proc. "
        "w 2025 roku, do 213,5 TWh, z czego LNG stanowilo 42,9 proc."
    )
    level_a = verify.verify_figures_local(pairs, bodies=[body], data_text="")
    found = sum(1 for r in level_a if r["status"] == "FOUND")
    assert found >= 8


# ---------------------------------------------------------------- T11f: тела по номеру сюжета
#
# Живой прогон 02.08.2026: bodies_for_source() сопоставляла сырое SOURCE
# черновика с selected[i]["item"].url подстрочно в обе стороны. Опечатка модели
# в одну букву ("na-ryku" вместо "na-rynku") дала False в обе стороны - тело
# статьи не находилось, хотя оно было полностью загружено, и черновик получал
# "source text not fetched" незаслуженно. Верификатор существует, чтобы ловить
# ошибки модели - доверять ей же выбор текста для проверки было неправильно.

def _ns_selected(bodies: dict[int, str], urls: dict[int, str] | None = None) -> list[dict]:
    """selected[i] с телом bodies[i+1] и (опционально) своим URL - индексация с 1,
    как везде в FIGURES ("Story [N]")."""
    from types import SimpleNamespace
    urls = urls or {}
    n = max(bodies)
    return [{"item": SimpleNamespace(url=urls.get(i, f"https://example.com/story{i}")),
            "body": bodies.get(i, "")}
           for i in range(1, n + 1)]


def test_bodies_for_source_finds_body_by_number_despite_source_url_typo():
    """Реальный кейс прогона 02.08: FIGURES ссылается на сюжет 2, SOURCE-поле
    черновика содержит опечатку в URL той же статьи - тело всё равно находится,
    потому что берётся по номеру, не по строке SOURCE."""
    selected = _ns_selected(
        {1: "story one body", 2: "story two body, the real one"},
        urls={2: "https://www.bankier.pl/wiadomosc/na-rynku-walutowym-x.html"})
    typoed_source = "https://www.bankier.pl/wiadomosc/na-ryku-walutowym-x.html"  # без "n"
    figures = '58.97 billion dollars -> Story [2] source text ("58,97 mld dolarow")'

    bodies = verify.bodies_for_source(typoed_source, selected, figures)
    assert bodies == ["story two body, the real one"]


def test_bodies_for_source_falls_back_to_string_match_when_figures_has_no_numbers():
    """FIGURES без ссылок на номер сюжета (например "none used" или числа из
    FRESH DATA) - поведение идентично тому, что было до T11f: сопоставление по
    URL из поля SOURCE."""
    selected = _ns_selected({1: "story one body"}, urls={1: "https://example.com/story1"})
    bodies = verify.bodies_for_source("https://example.com/story1", selected, "none used")
    assert bodies == ["story one body"]


def test_bodies_for_source_out_of_range_number_falls_back_not_raises():
    """Номер из FIGURES не попадает в диапазон selected - фолбэк на строковое
    сопоставление, не исключение."""
    selected = _ns_selected({1: "story one body"}, urls={1: "https://example.com/story1"})
    figures = '999 -> Story [9] source text'   # 9 вне диапазона (selected короче)
    bodies = verify.bodies_for_source("https://example.com/story1", selected, figures)
    assert bodies == ["story one body"]


def test_bodies_for_source_number_match_excludes_other_stories_body():
    """FIGURES ссылается только на сюжет 2 - тело сюжета 3 в bodies не попадает,
    даже если SOURCE-поле (текст модели) технически упоминает и его URL тоже.
    Не объединяем номера со строковым совпадением: иначе число из статьи 3
    "нашлось" бы в статье 2 - ложный FOUND, которого избегали в T11b."""
    selected = _ns_selected(
        {2: "story two body", 3: "story three body"},
        urls={2: "https://example.com/story2", 3: "https://example.com/story3"})
    figures = '406,000 -> Story [2] source text'
    source_field = "https://example.com/story2, https://example.com/story3"

    bodies = verify.bodies_for_source(source_field, selected, figures)
    assert bodies == ["story two body"]
    assert "story three body" not in bodies


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

    _, stats, _blocks = verify.verify_drafts(raw, selected=selected, data_text=data_text)

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
    out, stats, _ = verify.verify_drafts(draft, selected=[], data_text="")
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
    out, _, _blocks = verify.verify_drafts(raw, selected=[], data_text="")
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
    out, _, _blocks = verify.verify_drafts(raw, selected=[], data_text="")
    assert "DRAFT 2DRAFT 2" not in out
    assert out.count("DRAFT 2") == 1


def test_strip_model_header_leaves_non_header_content_alone():
    assert verify._strip_model_header("\n\n---\n\n") == "\n\n---"
    assert verify._strip_model_header("\n\nDRAFT 3\n") == ""
    assert verify._strip_model_header("") == ""
