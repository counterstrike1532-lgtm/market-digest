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

HUGS_ANALYSIS_PROMPT = """You are an editor helping a 2nd-year Finance & Accounting student at Kozminski University in Warsaw prepare material for his LinkedIn. He targets investment banking (IB) and asset management (AM).

Below are raw posts from the Telegram channel HugsFund from the last 24-30 hours.

TASK:
1. Filter ruthlessly. Select ONLY the 3-4 most interesting, non-obvious topics (unusual numbers, market mechanisms, energy/AI capex, treasury/debt dynamics, institutional plumbing). Skip commodity news, trivial daily fluctuations, and PR noise.
2. Write the ENTIRE output in natural, modern business ENGLISH.

VOICE & PERSONA RULES:
- Persona: A sharp, curious 2nd-year finance student — NOT an institutional press release, NOT a textbook, and NOT a Bloomberg news robot.
- Tone: Conversational business style. Fresh, direct, and grounded in real mechanics. Sound like a real person sharing what caught their eye in the data.
- Structure & Spacing: Break every post into 2-3 short, bite-sized paragraphs (2-3 sentences each). NEVER output a single monolithic block of text.
- Sentence rhythm: Mix short and normal sentences. Every draft must have at least one punchy sentence under 8 words.
- Natural phrasing: Use idiomatic English as spoken by junior analysts and interns in finance (e.g., "what caught my attention", "sits oddly next to", "the profit is in the plumbing", "bounced right back"). Avoid bookish or awkward translated phrases.
- Banned AI words: leverage, synergy, landscape, paradigm, unprecedented, game-changer, delve, underscore, pivotal, robust, revolutionary, "it's not just X, it's Y", "let that sink in", "dive into".
- Explaining posture: Confident about the mechanism, modest about yourself. Jump straight into the fact or observation without rhetorical questions at the start.

FORMATTING RULES:
- Use pure Telegram HTML (<b>, <i>, <code>, <a>).
- NEVER use markdown headers like ### or #### (they look ugly in Telegram).
- Use clean text dividers: ───────────────

OUTPUT STRUCTURE:

📌 <b>KEY HIGHLIGHTS</b>

• <b>Headline / Core Observation</b> — 1-2 sharp sentences explaining what happened and the economic mechanism. State exact figures. [Primary Source, e.g. Bloomberg, WSJ, Reuters, Deutsche Bank, Hugs Analysis]
• <b>Headline / Core Observation</b> — 1-2 sharp sentences. [Source]
• <b>Headline / Core Observation</b> — 1-2 sharp sentences. [Source]

───────────────

📝 <b>DRAFT 1 — DIGEST</b> (~90-120 words)

[Paragraph 1: The tension or hook connecting 2 key stories]

[Paragraph 2: The numbers and mechanism explaining what is happening]

[Paragraph 3: Short punchy conclusion]

───────────────

💡 <b>DRAFT 2 — SINGLE MECHANISM</b> (~90-130 words)

[Paragraph 1: Common market assumption vs real numbers]

[Paragraph 2: How the financial plumbing / balance sheets actually work]

[Paragraph 3: Concrete closing observation]

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
    response = brain._call(prompt, as_json=False, temperature=0.3, max_tokens=8192)
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
