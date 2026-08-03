"""T13a: деградированный режим. Ни один отказ Gemini не должен приводить к
пустому сообщению или к упавшему процессу - цифры (NBP/рынки/HICP) и
эвристический предотбор их не требуют и обязаны уходить всегда.

brain._call мокается напрямую (не requests.post) - реальные вызовы Gemini в
этих тестах невозможны физически, NEWSBOT_ALLOW_LIVE не выставляется нигде и
не используется как основная защита (T11a остаётся второй линией, см. её
собственные тесты в test_brain.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

from src import brain, collect, enrich, main, numbers, verify
from src.collect import Item

_RANK_JSON = (
    '[{"id": 0, "score": 8, "angle": "test angle", "why_nonobvious": "test"}, '
    '{"id": 1, "score": 7, "angle": "second angle", "why_nonobvious": "test2"}, '
    '{"id": 2, "score": 6, "angle": "third angle", "why_nonobvious": "test3"}]'
)

_DRAFT_OK = (
    "SHAPE: digest\n"
    "BODY: Nothing much happened today.\n"
    "FIGURES: none used\n"
    "SOURCE: https://example.com/story0\n"
    "WHY_THIS_ONE: filler\n"
    "VERDICT: SKIP\n"
    "WHY: commodity news, no edge\n"
    "CHECK_FIRST: -"
)


def _items(n: int) -> list[Item]:
    now = datetime.now(timezone.utc).isoformat()
    return [Item(title=f"Story {i}", url=f"https://example.com/story{i}",
                source="example.com", tag="misc", published=now)
           for i in range(n)]


def _stub_pipeline(monkeypatch, tmp_path, items: list[Item]) -> list[str]:
    """Общая обвязка: сеть/Телеграм/файлы состояния замоканы, конвейер отбора/
    черновиков/верификации - настоящий код, не мок (иначе T13a-логика внутри
    него не проверяется вообще)."""
    monkeypatch.setattr(main, "SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(main, "METRICS", tmp_path / "metrics.json")
    monkeypatch.setattr(collect, "collect_all", lambda cfg, hours: items)
    monkeypatch.setattr(numbers, "gather", lambda cfg: {
        "PLN/USD": {"value": 4.0, "as_of": "2026-08-03"},
        "_market_source": {},
    })
    monkeypatch.setattr(enrich, "enrich", lambda selected, limit: 0)
    monkeypatch.delenv("NEWSBOT_ALLOW_LIVE", raising=False)

    sent: list[str] = []
    monkeypatch.setattr("src.deliver.send", lambda text, domain_urls=None: sent.append(text))
    monkeypatch.setattr("src.deliver.send_photo", lambda *a, **kw: None)
    monkeypatch.setattr("sys.argv", ["main.py", "--no-charts"])
    return sent


def test_rank_fails_heuristic_top_n_no_drafts(monkeypatch, tmp_path):
    """Матрица: rank падает -> эвристический top-N вместо ранжирования модели,
    строка про отбор без модели, черновиков нет, цифры уходят."""
    items = _items(2)
    sent = _stub_pipeline(monkeypatch, tmp_path, items)

    calls = []

    def fake_call(prompt, **kw):
        calls.append(prompt)
        raise RuntimeError("дневная квота Gemini исчерпана на всех моделях")

    monkeypatch.setattr(brain, "_call", fake_call)

    rc = main.main()
    assert rc == 0
    assert len(calls) == 1, "rank должен был попробовать ровно один раз - без скрытых ретраев"

    assert sent, "цифры и сюжеты обязаны уйти даже при полном падении rank"
    assert len(sent) == 1, "черновиков быть не должно - ровно одно сообщение (сводка)"
    summary = sent[0]
    assert "USD 4.0" in summary
    assert "СЮЖЕТЫ (2)" in summary
    assert "Story 0" in summary and "Story 1" in summary
    assert "отбор сюжетов сегодня без модели" in summary


def test_draft_fails_summary_still_sent_with_notice(monkeypatch, tmp_path):
    """Матрица: draft падает -> сводка уходит полностью (цифры + сюжеты с
    настоящими score/angle от модели), одна строка про отсутствие черновиков
    с причиной, ни одного черновика."""
    items = _items(2)
    sent = _stub_pipeline(monkeypatch, tmp_path, items)

    calls = []

    def fake_call(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return _RANK_JSON
        raise RuntimeError("Gemini недоступен. Последнее: HTTP 500 x5")

    monkeypatch.setattr(brain, "_call", fake_call)

    rc = main.main()
    assert rc == 0
    assert len(calls) == 2, "rank (успех) + draft (падение) - без скрытых ретраев поверх этого"

    assert sent
    assert len(sent) == 1, "черновиков уйти не должно, только сводка"
    summary = sent[0]
    assert "СЮЖЕТЫ (2)" in summary
    assert "test angle" in summary          # это настоящий отбор модели, не эвристика
    assert "черновиков сегодня нет" in summary
    assert "Gemini недоступен" in summary


def test_verify_fails_entirely_drafts_still_sent_one_notice_line(monkeypatch, tmp_path):
    """Матрица: verify падает целиком - черновики всё равно уходят, картинки нет,
    одна строка на сообщение "числа не верифицированы" (не пометка на каждое
    число - alarm fatigue).

    Падение уровня b (единственный Gemini-вызов внутри verify_drafts) уже
    перехватывается внутри verify._verify_context_llm и НЕ валит verify_drafts
    целиком - это проверено трассировкой (T13a step 0) и является уже правильным
    поведением, отдельно не тестируется здесь заново. Этот тест бьёт по другому
    пути - verify_drafts() падает целиком по любой другой причине (баг парсинга
    и т.п.), и именно этот путь раньше молча терял все черновики (main.py
    оставлял draft_blocks=[])."""
    items = _items(1)
    sent = _stub_pipeline(monkeypatch, tmp_path, items)

    calls = []

    def fake_call(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return _RANK_JSON
        return _DRAFT_OK

    monkeypatch.setattr(brain, "_call", fake_call)

    def _boom(*a, **kw):
        raise RuntimeError("неожиданный баг парсинга уровня a")

    monkeypatch.setattr(verify, "verify_figures_local", _boom)

    rc = main.main()
    assert rc == 0
    assert len(calls) == 2, "verify не должен добавлять новые попытки Gemini при падении"

    assert sent
    assert len(sent) == 2, "сводка + ровно один черновик должны уйти"
    draft_msg = sent[1]
    assert "Nothing much happened today" in draft_msg   # тело черновика дошло как есть (сырой блок)
    assert draft_msg.count("числа не верифицированы") == 1


def test_all_stages_fail_digits_and_heuristic_top_n_only(monkeypatch, tmp_path):
    """Матрица: всё падает -> блок цифр + эвристический top-N + строка про
    отсутствие черновиков. rank падает первым в конвейере и уже закрывает
    draft/verify - лишних попыток вызова Gemini быть не должно."""
    items = _items(3)
    sent = _stub_pipeline(monkeypatch, tmp_path, items)

    calls = []

    def fake_call(prompt, **kw):
        calls.append(prompt)
        raise RuntimeError("everything is down")

    monkeypatch.setattr(brain, "_call", fake_call)

    rc = main.main()
    assert rc == 0
    assert len(calls) == 1, "всё падает через rank - draft/verify не должны даже начинаться"

    assert len(sent) == 1
    summary = sent[0]
    assert "USD 4.0" in summary
    assert "СЮЖЕТЫ (3)" in summary
    assert "отбор сюжетов сегодня без модели" in summary
