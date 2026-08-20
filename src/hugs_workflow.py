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

HUGS_ANALYSIS_PROMPT = """You are an elite financial analyst and editor assisting a 2nd-year Finance & Accounting student at Kozminski University (Warsaw) who is preparing for investment banking (IB) and asset management (AM) roles.

Below are raw Telegram posts from the financial channel HugsFund from the last 24-30 hours.

TASK:
Analyze the posts and produce a structured, high-signal morning briefing in RUSSIAN, followed by 2-3 deep post ideas in the author's established style.

---
### PART 1: ФАКТЫ И КАТЕГОРИИ (Morning Briefing)
Group all important events and data into EXACTLY these 4 categories (omit a category only if there are genuinely zero news items for it):

1. 📊 <b>Макроэкономика и геополитика</b>
(Monetary policy, interest rates, inflation, debt/treasuries, GDP, FX, central banks, geopolitical shifts)

2. 📈 <b>Фондовый рынок и отчеты компаний</b>
(Equities, indices, earnings reports, valuation multiples, corporate strategy, M&A, sector rotation)

3. 💻 <b>Крипта и AI / Tech</b>
(AI models, enterprise tech capex, datacenters/semiconductors, crypto market structure, liquidations, stablecoins)

4. 🛢️ <b>Сырье и облигации</b>
(Commodities, oil/gas, metals, 10Y/30Y Treasury yields, bond auctions, yield curve dynamics, credit spreads)

RULES FOR PART 1:
- Bullet points only (format: • <b>Headline / core fact</b> — concise explanation).
- MANDATORY: For EVERY news item, explicitly name the PRIMARY SOURCE in brackets at the end (e.g. [Bloomberg], [WSJ], [Reuters], [Financial Times], [BofA], [The Information], [SEC], [Fed], [Hugs / Аналитика]). If the channel is quoting an analyst or primary report, cite that original source.
- Preserve hard numbers, percentages, and currencies accurately.
- No buzzwords or fluff. Focus on economic cause and mechanism.

---
### PART 2: ИДЕИ ДЛЯ АВТОРСКИХ ПОСТОВ (2-3 Ideas)
Suggest 2 or 3 ready-to-write post concepts based on the strongest themes of the day.

Target author profile:
- 2nd-year Kozminski finance student targeting IB/AM.
- Modest frame ("active student who reads primary sources"), NOT pretending to be a managing director.
- Explaining posture ("here is how this mechanism works"), NEVER asking ("what am I missing", "correct me if I'm wrong").
- Tone ceiling: clear and direct financial logic. Banned words: leverage, synergy, landscape, paradigm, unprecedented, game-changer, delve, underscore, pivotal, robust, revolutionary.

FORMAT FOR EACH IDEA:
💡 <b>Идея [N]: [Запоминающийся заголовок / Парадокс / Угол]</b>
• <b>Категория и источник:</b> [Категория | Первоисточник и ключевая цифра]
• <b>Структура поста:</b> [Mechanism / Two Numbers / Common Belief vs Reality]
• <b>Тезисный план аргументации:</b>
  1. <i>Исходный факт:</i> [Конкретная цифра / событие]
  2. <i>Экономический механизм:</i> [Как работает стимул, маржинальность, фондирование или баланс]
  3. <i>Неочевидный вывод:</i> [Конкретный вывод без банальной морали]

---
RAW POSTS FROM HUGSFUND:
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
        return "Нет свежих постов HugsFund за указанный период."

    posts_text = format_posts_for_prompt(posts)
    prompt = HUGS_ANALYSIS_PROMPT.format(posts_text=posts_text)

    log.info("Отправка запроса в Gemini (%d постов, %d симв. промпта)...", len(posts), len(prompt))
    response = brain._call(prompt, as_json=False, temperature=0.3, max_tokens=8192)
    return response.strip()


def build_final_message(analysis_text: str, posts_count: int, hours: int) -> str:
    """Собирает итоговое сообщение для отправки в Telegram с заголовком."""
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    header = (
        f"📊 <b>ДАЙДЖЕСТ HUGS FUND | {now_str}</b>\n"
        f"<i>Обработано постов: {posts_count} (за последние {hours}ч)</i>\n\n"
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
