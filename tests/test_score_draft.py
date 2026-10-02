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
    # Single topic (95-135 words)
    words_90 = "word " * 89 + "$100."
    ok, _ = score_draft.check_length(words_90, words_90.lower(), shape="single")
    assert not ok

    words_95 = "word " * 94 + "$100."
    ok, _ = score_draft.check_length(words_95, words_95.lower(), shape="single")
    assert ok

    words_105 = "word " * 104 + "$100."
    ok, _ = score_draft.check_length(words_105, words_105.lower(), shape="single")
    assert ok

    words_135 = "word " * 134 + "$100."
    ok, _ = score_draft.check_length(words_135, words_135.lower(), shape="single")
    assert ok

    words_140 = "word " * 139 + "$100."
    ok, _ = score_draft.check_length(words_140, words_140.lower(), shape="single")
    assert not ok

    # Digest (105-140 words)
    prefix_d = "Header:\n• 1. Item one: "
    mid_d = "\n• 2. Item two: "
    end_d = " $100."
    fixed_count = len((prefix_d + mid_d + end_d).split())

    digest_100 = prefix_d + " ".join(["word"] * (100 - fixed_count)) + mid_d + end_d
    assert len(digest_100.split()) == 100
    ok, _ = score_draft.check_length(digest_100, digest_100.lower(), shape="digest")
    assert not ok

    digest_105 = prefix_d + " ".join(["word"] * (105 - fixed_count)) + mid_d + end_d
    assert len(digest_105.split()) == 105
    ok, _ = score_draft.check_length(digest_105, digest_105.lower(), shape="digest")
    assert ok

    digest_115 = prefix_d + " ".join(["word"] * (115 - fixed_count)) + mid_d + end_d
    assert len(digest_115.split()) == 115
    ok, _ = score_draft.check_length(digest_115, digest_115.lower(), shape="digest")
    assert ok

    digest_140 = prefix_d + " ".join(["word"] * (140 - fixed_count)) + mid_d + end_d
    assert len(digest_140.split()) == 140
    ok, _ = score_draft.check_length(digest_140, digest_140.lower(), shape="digest")
    assert ok

    digest_145 = prefix_d + " ".join(["word"] * (145 - fixed_count)) + mid_d + end_d
    assert len(digest_145.split()) == 145
    ok, _ = score_draft.check_length(digest_145, digest_145.lower(), shape="digest")
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


def test_check_truncation_additional_rules():
    # Valid endings
    assert score_draft.check_truncation("Text ending with dot.", "text ending with dot.")[0] is True
    assert score_draft.check_truncation("Text ending with exclamation!", "text ending with exclamation!")[0] is True
    assert score_draft.check_truncation('Text ending with quote."', 'text ending with quote."')[0] is True
    assert score_draft.check_truncation('Text ending with question mark?', 'text ending with question mark?')[0] is True

    # Invalid endings (FAIL)
    fail_no_punct = score_draft.check_truncation("Text ending abruptly without punctuation", "text ending abruptly without punctuation")
    assert fail_no_punct[0] is False
    assert "закрывающей пунктуацией" in fail_no_punct[1]

    fail_comma = score_draft.check_truncation("Text ending with a comma,", "text ending with a comma,")
    assert fail_comma[0] is False

    fail_colon = score_draft.check_truncation("Text ending with a colon:", "text ending with a colon:")
    assert fail_colon[0] is False


def test_anti_template_bleed_new_cases():
    bleed_1 = "Hyperscale tech is running into an insurance wall as syndicates pull capacity."
    ok, detail = score_draft.check_anti_template_bleed(bleed_1, bleed_1.lower())
    assert not ok
    assert "template bleed" in detail

    bleed_2 = "Persistent energy inflation and heavy debt issuance are breaking the market's rate-cut bets across G10."
    ok, detail = score_draft.check_anti_template_bleed(bleed_2, bleed_2.lower())
    assert not ok
    assert "template bleed" in detail

    clean_text = "European defense contractors are locking in multi-year procurement backlogs."
    ok, detail = score_draft.check_anti_template_bleed(clean_text, clean_text.lower())
    assert ok
    assert detail == "нет"


def test_new_banned_words_local_checker():
    new_banned = [
        ("The math is simple: expenses exceed revenues.", "the math is simple"),
        ("Fuel security is tight across Central Europe.", "fuel security is tight"),
        ("Liquidity is drying up in secondary corporate credit.", "liquidity is drying up"),
        ("Expect de-rating if growth slows below 2%.", "expect de-rating if growth slows"),
        ("If margins contract, earnings get crushed in H2.", "earnings get crushed"),
    ]
    for text, target in new_banned:
        ok, detail = score_draft.check_banned_words(text, text.lower())
        assert not ok, f"Expected '{text}' to fail banned words on '{target}'"
        assert target in detail




def test_multiparagraph_draft_with_blank_lines_passes_length_and_local_checks():
    # Two/three-part structure with \n\n blank lines separating paragraphs
    post = (
        "Broadcom isn't just selling AI chips to Anthropic. It’s lending them the money to buy them.\n\n"
        "Broadcom opened a $42 billion credit line to cover a third of Anthropic’s $125 billion TPU order. "
        "The mechanic is simple: Broadcom books massive silicon sales today, but takes all the customer credit risk directly onto its own balance sheet. "
        "If enterprise software monetization stalls, that loan doesn't vanish—Broadcom eats the write-down.\n\n"
        "Credit desks already see the trap. Surging debt insurance costs prove that bondholders will not ignore heavy borrowing used to subsidize cloud hardware.\n\n"
        "Stock investors are still chasing sales headlines. Bond investors have already started pricing in loan default risk."
    )
    words = post.split()
    assert 95 <= len(words) <= 135
    ok, msg = score_draft.check_length(post, post.lower(), shape="single")
    assert ok, f"Length check failed: {msg}"
    ok_trunc, msg_trunc = score_draft.check_truncation(post, post.lower())
    assert ok_trunc, f"Truncation check failed: {msg_trunc}"
    results, passed = score_draft.run_local_checks(post, shape="single")
    assert passed, f"Local checks failed: {[(n, d) for n, o, d in results if not o]}"


def test_check_number_preservation_identical():
    s1 = "Broadcom opened a $42 billion credit line covering $125.2 billion order with 5.1% yield."
    s2 = "Covering $125.2 billion order, Broadcom opened a $42 billion credit facility at 5.1% yield."
    ok, msg = score_draft.check_number_preservation(s1, s2)
    assert ok
    assert msg == "ок"


def test_check_number_preservation_mutated_number():
    s1 = "Broadcom opened a $42 billion credit line."
    s2 = "Broadcom opened a $24 billion credit line."
    ok, msg = score_draft.check_number_preservation(s1, s2)
    assert not ok
    assert "Stage 1" in msg
    assert "42" in msg


def test_check_number_preservation_lost_number():
    s1 = "Broadcom committed $42 billion and $125.2 billion across 5 facilities."
    s2 = "Broadcom committed $42 billion across 5 facilities."
    ok, msg = score_draft.check_number_preservation(s1, s2)
    assert not ok
    assert "125.2" in msg


def test_check_number_preservation_percentage_to_bps():
    s1 = "Spreads widened by 0.5% across primary issuance."
    s2 = "Spreads widened by 50 bps across primary issuance."
    ok, msg = score_draft.check_number_preservation(s1, s2)
    assert ok


def test_check_number_preservation_word_numbers():
    s1 = "The facility matures in 30 years with 2 tranches."
    s2 = "The facility matures in thirty years with two tranches."
    ok, msg = score_draft.check_number_preservation(s1, s2)
    assert ok


def test_check_number_preservation_ranges():
    s1 = "Capex is projected at 20 to 22 billion PLN, absorbing 40% to 45% of free cash flow."
    s2 = "Projected capex reaches up to 22 billion PLN, taking 45% of free cash flow."
    ok, msg = score_draft.check_number_preservation(s1, s2)
    assert ok


def test_check_number_preservation_comma_decimal():
    s1 = "Net profit reached 15,8 mld PLN with 22% ROE."
    s2 = "Net profit was 15.8 billion PLN with 22% return on equity."
    ok, msg = score_draft.check_number_preservation(s1, s2)
    assert ok

