"""main.filter_by_age: свежая / старая / без даты (T9a). Без сети и без файлов -
элементы строятся напрямую как collect.Item."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from src import brain, collect, deliver, enrich, main, numbers, verify
from src.collect import Item
from src.main import (build_domain_urls, build_message, filter_by_age,
                      first_draft_covers_one_story, _fix_glued_punctuation)


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


# ---------------------------------------------------------------- T9f: один сюжет ли

def _draft_with_source(source_field: str) -> str:
    return (
        "SHAPE: digest\n"
        "BODY: text.\n"
        "FIGURES: none used\n"
        f"SOURCE: {source_field}\n"
        "WHY_THIS_ONE: reason\n"
        "VERDICT: SKIP\n"
        "WHY: no edge\n"
        "CHECK_FIRST: -"
    )


def test_first_draft_covers_one_story_single_url():
    text = _draft_with_source("https://example.com/story1")
    assert first_draft_covers_one_story(text) is True


def test_first_draft_covers_one_story_multiple_urls_comma_separated():
    text = _draft_with_source("https://example.com/story1, https://example.com/story2")
    assert first_draft_covers_one_story(text) is False


def test_first_draft_covers_one_story_no_match_is_conservative_false():
    assert first_draft_covers_one_story("garbage, no SOURCE field at all") is False


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
