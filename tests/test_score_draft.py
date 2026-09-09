from __future__ import annotations

import pytest

from src import score_draft


def test_banned_words_detects_cliches():
    text = "The plumbing of these structured products ensures that the house always wins."
    low = text.lower()
    ok, detail = score_draft.check_banned_words(text, low)
    assert not ok
    assert "plumbing" in detail
    assert "the house always wins" in detail


def test_banned_words_passes_institutional_text():
    text = "Hyperscalers face severe balance-sheet concentration risk and contractual fixed-charge burdens."
    low = text.lower()
    ok, detail = score_draft.check_banned_words(text, low)
    assert ok
    assert detail == "нет"


def test_banned_openers_detects_bad_hooks():
    bad_hooks = [
        "What caught my eye this week was Poland's debt print.",
        "A few developments caught my eye during the morning session.",
        "These two numbers sit oddly next to each other in Eurostat.",
        "Many investors assume that state dividend payouts are guaranteed.",
        "Most retail investors think tech giants hold standard insurance.",
        "Everyone is watching AI chips, but the real issue is power.",
        "In today's volatile market, liquidity is drying up.",
        "It is no secret that European banks are hoarding cash.",
    ]
    for hook in bad_hooks:
        ok, detail = score_draft.check_banned_openers(hook, hook.lower())
        assert not ok, f"Expected '{hook}' to fail opener check"
        assert "запрещённый зачин" in detail


def test_banned_openers_passes_pure_data_hook():
    good_hook = "Poland’s credit-to-deposit ratio sits at 57.7% against an EU-27 median of 106.1%."
    ok, detail = score_draft.check_banned_openers(good_hook, good_hook.lower())
    assert ok
    assert detail == "нет"


def test_no_closing_question():
    bad_ending = "With spreads tightening, will private credit funds absorb this duration risk?"
    ok, detail = score_draft.check_no_closing_question(bad_ending, bad_ending.lower())
    assert not ok
    assert "заканчивается вопросом" in detail

    good_ending = "Ignoring footnote commitments misprices the true enterprise cost of capital."
    ok, detail = score_draft.check_no_closing_question(good_ending, good_ending.lower())
    assert ok
    assert detail == "ок"


def test_role_phrases_detects_amateur_framing():
    bad_texts = [
        "As a student following banking, this transaction looks unusual.",
        "As someone analyzing asset management, this divergence matters.",
        "It makes you wonder how long this yield curve inversion holds.",
        "Time will tell if these commitments hit equity multiples.",
    ]
    for text in bad_texts:
        ok, detail = score_draft.check_role_phrases(text, text.lower())
        assert not ok, f"Expected '{text}' to fail role phrases check"


def test_banned_words_detects_institutional_throat_clearing():
    text = "Bottleneck is emerging as a structural shift consequently far exceeding capacity."
    low = text.lower()
    ok, detail = score_draft.check_banned_words(text, low)
    assert not ok
    assert "is emerging as" in detail
    assert "structural shift" in detail
    assert "consequently" in detail
    assert "far exceeding" in detail


def test_full_institutional_draft_passes_local_checks():
    draft = (
        "The pristine balance sheets of Big Tech are masking a strategic move from debt capital to off-balance-sheet operating leverage.\n\n"
        "Alphabet, Amazon, Meta, and Microsoft currently hold over $2.4 trillion in total contractual commitments. "
        "Because these obligations take the form of long-term power purchase agreements, colocation capacity contracts, and land reservations, "
        "they bypass headline balance-sheet debt metrics.\n\n"
        "Yet economically, they carry the exact same credit risk as senior secured debt: they are non-cancellable, multi-decade cash outflow mandates, "
        "and they subordinate common equity holders by creating a massive, senior fixed-charge burden against future operating cash flow.\n\n"
        "When hyperscalers commit trillions off-balance-sheet to lock in physical energy and compute real estate, they are trading operational flexibility for capacity certainty. "
        "When modeling terminal tech cash flows, ignoring footnote commitments misprices the true enterprise cost of capital."
    )
    results, passed = score_draft.run_local_checks(draft)
    assert passed, f"Checks failed: {[(name, detail) for name, ok, detail in results if not ok]}"
