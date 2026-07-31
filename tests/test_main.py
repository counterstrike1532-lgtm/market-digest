"""main.filter_by_age: свежая / старая / без даты (T9a). Без сети и без файлов -
элементы строятся напрямую как collect.Item."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src import brain, collect, enrich, main, numbers, verify
from src.collect import Item
from src.main import filter_by_age, first_draft_covers_one_story


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
    monkeypatch.setattr("src.deliver.send", lambda text: sent.append(text))
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
