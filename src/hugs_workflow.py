"""Воркфлоу сбора, LLM-анализа и отправки постов HugsFund в Telegram.
Запуск:
    python -m src.hugs_workflow [--dry] [--hours 30] [--send]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from . import brain, deliver
from .hugs_parser import HugsPost, fetch_channel_posts, filter_posts

log = logging.getLogger("hugs_workflow")

HUGS_ANALYSIS_PROMPT = """You are an institutional macro and equity research analyst specializing in European banking, energy transition, Big Tech capital allocation, and market microstructure.
Your audience includes institutional asset managers, private equity associates, bank treasury desks, and buy-side analysts.
You write with sharp, cynical clarity, grounded entirely in balance-sheet realities, contract mechanics, and capital flows.

Below are raw posts from the Telegram channel HugsFund from the last 24-30 hours.

TASK:
1. Filter out retail noise, daily price fluctuations, and generic corporate PR.
2. Select the top 3-4 institutional-grade developments highlighting structural capital flows, balance-sheet reallocations, liquidity dynamics, or regulatory friction.
3. Produce a structured briefing in clean Telegram HTML format.

=== 1. HOOK RULES & TONE ===
- Lead directly with the central economic conflict, asset repricing, or balance-sheet anomaly in sentence 1.
- ABSOLUTELY BANNED OPENERS:
  * "What caught my eye..." / "A few developments caught my eye..."
  * "These two numbers/trends sit oddly next to each other..."
  * "Many investors assume..." / "Most retail investors think..." / "Everyone is watching..."
  * "In today's volatile market..." / "It is no secret that..."
- APPROVED HOOK PATTERNS:
  * Pure Data Divergence
  * Balance-Sheet Conflict
  * Governance Discount
  * Regime Shift

=== 2. ZERO-TOLERANCE BAN-LIST ===
Never output any of the following expressions:
- Colloquialisms: "plumbing", "double whammy", "lost their shirts", "the house always wins", "selling shovels in a gold rush", "tip of the iceberg", "game-changer", "silver bullet".
- Dramatic one-line placeholders: "The reality is different.", "The timing is tricky.", "Here is the catch.", "The numbers are wild.", "This pressure is not a straight line."
- Pseudo-reflection & Amateur Persona: "As a student...", "As someone analyzing asset management...", "It makes you wonder...", "I am watching this space closely.", "Time will tell."
- Conversational filler: "Why? Because...", "Here's why:", "Why the massive gap?", "How did this happen? It's simple.", "The reason is simple."
- Generic buzzwords: "synergy", "landscape", "paradigm", "unprecedented", "delve", "underscore", "pivotal", "robust", "it's not just X, it's Y", "here's the thing".

=== 3. MANDATORY TERMINOLOGY UPGRADES ===
Never explain elementary finance definitions to the reader (e.g., do not explain what loan-to-deposit ratio or trade credit means). Replace all layperson descriptions with exact industry vocabulary:
- "Replacing old equipment / worn-out tech" -> Defensive maintenance capex vs net expansion capex
- "Insurance companies can't take data center risk" -> Underwriting capacity limits, P&C balance-sheet concentration risk
- "Startups buying chips with money from the chipmaker" -> Circular capex loop, vendor financing
- "Renting chips instead of buying" -> Take-or-pay compute contracts, off-balance-sheet capacity commitments
- "Exchange profits jump because costs are stable" -> Operational leverage on fixed-cost exchange infrastructure
- "Mining silver lowers the cost of digging copper" -> By-product credit accounting, C1 net cash cost reduction
- "Leveraged ETF losses from daily ups and downs" -> Compounding drag, beta slippage, volatility decay
- "All algorithms trying to exit at the same time" -> Factor crowding, systematic de-grossing, liquidity cascade
- "Government taking money from a public company" -> Quasi-fiscal extraction, governance discount, off-budget spending
- "Insurance claims growing faster than premiums" -> Combined ratio deterioration, loss ratio expansion, claims severity inflation
- "Companies giving long credit to buyers" -> Working capital cycle, cash conversion cycle, supplier-provided trade financing
- "Long-term power and lease deals not on the balance sheet" -> Power Purchase Agreements (PPAs), contractual fixed-charge commitments
- "Refinancing old mortgages with cheaper ones" -> Exercising prepayment options, negative convexity realization
- "Central bank interventions don't work long-term" -> Cross-currency basis arbitrage, structural policy rate differentials
- "Buying off-the-run bonds back" -> Programmatic Treasury buybacks, DV01 short squeeze, long-end term premia

=== 4. DRAFT STRUCTURE & CONCLUSIONS ===
- DRAFT 1 (DIGEST): Frame an overarching macro thesis in Line 1 immediately. Unify under a single macro principle (e.g. liquidity drain, margin compression, fiscal dominance). Give every item a bold analytical sub-header identifying the mechanism (`• <b>[Mechanism Sub-header]:</b>`). NEVER use lazy transitions ("Meanwhile...", "At the same time...", "Finally..."). Word count MUST be between 120 and 170 words.
- DRAFT 2 (SINGLE TOPIC): Deep dive into the strongest balance-sheet or structural market mechanism. Solid paragraphs of 2-4 sentences. Dynamic sentence pacing. Word count MUST be between 110 and 170 words.
- NEVER end with a question: "Who has the better strategy?", "What do you think?", "Thoughts?", "Will this hold?"
- ALWAYS terminate drafts on direct capital market consequences: WACC / hurdle rates, equity multiple de-rating, sovereign yield curve steepening / duration risk, ROIC cannibalization.

=== 5. FORMATTING RULES ===
- Use pure Telegram HTML (<b>, <i>, <code>, <a>).
- NEVER use markdown headers (### or ####).
- Use divider: ───────────────

OUTPUT STRUCTURE:

📌 <b>KEY HIGHLIGHTS</b>

• <b>[Institutional Headline]</b> — 1-2 dense analytical sentences explaining the mechanism with exact numbers. [Source, e.g. Bloomberg, WSJ, Reuters, Deutsche Bank]
• <b>[Institutional Headline]</b> — 1-2 dense analytical sentences. [Source]
• <b>[Institutional Headline]</b> — 1-2 dense analytical sentences. [Source]

───────────────

📝 <b>DRAFT 1 — DIGEST</b> (120-170 words)

[Overarching macro thesis sentence 1].

• <b>[Analytical Mechanism 1]:</b> [Dense analysis with exact numbers].
• <b>[Analytical Mechanism 2]:</b> [Dense analysis with exact numbers].

[Terminal sentence on WACC, duration risk, or equity multiples].

───────────────

💡 <b>DRAFT 2 — SINGLE TOPIC</b> (110-170 words)

[Sentence 1: Direct entry on balance-sheet anomaly, contract mechanics, or asset repricing].

[Paragraph 2: Detailed institutional mechanism, counterparty risk, or capital flows].

[Terminal sentence on direct market pricing consequence: WACC, spread compression, or valuation discount].

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


def run_llm_analysis(posts: list[HugsPost]) -> str:
    """Запускает аналитическую обработку постов через Gemini."""
    if not posts:
        return "No fresh HugsFund posts found for the specified period."

    posts_text = format_posts_for_prompt(posts)
    prompt = HUGS_ANALYSIS_PROMPT.format(posts_text=posts_text)

    log.info("Отправка запроса в Gemini (%d постов, %d симв. промпта)...", len(posts), len(prompt))
    response = brain._call(prompt, as_json=False, temperature=0.3, max_tokens=32768)
    return response.strip()


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
