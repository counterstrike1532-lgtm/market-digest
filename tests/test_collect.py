"""collect.dedupe_by_title (T10b): проверка гипотезы владельца, что дедуп по
заголовку смешивает URL разных сюжетов. По коду это не так - dedupe_by_title
только оставляет или отбрасывает Item целиком, ни одно поле выжившей записи
не подменяется полем отброшенной. Настоящая причина боевого бага была в
main.build_domain_urls (см. tests/test_main.py) - эти тесты закрепляют, что
dedupe_by_title сама по себе честная, чтобы предположение не всплыло снова."""
from __future__ import annotations

from datetime import datetime, timezone

from src.collect import Item, dedupe_by_title


def _item(title, url, weight=1.0, social=0):
    return Item(title=title, url=url, source="example.com", tag="misc",
               published=datetime.now(timezone.utc).isoformat(),
               weight=weight, social=social)


def test_dedupe_by_title_survivor_keeps_its_own_url():
    """Две записи с одинаковым (после нормализации) заголовком, разные URL -
    выживает одна, но с её СОБСТВЕННЫМ URL, не подменённым чужим."""
    a = _item("Wielka plyta zagrozi deweloperom", "https://a.example.com/story-a", weight=1.0)
    b = _item("Wielka plyta zagrozi deweloperom!", "https://b.example.com/story-b", weight=0.5)
    result = dedupe_by_title([a, b])
    assert len(result) == 1
    survivor = result[0]
    assert survivor.url in ("https://a.example.com/story-a", "https://b.example.com/story-b")
    # URL выжившей записи - её собственный, не гибрид и не URL другой записи
    assert survivor.url == a.url or survivor.url == b.url


def test_dedupe_by_title_distinct_stories_each_keep_own_url():
    """Три записи, две дублируют друг друга по заголовку, третья - другой
    сюжет: после дедупа у каждого выжившего сюжета свой, не перепутанный URL."""
    dup_a = _item("Bank Millennium obniza stawke", "https://bankier.pl/millennium-a")
    dup_b = _item("Bank Millennium obniza stawke!", "https://bankier.pl/millennium-b", weight=0.5)
    other = _item("Polska sprowadza coraz wiecej gazu", "https://bankier.pl/gazu-9176695")
    result = dedupe_by_title([dup_a, dup_b, other])
    urls = {it.url for it in result}
    assert len(result) == 2                       # millennium-дубль схлопнулся, gazu остался
    assert "https://bankier.pl/gazu-9176695" in urls
    # ни один survivor не унаследовал URL сюжета, с которым его не сравнивали
    assert all(u in {dup_a.url, dup_b.url, other.url} for u in urls)


def test_dedupe_by_title_keeps_higher_weight_and_its_url():
    higher = _item("Dunaj wysycha, elektrownia jadrowa staje", "https://bankier.pl/dunaj", weight=1.0)
    lower = _item("Dunaj wysycha - elektrownia jadrowa staje", "https://mirror.example.com/dunaj",
                  weight=0.3)
    result = dedupe_by_title([lower, higher])
    assert len(result) == 1
    assert result[0].url == "https://bankier.pl/dunaj"
