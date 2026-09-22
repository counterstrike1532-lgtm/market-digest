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
        "Hyperscale tech balance sheets are masking a strategic move from debt capital to off-balance-sheet operating leverage.\n\n"
        "Alphabet, Amazon, Meta, and Microsoft currently hold over $2.4 trillion in total contractual commitments. "
        "Because these obligations take the form of long-term power purchase agreements, colocation capacity contracts, and land reservations, "
        "they bypass headline balance-sheet debt metrics.\n\n"
        "Yet economically, they carry the exact same credit risk as senior secured debt: "
        "they are non-cancellable, multi-decade cash outflow mandates that subordinate equity holders through senior fixed-charge burdens.\n\n"
        "When hyperscalers commit trillions off-balance-sheet to secure power, they trade operational flexibility for capacity certainty. "
        "Ignoring footnote commitments misprices the true enterprise cost of capital."
    )
    results, passed = score_draft.run_local_checks(draft)
    assert passed, f"Checks failed: {[(name, detail) for name, ok, detail in results if not ok]}"


def test_word_count_gate_single_and_digest():
    # Single topic (100-120 words)
    words_90 = "word " * 89 + "$100."
    ok, _ = score_draft.check_length(words_90, words_90.lower(), shape="single")
    assert not ok

    words_105 = "word " * 104 + "$100."
    ok, _ = score_draft.check_length(words_105, words_105.lower(), shape="single")
    assert ok

    words_125 = "word " * 124 + "$100."
    ok, _ = score_draft.check_length(words_125, words_125.lower(), shape="single")
    assert not ok

    # Digest (110-130 words)
    digest_78 = "Header:\n• 1. Item one: " + "word " * 35 + "\n• 2. Item two: " + "word " * 35 + "$100."
    ok, _ = score_draft.check_length(digest_78, digest_78.lower(), shape="digest")
    assert not ok

    digest_115 = "Header:\n• 1. Item one: " + "word " * 54 + "\n• 2. Item two: " + "word " * 54 + "$100."
    ok, _ = score_draft.check_length(digest_115, digest_115.lower(), shape="digest")
    assert ok

    digest_135 = "Header:\n• 1. Item one: " + "word " * 64 + "\n• 2. Item two: " + "word " * 64 + "$100."
    ok, _ = score_draft.check_length(digest_135, digest_135.lower(), shape="digest")
    assert not ok


def test_truncation_gate():
    ok_dot = "The sovereign debt servicing cost climbed across a $40T debt load."
    ok, _ = score_draft.check_truncation(ok_dot, ok_dot.lower())
    assert ok

    ok_excl = "The sovereign debt servicing cost climbed across a $40T debt load!"
    ok, _ = score_draft.check_truncation(ok_excl, ok_excl.lower())
    assert ok

    fail_truncated = "The sovereign debt servicing cost climbed across a $40T debt load"
    ok, detail = score_draft.check_truncation(fail_truncated, fail_truncated.lower())
    assert not ok
    assert "закрывающей пунктуацией" in detail

    fail_comma = "The sovereign debt servicing cost climbed across a $40T debt load,"
    ok, _ = score_draft.check_truncation(fail_comma, fail_comma.lower())
    assert not ok


def test_digest_bullets_gate():
    digest_2 = "Macro thesis line:\n• 1. Defense capex: $10B deployed.\n• 2. Margin squeeze: spreads hit 2.5%."
    ok, _ = score_draft.check_digest_bullets(digest_2, digest_2.lower(), shape="digest")
    assert ok

    digest_3 = "Macro thesis line:\n• 1. Defense capex: $10B.\n• 2. Margin squeeze: 2.5%.\n• 3. Reserve drain: $5B."
    ok, _ = score_draft.check_digest_bullets(digest_3, digest_3.lower(), shape="digest")
    assert ok

    digest_4 = "Macro thesis line:\n• 1. One: $1B.\n• 2. Two: $2B.\n• 3. Three: $3B.\n• 4. Four: $4B."
    ok, detail = score_draft.check_digest_bullets(digest_4, digest_4.lower(), shape="digest")
    assert not ok
    assert "4 буллетов" in detail

    digest_1 = "Macro thesis line:\n• 1. Only one item: $1B."
    ok, _ = score_draft.check_digest_bullets(digest_1, digest_1.lower(), shape="digest")
    assert not ok


def test_anti_leak_gate():
    leaked_1 = "AI data centers are running into an insurance wall. Capital expenditure reached $50B."
    ok, detail = score_draft.check_anti_leak(leaked_1, leaked_1.lower())
    assert not ok
    assert "утечка из промпта" in detail

    leaked_2 = "Persistent energy inflation and heavy debt issuance are breaking the market's rate-cut bets: yields hit 4.8%."
    ok, detail = score_draft.check_anti_leak(leaked_2, leaked_2.lower())
    assert not ok
    assert "утечка из промпта" in detail

    fresh = "Convective storm clusters in northern Texas are capping insurance syndication for $50B compute hubs."
    ok, _ = score_draft.check_anti_leak(fresh, fresh.lower())
    assert ok


def test_expanded_banned_words():
    bad_samples = [
        ("The price: 50 million PLN.", "the price:"),
        ("This pushes borrowing costs higher across 10-year bonds.", "this pushes borrowing costs higher"),
        ("Supply chains remain tight for 5nm packaging.", "supply chains remain tight"),
        ("The problem is refinancing at 6% yields.", "the problem is"),
        ("The central bank is stepping in with $6B buybacks.", "the central bank is stepping in"),
        ("Hedge funds snapped up the debt at parity.", "snapped up the debt"),
        ("Taxpayers pay the bill for sovereign liabilities.", "pay the bill"),
        ("Expensive imports drove the current account deficit to $3B.", "expensive imports"),
        ("The fuel tax reduced operating cash flows by 4%.", "the fuel tax"),
        ("The bank squeeze tightened net interest margins to 2.1%.", "the bank squeeze"),
        ("When yields spike, corporate margins shrink rapidly.", "corporate margins shrink"),
        ("Expect de-rating if growth slows to 1%.", "expect de-rating if growth slows"),
        ("If margins drop, earnings get crushed in Q4.", "earnings get crushed"),
    ]
    for text, target in bad_samples:
        ok, detail = score_draft.check_banned_words(text, text.lower())
        assert not ok, f"Expected '{text}' to fail banned words on '{target}'"
        assert target in detail


def test_word_counter_leakage():
    # 3+ parenthetical counters between words -> FAIL
    bad_text = "The government is capping (75) daily (76) fuel (77) prices (78) at retail stations."
    ok, detail = score_draft.check_word_counter_leak(bad_text, bad_text.lower())
    assert not ok
    assert "Word Counter Leakage" in detail

    # Check full run_local_checks rejects it
    results, all_ok = score_draft.run_local_checks(bad_text)
    assert not all_ok
    leak_check = next(r for r in results if "Word Counter Leakage" in r[0])
    assert leak_check[1] is False
    assert "Word Counter Leakage" in leak_check[2]

    # Clean text without numbering -> PASS
    good_text = "The government is capping daily fuel prices at retail stations to stabilize consumer inflation."
    ok_good, detail_good = score_draft.check_word_counter_leak(good_text, good_text.lower())
    assert ok_good
    assert detail_good == "ок"

    # Normal text with 1 or 2 parenthetical numbers -> PASS
    normal_text = "Section (1) specifies that the fund (2) cannot invest in distressed debt."
    ok_normal, _ = score_draft.check_word_counter_leak(normal_text, normal_text.lower())
    assert ok_normal

