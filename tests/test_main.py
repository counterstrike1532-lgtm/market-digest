"""main.filter_by_age: свежая / старая / без даты (T9a). Без сети и без файлов -
элементы строятся напрямую как collect.Item."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from src import brain, collect, deliver, enrich, main, numbers, verify
from src.collect import Item
from src.main import (build_domain_urls, build_message, draft_card, filter_by_age,
                      found_figure_pairs, render_draft_message,
                      render_summary, _fix_glued_punctuation, _word_count)


def _item(age_days=None, published_known=True):
    if age_days is None:
        published = datetime.now(timezone.utc).isoformat()
    else:
        published = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return Item(title="t", url="https://example.com/x", source="x", tag="misc",
               published=published, published_known=published_known)


def test_filter_by_age_keeps_fresh_item():
    items = [_item(age_days=0.5)]
    assert filter_by_age(items, max_age_days=7) == items


def test_filter_by_age_drops_30_day_old_item():
    items = [_item(age_days=30)]
    assert filter_by_age(items, max_age_days=7) == []


def test_filter_by_age_keeps_boundary_item():
    items = [_item(age_days=6)]
    assert filter_by_age(items, max_age_days=7) == items


def test_filter_by_age_keeps_unknown_date_item_regardless_of_age():
    """published - заглушка "сейчас" при published_known=False, возраст не проверяем."""
    old_but_unknown = _item(age_days=400, published_known=False)
    assert filter_by_age([old_but_unknown], max_age_days=7) == [old_but_unknown]


def test_filter_by_age_mixed_batch():
    fresh = _item(age_days=1)
    stale = _item(age_days=30)
    unknown = _item(age_days=999, published_known=False)
    result = filter_by_age([fresh, stale, unknown], max_age_days=7)
    assert result == [fresh, unknown]


# ---------------------------------------------------------------- T9 fix 5: слипание строк

def test_fix_glued_punctuation_bullet_glued_to_prior_sentence():
    text = "I looked at energy transitions.- An AI-focused fund is doing X."
    fixed = _fix_glued_punctuation(text)
    assert "transitions.\n- An AI-focused" in fixed
    assert ".- An" not in fixed


def test_fix_glued_punctuation_missing_space_between_sentences():
    text = "This is forcing more selling.This feedback loop keeps going."
    fixed = _fix_glued_punctuation(text)
    assert "selling. This feedback loop" in fixed
    assert ".This" not in fixed


def test_fix_glued_punctuation_leaves_decimals_alone():
    text = "The company raised $3.5 billion in the round, up 12.5% year over year."
    assert _fix_glued_punctuation(text) == text


def test_fix_glued_punctuation_leaves_well_formed_text_alone():
    text = "First sentence. Second sentence.\n- A bullet point.\n- Another one."
    assert _fix_glued_punctuation(text) == text


def test_fix_glued_punctuation_leaves_negative_number_after_period_alone():
    """Дефис как минус после точки - не буллет, следующий символ цифра, не
    заглавная буква: не трогаем."""
    text = "Profit fell.-5% for the quarter."
    assert _fix_glued_punctuation(text) == text


# ---------------------------------------------------------------- T9 fix 2: уровень WIG20 TR

def test_build_message_hides_level_for_wig20_tr_etf():
    """Цена пая ETFBW20TR.WA (~80) - не уровень индекса WIG20 (~2500+). В ЦИФРЫ
    печатаем только проценты изменения, голое value не выводим (T9 fix 2)."""
    data = {
        "WIG20 TR (ETF)": {"value": 80.13, "chg_1d_pct": 0.46, "chg_1m_pct": 10.16,
                           "as_of": "2026-07-31"},
        "sp500": {"value": 7437.63, "chg_1d_pct": 1.66, "as_of": "2026-07-30"},
    }
    msg = build_message(selected=[], data=data, drafts="")
    lines = msg.splitlines()
    wig_line = next(l for l in lines if l.strip().startswith("WIG20 TR (ETF):"))
    sp_line = next(l for l in lines if l.strip().startswith("sp500:"))
    assert "80.13" not in wig_line
    assert "+0.46%" in wig_line and "+10.16%" in wig_line
    # sp500 - настоящий уровень индекса, печатать его как есть можно и нужно
    assert "7437.63" in sp_line


# ---------------------------------------------------------------- T10b: URL после дедупа
#
# Боевой прогон 01.08.2026: у сюжетов 1/2/3 первая ссылка в СЮЖЕТЫ вела на
# сюжет 5 (общий газовый материал), вторая - верно. Гипотеза владельца была
# "дедуп по заголовку смешивает URL" - проверка по коду (tests/test_collect.py)
# это не подтвердила: dedupe_by_title не трогает поля выживших записей.
# Настоящая причина: build_domain_urls (тогда - плоский словарь прямо в
# main()) при нескольких отобранных сюжетах с одного домена (bankier.pl,
# сюжеты 1/2/3/5 - все оттуда) молча оставлял URL того сюжета, что шёл в
# selected последним, и deliver._wrap_bare_domains подставляла этот URL
# ВЕЗДЕ, где в сообщении встречался голый домен - включая метку it.source в
# самом списке СЮЖЕТЫ, к SOURCE черновиков не относящуюся вовсе.

def test_build_domain_urls_excludes_ambiguous_domain():
    """Несколько сюжетов с одного домена - разных URL не выбрать однозначно,
    домен вообще не попадает в карту (лучше не ссылка, чем ссылка не туда)."""
    a = Item(title="a", url="https://bankier.pl/story-a", source="bankier.pl",
            tag="misc", published=datetime.now(timezone.utc).isoformat())
    b = Item(title="b", url="https://bankier.pl/story-b", source="bankier.pl",
            tag="misc", published=datetime.now(timezone.utc).isoformat())
    selected = [{"item": a}, {"item": b}]
    assert build_domain_urls(selected) == {}


def test_build_domain_urls_keeps_unambiguous_domain():
    a = Item(title="a", url="https://money.pl/story-a", source="money.pl",
            tag="misc", published=datetime.now(timezone.utc).isoformat())
    selected = [{"item": a}]
    assert build_domain_urls(selected) == {"money.pl": "https://money.pl/story-a"}


def test_build_domain_urls_real_run_regression():
    """Регрессия на реальные шесть сюжетов прогона 01.08.2026 (bankier.pl - у
    четырёх из шести, money.pl и dowjones.io - по одному): bankier.pl
    неоднозначен и не попадает в карту вовсе; money.pl - единственный сюжет
    с этого домена, попадает корректно."""
    now = datetime.now(timezone.utc).isoformat()
    urls = {
        1: "https://www.bankier.pl/wiadomosc/Dunaj-wysycha-elektrownia-jadrowa-staje-Wegry-wylacza-Paks-w-weekend-9176727.html",
        2: "https://www.bankier.pl/wiadomosc/Wielka-plyta-zagrozi-deweloperom-Tysiace-odziedziczonych-mieszkan-trafia-na-sprzedaz-9176697.html",
        3: "https://www.bankier.pl/wiadomosc/Kolejna-obnizka-oprocentowania-w-Banku-Millennium-Tym-razem-dotyczy-stawki-standardowej-9176304.html",
        4: "https://www.money.pl/finanse/mezczyzni-doplacaja-do-emerytur-kobiet-o-tym-sie-nie-mowi-opinia-7312364596259200a.html",
        5: "https://www.bankier.pl/wiadomosc/Polska-sprowadza-coraz-wiecej-gazu-Dania-i-USA-na-czele-dostawcow-9176695.html",
    }
    sources = {1: "www.bankier.pl", 2: "www.bankier.pl", 3: "www.bankier.pl",
              4: "www.money.pl", 5: "www.bankier.pl"}
    selected = [{"item": Item(title=f"story {n}", url=u, source=sources[n], tag="misc",
                              published=now)}
               for n, u in urls.items()]

    domain_urls = build_domain_urls(selected)
    assert "www.bankier.pl" not in domain_urls          # неоднозначен - не угадываем
    assert domain_urls["www.money.pl"] == urls[4]

    # рендер строки СЮЖЕТЫ для каждого сюжета: их собственный URL (напечатанный
    # явно после "-") остаётся верным. Для bankier.pl (неоднозначен) голая
    # метка домена вообще не превращается в ссылку - только явный URL. Для
    # money.pl (единственный сюжет с домена) метка тоже верно ссылается на
    # СВОЙ же URL - никакого другого URL с этого домена и не было.
    for n, u in urls.items():
        line = f"   {sources[n]}, 2026-08-01 - {u}"
        out = deliver._to_html(line, domain_urls)
        # каждая ссылка в отрендеренной строке ведёт на URL ЭТОГО сюжета,
        # ни на чей чужой (главная регрессия: сюжет 5 не просочился в 1/2/3)
        for m in re.finditer(r'href="([^"]*)"', out):
            assert m.group(1) == u


# ---------------------------------------------------------------- T10b req 4: utm-хвосты

def test_to_html_strips_utm_params_from_rendered_url():
    url = ("https://www.bankier.pl/wiadomosc/Dunaj-wysycha-9176727.html"
          "?utm_source=RSS&utm_medium=RSS&utm_campaign=Wiadomosci")
    out = deliver._to_html(url)
    assert "utm_source" not in out
    assert "utm_medium" not in out
    assert "utm_campaign" not in out
    assert 'href="https://www.bankier.pl/wiadomosc/Dunaj-wysycha-9176727.html"' in out


def test_to_html_strips_utm_but_keeps_other_query_params():
    url = "https://news.google.com/rss/articles/x?oc=5&utm_source=RSS&hl=en-US"
    out = deliver._to_html(url)
    assert "utm_source" not in out
    assert "oc=5" in out and "hl=en-US" in out


# ---------------------------------------------------------------- T10d: рендер сводки и черновиков

def _story(url="https://www.bankier.pl/x", source="www.bankier.pl", title="Test story",
          score=8, angle="An angle worth reading.", verified=True, why_nonobvious="obvious thing"):
    it = Item(title=title, url=url, source=source, tag="misc",
             published=datetime.now(timezone.utc).isoformat())
    return {"item": it, "score": score, "angle": angle, "why_nonobvious": why_nonobvious,
           "verified": verified}


def _draft_block_dict(shape="digest", body="Body text with a few words in it today.",
                      verdict="POST", figures="", source="https://example.com/a",
                      check_first="-", downgrade=False, offending=None, level_a=None):
    return {
        "shape": shape, "body": body, "verdict": verdict, "figures": figures,
        "source": source, "check_first": check_first, "_downgrade": downgrade,
        "_offending": offending or [], "_level_a": level_a or [],
    }


def test_render_summary_omits_debug_fields_and_second_link():
    """T10d: в отрендеренном выводе нет FIGURES/SHAPE/WHY_THIS_ONE/неочевидно -
    даже если сюжет содержит why_nonobvious, в сводку он не попадает вовсе."""
    selected = [_story()]
    out = render_summary(selected, data={})
    for forbidden in ("FIGURES", "SHAPE", "WHY_THIS_ONE", "неочевидно"):
        assert forbidden not in out
    assert "obvious thing" not in out          # why_nonobvious не печатается


def test_render_summary_shows_domain_link_and_verified_marker():
    """T11d: маркер недогрузки больше не построчный - агрегированная строка
    в конце блока СЮЖЕТЫ, см. тесты ниже."""
    selected = [_story(verified=False)]
    out = render_summary(selected, data={})
    assert "https://www.bankier.pl/x" in out
    assert "текст не догружен" in out


def test_render_summary_market_line_replaces_nan_with_placeholder():
    """Живой кейс 04.08: "S&P500 +nan% д / +nan% мес" - пропуск дня в ряду
    yfinance/stooq (numbers.py round() на nan не падает, отдаёт nan молча).
    Оба поля не число -> тикер без "д"/"мес" вообще, просто "нет данных"."""
    data = {
        "WIG20 TR (ETF)": {"value": 80.13, "chg_1d_pct": 0.3, "chg_1m_pct": 11.6,
                           "as_of": "2026-08-04"},
        "sp500": {"value": float("nan"), "chg_1d_pct": float("nan"),
                 "chg_1m_pct": float("nan"), "as_of": "2026-08-04"},
        "nasdaq": {"value": float("nan"), "chg_1d_pct": None,
                  "chg_1m_pct": None, "as_of": "2026-08-04"},
    }
    out = render_summary([], data)
    assert "nan" not in out
    assert "S&P500 нет данных" in out
    assert "Nasdaq нет данных" in out


def test_render_summary_market_line_replaces_only_the_bad_field():
    """Одно поле не число - тикер остаётся с обеими метками, плохое поле
    получает "нет данных", хорошее печатается как обычно."""
    data = {
        "sp500": {"value": 7500.0, "chg_1d_pct": 1.8, "chg_1m_pct": float("nan"),
                 "as_of": "2026-08-04"},
    }
    out = render_summary([], data)
    assert "nan" not in out
    assert "S&P500 +1.8% д / нет данных мес" in out


def test_render_summary_cifry_at_most_three_lines():
    data = {
        "PLN/USD": {"value": 3.7425, "chg_30d_pct": 0.38, "as_of": "2026-07-31"},
        "PLN/EUR": {"value": 4.3128, "chg_30d_pct": 1.02, "as_of": "2026-07-31"},
        "WIG20 TR (ETF)": {"value": 80.13, "chg_1d_pct": 0.33, "chg_1m_pct": 10.01,
                           "as_of": "2026-07-31"},
        "sp500": {"value": 7489.72, "chg_1d_pct": 0.70, "chg_1m_pct": -1.45,
                 "as_of": "2026-07-31"},
        "HICP PL": {"value": 2.5, "as_of": "2025-12"},
    }
    out = render_summary([], data)
    cifry_block = out.split("\n\n")[1]
    assert len(cifry_block.splitlines()) <= 3


# ---------------------------------------------------------------- T11d: сжатие сводки

def _live_run_selected():
    """8 сюжетов, 4 недогруженных - фикстура по мотивам живого прогона 02.08.2026.
    Пояснение ~157 символов - реалистичная длина под лимит RANK_PROMPT (angle
    максимум 35 слов), достаточно длинная, чтобы честно проверить сжатие блока."""
    unverified = {1, 3, 7, 8}
    return [_story(title=f"Story {i}",
                   angle=(f"Story {i} reveals a mechanism that most readers miss and "
                          "changes how the surrounding numbers should be read together, "
                          "which matters more than it first appears."),
                   verified=(i not in unverified))
           for i in range(1, 9)]


def test_render_summary_explains_only_top_n_stories():
    selected = _live_run_selected()
    out = render_summary(selected, data={})
    for i in range(1, 6):
        assert f"Story {i} reveals a mechanism" in out
    for i in range(6, 9):
        assert f"Story {i} reveals a mechanism" not in out


def test_render_summary_collapses_unverified_warnings_into_one_line():
    selected = _live_run_selected()
    out = render_summary(selected, data={})
    assert out.count("текст не догружен") == 1
    assert "1, 3, 7, 8" in out


def test_render_summary_truncates_long_angle_at_sentence_boundary():
    long_angle = ("This first sentence alone is short. " +
                 "But together with this second one the whole angle runs well past one "
                 "hundred and sixty characters, which is exactly the case this rule targets.")
    assert len(long_angle) > 160
    selected = [_story(angle=long_angle)]
    out = render_summary(selected, data={})
    assert "This first sentence alone is short." in out
    assert "But together with this second one" not in out


def test_render_summary_leaves_long_single_sentence_untouched():
    long_angle = ("This is one very long sentence with no early period in it at all that "
                 "keeps running well past one hundred and sixty characters before it finally "
                 "ends right here, well beyond the limit.")
    assert len(long_angle) > 160
    selected = [_story(angle=long_angle)]
    out = render_summary(selected, data={})
    assert long_angle in out


def test_render_summary_shrinks_block_by_a_third_vs_uncompressed():
    selected = _live_run_selected()
    out = render_summary(selected, data={})
    story_block = out.split("\n\n", 1)[1]

    uncompressed_lines = [f"СЮЖЕТЫ ({len(selected)})"]
    for i, s in enumerate(selected, 1):
        it = s["item"]
        lines = [f"{i}. [{s.get('score')}] {it.title}", f"   {s['angle']}", f"   {it.url}"]
        if not s["verified"]:
            lines.append("   ! текст статьи не догружен — цифры в угле не проверены")
        uncompressed_lines.append("\n".join(lines))
    uncompressed = "\n\n".join(uncompressed_lines)

    reduction = 1 - len(story_block) / len(uncompressed)
    assert reduction >= 1 / 3


def test_word_count_counts_body_words():
    assert _word_count("one two three") == 3
    assert _word_count("") == 0


def test_render_draft_message_header_appears_exactly_once():
    block = _draft_block_dict(shape="digest", verdict="SKIP")
    out = render_draft_message(block, 1)
    assert out.count("ЧЕРНОВИК 1") == 1
    for forbidden in ("FIGURES", "SHAPE:", "WHY_THIS_ONE", "неочевидно"):
        assert forbidden not in out


def test_render_draft_message_clean_case_is_one_counter_line():
    """При всех FOUND - одна строка со счётчиком, без списка чисел (T10d)."""
    level_a = [{"value": "406,000", "source": "x", "status": "FOUND"},
              {"value": "23%", "source": "y", "status": "FOUND"}]
    block = _draft_block_dict(verdict="POST", level_a=level_a)
    out = render_draft_message(block, 1)
    assert "POST · цифры 2/2 ✅" in out
    assert "406,000" not in out
    assert "23%" not in out


def test_render_draft_message_shows_first_three_offending_values_only():
    """При трёх и более проблемных числах печатаются первые три (T10d)."""
    figures = (
        '- 100 -> Story [1] source text ("100")\n'
        '- 200 -> Story [1] source text ("200 X")\n'
        '- 300 -> Story [1] source text ("300 Y")\n'
        '- 400 -> Story [1] source text ("400 Z")'
    )
    level_a = [{"value": v, "source": s, "status": "NOT_FOUND"} for v, s in [
        ("100", 'Story [1] source text ("100")'),
        ("200", 'Story [1] source text ("200 X")'),
        ("300", 'Story [1] source text ("300 Y")'),
        ("400", 'Story [1] source text ("400 Z")'),
    ]]
    block = _draft_block_dict(verdict="POST", figures=figures, downgrade=True,
                              offending=["100", "200", "300", "400"], level_a=level_a)
    out = render_draft_message(block, 1)
    assert "MAYBE" in out                       # эффективный вердикт, не сырой POST
    assert '100 ("100")' in out
    assert '200 ("200 X")' in out
    assert '300 ("300 Y")' in out
    assert "400" not in out                     # четвёртое не печатается
    assert "+1 more" in out


def test_render_draft_message_check_first_only_when_not_dash():
    clean = render_draft_message(_draft_block_dict(check_first="-"), 1)
    assert "CHECK_FIRST" not in clean

    with_check = render_draft_message(_draft_block_dict(check_first="verify the GUS figure"), 1)
    assert "CHECK_FIRST: verify the GUS figure" in with_check


def test_render_draft_message_parse_failed_shows_manual_check_notice():
    block = _draft_block_dict()
    block["_parse_failed"] = True
    out = render_draft_message(block, 1)
    assert "проверь числа руками" in out


# ---------------------------------------------------------------- T14b: пять веток "цифры" вместо ✅ на честном слове
#
# Живой лог 03.08 08:41: "✅ verified (1/1 found)" под черновиком с четырьмя
# числами. Причина - ✅ печаталась при пустом _offending, а NO_SOURCE_TEXT в
# _offending не входит по замыслу verify.py (не вина модели). Условие галочки
# теперь строго found == denom > 0, с отдельной веткой на каждый другой исход.

def test_render_draft_message_offending_nonpaired_branch_untouched():
    """Ветка 1 (offending непуст) - существующее поведение с перечислением,
    этот шаг её не трогает вообще."""
    figures = '- 100 -> Story [1] source text ("100")'
    level_a = [{"value": "100", "source": 'Story [1] source text ("100")',
               "status": "NOT_FOUND"}]
    block = _draft_block_dict(verdict="POST", figures=figures, downgrade=True,
                              offending=["100"], level_a=level_a)
    out = render_draft_message(block, 1)
    assert "MAYBE — сверь" in out
    assert '100 ("100")' in out


def test_render_draft_message_denom_zero_is_bare_verdict():
    """Ветка 2: черновик без проверяемых чисел (level_a пуст) - просто
    вердикт, без "цифры N/M" и без ✅."""
    block = _draft_block_dict(verdict="SKIP", level_a=[], offending=[])
    out = render_draft_message(block, 1)
    assert "\nSKIP" in out
    assert "цифры" not in out
    assert "✅" not in out


def test_render_draft_message_all_found_keeps_checkmark():
    """Ветка 3: found == denom > 0 - как раньше, с ✅. Не сломать (T10d)."""
    level_a = [{"value": "406,000", "source": "x", "status": "FOUND"},
              {"value": "23%", "source": "y", "status": "FOUND"}]
    block = _draft_block_dict(verdict="POST", level_a=level_a, offending=[])
    out = render_draft_message(block, 1)
    assert "POST · цифры 2/2 ✅" in out


def test_render_draft_message_all_no_source_text_no_checkmark():
    """Ветка 4: все пары NO_SOURCE_TEXT (found == 0, offending пуст по
    конструкции verify._render - NO_SOURCE_TEXT туда не входит) - явная
    строка про недогруженный источник, без ✅, вместо ложной "0/3 ✅"."""
    level_a = [{"value": "300 million PLN", "source": "x", "status": "NO_SOURCE_TEXT"},
              {"value": "4 years", "source": "y", "status": "NO_SOURCE_TEXT"},
              {"value": "8 years", "source": "z", "status": "NO_SOURCE_TEXT"}]
    block = _draft_block_dict(verdict="POST", level_a=level_a, offending=[])
    out = render_draft_message(block, 1)
    assert "POST · цифры 0/3 — источник не догружен, сверь вручную" in out
    assert "✅" not in out


def test_render_draft_message_partial_no_source_text_distinct_line():
    """Ветка 5 (новая, 0 < found < denom, offending пуст): достижимо, когда
    FIGURES черновика ссылается и на число из FRESH DATA, и на число
    конкретного сюжета. FRESH DATA матчится в verify_figures_local через
    data_text - независимо от bodies (verify.py:424, `any(v in data_text
    ...)` стоит раньше и отдельно от проверки combined_body) - это даёт
    FOUND, даже когда тело сюжета не догрузилось вовсе. А число самого
    сюжета в этом же черновике уходит в NO_SOURCE_TEXT, потому что
    combined_body общий на весь черновик (verify.py:414), а не per-pair, и
    он пуст, если тело единственного сюжета не загрузилось. Офендинг при
    этом остаётся пустым (NO_SOURCE_TEXT в него не входит), а найдено не
    всё - строка обязана отличаться и от ✅, и от "источник не догружен,
    сверь вручную" (который держится за found == 0)."""
    level_a = [{"value": "23%", "source": "FRESH DATA", "status": "FOUND",
               "matched_in": "data"},
              {"value": "96.5 million PLN", "source": "Story [1] source text",
               "status": "NO_SOURCE_TEXT"}]
    block = _draft_block_dict(verdict="POST", level_a=level_a, offending=[])
    out = render_draft_message(block, 1)
    assert "POST · цифры 1/2 — часть чисел не сверена, источник не догружен" in out
    assert "✅" not in out
    assert "сверь вручную" not in out


# ---------------------------------------------------------------- T14a: футер - номера сюжетов
#
# Боевой прогон 03.08: под digest-черновиком из трёх буллетов
# (money.pl + bankier.pl + bankier.pl) печатался один URL www.money.pl -
# выглядел как источник всего текста, хотя два буллета из трёх пришли с
# другого домена. Номер сюжета такого класса ошибки не имеет вовсе: ссылки на
# сами сюжеты уже кликабельны в блоке СЮЖЕТЫ выше, футер только называет,
# какие именно номера черновик использовал.

def _bankier_selected(n=5):
    """N сюжетов с одного домена - домен тут не важен (футер T14a URL вообще
    не печатает), важно только количество/нумерация сюжетов."""
    return [_story(url=f"https://www.bankier.pl/story-{i}", source="www.bankier.pl",
                   title=f"story {i}")
           for i in range(1, n + 1)]


def test_render_draft_message_footer_lists_story_numbers_ascending():
    """Номера в FIGURES пришли в порядке 5, 1, 3 - в футере напечатаны по
    возрастанию, не в порядке появления."""
    selected = _bankier_selected()
    block = _draft_block_dict(
        figures='4.1 mln ton -> Story [5] source text\n'
               '2.8 mln ton -> Story [1] source text\n'
               '1.2 mln ton -> Story [3] source text')
    out = render_draft_message(block, 1, selected)
    assert "источники: сюжеты 1, 3, 5" in out


def test_render_draft_message_footer_singular_form_for_one_number():
    selected = _bankier_selected()
    block = _draft_block_dict(figures='58.97 billion dollars -> Story 2 source text')
    out = render_draft_message(block, 1, selected)
    assert "источник: сюжет 2" in out
    assert "источники" not in out


def test_render_draft_message_footer_out_of_range_number_dropped_others_kept():
    selected = _bankier_selected()
    block = _draft_block_dict(
        figures='4.1 mln ton -> Story [2] source text\n406 tys. -> Story [9] source text')
    out = render_draft_message(block, 1, selected)
    assert "источник: сюжет 2" in out


def test_render_draft_message_footer_all_numbers_out_of_range_omits_line():
    selected = _bankier_selected()
    block = _draft_block_dict(figures='406 tys. -> Story [9] source text')
    out = render_draft_message(block, 1, selected)
    assert "источник" not in out


def test_render_draft_message_footer_no_story_numbers_omits_line_entirely():
    """Приёмка T12a/T14a: FIGURES без номеров сюжетов -> в сообщении нет
    строки-футера вообще, домен не подставляется никогда."""
    selected = _bankier_selected()
    block = _draft_block_dict(figures="6,872 -> Money.pl", source="www.bankier.pl")
    out = render_draft_message(block, 1, selected)
    assert "источник" not in out
    assert "bankier.pl" not in out


def test_render_draft_message_footer_omitted_without_selected():
    """T12a: номер сюжета не разрешился (selected пуст/не передан) - строки
    футера нет, не откат на сырой текст модели в поле SOURCE."""
    block = _draft_block_dict(figures="none used", source="www.bankier.pl")
    out = render_draft_message(block, 1, selected=None)
    assert "источник" not in out
    assert "www.bankier.pl" not in out


def test_digest_log_receives_full_report_with_figures(monkeypatch, tmp_path, caplog):
    """Всё вырезанное из Telegram (FIGURES и т.д.) уходит в digest.log на уровне
    INFO - полный отчёт верификатора, не только то, что реально отправляется (T10d)."""
    seen_path = tmp_path / "seen.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(main, "SEEN", seen_path)
    monkeypatch.setattr(main, "METRICS", metrics_path)

    item = Item(title="Test story", url="https://example.com/story1",
               source="example.com", tag="misc",
               published=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(collect, "collect_all", lambda cfg, hours: [item])
    monkeypatch.setattr(numbers, "gather", lambda cfg: {})
    selected = [{"item": item, "score": 8, "angle": "x", "why_nonobvious": "x",
                "body": "", "verified": False}]
    monkeypatch.setattr(brain, "rank", lambda candidates, top_n: selected)
    monkeypatch.setattr(enrich, "enrich", lambda selected, limit: 0)
    monkeypatch.setattr(brain, "draft", lambda *a, **kw:
                        'SHAPE: digest\nBODY: text.\nFIGURES: 6,872 -> Money.pl\n'
                        'SOURCE: https://example.com/story1\nWHY_THIS_ONE: why not obvious\n'
                        'VERDICT: SKIP\nWHY: commodity news, no edge\nCHECK_FIRST: -')
    monkeypatch.setattr(brain, "last_model_used", lambda: "gemini-3.5-flash")
    monkeypatch.setattr(brain, "quota_summary",
                        lambda: {"total": 1, "successful": 1, "quota_refused": 0})
    monkeypatch.setattr("src.deliver.send", lambda *a, **kw: None)
    monkeypatch.setattr("src.deliver.send_photo", lambda *a, **kw: None)

    monkeypatch.setattr("sys.argv", ["main.py", "--no-charts"])
    import logging
    with caplog.at_level(logging.INFO, logger="digest"):
        rc = main.main()

    assert rc == 0
    full_log = "\n".join(r.message for r in caplog.records)
    assert "6,872" in full_log
    assert "FIGURES" in full_log
    assert "WHY_THIS_ONE" in full_log


# ---------------------------------------------------------------- T10e: картинка к черновику

def test_found_figure_pairs_excludes_not_found():
    level_a = [
        {"value": "406,000", "source": 'Story [1] ("406 tys.")', "status": "FOUND"},
        {"value": "999", "source": 'Story [1] ("999")', "status": "NOT_FOUND"},
    ]
    block = {
        "figures": '- 406,000 -> Story [1] ("406 tys.")\n- 999 -> Story [1] ("999")',
        "_level_a": level_a,
    }
    pairs = found_figure_pairs(block)
    assert pairs == [("406,000", 'Story [1] ("406 tys.")')]


def test_draft_card_calls_figures_chart_for_every_draft(monkeypatch):
    """T11e.2: откат фикса ревью №2 - figures_chart больше не единственный
    путь только для draft-3/single, вызывается единообразно для любого
    номера черновика, и отказ не откатывается на stat_card/quote_card."""
    calls = []
    monkeypatch.setattr("src.charts.figures_chart",
                        lambda *a, **kw: calls.append("figures_chart") or "chart.png")

    level_a = [{"value": "1", "source": "x", "status": "FOUND"},
              {"value": "2", "source": "y", "status": "FOUND"}]
    block = {"body": "1 and 2 mentioned here today.", "figures": "1 -> x\n2 -> y",
            "source": "https://a.com", "shape": "digest", "_level_a": level_a}

    draft_card(block, 1)
    draft_card(block, 2)

    assert calls == ["figures_chart", "figures_chart"]


def test_draft_card_returns_none_when_no_found_figures(monkeypatch):
    """T11e.2: ноль проверенных чисел -> ноль картинок за прогон, валидный
    исход (stat_card/quote_card-фолбэк удалён в T13b как мёртвый код)."""
    block = {"body": "Nothing verifiable here today.", "figures": "999 -> x",
            "source": "https://a.com", "shape": "digest",
            "_level_a": [{"value": "999", "source": "x", "status": "NOT_FOUND"}]}
    assert draft_card(block, 1) is None


def test_draft_card_draft_one_and_two_write_distinct_image_files(monkeypatch, tmp_path):
    """Тот же класс бага, что T10g нашёл для stat_card, теперь для figures_chart:
    раньше она вызывалась не больше раза за прогон (только draft 3/single),
    коллизия имени файла была невозможна физически. T11e зовёт её для каждого
    черновика единообразно - без различающего key оба писали бы в один и тот
    же "figures_dark.png", и черновик 1 получал бы картинку черновика 2."""
    import src.charts as charts_mod
    monkeypatch.setattr(charts_mod, "_OUT_DIR", tmp_path)

    level_a = [{"value": "1", "source": "unemployment rate", "status": "FOUND"},
              {"value": "2", "source": "deficit to GDP", "status": "FOUND"}]
    block = {"body": "1 and 2 mentioned today.",
            "figures": "1 -> unemployment rate\n2 -> deficit to GDP",
            "source": "https://a.com", "shape": "digest", "_level_a": level_a}

    path1 = draft_card(block, 1)
    path2 = draft_card(block, 2)

    assert path1 is not None and path2 is not None
    assert path1 != path2
    assert path1.exists() and path2.exists()


def test_draft_image_failure_does_not_crash_main_run(monkeypatch, tmp_path):
    """Падение matplotlib не роняет прогон - draft_card падает, main() продолжает,
    черновик всё равно уходит текстом (T10e)."""
    seen_path = tmp_path / "seen.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(main, "SEEN", seen_path)
    monkeypatch.setattr(main, "METRICS", metrics_path)

    item = Item(title="Test story", url="https://example.com/story1",
               source="example.com", tag="misc",
               published=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(collect, "collect_all", lambda cfg, hours: [item])
    monkeypatch.setattr(numbers, "gather", lambda cfg: {})
    selected = [{"item": item, "score": 8, "angle": "x", "why_nonobvious": "x",
                "body": "", "verified": False}]
    monkeypatch.setattr(brain, "rank", lambda candidates, top_n: selected)
    monkeypatch.setattr(enrich, "enrich", lambda selected, limit: 0)
    monkeypatch.setattr(brain, "draft", lambda *a, **kw: _CANNED_DRAFT)
    monkeypatch.setattr(brain, "last_model_used", lambda: "gemini-3.5-flash")
    monkeypatch.setattr(brain, "quota_summary",
                        lambda: {"total": 1, "successful": 1, "quota_refused": 0})
    monkeypatch.setattr(main, "draft_card",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("matplotlib exploded")))

    sent = []
    monkeypatch.setattr("src.deliver.send", lambda text, domain_urls=None: sent.append(text))
    monkeypatch.setattr("src.deliver.send_photo", lambda *a, **kw: None)

    monkeypatch.setattr("sys.argv", ["main.py"])
    rc = main.main()

    assert rc == 0
    assert sent, "деliver.send должен был вызваться, несмотря на упавшую картинку"


# ---------------------------------------------------------------- T9d: сквозной прогон

_CANNED_DRAFT = (
    "SHAPE: digest\n"
    "BODY: Nothing much happened today.\n"
    "FIGURES: none used\n"
    "SOURCE: https://example.com/story1\n"
    "WHY_THIS_ONE: filler\n"
    "VERDICT: SKIP\n"
    "WHY: commodity news, no edge\n"
    "CHECK_FIRST: -"
)


def test_main_send_run_writes_metrics_record(monkeypatch, tmp_path):
    """Полный прогон main() с замоканными внешними вызовами (Gemini, сеть,
    Телеграм) - проверяет, что T9d-проводка (main.py -> metrics.py) реально
    работает, а не только что каждый кусок по отдельности проходит юнит-тесты.

    NEWSBOT_PERSIST_STATE=1 - явное локальное сохранение (T13c): без него send
    больше не трогает state/ вообще, см. test_send_without_persist_state_*
    ниже."""
    monkeypatch.setenv("NEWSBOT_PERSIST_STATE", "1")
    seen_path = tmp_path / "seen.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(main, "SEEN", seen_path)
    monkeypatch.setattr(main, "METRICS", metrics_path)

    item = Item(title="Test story", url="https://example.com/story1",
               source="example.com", tag="misc",
               published=datetime.now(timezone.utc).isoformat())

    monkeypatch.setattr(collect, "collect_all", lambda cfg, hours: [item])
    monkeypatch.setattr(numbers, "gather", lambda cfg: {
        "PLN/USD": {"value": 4.0, "as_of": "2026-07-30"},
        "_market_source": {"wig20": "yfinance"},
    })
    selected = [{"item": item, "score": 8, "angle": "test angle",
                "why_nonobvious": "test", "body": "", "verified": False}]
    monkeypatch.setattr(brain, "rank", lambda candidates, top_n: selected)
    monkeypatch.setattr(enrich, "enrich", lambda selected, limit: 0)
    monkeypatch.setattr(brain, "draft", lambda *a, **kw: _CANNED_DRAFT)
    monkeypatch.setattr(brain, "last_model_used", lambda: "gemini-3.5-flash")
    monkeypatch.setattr(brain, "quota_summary",
                        lambda: {"total": 2, "successful": 2, "quota_refused": 0})

    sent = []
    monkeypatch.setattr("src.deliver.send", lambda text, domain_urls=None: sent.append(text))
    monkeypatch.setattr("src.deliver.send_photo", lambda *a, **kw: None)

    monkeypatch.setattr("sys.argv", ["main.py", "--no-charts"])
    rc = main.main()

    assert rc == 0
    assert sent, "deliver.send должен был вызваться хотя бы раз"
    assert seen_path.exists()

    records = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert len(records) == 1
    rec = records[0]
    assert rec["collected"] == 1
    assert rec["after_dedup"] == 1
    assert rec["ranked"] == 1
    assert rec["drafted"] == 1
    assert rec["verdicts"] == {"POST": 0, "MAYBE": 0, "SKIP": 1}
    assert rec["gemini_successful"] == 2
    assert rec["draft_model"] == "gemini-3.5-flash"
    assert rec["market_source"] == {"wig20": "yfinance"}


# ---------------------------------------------------------------- T13c: state/ только с Actions

def _stub_send_run(monkeypatch, tmp_path):
    seen_path = tmp_path / "seen.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(main, "SEEN", seen_path)
    monkeypatch.setattr(main, "METRICS", metrics_path)

    item = Item(title="Test story", url="https://example.com/story1",
               source="example.com", tag="misc",
               published=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(collect, "collect_all", lambda cfg, hours: [item])
    monkeypatch.setattr(numbers, "gather", lambda cfg: {"_market_source": {}})
    selected = [{"item": item, "score": 8, "angle": "x", "why_nonobvious": "x",
                "body": "", "verified": False}]
    monkeypatch.setattr(brain, "rank", lambda candidates, top_n: selected)
    monkeypatch.setattr(enrich, "enrich", lambda selected, limit: 0)
    monkeypatch.setattr(brain, "draft", lambda *a, **kw: _CANNED_DRAFT)
    monkeypatch.setattr(brain, "last_model_used", lambda: "gemini-3.5-flash")
    monkeypatch.setattr(brain, "quota_summary",
                        lambda: {"total": 1, "successful": 1, "quota_refused": 0})
    monkeypatch.setattr("src.deliver.send", lambda text, domain_urls=None: None)
    monkeypatch.setattr("src.deliver.send_photo", lambda *a, **kw: None)
    monkeypatch.setattr("sys.argv", ["main.py", "--no-charts"])
    return seen_path, metrics_path


def test_send_without_persist_state_env_leaves_state_untouched(monkeypatch, tmp_path):
    """T13c (грабля 6): локальный send без NEWSBOT_PERSIST_STATE не пишет ни
    seen.json, ни metrics.json - иначе ручной вечерний прогон помечает сюжеты
    виденными и утренний прогон Actions их теряет."""
    monkeypatch.delenv("NEWSBOT_PERSIST_STATE", raising=False)
    seen_path, metrics_path = _stub_send_run(monkeypatch, tmp_path)

    rc = main.main()

    assert rc == 0
    assert not seen_path.exists()
    assert not metrics_path.exists()


def test_send_with_persist_state_env_writes_state(monkeypatch, tmp_path):
    """С явным NEWSBOT_PERSIST_STATE=1 (то, что ставит digest.yml, или ручной
    run.ps1 send -PersistState) состояние пишется как раньше."""
    monkeypatch.setenv("NEWSBOT_PERSIST_STATE", "1")
    seen_path, metrics_path = _stub_send_run(monkeypatch, tmp_path)

    rc = main.main()

    assert rc == 0
    assert seen_path.exists()
    assert metrics_path.exists()


# ---------------------------------------------------------------- T11e.2: график рынков по флагу

def _stub_common_run(monkeypatch, tmp_path, argv):
    seen_path = tmp_path / "seen.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(main, "SEEN", seen_path)
    monkeypatch.setattr(main, "METRICS", metrics_path)

    item = Item(title="Test story", url="https://example.com/story1",
               source="example.com", tag="misc",
               published=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(collect, "collect_all", lambda cfg, hours: [item])
    monkeypatch.setattr(numbers, "gather", lambda cfg: {"_market_source": {}})
    selected = [{"item": item, "score": 8, "angle": "x", "why_nonobvious": "x",
                "body": "", "verified": False}]
    monkeypatch.setattr(brain, "rank", lambda candidates, top_n: selected)
    monkeypatch.setattr(enrich, "enrich", lambda selected, limit: 0)
    monkeypatch.setattr(brain, "draft", lambda *a, **kw: _CANNED_DRAFT)
    monkeypatch.setattr(brain, "last_model_used", lambda: "gemini-3.5-flash")
    monkeypatch.setattr(brain, "quota_summary",
                        lambda: {"total": 1, "successful": 1, "quota_refused": 0})
    monkeypatch.setattr("src.deliver.send", lambda text, domain_urls=None: None)
    monkeypatch.setattr("src.deliver.send_photo", lambda *a, **kw: None)
    monkeypatch.setattr("sys.argv", argv)


def test_market_chart_not_built_by_default(monkeypatch, tmp_path):
    """T11e.2: график рынков убран из ежедневной отправки - владелец им не
    пользовался, а WIG20 TR против ценового S&P 500 систематически рисует
    Польшу лучше конкурента. Цифры остаются в блоке ЦИФРЫ, картинки - нет."""
    calls = []
    monkeypatch.setattr("src.charts.market_overview",
                        lambda *a, **kw: calls.append(1) or "chart.png")
    _stub_common_run(monkeypatch, tmp_path, ["main.py", "--no-charts"])
    rc = main.main()
    assert rc == 0
    assert calls == []


def test_market_chart_built_with_markets_flag(monkeypatch, tmp_path):
    """Доступен по требованию (run.ps1 -Markets), код и тесты market_overview
    не удалены (T11 ТЗ)."""
    calls = []
    monkeypatch.setattr("src.charts.market_overview",
                        lambda *a, **kw: calls.append(1) or "chart.png")
    _stub_common_run(monkeypatch, tmp_path, ["main.py", "--markets"])
    rc = main.main()
    assert rc == 0
    assert calls == [1]


def test_main_dry_run_does_not_write_metrics(monkeypatch, tmp_path):
    seen_path = tmp_path / "seen.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(main, "SEEN", seen_path)
    monkeypatch.setattr(main, "METRICS", metrics_path)

    item = Item(title="Test story", url="https://example.com/story1",
               source="example.com", tag="misc",
               published=datetime.now(timezone.utc).isoformat())

    monkeypatch.setattr(collect, "collect_all", lambda cfg, hours: [item])
    monkeypatch.setattr(numbers, "gather", lambda cfg: {})
    selected = [{"item": item, "score": 8, "angle": "x", "why_nonobvious": "x",
                "body": "", "verified": False}]
    monkeypatch.setattr(brain, "rank", lambda candidates, top_n: selected)
    monkeypatch.setattr(enrich, "enrich", lambda selected, limit: 0)
    monkeypatch.setattr(brain, "draft", lambda *a, **kw: _CANNED_DRAFT)
    monkeypatch.setattr(brain, "last_model_used", lambda: "gemini-3.5-flash")
    monkeypatch.setattr(brain, "quota_summary",
                        lambda: {"total": 1, "successful": 1, "quota_refused": 0})

    monkeypatch.setattr("sys.argv", ["main.py", "--no-charts", "--dry"])
    rc = main.main()

    assert rc == 0
    assert not metrics_path.exists()
    assert not seen_path.exists()
