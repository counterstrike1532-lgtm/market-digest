"""brain._call: расход квоты Gemini (T9a). requests.post замокан целиком,
в сеть тесты не ходят. time.sleep замокан, чтобы ретраи не спали по-настоящему.
"""
from __future__ import annotations

import pytest

from src import brain


class FakeResponse:
    def __init__(self, status_code, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _ok(text="hello"):
    return FakeResponse(200, json_data={"candidates": [
        {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
    ]})


@pytest.fixture(autouse=True)
def _reset_brain_state(monkeypatch):
    monkeypatch.setattr(brain, "_requests_made", 0)
    monkeypatch.setattr(brain, "_successful_calls", 0)
    monkeypatch.setattr(brain, "_quota_refusals", 0)
    monkeypatch.setattr(brain, "_day_exhausted", set())
    monkeypatch.setattr(brain, "_last_rank_degraded", False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setenv("NEWSBOT_ALLOW_LIVE", "1")
    monkeypatch.setattr(brain.time, "sleep", lambda *_a, **_k: None)


def test_call_success_counts_one_request(monkeypatch):
    monkeypatch.setattr(brain, "MODELS", ["gemini-3.5-flash"])
    calls = []

    def fake_post(*a, **kw):
        calls.append(kw)
        return _ok("some text back")

    monkeypatch.setattr(brain.requests, "post", fake_post)
    result = brain._call("prompt")
    assert result == "some text back"
    q = brain.quota_summary()
    assert q == {"total": 1, "successful": 1, "quota_refused": 0}


def test_call_success_logs_which_model_answered(monkeypatch, caplog):
    """T10g: расход квоты в логе уже был, а имени отработавшей модели не было -
    "не 3.1-lite ли это была" не на что ответить, не заглянув в код. Пишем имя
    при каждом успешном вызове, не только в brain.last_model_used()."""
    monkeypatch.setattr(brain, "MODELS", ["gemini-3.5-flash", "gemini-3.6-flash"])
    monkeypatch.setattr(brain.requests, "post", lambda *a, **kw: _ok("some text"))

    import logging
    with caplog.at_level(logging.INFO):
        brain._call("prompt")

    assert "gemini-3.5-flash" in caplog.text


def test_call_success_logs_second_model_after_first_fails(monkeypatch, caplog):
    """Отработавшая модель не всегда первая в списке - лог должен называть ту,
    что реально ответила, не первую по порядку."""
    monkeypatch.setattr(brain, "MODELS", ["gemini-3.5-flash", "gemini-3.6-flash"])

    def fake_post(url, **kw):
        if "gemini-3.5-flash" in url:
            return FakeResponse(404)
        return _ok("some text")

    monkeypatch.setattr(brain.requests, "post", fake_post)

    import logging
    with caplog.at_level(logging.INFO):
        brain._call("prompt")

    assert "gemini-3.6-flash: ответ получен" in caplog.text


def test_call_429_per_day_falls_through_to_next_model(monkeypatch):
    monkeypatch.setattr(brain, "MODELS", ["gemini-3.5-flash", "gemini-3.6-flash"])
    seq = [
        FakeResponse(429, text='{"error": {"status": "RESOURCE_EXHAUSTED", "quotaId": "PerDay"}}'),
        _ok("second model answered"),
    ]

    def fake_post(*a, **kw):
        return seq.pop(0)

    monkeypatch.setattr(brain.requests, "post", fake_post)
    result = brain._call("prompt")
    assert result == "second model answered"
    q = brain.quota_summary()
    assert q["total"] == 2
    assert q["successful"] == 1
    assert q["quota_refused"] == 1
    assert "gemini-3.5-flash" in brain._day_exhausted


def test_call_503_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(brain, "MODELS", ["gemini-3.5-flash"])
    seq = [FakeResponse(503, text="server hiccup"), _ok("recovered")]

    def fake_post(*a, **kw):
        return seq.pop(0)

    monkeypatch.setattr(brain.requests, "post", fake_post)
    result = brain._call("prompt")
    assert result == "recovered"
    q = brain.quota_summary()
    assert q["total"] == 2
    assert q["successful"] == 1


def test_call_blocked_without_allow_live_env(monkeypatch):
    """T11a: без NEWSBOT_ALLOW_LIVE=1 _call падает раньше любого HTTP - предохранитель
    от несанкционированных живых прогонов, который просьбами не лечился (T10 - 4 раза)."""
    monkeypatch.delenv("NEWSBOT_ALLOW_LIVE", raising=False)
    monkeypatch.setattr(brain, "MODELS", ["gemini-3.5-flash"])
    calls = []
    monkeypatch.setattr(brain.requests, "post", lambda *a, **kw: calls.append(kw))

    with pytest.raises(RuntimeError, match="NEWSBOT_ALLOW_LIVE"):
        brain._call("prompt")
    assert calls == []


def test_call_blocked_when_allow_live_not_exactly_one(monkeypatch):
    monkeypatch.setenv("NEWSBOT_ALLOW_LIVE", "true")
    monkeypatch.setattr(brain, "MODELS", ["gemini-3.5-flash"])
    calls = []
    monkeypatch.setattr(brain.requests, "post", lambda *a, **kw: calls.append(kw))

    with pytest.raises(RuntimeError, match="NEWSBOT_ALLOW_LIVE"):
        brain._call("prompt")
    assert calls == []


def test_call_full_exhaustion_raises(monkeypatch):
    monkeypatch.setattr(brain, "MODELS", ["gemini-3.5-flash", "gemini-3.6-flash"])

    def fake_post(*a, **kw):
        return FakeResponse(429, text='{"quotaId": "PerDay"}')

    monkeypatch.setattr(brain.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="дневная квота Gemini исчерпана"):
        brain._call("prompt")
    q = brain.quota_summary()
    assert q["quota_refused"] == 2
    assert q["successful"] == 0


# ---------------------------------------------------------------- T13a: rank() без модели

from types import SimpleNamespace


def _fake_item(i):
    return SimpleNamespace(source="example.com", tag="misc", social=0,
                           title=f"Story {i}", summary="body", weight=1.0)


def test_rank_falls_back_to_heuristic_order_when_model_unavailable(monkeypatch):
    """T13a: модель недоступна целиком - rank() отдаёт первые top_n items В ТОМ
    ЖЕ ПОРЯДКЕ, в каком их передал вызывающий код (main.heuristic_prefilter уже
    отсортировал по весу/тегу/свежести/social) - никакого нового скоринга внутри
    rank() самого."""
    items = [_fake_item(i) for i in range(5)]
    monkeypatch.setattr(brain, "_call",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    result = brain.rank(items, top_n=3)

    assert brain.rank_degraded() is True
    assert [r["item"] for r in result] == items[:3]
    assert all("score" not in r and "angle" not in r for r in result)


def test_rank_success_leaves_degraded_flag_false(monkeypatch):
    items = [_fake_item(0), _fake_item(1)]
    monkeypatch.setattr(brain, "_call",
                        lambda *a, **kw: '[{"id": 0, "score": 8, "angle": "a", '
                                        '"why_nonobvious": "b"}]')

    result = brain.rank(items, top_n=5)

    assert brain.rank_degraded() is False
    assert len(result) == 1
    assert result[0]["item"] is items[0]


def test_draft_prompt_bans_two_factless_opening_sentences():
    """T16 шаг 1: если ни из первого, ни из второго предложения нельзя узнать
    ни одного факта - выкинуть оба. Framing-строка DRAFT 1 убрана как источник
    конфликта с FIRST PERSON: OFF BY DEFAULT (07.08: безличные анонсы четыре
    дня подряд были исполнением этой снятой инструкции, не тиком модели)."""
    assert 'Frame it as' not in brain.DRAFT_PROMPT
    assert 'Three things I read this week that stuck with me' not in brain.DRAFT_PROMPT
    assert 'If neither the first nor the second sentence contains a fact' in brain.DRAFT_PROMPT
    assert ('BAD: "A few global and local financial developments that stood out this week."'
            in brain.DRAFT_PROMPT)
    assert ('BAD: "Three market and policy developments stood out in the news this week."'
            in brain.DRAFT_PROMPT)
