"""Воркфлоу сбора, LLM-анализа и отправки постов HugsFund в Telegram.
Запуск:
    python -m src.hugs_workflow [--dry] [--hours 30] [--send]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from . import brain, deliver, verify
from .hugs_parser import HugsPost, fetch_channel_posts, filter_posts

log = logging.getLogger("hugs_workflow")

HUGS_DRAFT2_HISTORY_PATH = Path(__file__).resolve().parent.parent / "state" / "hugs_draft2_history.json"

EDITOR_HUMANIZER_PROMPT = """
You are an expert financial editor refining LinkedIn drafts.
Author Persona: A finance student passionate about macroeconomics, corporate finance, regulation, and big tech.
Tone: A smart, calm, pragmatic observer speaking in natural, living language with peers and colleagues.
Audience: Institutional investors, hedge fund analysts, founders, and portfolio managers.

TONE & PERSONA GUARDRAILS:
- NEVER impersonate a cynical Wall Street trader (strictly NO "catching falling knives", "who holds the bag", "retail trap", or casino trading slang).
- NEVER impersonate a boring professor (strictly NO 35-word academic constructions or bloated Latinate jargon).
- NEVER use fake self-reflection ("As a student...", "It makes you wonder...").

YOUR SOLE MISSION:
Rewrite the raw input draft into a readable, punchy, authentic human post. Eliminate all robotic stiffness, textbook bloat, and dense walls of text, while preserving 100% of the hard numbers and financial mechanics.

CRITICAL FORMATTING & READABILITY RULES:
1. TWO-PART STRUCTURE (NO MONOLITHS):
   - Every Single Topic post MUST be broken into 2–3 short, distinct paragraphs separated by blank lines.
   - Part 1 (Hook & Setup): The raw fact/transaction explained simply.
   - Part 2 (The Friction & Balance Sheet Risk): The operational tension, capital constraints, or balance-sheet friction.
   - Part 3 / Final Line: Natural analytical conclusion grounded in facts (balance-sheet divergence, regulatory consequence, or market divergence if supported by data).
   - Paragraphs must be 2–3 sentences max. Never produce a solid block of continuous text.

2. SIMPLE, CONVERSATIONAL VOCABULARY & STRICT CONTEXTUALITY:
   - Explain advanced mechanics in plain English.
   - STRICT CONTEXTUAL TERMINOLOGY: Use only the financial and operational concepts that directly describe the actual event. If the news is about server depreciation and capex gaps, discuss depreciation and capex gaps. NEVER shoehorn external buzzwords (like 'circular financing' or 'underwriting constraints') where they do not belong.
   - Ban 35-word academic run-on sentences. Alternate short punchy statements (3–6 words) with clear explanations.
   - NO scaffolding/announcements: ban "The logic is simple:", "The strategic play is clear:", "The mechanics are obvious:", "The mechanism is clear:", "introduces two critical...", "The takeaway is simple:". Jump straight into the fact.
   - FIRST-PERSON ALLOWANCE: Natural student researcher perspective ("I", "my") is permitted ("Looking at the filings, what stands out is..."). Strictly no fake trading desk claims.
   - STRICT TONE CEILING: Strip out directive preaching ("must now", "executive teams must", "will ultimately force", "pivoting permanently"). Frame as analytical reality ("This leaves CFOs with...", "It raises the hurdle rate for...").
   - LEXICAL HYGIENE: "balance sheet" strictly at most once per post. Avoid antitheses ("isn't just", "is not just... it is").
   - NO telegraphic fragments ("Fuel security is tight.", "Liquidity is drying up.", "The price: WIBOR...").

3. NATURAL FACT-BASED CLOSINGS (BAN ARTIFICIAL DRAMA):
   - End on a natural analytical conclusion from facts: clean balance-sheet difference (e.g. payback requires 59% revenue growth), regulatory consequence (e.g. state commercial banks absorbing bad assets), or market divergence only if directly supported by data.
   - NEVER force artificial drama or repetitive formulas like "...forcing valuation multiple compression".

4. HARD WORD COUNT DISCIPLINE:
   - Single Topic: STRICTLY 105–115 words (allowable: 95–135 words).
   - Digest: STRICTLY 115–125 words (allowable: 105–140 words) (strictly 2–3 bullets with action headers like "1. Buying your own customer:").
   - Every post MUST end with complete closing punctuation (. or !). Never cut off mid-thought.

5. ACCOUNTING CLASSIFICATION, CAUSAL EVIDENCE & ANTI-ABSOLUTISM:
   - ACCOUNTING CLASSIFICATION CHECK (NO TERM CONFLATION): Strictly distinguish and NEVER conflate or swap:
     * Contractual commitments / backlog != funded debt (cloud commitments or orders are NOT balance-sheet debt and NOT guaranteed cash).
     * Debt guarantee / contingent exposure != loan (a guarantee is a contingent liability, NOT a funded loan).
     * Net borrowing / budget deficit != gross bond issuance (net deficit borrowing != total gross securities issuance).
     * GAAP net loss != operating cash burn (separate non-cash mark-to-market revaluations of convertible bonds or derivatives from operating cash flow).
     * Secondary market bond yield != coupon / current interest expense (secondary market yield reflects risk pricing, NOT the interest rate on already-issued loans/bonds).
     * Gross mortgage originations != net credit growth (origination volume including refinancing does NOT prove net loan book growth).
     * Liabilities of non-monetary financial institutions (leasing/factoring) != corporate capex debt (never equate intermediary liabilities with a specific asset class without data).
   - CAUSAL EVIDENCE REQUIREMENT & NO LEGAL PROPHECIES:
     * Forbidden to make causal leaps to WACC, ROIC, NIM, or deposit rates if the source lacks the intermediate link. When the causal chain is incomplete, use probabilistic phrasing ("Whether this narrows margins depends on...", "adds upward pressure on...").
     * Preserve exact event status from the draft/source: if it says considers / proposed / draft, never escalate to negotiating / introduces / launched.
     * Categorically forbidden to make legal prophecies about EU court or national regulator rulings (never write "will no longer hold up under EU law" or "zero judicial recourse"). Describe only the specific evidentiary standard and its impact on corporate liquidity or provisions.
   - BAN ON FALSE ABSOLUTISM, CLICHÉ OPENERS & DIGEST COHERENCE:
     * Strip out deterministic intensifiers: inevitably, permanently, entirely, directly, effectively, immediately, fundamentally, will force, no longer.
     * Strip out cliché openers: "Most investors assume", "Many investors assume", "Everyone is watching", "These two numbers sit oddly next to each other".
     * DIGEST COHERENCE: Combine 2–3 stories into a digest ONLY when they share a single concrete financial mechanism. Strictly forbidden to stitch unrelated stories together via generic filler about "cost of capital".

OUTPUT FORMAT:
Return ONLY the final edited post text. No introductory remarks, no quotes, no word counts in brackets.
"""

HUGS_ANALYSIS_PROMPT = """=== SYSTEM ROLE & VOICE (SMART FINANCE STUDENT) ===
Author: A finance student passionate about macroeconomics, corporate finance, regulation, and big tech.
Tone: A smart, calm, pragmatic observer speaking in natural, living language with peers and colleagues.
Your audience: institutional investors, hedge fund analysts, founders, and portfolio managers.

TONE & PERSONA GUARDRAILS:
- NEVER impersonate a cynical Wall Street trader (strictly NO "catching falling knives", "who holds the bag", "retail trap", or casino trading slang).
- NEVER impersonate a boring professor (strictly NO 35-word academic/bureaucratic constructions or bloated Latinate jargon).
- NEVER use fake self-reflection ("As a student...", "It makes you wonder...", "As someone analyzing...").
- Keep vocabulary simple, direct, and conversational. Speak like a normal human being in a room with peers.
- Strip out all stuffy sell-side filler. Let simple, punchy facts and numbers do the heavy lifting.

Below are raw posts from the Telegram channel HugsFund from the last 24-30 hours.

TASK:
1. Filter out retail noise, daily price fluctuations, and generic corporate PR.
2. Select the top 2-3 (strictly maximum 3, never 4) institutional-grade developments highlighting structural capital flows, balance-sheet reallocations, liquidity dynamics, or regulatory friction.
3. Produce a structured briefing in clean Telegram HTML format.

=== 1. HOOK RULES (LINE 1) ===
- Lead immediately with the hard fact, numerical divergence, or balance-sheet tension in sentence 1.
- ZERO TOLERANCE FOR:
  * Observer openers: "What caught my eye...", "A few stories caught my eye...", "I've been tracking..."
  * Fake contrasts: "These two numbers sit oddly next to each other...", "At the same time..."
  * False consensus / strawmen: "Most investors assume...", "Many investors assume...", "Most retail investors think...", "Everyone is watching/talking about...", "Everyone is watching..."
  * Generic setups: "In today's fast-paced world...", "In today's volatile market...", "It is no secret that...", "The market is shifting..."
  * Anti-Leak check: NEVER copy-paste verbatim openers from few-shot examples ("AI data centers are running into an insurance wall", "Persistent energy inflation and heavy debt issuance are breaking the market's rate-cut bets"). Hook must be original to today's data.
- APPROVED HOOK PATTERNS:
  * Pure Data Divergence: "Poland’s credit-to-deposit ratio sits at 57.7% against an EU-27 median of 106.1%."
  * Balance-Sheet Conflict: "AI data centers are running into an insurance wall."
  * Governance Discount: "If you want to see how the state governance discount works in real time, look at Orlen."
  * Regime Shift: "Persistent energy inflation and heavy debt issuance are breaking the market's rate-cut bets:"

=== 2. STRICT BLACKLIST OF CLICHÉS & FLUFF ===
Never output any of the following expressions:
- BANNED SLANG & CASINO CLICHÉS: "plumbing", "double whammy", "the house always wins", "lost their shirts", "selling shovels in a gold rush", "bottleneck" (use specific constraint), "game-changer", "tip of the iceberg", "silver bullet", "catching falling knives", "who holds the bag", "retail trap".
- BANNED DRAMATIC ONE-LINERS: "The reality is different.", "The timing is tricky.", "Here is the catch.", "The numbers are wild.", "This pressure is not a straight line."
- BANNED RHETORICAL CONNECTORS: "Why? Because...", "Why the massive gap?", "How did this happen? It's simple.", "Here's why:", "The reason is simple."
- BANNED STUDENT INSECURITIES: "As a finance student...", "As a student...", "As someone analyzing...", "for a finance student", "as someone learning", "It makes you wonder...", "I am watching this space...", "I am watching this space closely.", "Time will tell."
- BANNED TELEGRAPHIC FRAGMENTS: "The price:", "This pushes borrowing costs higher", "Supply chains remain tight", "The problem is...", "The problem is", "The central bank is stepping in".
- BANNED CONVERSATIONAL SLOPPINESS: "snapped up the debt", "pay the bill", "piggy banks".
- BANNED CHILDISH BULLET HEADERS: "Expensive imports", "The fuel tax", "The bank squeeze".
- BANNED EMPTY DRAMATIC ONE-LINERS: "earnings get crushed", "Expect de-rating if growth slows", "corporate margins shrink".
- BANNED ACADEMIC/SELL-SIDE FILLER: "characterized by a shift toward...", "consequently, the persistence of...", "serves as a testament to...", "are emerging as the primary...", "structural shift", "is emerging as", "consequently", "far exceeding", "crowding out net expansion", "this mechanism shows", "two numbers stand out", "the common view is that", "primary bottleneck", "underlying economic reality", "synergy", "landscape", "paradigm", "unprecedented", "delve", "underscore", "pivotal", "robust", "it's not just X, it's Y", "here's the thing", "The mechanism is clear", "The mechanism is clear:", "introduces two critical...", "presents the illusion of", "AI integration isn't just about speed. It is...".
- False instant causation: Do not claim that one event caused another instantly unless the material establishes how fast the reaction actually was.
  BAD: "Yet this surge immediately reignited political debates."
- FIRST-PERSON ALLOWANCE & RESEARCH OPTIC: First-person pronouns ("I", "my") ARE ALLOWED when framing a natural student-researcher inquiry (e.g. "I used to assume leasing was the safest corner of credit, but the VB Leasing filing shows...", "Looking at the statutory filings, what stands out is..."). NEVER invent fake trading desk experience ("On my trading desk", "Traders told me"). Only an authentic student researcher unpacking figures.
- STRICT TONE CEILING (NO DIRECTIVE PREACHING): Never issue directives to market participants, investors, or executives. Strictly forbidden: "Institutional investors must now adjust...", "Executive teams must reallocate...", "Firms will be forced to...", "pivoting permanently". Replace with objective analytical exposure: "This leaves lenders with...", "The trade-off for CFOs is...", "It raises the hurdle rate for...".
- LEXICAL HYGIENE (BALANCE SHEET): Mention "balance sheet" or "balance-sheet" strictly AT MOST ONCE per post. Never repeat it.

=== 3. STRICT CONTEXTUAL TERMINOLOGY (NO BINGO BUZZWORDS) ===
Use only the financial and operational concepts that directly describe the actual event. If the news is about server depreciation and capex gaps, discuss depreciation and capex gaps. NEVER shoehorn external buzzwords (like 'circular financing' or 'underwriting constraints') where they do not belong.
Keep the financial concept exact, but the phrasing natural, simple, and conversational.

=== 4. DRAFT STRUCTURE & CONCLUSIONS ===
- STRICT WORD COUNT LIMITS:
  * SINGLE TOPIC POST: Strictly 100–120 words.
  * MULTI-TOPIC DIGEST: Strictly 110–130 words. Must contain strictly 2 to 3 bullets (hard limit: strictly maximum 3, never 4; 4 or more bullets are strictly forbidden).
- TRUNCATION GATE: Every draft must finish with proper closing punctuation (. or !). Never leave a sentence or thought cut off.
- Format in tight paragraphs (2–3 sentences max). NEVER put every single sentence on a new line to create fake "LinkedIn white space".
- CRITICAL FORMATTING RULE: Write clean, continuous plain text. NEVER include word count numbers, token numbers, or index numbers in parentheses after words (e.g. NEVER output 'market (12) rally (13)'). Calculate and verify word counts purely internally. Do not pollute the draft body with counters.
- SENTENCE RULE: one sentence = one fact + one implication. No sentence over ~20 words. Include at least one short sentence (under 8 words) per post.
- NUMERICAL DENSITY DISCIPLINE:
  * Single Topic Post: Strictly 3 to 5 core quantitative figures. NEVER overload a 100-word draft with 7+ data points. Prioritize the central economic mechanism over listing every secondary contract number.
  * Digest: Strictly 1-2 numbers per bullet (maximum 5 numbers across the entire digest).
- DRAFT 1 (DIGEST): Line 1 frames the single core conflict immediately. Keep it short, dense, and name the sectors or core conflict right away. Never open with a long, empty abstract setup ending in a colon (e.g. NEVER "Divergent operational input costs are fundamentally reshaping the long-term baseline margin profile:"). Must contain strictly 2 to 3 bullets (hard limit: strictly 2 or 3 items, maximum 3, never 4). Give every item a bold 2–4 word header stating the exact action or mechanism (e.g. `• <b>1. Off-budget defense spending:</b>`). NEVER use lazy transitions ("Meanwhile...", "At the same time...", "Finally...").
- DRAFT 2 (SINGLE TOPIC): Deep dive into the strongest balance-sheet or structural market mechanism.
- NEVER end with open questions ("What do you think?"), moral lessons, or empty advice ("Watch this space").
- Natural analytical conclusion grounded in facts: End the post with a natural analytical conclusion based on the facts, without forcing artificial drama or repetitive tropes (no forced "forcing multiple compression").
  Permitted conclusion types:
  * Pure balance-sheet divergence (e.g. payback requires 59% annual revenue growth, a pace not yet reached).
  * Regulatory consequence (e.g. distressed assets will have to be absorbed by commercial state banks).
  * Market divergence — only if it directly follows from the news data.
  * Concrete capital or operational impact: effect on borrowing costs, cash flow, or margins.

=== 5. UNIFIED NARRATIVE & NO TOPIC STITCHING ===
- One Single Topic post = one single through-line logical chain. Catastrophically forbidden to stitch together unrelated themes (e.g. macro diesel/refining margins with chip supply bottlenecks, or cybersecurity with insurance).
- In a Digest, line 1 MUST state a single overarching unifying thesis that governs all 2–3 bullets. Forbidden to use an opening hook about interest rates or inflation if the bullets discuss industrial contracts or defense procurement.
- THEMATIC INTEGRITY IN DIGEST: The 2-3 items in DRAFT 1 must share an underlying macro mechanism (e.g., all items relate to sovereign debt/liquidity, or energy/supply chains, or corporate capex). DO NOT stitch completely unrelated niches together (e.g., do not combine sovereign bond yields with altcoin trading cycles in the same post unless linked by systemic dollar liquidity).

=== 6. 3-IN-1 REPETITION BAN ===
- State the cause exactly ONCE per post. Forbidden to repeat the same core thought in different phrases (e.g., repeating "cuts bank profits", "slashes the value of loans", "massive write-downs" within the same post).
- Once the driver is named, move immediately downstream to concrete balance-sheet transmission: regulatory capital, provisioning, spread widening, or equity valuation multiples.

=== 7. TRANSMISSION CHAIN: CAUSE → CHANNEL → RESULT ===
- Never fill missing length with filler or abstract fluff. Reach the target word count (100–120 words for Single, 110–130 for Digest) by tracing the complete mechanical transmission chain:
  * "Tanker transponder blackouts → War risk insurance premium spike → Freight rate surge and higher spot crude."
  * "Widening rate differentials and unhedged carry trades → Yen depreciation pressure despite domestic rate hikes."
  * "WIBOR to POLSTR transition without compensatory spread → Net Interest Margin (NIM) compression → Asset portfolio write-downs."

=== 8. BALANCE-SHEET LOGIC (M&A + TIER II) ===
- When analyzing loan portfolio acquisitions or subordinated debt (Tier II) issuance, always explicitly identify the regulatory objective: raising non-dilutive capital to satisfy capital adequacy ratios (Tier 1/Tier 2 capital buffers) following a sudden expansion in risk-weighted assets.
- Never confuse commercial M&A with bank resolution (sanacja).

=== 9. CONTEXTUAL TERM VALIDITY ===
- Apply "circular vendor financing loop" ONLY when a hardware supplier or chipmaker finances the buyer of its own products via equity or venture capital. Purchasing corporate enterprise software or network firewalls from Palo Alto Networks is defensive maintenance opex, NOT a circular vendor financing loop.

=== 10. INPUT GROUNDING (NO HALLUCINATIONS) ===
- The hook, every mechanism, and all numbers must be grounded strictly in today's source text or fresh data. Extrapolating or hallucinating external facts beyond the briefing is strictly forbidden.

=== 10A. ACCOUNTING CLASSIFICATION CHECK (NO TERM CONFLATION) ===
Strictly distinguish and NEVER conflate or swap the following financial concepts:
- Contractual commitments / backlog != funded debt (cloud commitments or customer orders are NOT balance-sheet debt and NOT guaranteed cash).
- Debt guarantee / contingent exposure != loan (a guarantee is a contingent liability, NOT a funded loan).
- Net borrowing / budget deficit != gross bond issuance (net deficit borrowing != total gross securities issuance).
- GAAP net loss != operating cash burn (separate non-cash mark-to-market revaluations of convertible bonds or derivatives from operating cash flow).
- Secondary market bond yield != coupon / current interest expense (secondary market yield reflects risk pricing, NOT the interest rate on already-issued loans/bonds).
- Gross mortgage originations != net credit growth (origination volume including refinancing does NOT prove net loan book growth).
- Liabilities of non-monetary financial institutions (leasing/factoring) != corporate capex debt (never equate intermediary liabilities with a specific asset class without data).

=== 10B. CAUSAL EVIDENCE REQUIREMENT & NO LEGAL PROPHECIES ===
- Forbidden to make causal leaps to WACC, ROIC, NIM, or deposit rates if the source lacks the intermediate link. When the causal chain is incomplete, use probabilistic phrasing ("Whether this narrows margins depends on...", "adds upward pressure on...").
- Preserve the exact event status from the source: if the source says considers / proposed / draft, it is strictly forbidden to write negotiating / introduces / launched.
- Categorically forbidden to make legal prophecies about EU court or national regulator rulings (never write "will no longer hold up under EU law" or "zero judicial recourse"). Describe only the specific evidentiary standard and its impact on corporate liquidity or provisions.

=== 10C. BAN ON FALSE ABSOLUTISM, CLICHÉ OPENERS & DIGEST COHERENCE ===
- Banned deterministic intensifiers: inevitably, permanently, entirely, directly, effectively, immediately, fundamentally, will force, no longer.
- Banned cliché openers: "Most investors assume", "Many investors assume", "Everyone is watching", "These two numbers sit oddly next to each other".
- DIGEST COHERENCE: Combine 2–3 stories into a digest ONLY when they share a single concrete financial mechanism. Strictly forbidden to stitch unrelated stories together via generic filler about "cost of capital".

=== 11. FORMATTING RULES ===
- Use pure Telegram HTML (<b>, <i>, <code>, <a>).
- NEVER use markdown headers (### or ####).
- Use divider: ───────────────
- CRITICAL FORMATTING RULE: Write clean, continuous plain text. NEVER include word count numbers, token numbers, or index numbers in parentheses after words (e.g. NEVER output 'market (12) rally (13)'). Calculate and verify word counts purely internally. Do not pollute the draft body with counters.

OUTPUT STRUCTURE:

📌 <b>KEY HIGHLIGHTS</b>

• <b>[Institutional Headline]</b> — 1-2 dense analytical sentences explaining the mechanism with exact numbers. [Source, e.g. Bloomberg, WSJ, Reuters, Deutsche Bank]
• <b>[Institutional Headline]</b> — 1-2 dense analytical sentences. [Source]
• <b>[Institutional Headline]</b> — 1-2 dense analytical sentences. [Source]

───────────────

📝 <b>DRAFT 1 — DIGEST</b> (110-130 words)

[Line 1: Core conflict sentence framing the macro theme].

• <b>[Mechanism Header]:</b> [Plain explanation with exact numbers].
• <b>[Mechanism Header]:</b> [Plain explanation with exact numbers].
• <b>[Mechanism Header]:</b> [Plain explanation with exact numbers (optional 3rd bullet; NEVER 4th)].

[Terminal sentence: natural analytical conclusion on balance sheet, regulation, costs, or margins].

───────────────

💡 <b>DRAFT 2 — SINGLE TOPIC</b> (100-120 words)

[Line 1: Immediate entry on balance-sheet anomaly, contract mechanics, or asset repricing].

[Paragraph 2: Detailed institutional mechanism, incentives, or capital flows in simple, direct language].

[Terminal sentence: natural analytical conclusion on balance sheet, regulation, costs, or margins].

---
RAW CHANNEL POSTS:
{posts_text}
"""




def format_posts_for_prompt(posts: list[HugsPost]) -> str:
    """Форматирует список постов для подачи в LLM промпт."""
    parts = []
    for i, p in enumerate(posts, 1):
        dt_str = p.published_at.strftime("%Y-%m-%d %H:%M UTC")
        links_str = f" | Links: {', '.join(p.links)}" if p.links else ""
        parts.append(
            f"--- POST {i} (ID: {p.post_id}, Date: {dt_str}{links_str}) ---\n"
            f"{p.clean_text}\n"
        )
    return "\n".join(parts)


_DRAFT2_ENTITY_STOPWORDS = {
    "The", "This", "That", "These", "Those", "There", "Here", "When", "While", "Where",
    "What", "Why", "How", "Most", "Many", "Some", "Each", "Every", "All", "Both",
    "After", "Before", "Since", "Until", "Despite", "Although", "Because", "However",
    "Instead", "Meanwhile", "Yet", "Still", "Only", "Even", "More", "Less", "Higher",
    "Lower", "Rising", "Falling", "Global", "Local", "Regional", "National", "Federal",
    "Central", "Public", "Private", "Corporate", "Commercial", "Financial", "European",
    "American", "Polish", "Chinese", "Japanese", "Treasury", "Market", "Markets",
    "Investors", "Banks", "Lenders", "Firms", "Companies", "Shareholders", "Bondholders",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday", "Sunday", "DRAFT", "SINGLE", "TOPIC", "DIGEST",
    "KEY", "HIGHLIGHTS", "POST", "MAYBE", "SKIP", "SOURCE", "FIGURES", "PLN", "EUR",
    "USD", "GDP", "NIM", "WACC", "ROIC", "GAAP", "EBITDA", "CAPEX", "IPO", "ETF",
    "US", "EU", "UK", "AI", "Q1", "Q2", "Q3", "Q4", "H1", "H2",
}


def extract_key_highlights(analysis_text: str) -> str:
    """Извлекает блок KEY HIGHLIGHTS из сгенерированного брифинга HugsFund."""
    if not analysis_text:
        return ""
    m = re.search(
        r"(?:📌\s*)?(?:<b>\s*)?KEY\s+HIGHLIGHTS.*?(?=\n+─{3,}|\n+---|\n+(?:📝\s*)?<b>\s*DRAFT\s*1|\Z)",
        analysis_text,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(0).strip()
    m_before = re.search(
        r"^.*?(?=\n+─{3,}|\n+(?:📝\s*)?<b>\s*DRAFT\s*1)",
        analysis_text,
        re.DOTALL | re.IGNORECASE,
    )
    return m_before.group(0).strip() if m_before else ""


def extract_draft2_section(analysis_text: str) -> str:
    """Извлекает тело секции DRAFT 2 — SINGLE TOPIC из брифинга HugsFund."""
    if not analysis_text:
        return ""
    d2_pattern = re.compile(
        r"(?:💡\s*)?<b>\s*DRAFT\s*2[^<]*</b>[^\n]*\n+(.*?)(?=(\n+─{3,}|\n+---|\Z))",
        re.DOTALL | re.IGNORECASE,
    )
    m = d2_pattern.search(analysis_text)
    if m:
        return m.group(1).strip()
    m_plain = re.search(
        r"DRAFT\s*2[^\n]*\n+(.*?)(?=(\n+─{3,}|\n+---|\Z))",
        analysis_text,
        re.DOTALL | re.IGNORECASE,
    )
    return m_plain.group(1).strip() if m_plain else analysis_text.strip()


def extract_draft2_entities(text: str) -> list[str]:
    """Извлекает ключевые сущности/имена компаний из текста DRAFT 2."""
    body = extract_draft2_section(text)
    clean = re.sub(r"<[^>]+>", " ", body or "")
    if not clean.strip():
        return []

    first_sentence = re.split(r"(?<=[.!?])\s+|\n+", clean.strip(), maxsplit=1)[0]
    camel_re = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z0-9]*\b")
    proper_re = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")

    seen: set[str] = set()
    ordered: list[str] = []

    def _add(tok: str) -> None:
        if tok in _DRAFT2_ENTITY_STOPWORDS:
            return
        low = tok.lower()
        if low in seen:
            return
        seen.add(low)
        ordered.append(tok)

    # Сначала CamelCase-бренды (CoreWeave, OpenAI, SpaceX, DeepSeek, JPMorgan)
    for tok in camel_re.findall(clean):
        _add(tok)

    # Затем сущности из первого предложения (заголовок / хук)
    for idx, tok in enumerate(proper_re.findall(first_sentence)):
        if idx == 0 and tok in _DRAFT2_ENTITY_STOPWORDS:
            continue
        _add(tok)

    # Затем повторяющиеся или прочие собственные имена в тексте (без стартовых слов предложений)
    non_start_clean = re.sub(r"(?:^|[.!?]\s+|\n\s*)[A-Z][a-z]+\b", " ", clean)
    for tok in proper_re.findall(non_start_clean):
        _add(tok)

    return ordered[:5]


def load_recent_draft2_history(
    hours: int = 48,
    now: datetime | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Загружает записи истории DRAFT 2 за последние `hours` часов."""
    target = path or HUGS_DRAFT2_HISTORY_PATH
    if not target.exists():
        return []
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else []
    except Exception as exc:
        log.warning("Ошибка чтения истории DRAFT 2 (%s): %s", target, exc)
        return []
    if not isinstance(data, list):
        return []

    ref_now = now or datetime.now(timezone.utc)
    cutoff = ref_now - timedelta(hours=hours)
    recent: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ts_raw = entry.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            recent.append(entry)
    return recent


def get_recent_draft2_entities(
    hours: int = 48,
    now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Возвращает уникальный список сущностей DRAFT 2 за последние `hours` часов."""
    recent = load_recent_draft2_history(hours=hours, now=now, path=path)
    seen: set[str] = set()
    entities: list[str] = []
    for entry in recent:
        items = entry.get("entities") or ([] if not entry.get("entity") else [entry.get("entity")])
        for ent in items:
            s = str(ent).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                entities.append(s)
    return entities


def save_draft2_history(
    text: str,
    entities: list[str] | None = None,
    headline: str | None = None,
    hours: int = 48,
    now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Сохраняет ключевые сущности и заголовок DRAFT 2 в state/ с очисткой записей старше `hours` часов."""
    target = path or HUGS_DRAFT2_HISTORY_PATH
    ref_now = now or datetime.now(timezone.utc)
    recent = load_recent_draft2_history(hours=hours, now=ref_now, path=target)

    body = extract_draft2_section(text)
    clean_body = re.sub(r"<[^>]+>", " ", body or "").strip()
    extracted_entities = entities if entities is not None else extract_draft2_entities(body)
    if not extracted_entities:
        return []

    first_line = headline or (re.split(r"(?<=[.!?])\s+|\n+", clean_body, maxsplit=1)[0][:180] if clean_body else "")
    recent.append({
        "timestamp": ref_now.isoformat(),
        "entities": extracted_entities,
        "headline": first_line,
    })

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(recent, indent=2, ensure_ascii=False), encoding="utf-8")
    return extracted_entities


def run_llm_analysis(
    posts: list[HugsPost],
    history_path: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Запускает аналитическую обработку постов через Gemini."""
    if not posts:
        return "No fresh HugsFund posts found for the specified period."

    posts_text = format_posts_for_prompt(posts)
    prompt = HUGS_ANALYSIS_PROMPT.format(posts_text=posts_text)

    recent_entities = get_recent_draft2_entities(hours=48, now=now, path=history_path)
    if recent_entities:
        matched = [e for e in recent_entities if e.lower() in posts_text.lower()]
        banned_list = ", ".join(matched if matched else recent_entities)
        dedup_block = (
            "\n=== 48-HOUR ANTI-DUPLICATE BAN FOR DRAFT 2 (SINGLE TOPIC) ===\n"
            f"STRICTLY FORBIDDEN to use the following entity/topic for DRAFT 2 — SINGLE TOPIC, "
            f"because it was already covered in DRAFT 2 within the last 48 hours: {banned_list}.\n"
            "If today's top story centers on this entity, you MUST NOT use it for DRAFT 2 — "
            "select the second most significant topic of the day for DRAFT 2 instead.\n"
        )
        prompt = prompt + dedup_block

    log.info("Отправка запроса в Gemini (%d постов, %d симв. промпта)...", len(posts), len(prompt))
    response = brain._call(prompt, as_json=False, temperature=0.3, max_tokens=32768)
    result = response.strip()
    if re.search(r"DRAFT\s*2", result, re.IGNORECASE):
        save_draft2_history(result, now=now, path=history_path)
    return result


def verify_hugs_draft_section(
    draft_text: str,
    posts: list[HugsPost] | list[str] | None = None,
    key_highlights: str = "",
) -> str:
    """Верифицирует цифры в тексте черновика HugsFund с помощью verify.verify_drafts.
    Передаёт в качестве эталонного корпуса склеенный текст всех отфильтрованных
    постов Hugs + весь блок KEY HIGHLIGHTS.
    Возвращает строку футера (например, 'POST · цифры 2/2 ✅')."""
    if not draft_text or not draft_text.strip():
        return ""

    corpus_parts: list[str] = []
    first_url = "https://t.me/HugsFund"
    if posts:
        for p in posts:
            if isinstance(p, str):
                txt = p.strip()
            else:
                txt = (getattr(p, "clean_text", "") or getattr(p, "text", "") or "").strip()
                if getattr(p, "url", "") and first_url == "https://t.me/HugsFund":
                    first_url = p.url
            if txt:
                corpus_parts.append(txt)

    if key_highlights and key_highlights.strip():
        kh_clean = re.sub(r"<[^>]+>", " ", key_highlights).strip()
        corpus_parts.append(key_highlights.strip())
        if kh_clean and kh_clean != key_highlights.strip():
            corpus_parts.append(kh_clean)

    combined_corpus = "\n\n".join(corpus_parts)
    selected = (
        [{"item": type("ItemDummy", (), {"url": first_url})(), "body": combined_corpus}]
        if combined_corpus
        else []
    )

    draft_block_format = (
        "SHAPE: single\n"
        f"BODY: {draft_text.strip()}\n"
        "FIGURES: none used\n"
        "SOURCE: Story 1\n"
        "WHY_THIS_ONE: hugs briefing\n"
        "VERDICT: POST\n"
        "WHY: hugs briefing\n"
        "CHECK_FIRST: -\n"
    )
    try:
        _, _, blocks = verify.verify_drafts(draft_block_format, selected=selected, data_text="")
        if not blocks:
            return ""
        b = blocks[0]
        countable = [r for r in b.get("_level_a", []) if r["status"] != "YEAR"]
        denom = len(countable)
        found = sum(1 for r in countable if r["status"] == "FOUND") if denom else 0
        verdict = (b.get("verdict") or "").strip().upper()
        if b.get("_downgrade") and verdict == "POST":
            verdict = "MAYBE"

        offending = b.get("_offending") or []
        if not denom:
            return f"{verdict} · цифры не заявлены"
        elif found == denom:
            return f"{verdict} · цифры {found}/{denom} ✅"
        elif offending:
            shown = offending[:3]
            return f"{verdict} · цифры {found}/{denom} — сверь {', '.join(shown)}"
        else:
            return f"{verdict} · цифры {found}/{denom}"
    except Exception as exc:
        log.warning("Верификация Hugs черновика упала: %s", exc)
        return "POST — числа не верифицированы"


def humanize_hugs_briefing(analysis_text: str, posts: list[HugsPost] | None = None) -> str:
    """Применяет edit_and_humanize_draft к секциям DRAFT 1 (digest) и DRAFT 2 (single)
    внутри сгенерированного брифинга HugsFund."""
    if not analysis_text:
        return analysis_text

    text = analysis_text
    key_highlights = extract_key_highlights(analysis_text)
    badge = " ⚠️ [RAW DRAFT: EDITOR FAILED]"

    # 1. Секция DRAFT 1 (Digest)
    d1_pattern = re.compile(
        r"((?:📝\s*)?<b>\s*DRAFT\s*1[^<]*</b>[^\n]*\n+)(.*?)(?=(\n+─{3,}|\n+---|\n+(?:💡\s*)?<b>\s*DRAFT\s*2|\Z))",
        re.DOTALL | re.IGNORECASE,
    )
    m1 = d1_pattern.search(text)
    if m1:
        raw_d1 = m1.group(2).strip()
        if raw_d1:
            raw_d1 = brain.prune_digest_bullets(raw_d1, max_bullets=3)
            edited_d1 = brain.edit_and_humanize_draft(raw_d1, draft_type="digest")
            if brain.FALLBACK_BADGE in edited_d1:
                clean_body = edited_d1.replace(brain.FALLBACK_BADGE, "").strip()
                header = m1.group(1).rstrip("\n") + badge + "\n\n"
                text = text[:m1.start(1)] + header + clean_body + "\n\n" + text[m1.end(2):]
                brain.inspect_and_collect_flagged_phrases(clean_body, "DRAFT 1 — DIGEST")
            elif edited_d1:
                d1_footer = verify_hugs_draft_section(edited_d1, posts=posts, key_highlights=key_highlights)
                d1_with_footer = f"{edited_d1}\n\n{d1_footer}" if d1_footer else edited_d1
                text = text[:m1.start(2)] + d1_with_footer + "\n\n" + text[m1.end(2):]
                brain.inspect_and_collect_flagged_phrases(edited_d1, "DRAFT 1 — DIGEST")

    # 2. Секция DRAFT 2 (Single Topic)
    d2_pattern = re.compile(
        r"((?:💡\s*)?<b>\s*DRAFT\s*2[^<]*</b>[^\n]*\n+)(.*?)(?=(\n+─{3,}|\n+---|\Z))",
        re.DOTALL | re.IGNORECASE,
    )
    m2 = d2_pattern.search(text)
    if m2:
        raw_d2 = m2.group(2).strip()
        if raw_d2:
            edited_d2 = brain.edit_and_humanize_draft(raw_d2, draft_type="single")
            if brain.FALLBACK_BADGE in edited_d2:
                clean_body = edited_d2.replace(brain.FALLBACK_BADGE, "").strip()
                header = m2.group(1).rstrip("\n") + badge + "\n\n"
                text = text[:m2.start(1)] + header + clean_body + ("\n" if not clean_body.endswith("\n") else "") + text[m2.end(2):]
                brain.inspect_and_collect_flagged_phrases(clean_body, "DRAFT 2 — SINGLE TOPIC")
            elif edited_d2:
                d2_footer = verify_hugs_draft_section(edited_d2, posts=posts, key_highlights=key_highlights)
                d2_with_footer = f"{edited_d2}\n\n{d2_footer}" if d2_footer else edited_d2
                text = text[:m2.start(2)] + d2_with_footer + ("\n" if not d2_with_footer.endswith("\n") else "") + text[m2.end(2):]
                brain.inspect_and_collect_flagged_phrases(edited_d2, "DRAFT 2 — SINGLE TOPIC")

    return text


def build_final_message(analysis_text: str, posts_count: int, hours: int) -> str:
    """Собирает итоговое сообщение для отправки в Telegram с заголовком."""
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    header = (
        f"📊 <b>HUGS FUND BRIEFING | {now_str}</b>\n"
        f"<i>Filtered top stories from {posts_count} posts ({hours}h window)</i>\n\n"
    )
    return header + analysis_text



def run_workflow(
    hours: int = 30,
    dry_run: bool = False,
    send_telegram: bool = True,
    channel: str = "HugsFund",
) -> Optional[str]:
    """Основной пайплайн воркфлоу HugsFund."""
    log.info("=== Запуск воркфлоу HugsFund (окно: %d часов, dry_run=%s) ===", hours, dry_run)

    # 1. Сбор данных
    try:
        raw_posts = fetch_channel_posts(channel=channel, hours=hours)
    except Exception as exc:
        log.error("Критическая ошибка при парсинге постов: %s", exc)
        return None

    if not raw_posts:
        log.warning("Не удалось получить посты из канала %s", channel)
        return None

    # 2. Фильтрация
    filtered = filter_posts(raw_posts, hours=hours)
    if not filtered:
        log.warning("Все посты были отфильтрованы (по возрасту или промо)")
        return None

    log.info("К обработке готово постов: %d", len(filtered))

    # 3. LLM-анализ
    try:
        analysis = run_llm_analysis(filtered)
        analysis = humanize_hugs_briefing(analysis, posts=filtered)
    except Exception as exc:
        log.error("Ошибка при обработке LLM: %s", exc)
        # Фолбэк: если LLM недоступен, формируем краткий список заголовков/постов
        fallback_lines = ["⚠️ <i>LLM-анализ недоступен, исходные посты:</i>\n"]
        for p in filtered[:8]:
            fallback_lines.append(f"• <a href=\"{p.url}\">{p.post_id}</a> ({p.published_at.strftime('%H:%M')}): {p.clean_text[:120]}...")
        analysis = "\n".join(fallback_lines)

    # 4. Формирование сообщения
    full_message = build_final_message(analysis, posts_count=len(filtered), hours=hours)

    # 5. Отправка / вывод
    if dry_run:
        log.info("=== РЕЖИМ DRY-RUN (сообщение не отправляется) ===")
        print("\n" + "=" * 60 + "\n")
        print(full_message)
        print("\n" + "=" * 60 + "\n")
    elif send_telegram:
        log.info("Отправка в Telegram...")
        try:
            deliver.send(full_message)
            log.info("Дайджест HugsFund успешно отправлен в Telegram.")
        except Exception as exc:
            log.error("Ошибка при отправке в Telegram: %s", exc)
            raise

    return full_message


def main():
    parser = argparse.ArgumentParser(description="Сбор и анализ постов Telegram-канала HugsFund")
    parser.add_argument("--hours", type=int, default=30, help="Окно свежести постов в часах (default: 30)")
    parser.add_argument("--dry", action="store_true", help="Dry-run режим: вывод в консоль без отправки в Telegram")
    parser.add_argument("--send", action="store_true", help="Явная отправка в Telegram")
    parser.add_argument("--channel", type=str, default="HugsFund", help="Имя канала (default: HugsFund)")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    # Если передан --dry, не шлем в Телеграм
    send_flag = args.send or (not args.dry)
    dry_flag = args.dry

    run_workflow(
        hours=args.hours,
        dry_run=dry_flag,
        send_telegram=send_flag,
        channel=args.channel,
    )


if __name__ == "__main__":
    main()
