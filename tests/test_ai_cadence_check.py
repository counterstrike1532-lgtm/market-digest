"""Tests for tools.ai_cadence_check (EN-CADENCE).

Zero Gemini calls, zero dependencies on src/.
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

from tools.ai_cadence_check import (
    CadenceReport,
    check_cadence,
    main,
    split_sentences,
)


def test_category_a_antithesis_variants():
    # 1. not X, but Y
    r1 = check_cadence("It was not inflation, but wages driving the monthly increase.")
    a1 = r1.findings_by_category("A")
    assert len(a1) >= 1
    assert any(f.pattern_name == "not_x_but_y" for f in a1)

    # 2. split antithesis (It's not X. It's Y)
    r2 = check_cadence("It's not a market crash. It's a healthy correction.")
    a2 = r2.findings_by_category("A")
    assert len(a2) >= 1
    assert any(f.pattern_name == "split_antithesis" for f in a2)

    # 3. inversion (Y, not X)
    r3 = check_cadence("We saw disciplined capital allocation, not reckless growth.")
    a3 = r3.findings_by_category("A")
    assert len(a3) >= 1
    assert any(f.pattern_name == "inversion_y_not_x" for f in a3)

    # 4. mirror (you're not X, you're Y)
    r4 = check_cadence("You're not building a utility, you're building a network effect.")
    a4 = r4.findings_by_category("A")
    assert len(a4) >= 1
    assert any(f.pattern_name == "mirror_antithesis" for f in a4)


def test_category_b_single_word_chains():
    # 3 single-word sentences in a row -> flagged
    r_chain = check_cadence("Fast. Scalable. Robust. That is the architecture we chose.")
    b_findings = r_chain.findings_by_category("B")
    assert len(b_findings) == 1
    assert "Fast. Scalable. Robust." in b_findings[0].matched_text

    # Only 2 single-word sentences -> not flagged
    r_two = check_cadence("Fast. Scalable. The architecture handles peak traffic smoothly.")
    assert len(r_two.findings_by_category("B")) == 0


def test_category_c_cliches_pathos():
    text = (
        "In today's world, the reality is that energy prices dictate industrial policy. "
        "Picture this: factories shutting down overnight. Let's dive in."
    )
    r = check_cadence(text)
    c_findings = r.findings_by_category("C")
    assert len(c_findings) >= 3
    names = {f.pattern_name for f in c_findings}
    assert "in_todays_world" in names
    assert "the_reality_is" in names
    assert "picture_this" in names
    assert "lets_dive_in" in names


def test_category_d_bureaucratese_connectors():
    text = (
        "Moreover, the committee will delve into the regulatory landscape to leverage "
        "a robust framework for seamless cross-border settlement."
    )
    r = check_cadence(text)
    d_findings = r.findings_by_category("D")
    assert len(d_findings) >= 4
    names = {f.pattern_name for f in d_findings}
    assert "moreover" in names
    assert "delve_into" in names
    assert "landscape" in names
    assert "leverage" in names
    assert "robust" in names
    assert "seamless" in names


def test_category_e_impersonal_hooks():
    text = (
        "Here's the thing: let's break down why this quarterly result matters. "
        "In this post you'll learn how banks price credit risk."
    )
    r = check_cadence(text)
    e_findings = r.findings_by_category("E")
    assert len(e_findings) >= 3
    names = {f.pattern_name for f in e_findings}
    assert "heres_the_thing" in names
    assert "lets_break_down" in names
    assert "in_this_post_youll_learn" in names


def test_category_f_hard_flag_dashes():
    # Long text so density of other markers is 0, but em-dash triggers REVIEW
    clean_filler = (
        "The central bank held rates steady today at 5.75 percent. "
        "Most analysts expected this pause given recent core inflation figures. "
        "The governor confirmed policy will remain restrictive until quarterly data improves. "
        "Treasury bond yields were largely unchanged after the statement."
    )
    text_with_em_dash = clean_filler + " The margin was thin — just twenty basis points."
    r = check_cadence(text_with_em_dash)
    assert r.has_dashes is True
    assert len(r.dash_findings) == 1
    assert r.density_count == 0  # Dash is hard flag, not added to category density
    assert r.verdict == "REVIEW"
    assert r.exit_code == 1

    text_with_en_dash = clean_filler + " The range was 10–20 basis points."
    r_en = check_cadence(text_with_en_dash)
    assert r_en.has_dashes is True
    assert r_en.verdict == "REVIEW"
    assert r_en.exit_code == 1


def test_category_g_parallel_pairs():
    # Two consecutive sentences starting with the same non-excluded word
    text_parallel = (
        "Founders took the early risk. Founders built the core distribution channels. "
        "Now private equity firms want to acquire them."
    )
    r = check_cadence(text_parallel)
    g_findings = r.findings_by_category("G")
    assert len(g_findings) == 1
    assert "'founders'" in g_findings[0].matched_text

    # Consecutive sentences starting with excluded words (e.g. 'the', 'and', 'this') -> NOT flagged
    text_excluded = (
        "The index fell 20 points yesterday. The market recovered all losses this morning. "
        "And volume was high throughout the afternoon. And traders remained cautious."
    )
    r_ex = check_cadence(text_excluded)
    assert len(r_ex.findings_by_category("G")) == 0


def test_clean_text_gives_clean_verdict():
    clean_post = (
        "Poland's HICP came in at 4.2% while the euro area sat at 2.4%. "
        "I pulled both figures from Eurostat this morning and the gap is notable. "
        "Most commentary treats this as a single story about energy subsidies. "
        "Looking into unit labor costs suggests wage growth explains much of the difference. "
        "Services inflation in Warsaw continues running above six percent year on year. "
        "Unless private sector wage demands moderate, the monetary policy council will hold rates steady."
    )
    report = check_cadence(clean_post)
    assert report.verdict == "CLEAN"
    assert report.exit_code == 0
    assert report.has_dashes is False
    assert report.density_count == 0
    assert report.density_per_1000 == 0.0
    assert report.sentence_count >= 5
    assert report.burstiness_cv > 0.0


def test_burstiness_cv_calculation():
    # Sentences with identical lengths -> CV == 0.0
    uniform_text = "Alpha beta gamma. Delta epsilon zeta. Eta theta iota."
    r_uniform = check_cadence(uniform_text)
    assert r_uniform.burstiness_cv == 0.0

    # Sentences with varied lengths -> CV > 0.0
    varied_text = "Short sentence. This is a significantly longer sentence with many more words in it."
    r_varied = check_cadence(varied_text)
    assert r_varied.burstiness_cv > 0.4


def test_cli_contract_with_file_and_stdin(tmp_path: Path, monkeypatch):
    clean_text = (
        "The central bank held the benchmark interest rate steady today. "
        "Analysts broadly expected this outcome following yesterday's release."
    )
    clean_file = tmp_path / "clean_draft.txt"
    clean_file.write_text(clean_text, encoding="utf-8")

    # Run via main() with file argument
    code = main([str(clean_file)])
    assert code == 0

    # Run via main() with stdin
    monkeypatch.setattr(sys, "stdin", io.StringIO("In today's world, let's dive in. Picture this now."))
    code_review = main(["-"])
    assert code_review == 1
