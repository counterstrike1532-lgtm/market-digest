"""main.filter_by_age: свежая / старая / без даты (T9a). Без сети и без файлов -
элементы строятся напрямую как collect.Item."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from src import brain, collect, deliver, enrich, main, numbers, verify
from src.collect import Item
from src.main import (build_domain_urls, build_message, draft_card, filter_by_age,
                      found_figure_pairs, quote_card_args, render_draft_message,
                      render_summary, stat_card_rows, _fix_glued_punctuation, _word_count)


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


def test_stat_card_rows_only_found_capped_at_three():
    level_a = [{"value": str(i), "source": f"src {i}", "status": "FOUND"} for i in range(4)]
    level_a.append({"value": "bad", "source": "src bad", "status": "NOT_FOUND"})
    block = {"body": "0 1 2 3 bad appear here in this sentence today.", "_level_a": level_a}
    rows = stat_card_rows(block)
    assert len(rows) == 3
    assert all(v in {"0", "1", "2", "3"} for v, _ in rows)
    assert "bad" not in [v for v, _ in rows]


def test_stat_card_rows_two_of_three_when_one_not_found():
    level_a = [
        {"value": "406,000", "source": "x", "status": "FOUND"},
        {"value": "0.50%", "source": "y", "status": "FOUND"},
        {"value": "213.5 TWh", "source": "z", "status": "NOT_FOUND"},
    ]
    block = {"body": "406,000 people and 0.50% rate were both mentioned today.",
            "_level_a": level_a}
    rows = stat_card_rows(block)
    assert len(rows) == 2


def test_quote_card_args_uses_first_sentence_and_domain():
    block = {"body": "DDM understates banks with low payout. Second sentence here.",
            "source": "https://www.bankier.pl/wiadomosc/x"}
    sentence, source = quote_card_args(block)
    assert sentence == "DDM understates banks with low payout."
    assert source == "www.bankier.pl"


def test_draft_card_uses_figures_chart_for_draft_three_only(monkeypatch):
    """figures_chart вызывается для черновика 3 и не вызывается для 1/2 (T10e) -
    перенацелено с DRAFT 1 (T9f), DRAFT 1/2 теперь получают карточку."""
    calls = []
    monkeypatch.setattr("src.charts.figures_chart",
                        lambda *a, **kw: calls.append("figures_chart") or "chart.png")
    monkeypatch.setattr("src.charts.stat_card",
                        lambda *a, **kw: calls.append("stat_card") or "stat.png")
    monkeypatch.setattr("src.charts.quote_card",
                        lambda *a, **kw: calls.append("quote_card") or "quote.png")

    level_a = [{"value": "1", "source": "x", "status": "FOUND"}]
    block = {"body": "1 is mentioned here today.", "figures": "1 -> x", "source": "https://a.com",
            "shape": "digest", "_level_a": level_a}

    draft_card(block, 1)
    draft_card(block, 2)
    draft_card(block, 3)

    assert calls.count("figures_chart") == 1
    assert calls == ["stat_card", "stat_card", "figures_chart"]


def test_draft_card_draft_one_and_two_write_distinct_image_files(monkeypatch, tmp_path):
    """Живой прогон (T10g review): draft_card(block, 1) и draft_card(block, 2)
    оба рисуют stat_card в одном прогоне - без различающего key оба писали в
    один и тот же "stat_dark.png", и черновик 1 в итоге получал картинку
    черновика 2 (файл перезаписывался до отправки). Через настоящий
    charts.stat_card (не мок) - проверяем реальные пути, не только что key
    передаётся."""
    import src.charts as charts_mod
    monkeypatch.setattr(charts_mod, "_OUT_DIR", tmp_path)

    level_a = [{"value": "406,000", "source": 'x ("406 tys.")', "status": "FOUND"}]
    block = {"body": "406,000 people were mentioned today.", "figures": '406,000 -> x ("406 tys.")',
            "source": "https://a.com", "shape": "digest", "_level_a": level_a}

    path1 = draft_card(block, 1)
    path2 = draft_card(block, 2)

    assert path1 is not None and path2 is not None
    assert path1 != path2
    assert path1.exists() and path2.exists()


def test_draft_card_falls_back_to_quote_card_when_nothing_found(monkeypatch):
    monkeypatch.setattr("src.charts.stat_card", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("stat_card must not be called with zero FOUND rows")))
    monkeypatch.setattr("src.charts.quote_card", lambda *a, **kw: "quote.png")

    block = {"body": "Nothing verifiable here today.", "figures": "999 -> x",
            "source": "https://a.com", "shape": "digest",
            "_level_a": [{"value": "999", "source": "x", "status": "NOT_FOUND"}]}
    result = draft_card(block, 1)
    assert result == "quote.png"


def test_draft_card_draft_three_falls_back_to_quote_card_when_figures_chart_declines(monkeypatch):
    """Живой прогон (T10g review): DRAFT 3 с 0/4 FOUND не рисовал вообще
    ничего - figures_chart вернул None (меньше 2 значений после фильтра по
    FOUND), а фолбэка на quote_card для num==3 не было. "У каждого из трёх
    черновиков ровно одна картинка" не делает для третьего исключения -
    figures_chart отказал по ЛЮБОЙ из своих причин (не только "0 FOUND") ->
    quote_card, как и для карточек."""
    monkeypatch.setattr("src.charts.figures_chart", lambda *a, **kw: None)
    monkeypatch.setattr("src.charts.quote_card", lambda *a, **kw: "quote.png")

    block = {
        "body": "This mismatch destroys the industrial crushing margin.",
        "figures": "4.1 million tons -> Source [3] text",   # один FOUND - меньше 2, chart отказывает
        "source": "https://bankier.pl/a", "shape": "B",
        "_level_a": [{"value": "4.1 million tons", "source": "Source [3] text",
                     "status": "FOUND"}],
    }
    result = draft_card(block, 3)
    assert result == "quote.png"


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
    работает, а не только что каждый кусок по отдельности проходит юнит-тесты."""
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
