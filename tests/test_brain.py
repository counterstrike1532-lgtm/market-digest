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
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
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
