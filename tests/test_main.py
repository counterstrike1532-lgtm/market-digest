"""main.filter_by_age: свежая / старая / без даты (T9a). Без сети и без файлов -
элементы строятся напрямую как collect.Item."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.collect import Item
from src.main import filter_by_age


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
