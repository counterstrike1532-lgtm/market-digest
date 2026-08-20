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

HUGS_ANALYSIS_PROMPT = """You are a ruthless financial editor and analyst assisting a 2nd-year Finance & Accounting student at Kozminski University in Warsaw preparing for investment banking (IB) and asset management (AM) roles.

Below are raw posts from the Telegram channel HugsFund from the last 24-30 hours.

TASK:
Filter ruthlessly. Ignore commodity news, trivial daily noise, and routine market chatter. Select ONLY the top 3-4 most significant, non-obvious themes (unexpected numbers, structural mechanisms, AI capex reality, debt/treasury mechanics, institutional market structure).

Write the ENTIRE output in ENGLISH, concise and sharp.

---
### 📌 HIGH-SIGNAL HIGHLIGHTS (Top 3-4 Themes)
Select ONLY 3 or 4 top stories. For each:
• <b>Headline / Core Fact</b> — 1-2 terse sentences explaining the economic mechanism and hard figures. MANDATORY: explicitly cite the PRIMARY SOURCE in brackets at the end (e.g. [Bloomberg], [WSJ], [Reuters], [Financial Times], [Deutsche Bank], [BofA], [SEC], [Fed], [Hugs Analysis]).

---
### ✍️ LINKEDIN POST DRAFTS (English, ~100-140 words each)

Write exactly 2 concise, ready-to-publish LinkedIn post drafts in ENGLISH:

<b>DRAFT 1 — Digest / Roundup (~100-130 words):</b>
Connect 2 key stories or highlight a core macro tension of the day.

<b>DRAFT 2 — Single Mechanism (~100-140 words):</b>
One deep story examined through ONE specific shape (do not write the label, write the post):
- Shape A (Mechanism): Name the financial mechanism, explain how incentives/balance sheets work, show where it appeared.
- Shape B (Two Numbers): Put two contrasting figures side by side, explain what the pairing reveals.
- Shape C (Common Belief): State the widespread market assumption, then the hard fact that complicates it.

VOICE & ANTI-SLOP RULES:
- Author: 2nd-year Kozminski finance student who reads primary sources and runs numbers.
- Posture: Explaining the mechanism with confidence, NEVER asking ("What am I missing?", "I might be wrong", "Correct me if").
- Banned AI words: leverage, synergy, landscape, paradigm, unprecedented, game-changer, delve, underscore, pivotal, robust, revolutionary, "it's not just X, it's Y", "let that sink in".
- No rhetorical questions at opening. End on a concrete number, fact, or mechanism — never an abstract textbook moral.
- Keep sentences punchy. Zero fluff.

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
