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


def test_draft_prompt_bans_false_instant_causation():
    """T16 шаг 3: запрет на класс (утверждение мгновенной причинности без
    данных о скорости реакции в материале), не на список слов - grep
    по instantly/immediately 08.08 подтвердил, что этой конструкции в
    промпте не было; "This decision instantly traps cash" модель придумала
    сама, дважды дословно, до этой правки."""
    assert ('Do not claim that one event caused another instantly unless the material '
            'establishes how' in brain.DRAFT_PROMPT)
    assert ('BAD: "Yet this surge immediately reignited political debates."'
            in brain.DRAFT_PROMPT)
    for banned_word in ("figure", "number", "source", "data"):
        bullet_start = brain.DRAFT_PROMPT.index("Do not claim that one event caused another")
        bullet_end = brain.DRAFT_PROMPT.index("BAD:", bullet_start) + len(
            'BAD: "Yet this surge immediately reignited political debates."')
        assert banned_word not in brain.DRAFT_PROMPT[bullet_start:bullet_end]


def test_draft_prompt_closing_generalization_ban_has_three_bad_examples():
    """T16 шаг 2: запрет закрывающего обобщения формулировку не менял - только
    добавлены два новых BAD к уже существующему. Второй новый пример ("is not
    just X, it is Y") нарушает и этот запрет, и отдельный бан "it's not just
    X, it's Y" в списке VOICE выше - 06.08 черновик нарушил оба запрета одной
    фразой, и ни один не сработал."""
    assert ('the closing sentence must be a specific, concrete thing - not a\n'
            '  sentence that restates what was just said in broader words.'
            in brain.DRAFT_PROMPT)
    assert ('BAD: "These movements show how\n'
            '  quickly regional cost structures and trade policy can reshape corporate '
            'performance."' in brain.DRAFT_PROMPT)
    assert ('BAD: "These figures show how easily headline numbers can mask the underlying '
            'economic\n  reality."' in brain.DRAFT_PROMPT)
    assert ('BAD: "Political gridlock is not just a headline. It is a direct driver of bond '
            'market\n  supply."' in brain.DRAFT_PROMPT)
    normalized = ' '.join(brain.DRAFT_PROMPT.split())
    assert ('If a closing sentence would fit equally well after three different, unrelated '
            'stories, it is not concrete enough - end on the last number, name, or fact '
            'instead.' in normalized)


def test_draft_uses_fallback_when_style_text_is_placeholder_template(monkeypatch):
    """Служебный шаблон style/my_posts.md подменяется на штатный фолбэк в brain.draft."""
    captured_prompt = []

    def fake_post(url, **kw):
        captured_prompt.append(kw["json"]["contents"][0]["parts"][0]["text"])
        return _ok("draft text")

    monkeypatch.setattr(brain.requests, "post", fake_post)
    placeholder_text = (
        "# Мои прошлые посты — эталон стиля\n"
        "Вставь сюда 3–5 своих реальных постов...\n"
        "---\n"
        "(пример структуры — замени на своё)\n"
        "Poland's HICP came in at X%..."
    )
    item = SimpleNamespace(title="t", source="s", url="u", published_known=True)
    selected = [{"item": item, "angle": "a", "why_nonobvious": "n", "body": "b"}]

    brain.draft(selected, data={}, style_text=placeholder_text, n=1)

    assert len(captured_prompt) == 1
    assert "No past posts provided yet" in captured_prompt[0]
    assert "Poland's HICP came in at X%" not in captured_prompt[0]


# ---------------------------------------------------------------- critique-pass tests

def test_critique_draft_success(monkeypatch):
    captured_prompt = []

    def fake_post(url, **kw):
        captured_prompt.append(kw["json"]["contents"][0]["parts"][0]["text"])
        return _ok("Edited post text without fluff.")

    monkeypatch.setattr(brain.requests, "post", fake_post)
    original = "Draft with institutional throat-clearing."
    res = brain.critique_draft(original, shape="digest")

    assert res == "Edited post text without fluff."
    assert len(captured_prompt) == 1
    assert "DRAFT TYPE: digest post" in captured_prompt[0]
    assert original in captured_prompt[0]
    assert "HARD LENGTH LIMITS AFTER EDITING" in captured_prompt[0]


def test_critique_draft_strips_markdown_code_block(monkeypatch):
    monkeypatch.setattr(brain.requests, "post", lambda *a, **kw: _ok("```markdown\nClean edited text\n```"))
    res = brain.critique_draft("Original text")
    assert res == "Clean edited text"


def test_critique_draft_empty_input():
    assert brain.critique_draft("") == ""
    assert brain.critique_draft("   ") == ""


def test_critique_draft_fallback_on_empty_response(monkeypatch):
    monkeypatch.setattr(brain.requests, "post", lambda *a, **kw: _ok(""))
    original = "Draft text that should be kept."
    res = brain.critique_draft(original)
    assert res == original


def test_critique_draft_fallback_on_network_error(monkeypatch):
    def fake_post(*a, **kw):
        raise RuntimeError("API failure")

    monkeypatch.setattr(brain.requests, "post", fake_post)
    original = "Draft text that should be kept on error."
    res = brain.critique_draft(original)
    assert res == original


def test_draft_prompt_amendment_length_sentence_and_banned_phrases():
    prompt = brain.DRAFT_PROMPT
    assert "HARD LENGTH LIMIT: digest post ≤130 words, single-topic post ≤120 words." in prompt
    assert "SENTENCE RULE: one sentence = one fact + one implication." in prompt
    assert 'BANNED PHRASES (do not use in any form): "structural shift"' in prompt
    assert "style/banned_phrases.md" in prompt


def test_banned_phrases_file_exists_and_contains_required_phrases():
    import pathlib
    p = pathlib.Path("style/banned_phrases.md")
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    for phrase in [
        "structural shift", "is emerging as", "consequently", "far exceeding",
        "crowding out net expansion", "this mechanism shows", "two numbers stand out",
        "the common view is that", "primary bottleneck", "underlying economic reality"
    ]:
        assert f"`{phrase}`" in content


# ---------------------------------------------------------------- editor / humanizer pass tests

def test_load_golden_rewrites_success():
    content = brain.load_golden_rewrites()
    assert content, "style/golden_rewrites.md должен успешно считываться"
    assert "GOLDEN REWRITES: FEW-SHOT EDITING EXAMPLES" in content
    assert "EXAMPLE 1: SINGLE TOPIC" in content
    assert "EXAMPLE 2: SINGLE TOPIC" in content
    assert "EXAMPLE 3: MULTI-TOPIC DIGEST" in content


def test_golden_rewrites_paragraph_structure_and_limits():
    import re
    content = brain.load_golden_rewrites()
    target_posts = re.findall(r"\[TARGET EDITED POST\]:\s*\n(.*?)(?=\n---\n|\Z)", content, re.DOTALL)
    assert len(target_posts) == 3, f"Expected 3 target edited posts, found {len(target_posts)}"
    for i, post in enumerate(target_posts, 1):
        clean_post = post.strip()
        paragraphs = [p.strip() for p in clean_post.split("\n\n") if p.strip()]
        assert 2 <= len(paragraphs) <= 4, f"Example {i} should be split into 2-4 visual paragraphs"
        word_count = len(clean_post.split())
        if i in (1, 2):
            assert 80 <= word_count <= 135, f"Example {i} single topic word count {word_count} not in 80-135"
        else:
            assert 105 <= word_count <= 140, f"Example {i} digest word count {word_count} not in 105-140"



def test_edit_and_humanize_draft_success(monkeypatch):
    captured_calls = []

    def fake_call_api(prompt, system_instruction=None, temperature=0.7, max_tokens=4096):
        captured_calls.append({"prompt": prompt, "system_instruction": system_instruction, "temperature": temperature})
        return "Edited clean authentic text by student practitioner."

    monkeypatch.setattr(brain, "call_gemini_api", fake_call_api)
    raw = "Raw bot draft about tech capex and debt issuance."
    res = brain.edit_and_humanize_draft(raw, draft_type="single")

    assert res == "Edited clean authentic text by student practitioner."
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["temperature"] == 0.25
    assert call["system_instruction"] == brain.EDITOR_HUMANIZER_PROMPT
    assert raw in call["prompt"]
    assert "--- YOUR TASK ---" in call["prompt"]
    assert "105-115 words" in call["prompt"]


def test_edit_and_humanize_draft_digest_word_limits(monkeypatch):
    captured_calls = []

    def fake_call_api(prompt, system_instruction=None, temperature=0.7, max_tokens=4096):
        captured_calls.append({"prompt": prompt, "system_instruction": system_instruction})
        return "Edited clean digest text."

    monkeypatch.setattr(brain, "call_gemini_api", fake_call_api)
    raw = "Raw digest draft."
    res = brain.edit_and_humanize_draft(raw, draft_type="digest")

    assert res == "Edited clean digest text."
    assert "115-125 words" in captured_calls[0]["prompt"]


def test_edit_and_humanize_draft_fallback_on_error(monkeypatch):
    def exploding_api(*a, **kw):
        raise RuntimeError("Gemini API connection error")

    monkeypatch.setattr(brain, "call_gemini_api", exploding_api)
    raw = "Raw draft that must be preserved on API error."
    res = brain.edit_and_humanize_draft(raw, draft_type="single")
    assert brain.FALLBACK_BADGE in res
    assert raw in res


def test_edit_and_humanize_draft_empty_input():
    assert brain.edit_and_humanize_draft("") == ""
    assert brain.edit_and_humanize_draft("   ") == ""


def test_edit_and_humanize_draft_prunes_four_bullets_preventing_number_mismatch(monkeypatch):
    """Проверяет, что 4-й буллет отсекается до вызова модели и валидации,
    благодаря чему check_number_preservation не падает из-за отсутствия чисел 4-го буллета."""
    four_bullet_raw = (
        "Persistent energy inflation is breaking rate-cut bets:\n\n"
        "• <b>1. Fuel reserve drain:</b> SPR fell to historic lows under 350M barrels, removing Washington's primary tool to cap crude oil price spikes.\n\n"
        "• <b>2. Sovereign debt surge:</b> Deficit widened to $1.8T while yields hit 4.8%, driving interest servicing obligations past sustainable levels across global balance sheets.\n\n"
        "• <b>3. Corporate margin squeeze:</b> Refinancing costs rose by 150 bps as local banks passed wholesale funding pressure directly to private corporate borrowers.\n\n"
        "• <b>4. AI hardware commitments:</b> Cloud capex jumped to $52B annually, forcing permanent dependence on external funding.\n\n"
        "Higher baseline risk-free discount rates will force valuation multiple compression across tech and consumer equities."
    )

    def fake_editor_call(prompt, system_instruction=None, temperature=0.7, max_tokens=4096):
        # Редактор получает текст только с первыми тремя буллетами и сохраняет числа из них:
        assert "350M" in prompt
        assert "$1.8T" in prompt
        assert "4.8%" in prompt
        assert "150 bps" in prompt
        assert "$52B" not in prompt  # Число из 4-го пункта не должно передаваться редактору
        return (
            "Persistent energy inflation and unrelenting fiscal expansion are breaking the market's rate-cut bets across sovereign debt markets:\n\n"
            "1. Fuel reserve drain: SPR inventories fell to historic lows under 350M barrels, eliminating Washington's primary supply buffer to cap crude oil price spikes.\n\n"
            "2. Sovereign debt surge: The federal budget deficit widened to $1.8T while 10-year Treasury yields held near 4.8%, driving debt servicing obligations past sustainable operating levels.\n\n"
            "3. Corporate margin squeeze: Average corporate refinancing costs rose by 150 bps as commercial banks passed wholesale funding friction directly to corporate balance sheets.\n\n"
            "Higher baseline risk-free discount rates will keep debt servicing costs elevated, forcing prolonged valuation multiple compression across capital-intensive equities."
        )

    monkeypatch.setattr(brain, "call_gemini_api", fake_editor_call)
    res = brain.edit_and_humanize_draft(four_bullet_raw, draft_type="digest")

    # Валидация успешна, бейджа ошибки нет
    assert brain.FALLBACK_BADGE not in res
    assert "350M" in res
    assert "$52B" not in res


# ---------------------------------------------------------------- Weekly Phrase Ledger & Persona tests

import json
from pathlib import Path


def test_inspect_and_collect_flagged_phrases_writes_json(monkeypatch, tmp_path):
    target_json = tmp_path / "flagged_phrases.json"
    monkeypatch.setattr(brain, "FLAGGED_PHRASES_PATH", target_json)

    def fake_call(prompt, **kwargs):
        return '["quiet cleanup", "catching falling knives"]'

    monkeypatch.setattr(brain, "_call", fake_call)
    draft_body = "This is a test draft with quiet cleanup and catching falling knives."
    brain.inspect_and_collect_flagged_phrases(draft_body, "DRAFT 2 — SINGLE TOPIC")

    assert target_json.exists()
    data = json.loads(target_json.read_text(encoding="utf-8"))
    assert len(data) == 1
    record = data[0]
    assert record["draft_title"] == "DRAFT 2 — SINGLE TOPIC"
    assert record["flagged_phrases"] == ["quiet cleanup", "catching falling knives"]
    assert "quiet cleanup" in record["context_snippet"]
    assert "date" in record


def test_inspect_and_collect_flagged_phrases_appends_to_existing(monkeypatch, tmp_path):
    target_json = tmp_path / "flagged_phrases.json"
    initial_records = [{
        "date": "2026-10-01",
        "draft_title": "DRAFT 1 — DIGEST",
        "flagged_phrases": ["old phrase"],
        "context_snippet": "old snippet"
    }]
    target_json.write_text(json.dumps(initial_records), encoding="utf-8")
    monkeypatch.setattr(brain, "FLAGGED_PHRASES_PATH", target_json)

    monkeypatch.setattr(brain, "_call", lambda *a, **kw: '["new buzzword"]')
    brain.inspect_and_collect_flagged_phrases("Another draft text", "DRAFT 2 — SINGLE TOPIC")

    data = json.loads(target_json.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert data[0]["flagged_phrases"] == ["old phrase"]
    assert data[1]["flagged_phrases"] == ["new buzzword"]


def test_inspect_and_collect_flagged_phrases_empty_list_does_not_record(monkeypatch, tmp_path):
    target_json = tmp_path / "flagged_phrases.json"
    monkeypatch.setattr(brain, "FLAGGED_PHRASES_PATH", target_json)

    monkeypatch.setattr(brain, "_call", lambda *a, **kw: '[]')
    brain.inspect_and_collect_flagged_phrases("Clean and natural text", "DRAFT 1 — DIGEST")

    assert not target_json.exists()


def test_inspect_and_collect_flagged_phrases_empty_draft_early_return(monkeypatch):
    called = []
    monkeypatch.setattr(brain, "_call", lambda *a, **kw: called.append(True))
    brain.inspect_and_collect_flagged_phrases("", "DRAFT 1")
    brain.inspect_and_collect_flagged_phrases("   ", "DRAFT 2")
    assert len(called) == 0


def test_inspect_and_collect_flagged_phrases_failsafe_on_exception(monkeypatch, caplog):
    def exploding_call(*a, **kw):
        raise RuntimeError("API timeout or connection dropped")

    monkeypatch.setattr(brain, "_call", exploding_call)
    import logging
    with caplog.at_level(logging.WARNING):
        # Should not raise exception
        brain.inspect_and_collect_flagged_phrases("Draft text", "DRAFT 1 — DIGEST")

    assert "Weekly Phrase Ledger" in caplog.text


def test_inspect_and_collect_flagged_phrases_failsafe_on_invalid_json(monkeypatch, caplog):
    monkeypatch.setattr(brain, "_call", lambda *a, **kw: "invalid json string")
    import logging
    with caplog.at_level(logging.WARNING):
        brain.inspect_and_collect_flagged_phrases("Draft text", "DRAFT 1 — DIGEST")


def test_editor_humanizer_prompt_persona_and_no_vocabulary_guide():
    prompt = brain.EDITOR_HUMANIZER_PROMPT
    # Проверка новой ролевой установки студента финансов
    assert "Author Persona: A finance student passionate about macroeconomics, corporate finance, regulation, and big tech." in prompt
    assert "smart, calm, pragmatic observer" in prompt
    assert "catching falling knives" in prompt
    assert "who holds the bag" in prompt
    assert "retail trap" in prompt
    assert "As a student..." in prompt

    # Проверка строгого правила терминологической контекстности
    assert "STRICT CONTEXTUAL TERMINOLOGY: Use only the financial and operational concepts that directly describe the actual event." in prompt

    # Проверка отсутствия старой таблицы vocabulary guide и навязанных терминов
    assert "Vocabulary Guide" not in prompt
    assert "| Banned Fluff" not in prompt
    assert "circular capex loop" not in prompt
    assert "take-or-pay" not in prompt


def test_draft_prompt_persona_and_no_vocabulary_guide():
    prompt = brain.DRAFT_PROMPT
    assert "Author: A finance student passionate about macroeconomics, corporate finance, regulation, and big tech." in prompt
    assert "smart, calm, pragmatic observer" in prompt
    assert "catching falling knives" in prompt
    assert "who holds the bag" in prompt
    assert "retail trap" in prompt

    assert "Use only the financial and operational concepts that directly describe the actual event." in prompt
    assert "Vocabulary Guide" not in prompt
    assert "| Banned Fluff" not in prompt
    assert "circular capex loop" not in prompt


def test_hugs_workflow_prompts_persona_and_no_vocabulary_guide():
    from src import hugs_workflow
    for prompt in (hugs_workflow.EDITOR_HUMANIZER_PROMPT, hugs_workflow.HUGS_ANALYSIS_PROMPT):
        assert "finance student passionate about macroeconomics, corporate finance, regulation, and big tech" in prompt
        assert "smart, calm, pragmatic observer" in prompt
        assert "catching falling knives" in prompt
        assert "who holds the bag" in prompt
        assert "retail trap" in prompt
        assert "Use only the financial and operational concepts that directly describe the actual event." in prompt
        assert "Vocabulary Guide" not in prompt
        assert "| Banned Fluff" not in prompt
        assert "circular capex loop" not in prompt




