"""Mechanical detector for AI cadence in English LinkedIn drafts.

Standalone CLI tool: checks closed list of cadence markers, marker density
per 1000 words, sentence length variation (burstiness CV), and typographic dashes.
Pure standard library Python 3. No dependencies on src/ or external style files.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# Windows console encoding fix: prevent UnicodeEncodeError and decode piped text as utf-8
for stream in (sys.stdout, sys.stderr, sys.stdin):
    if stream and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# TODO: calibrate threshold against real posts once style/my_posts.md is populated
DEFAULT_THRESHOLD: float = 8.0

PARALLEL_PAIR_EXCLUSIONS: frozenset[str] = frozenset({
    "and", "but", "so", "or", "yet", "for", "nor",
    "this", "that", "these", "those",
    "it", "its", "here", "there",
    "the", "a", "an",
    "i", "we", "you", "he", "she", "they",
    "if", "when", "as", "while", "in", "on", "at", "to", "by", "with", "from", "of",
})

CATEGORY_A_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("not_x_but_y", re.compile(r"\bnot\s+([^,.;!?\n]{1,60}?),\s*but\s+([^,.;!?\n]{1,60}?)\b", re.IGNORECASE)),
    ("split_antithesis", re.compile(r"\b(?:it's|it\s+is|that's|that\s+is|this\s+is)\s+not\s+([^.!?\n]{1,60}?)[.!?]\s*(?:it's|it\s+is|that's|that\s+is|this\s+is)\s+([^.!?\n]{1,60}?)\b", re.IGNORECASE)),
    ("inversion_y_not_x", re.compile(r"\b(?!(?:however|unfortunately|obviously|clearly|perhaps|maybe|surely|certainly|indeed|well|yes|no)\b)([a-zA-Z0-9_-]+(?:\s+[a-zA-Z0-9_-]+)?),\s+not\s+([a-zA-Z0-9_-]+(?:\s+[a-zA-Z0-9_-]+)?)\b", re.IGNORECASE)),
    ("mirror_antithesis", re.compile(r"\b(you're|you\s+are|we're|we\s+are|they're|they\s+are)\s+not\s+([^,.;!?\n]{1,60}?),\s*\1\s+([^,.;!?\n]{1,60}?)\b", re.IGNORECASE)),
)

CATEGORY_C_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("in_todays_world", re.compile(r"\bin today's (?:fast-paced )?world\b", re.IGNORECASE)),
    ("the_reality_is", re.compile(r"\bthe reality is(?: that)?\b", re.IGNORECASE)),
    ("its_worth_noting", re.compile(r"\bit(?:'s|\s+is) worth noting(?: that)?\b", re.IGNORECASE)),
    ("picture_this", re.compile(r"\bpicture this\b", re.IGNORECASE)),
    ("lets_dive_in", re.compile(r"\blet's dive (?:in|deep)\b", re.IGNORECASE)),
    ("this_changes_everything", re.compile(r"\bthis changes everything\b", re.IGNORECASE)),
    ("a_new_era_of", re.compile(r"\ba new era of\b", re.IGNORECASE)),
    ("at_the_end_of_the_day", re.compile(r"\bat the end of the day\b", re.IGNORECASE)),
    ("game_changer", re.compile(r"\bgame[- ]changer\b", re.IGNORECASE)),
    ("testament_to", re.compile(r"\btestament to\b", re.IGNORECASE)),
    ("beacon_of", re.compile(r"\bbeacon of\b", re.IGNORECASE)),
    ("double_edged_sword", re.compile(r"\bdouble-edged sword\b", re.IGNORECASE)),
    ("only_time_will_tell", re.compile(r"\bonly time will tell\b", re.IGNORECASE)),
)

CATEGORY_D_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("moreover", re.compile(r"\bmoreover\b", re.IGNORECASE)),
    ("furthermore", re.compile(r"\bfurthermore\b", re.IGNORECASE)),
    ("delve_into", re.compile(r"\bdelve(?:s|d|ing)?(?:\s+into)?\b", re.IGNORECASE)),
    ("leverage", re.compile(r"\bleverage(?:s|d|ing)?\b", re.IGNORECASE)),
    ("robust", re.compile(r"\brobust\b", re.IGNORECASE)),
    ("comprehensive", re.compile(r"\bcomprehensive\b", re.IGNORECASE)),
    ("landscape", re.compile(r"\blandscape\b", re.IGNORECASE)),
    ("journey", re.compile(r"\bjourney\b", re.IGNORECASE)),
    ("realm", re.compile(r"\brealm\b", re.IGNORECASE)),
    ("tapestry", re.compile(r"\btapestry\b", re.IGNORECASE)),
    ("seamless", re.compile(r"\bseamless(?:ly)?\b", re.IGNORECASE)),
)

CATEGORY_E_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("heres_the_thing", re.compile(r"\bhere(?:'s|\s+is) the thing\b", re.IGNORECASE)),
    ("heres_the_kicker", re.compile(r"\b(?:and\s+)?here(?:'s|\s+is) the kicker\b", re.IGNORECASE)),
    ("lets_break_down", re.compile(r"\blet's break (?:this|it)?\s*down\b", re.IGNORECASE)),
    ("in_this_post_youll_learn", re.compile(r"\bin this post(?:,)? you(?:'ll|\s+will) learn\b", re.IGNORECASE)),
    ("a_few_numbers_stand_out", re.compile(r"\ba few numbers stand out\b", re.IGNORECASE)),
    ("the_common_view_is_that", re.compile(r"\bthe common view is that\b", re.IGNORECASE)),
    ("what_most_people_get_wrong", re.compile(r"\bwhat most people get wrong\b", re.IGNORECASE)),
    ("why_does_this_matter", re.compile(r"\bwhy does this matter\??\b", re.IGNORECASE)),
    ("read_that_again", re.compile(r"\bread that again\b", re.IGNORECASE)),
)

DASH_RE = re.compile(r"[—–]")
ABBREV_RE = re.compile(r"\b(?:e\.g|i\.e|u\.s|u\.k|vs|dr|mr|mrs|ms)\.$", re.IGNORECASE)
RAW_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"\'\u201c\u2014\-])')


@dataclass
class Finding:
    category: str
    pattern_name: str
    matched_text: str
    line_num: int = 1


@dataclass
class CadenceReport:
    word_count: int
    sentence_count: int
    burstiness_cv: float
    findings: list[Finding] = field(default_factory=list)
    dash_findings: list[Finding] = field(default_factory=list)
    threshold: float = DEFAULT_THRESHOLD

    @property
    def density_count(self) -> int:
        """Count of density-contributing markers (A, B, C, D, E, G)."""
        return len(self.findings)

    @property
    def density_per_1000(self) -> float:
        if self.word_count == 0:
            return 0.0
        return (self.density_count / self.word_count) * 1000.0

    @property
    def has_dashes(self) -> bool:
        return len(self.dash_findings) > 0

    @property
    def verdict(self) -> str:
        if self.has_dashes or self.density_per_1000 > self.threshold:
            return "REVIEW"
        return "CLEAN"

    @property
    def exit_code(self) -> int:
        return 1 if self.verdict == "REVIEW" else 0

    def findings_by_category(self, cat: str) -> list[Finding]:
        return [f for f in self.findings if f.category == cat]

    def format_text(self) -> str:
        lines: list[str] = []
        bar = "=" * 70
        sub_bar = "-" * 70

        lines.append(bar)
        lines.append(
            f"AI CADENCE REPORT: {self.verdict} "
            f"(density: {self.density_per_1000:.1f} / 1000 words, threshold: {self.threshold:.1f})"
        )
        lines.append(bar)
        lines.append("Text statistics:")
        lines.append(f"  Words:                {self.word_count}")
        lines.append(f"  Sentences:            {self.sentence_count}")
        lines.append(f"  Sentence length CV:   {self.burstiness_cv:.2f} (burstiness)")
        lines.append("")

        dash_status = "TRIGGERED REVIEW" if self.has_dashes else "PASS"
        lines.append("Typographic hard flag:")
        lines.append(f"  F) Em/en-dashes (—/–): {len(self.dash_findings)} [{dash_status}]")
        lines.append("")

        counts = {
            "A": len(self.findings_by_category("A")),
            "B": len(self.findings_by_category("B")),
            "C": len(self.findings_by_category("C")),
            "D": len(self.findings_by_category("D")),
            "E": len(self.findings_by_category("E")),
            "G": len(self.findings_by_category("G")),
        }

        lines.append(f"Marker summary (threshold: {self.threshold:.1f} / 1000 words):")
        lines.append(f"  A) Antitheses:                {counts['A']}")
        lines.append(f"  B) Single-word chains (3+):   {counts['B']}")
        lines.append(f"  C) Clichés / Pathos:          {counts['C']}")
        lines.append(f"  D) Bureaucratese / Connectors:{counts['D']}")
        lines.append(f"  E) Impersonal hooks:          {counts['E']}")
        lines.append(f"  G) Parallel pairs:            {counts['G']}")
        lines.append(f"  {sub_bar}")
        lines.append(f"  Total density markers:        {self.density_count}")
        lines.append(f"  Calculated density:           {self.density_per_1000:.1f} / 1000 words")
        lines.append("")

        category_labels = {
            "A": "Antitheses",
            "B": "Single-word chains",
            "C": "Clichés / Pathos",
            "D": "Bureaucratese / Connectors",
            "E": "Impersonal hooks",
            "G": "Parallel pairs",
        }

        has_any_findings = bool(self.findings or self.dash_findings)
        if has_any_findings:
            lines.append("Findings (up to 5 per category):")
            for cat in ("A", "B", "C", "D", "E", "G"):
                cat_items = self.findings_by_category(cat)
                if not cat_items:
                    continue
                label = category_labels.get(cat, cat)
                lines.append(f"  [{cat}] {label} ({len(cat_items)}):")
                for item in cat_items[:5]:
                    lines.append(f'    - L{item.line_num}: "{item.matched_text}"')
                if len(cat_items) > 5:
                    lines.append(f"    ... and {len(cat_items) - 5} more")

            if self.dash_findings:
                lines.append(f"  [F] Typographic dashes ({len(self.dash_findings)}):")
                for item in self.dash_findings[:5]:
                    lines.append(f'    - L{item.line_num}: "{item.matched_text}"')
                if len(self.dash_findings) > 5:
                    lines.append(f"    ... and {len(self.dash_findings) - 5} more")
            lines.append("")

        lines.append(bar)
        lines.append(f"VERDICT: {self.verdict} (exit code: {self.exit_code})")
        lines.append(bar)
        return "\n".join(lines)


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, respecting paragraph breaks and abbreviations."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    sentences: list[str] = []
    for p in paragraphs:
        tokens = RAW_SENTENCE_SPLIT_RE.split(p)
        merged: list[str] = []
        for tok in tokens:
            tok = tok.strip()
            if not tok:
                continue
            if merged and ABBREV_RE.search(merged[-1]):
                merged[-1] = merged[-1] + " " + tok
            else:
                merged.append(tok)
        sentences.extend(merged)
    return sentences


def is_single_word_sentence(s: str) -> bool:
    """Return True if sentence consists of exactly 1 word token."""
    clean = re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", s)
    words = re.findall(r"\b[A-Za-z0-9]+(?:'[A-Za-z]+)?\b", clean)
    return len(words) == 1


def calculate_burstiness(sentences: Sequence[str]) -> float:
    """Calculate Coefficient of Variation (CV = std / mean) of sentence lengths in words."""
    lengths = [len(s.split()) for s in sentences if s.strip()]
    if len(lengths) < 2:
        return 0.0
    mean = sum(lengths) / len(lengths)
    if mean <= 0.0:
        return 0.0
    variance = sum((x - mean) ** 2 for x in lengths) / len(lengths)
    return math.sqrt(variance) / mean


def check_cadence(text: str, threshold: float = DEFAULT_THRESHOLD) -> CadenceReport:
    """Analyze English draft text for AI cadence markers, density, CV, and typographic dashes."""
    words = text.split()
    word_count = len(words)
    sentences = split_sentences(text)
    sentence_count = len(sentences)
    burstiness_cv = calculate_burstiness(sentences)

    findings: list[Finding] = []
    dash_findings: list[Finding] = []

    def get_line_num(pos: int) -> int:
        return text[:pos].count("\n") + 1

    # F) Hard flag typographic dashes (em-dash / en-dash)
    for m in DASH_RE.finditer(text):
        line = get_line_num(m.start())
        start = max(0, m.start() - 20)
        end = min(len(text), m.end() + 20)
        snippet = text[start:end].replace("\n", " ").strip()
        dash_findings.append(Finding("F", "em_or_en_dash", f"...{snippet}...", line))

    # A) Antitheses
    for name, pat in CATEGORY_A_PATTERNS:
        for m in pat.finditer(text):
            line = get_line_num(m.start())
            findings.append(Finding("A", name, m.group(0).strip(), line))

    # B) Single-word sentences chained (3+ in a row)
    i = 0
    while i < len(sentences):
        if is_single_word_sentence(sentences[i]):
            run = [sentences[i]]
            j = i + 1
            while j < len(sentences) and is_single_word_sentence(sentences[j]):
                run.append(sentences[j])
                j += 1
            if len(run) >= 3:
                chain_text = " ".join(run)
                # Find line number of the first sentence in chain
                m_chain = re.search(re.escape(run[0]), text)
                line = get_line_num(m_chain.start()) if m_chain else 1
                findings.append(Finding("B", "single_word_chain", chain_text, line))
            i = j
        else:
            i += 1

    # C) Clichés / Pathos
    for name, pat in CATEGORY_C_PATTERNS:
        for m in pat.finditer(text):
            line = get_line_num(m.start())
            findings.append(Finding("C", name, m.group(0).strip(), line))

    # D) Bureaucratese / Connectors
    for name, pat in CATEGORY_D_PATTERNS:
        for m in pat.finditer(text):
            line = get_line_num(m.start())
            findings.append(Finding("D", name, m.group(0).strip(), line))

    # E) Impersonal hooks / formulaic starts
    for name, pat in CATEGORY_E_PATTERNS:
        for m in pat.finditer(text):
            line = get_line_num(m.start())
            findings.append(Finding("E", name, m.group(0).strip(), line))

    # G) Parallel pairs (two consecutive sentences with identical start word)
    for k in range(len(sentences) - 1):
        s1 = sentences[k]
        s2 = sentences[k + 1]
        c1 = re.sub(r"^[^a-zA-Z]+", "", s1)
        c2 = re.sub(r"^[^a-zA-Z]+", "", s2)
        m1 = re.match(r"^([a-zA-Z]+)", c1)
        m2 = re.match(r"^([a-zA-Z]+)", c2)
        if m1 and m2:
            w1 = m1.group(1).lower()
            w2 = m2.group(1).lower()
            if w1 == w2 and w1 not in PARALLEL_PAIR_EXCLUSIONS:
                m_pair = re.search(re.escape(s1), text)
                line = get_line_num(m_pair.start()) if m_pair else 1
                snippet_1 = s1 if len(s1) <= 40 else s1[:37] + "..."
                snippet_2 = s2 if len(s2) <= 40 else s2[:37] + "..."
                pair_repr = f"'{w1}': \"{snippet_1}\" / \"{snippet_2}\""
                findings.append(Finding("G", "parallel_pair", pair_repr, line))

    return CadenceReport(
        word_count=word_count,
        sentence_count=sentence_count,
        burstiness_cv=burstiness_cv,
        findings=findings,
        dash_findings=dash_findings,
        threshold=threshold,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mechanical detector for AI cadence in English LinkedIn drafts."
    )
    parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Path to draft text file. If omitted or '-', reads from stdin.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Density threshold per 1000 words (default: {DEFAULT_THRESHOLD}).",
    )

    args = parser.parse_args(argv)

    if args.file and args.file != "-":
        p = Path(args.file)
        if not p.is_file():
            sys.stderr.write(f"Error: file not found: {args.file}\n")
            return 2
        text = p.read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty() and not args.file:
            parser.print_help()
            sys.stderr.write("\nError: no input file provided and stdin is empty.\n")
            return 2
        text = sys.stdin.read()

    report = check_cadence(text, threshold=args.threshold)
    sys.stdout.write(report.format_text() + "\n")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
