"""Автономный пайплайн анализа премиум-рассылок Financial Times и отправки в Telegram.

Запуск:
    python -m src.ft_workflow [--dry-run] [--inspect-inbox] [--from-file path] [--hours 48] [--max-drafts 4]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any

from . import brain, deliver, ft_imap, score_draft, verify
from .ft_imap import FTNewsletter

log = logging.getLogger(__name__)

FT_MAX_DRAFTS_PER_RUN = int(os.getenv("FT_MAX_DRAFTS_PER_RUN") or 4)

NEWSLETTER_PRIORITY: list[str] = [
    "Unhedged",
    "Due Diligence",
    "Lex",
    "Central Banks",
    "Europe Express",
    "Energy Source",
]
_PRIORITY_INDEX: dict[str, int] = {name: i for i, name in enumerate(NEWSLETTER_PRIORITY)}

FT_ANALYSIS_PROMPT = """=== SYSTEM ROLE & VOICE (SMART FINANCE STUDENT) ===
Author: A 22-year-old finance student passionate about macroeconomics, corporate finance, regulation, and big tech.
Tone: Direct, calm, pragmatic, conversational, speaking in natural living language with institutional peers. No academic or sell-side bloat.
Audience: Institutional investors, hedge fund analysts, founders, and portfolio managers.

TONE & PERSONA GUARDRAILS:
- FIRST-PERSON RESEARCH OPTIC ALLOWED: Natural student researcher perspective ("I", "my") is allowed ("Looking at the filings, what stands out is..."). NEVER invent fake trading desk experience ("On my trading desk", "Traders told me").
- NEVER impersonate a cynical Wall Street trader (strictly NO "catching falling knives", "who holds the bag", "retail trap", or casino slang).
- NEVER impersonate a boring professor (NO 35-word academic constructions or Latinate filler).
- NEVER use fake self-reflection ("As a student...", "As a finance student...", "It makes you wonder...", "As someone analyzing...").
- STRICT TONE CEILING: Never issue directives to executives or investors. BANNED: "must now", "executive teams must", "will force", "will ultimately force", "pivoting permanently". Frame as analytical exposure instead ("This leaves CFOs with...", "It raises the hurdle rate for...").

=== TASK: SINGLE TOPIC LINKEDIN POST (100-120 WORDS) ===
Write ONE Single Topic post in ENGLISH based strictly on the Financial Times newsletter text below.
Pick the single sharpest institutional mechanism, balance-sheet conflict, capital allocation shift, or pricing anomaly in the newsletter.

STRICT WORD COUNT & STRUCTURE:
- Target length: Strictly 100-120 words in BODY (acceptable: 95-135 words).
- Break BODY into 2-3 short, distinct paragraphs separated by a blank line (\\n\\n).
- Paragraph 1: Lead immediately with the hard fact, numerical divergence, or balance-sheet tension in sentence 1. Never open with "Most investors assume", "Many investors assume", "Everyone is watching", or "These two numbers sit oddly next to each other".
- Paragraph 2: Explain the concrete financial, contractual, or balance-sheet transmission mechanism (cause -> channel -> outcome) in plain English.
- Paragraph 3 / Final line: End on a natural analytical conclusion grounded in facts, with complete closing punctuation (. or !). Never end with open questions or generic morals.
- Sentence rhythm: No sentence over ~20 words. Include at least one short punchy sentence (under 8 words).
- Strictly 3 to 5 core quantitative figures from the newsletter.

=== ACCOUNTING CLASSIFICATION & ARITHMETIC HYGIENE ===
Strictly distinguish and NEVER conflate:
- Contractual commitments / backlog != funded debt (orders or cloud commitments are NOT funded balance-sheet debt).
- Debt guarantee / contingent exposure != funded loan.
- Net borrowing / budget deficit != gross bond issuance.
- GAAP net loss != operating cash burn (separate non-cash mark-to-market revaluations from operating cash flow).
- Secondary market bond yield != coupon / current interest expense.
- Gross mortgage originations != net credit growth.
- Liabilities of non-monetary financial institutions (leasing/factoring) != corporate capex debt.
- Prepayments (customer advances): deferred revenue; the CUSTOMER is the creditor who paid upfront and bears the non-delivery / credit risk, NOT the supplier.
- Exact arithmetic: compute ratios/multiples accurately (e.g. 40.3% / 11.8% = 3.41 -> "3.4x", NEVER round up to "4x"). Always name the exact period and base for every figure.
- Causal evidence & no legal prophecies: never leap to WACC, ROIC, NIM, or deposit rates without the intermediate link. Preserve exact event status (considers/proposed/draft != launched). Never predict EU/court rulings.

=== TYPOGRAPHY, LEXICAL HYGIENE & BANNED PATTERNS ===
- ZERO DASHES RULE: Strictly forbid em-dashes (—) and en-dashes (–) anywhere in the post, and forbid hyphens (-) in numeric ranges. Write ALL numeric ranges with "to" (e.g. "20 to 22%", "$1.1 to $1.3 trillion", NEVER "20–22%" or "20-22%").
- LEXICAL HYGIENE: Mention "balance sheet" or "balance-sheet" strictly AT MOST ONCE in the entire post.
- NO WORD COUNTERS OR SCAFFOLDING: NEVER output word counts in parentheses (e.g. "market (12)"). Ban scaffolding: "The mechanism is clear", "The logic is simple:", "The takeaway is simple:", "introduces two critical...", "sits alongside", "two critical figures".
- NO ANTITHESES OR ABSOLUTISM: Ban "isn't just", "is not just ... it is", and deterministic intensifiers: "inevitably", "permanently", "entirely", "directly", "effectively", "immediately", "fundamentally", "will force", "no longer".
- NO PARASITES/CLICHÉS: Ban "structural", "compress/compressing", "aggressive/aggressively", "fundamentally reshaping", "presents the illusion", "bottleneck", "liquidity plumbing", "is emerging as", "consequently", "far exceeding", "crowding out", "this mechanism shows", "underlying economic reality".
- Clean plain text inside BODY (no markdown headers, no bullet checklists, no links, no hashtags unless strictly 1 specific tag).

=== OUTPUT FORMAT (EXACTLY THESE 3 FIELDS) ===
HEADLINE: (concise 6-10 word institutional headline for internal tracking)
BODY:
(2-3 short paragraphs separated by blank lines, 100-120 words total)
FIGURES: (each quantitative figure used -> source context from the newsletter, separated by "; ", e.g. "17.8 billion -> FT newsletter; 13% -> FT newsletter"; or "none used")

--- NEWSLETTER SOURCE ({newsletter_name}) ---
SUBJECT: {subject}
DATE: {date}

{clean_text}
"""


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def prioritize_newsletters(
    newsletters: list[FTNewsletter],
    max_drafts: int | None = None,
) -> list[FTNewsletter]:
    """Сортирует письма по приоритету рассылок и ограничивает до FT_MAX_DRAFTS_PER_RUN."""
    limit = max_drafts if max_drafts is not None else _env_int("FT_MAX_DRAFTS_PER_RUN", FT_MAX_DRAFTS_PER_RUN)
    if limit <= 0:
        return []
    ordered = sorted(
        newsletters,
        key=lambda nl: _PRIORITY_INDEX.get(nl.newsletter_name, len(NEWSLETTER_PRIORITY)),
    )
    return ordered[:limit]


def _build_stage1_prompt(
    newsletter: FTNewsletter,
    history_path: Path | None = None,
    now: datetime | None = None,
) -> str:
    prompt = FT_ANALYSIS_PROMPT.format(
        newsletter_name=newsletter.newsletter_name,
        subject=newsletter.subject,
        date=newsletter.date,
        clean_text=newsletter.clean_text[:12000],
    )

    target_history = history_path or brain.SINGLE_TOPIC_HISTORY_PATH
    recent_entities = brain.get_recent_single_topic_entities(
        days=7,
        now=now,
        path=target_history,
    )
    if recent_entities:
        text_low = newsletter.clean_text.lower()
        matched = [e for e in recent_entities if e.lower() in text_low]
        banned_list = ", ".join(matched if matched else recent_entities)
        prompt += (
            "\n=== 7-DAY ANTI-DUPLICATE BAN FOR SINGLE TOPIC ===\n"
            "STRICTLY FORBIDDEN to feature the following company/entity as the main subject "
            f"more than once in 7 days: {banned_list}.\n"
            "Exception: ONLY if a fundamentally new milestone event occurred (for example, official IPO pricing). "
            "Otherwise, if the newsletter is an overview covering multiple companies, pick a different company or macro mechanism instead.\n"
        )
    return prompt


_FIELD_SPLIT_RE = re.compile(
    r"^\s*\*{0,2}(HEADLINE|BODY|FIGURES)\*{0,2}\s*:\s*(.*)$",
    re.IGNORECASE,
)


def parse_stage1_output(raw: str) -> tuple[str, str, str]:
    """Разбирает ответ Stage 1 на (headline, body, figures_raw)."""
    text = re.sub(r"^```(?:markdown|text)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    if not text:
        return "", "", "none used"

    sections: dict[str, list[str]] = {}
    current_field: str | None = None
    unlabeled: list[str] = []

    for line in text.splitlines():
        m = _FIELD_SPLIT_RE.match(line)
        if m:
            current_field = m.group(1).upper()
            sections.setdefault(current_field, [])
            tail = m.group(2).strip()
            if tail:
                sections[current_field].append(tail)
        elif current_field is not None:
            sections[current_field].append(line)
        else:
            unlabeled.append(line)

    headline = "\n".join(sections.get("HEADLINE", [])).strip()
    body = "\n".join(sections.get("BODY", [])).strip()
    figures = "\n".join(sections.get("FIGURES", [])).strip() or "none used"

    if not body:
        body = "\n".join(unlabeled).strip() or text

    return headline, body, figures


_FT_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s+per\s+cent\b", re.IGNORECASE)
_FT_BN_RE = re.compile(r"(?<![A-Za-z0-9])([\$€£]?\d+(?:,\d{3})*(?:\.\d+)?)\s*bn\b", re.IGNORECASE)
_FT_MN_RE = re.compile(r"(?<![A-Za-z0-9])([\$€£]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:mn|m)\b", re.IGNORECASE)

_WORD_NUMBERS_1_TO_10: dict[str, str] = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}
_FT_WORD_NUM_PATTERN = "|".join(_WORD_NUMBERS_1_TO_10.keys())
_FT_WORD_TIME_RE = re.compile(
    rf"(?<![A-Za-z0-9-])(?P<num>{_FT_WORD_NUM_PATTERN})(?P<sep>[-\s]+)"
    r"(?P<unit>years?|months?|weeks?|days?|quarters?|decades?|hours?)\b",
    re.IGNORECASE,
)
_FT_WORD_PERCENT_RE = re.compile(
    rf"(?<![A-Za-z0-9-])(?P<num>{_FT_WORD_NUM_PATTERN})\s+per\s*cent\b",
    re.IGNORECASE,
)
_FT_WORD_POINTS_RE = re.compile(
    rf"(?<![A-Za-z0-9-])(?P<num>{_FT_WORD_NUM_PATTERN})\s+"
    r"(?P<unit>(?:percentage|basis)\s+points?|bps|pp)\b",
    re.IGNORECASE,
)


def normalize_ft_units_for_verify(text: str) -> str:
    """Нормализует британский стиль чисел FT для сверки в verify_drafts."""
    if not text:
        return ""
    out = _FT_PERCENT_RE.sub(r"\1%", text)
    out = _FT_BN_RE.sub(r"\1 billion", out)
    out = _FT_MN_RE.sub(r"\1 million", out)
    out = _FT_WORD_PERCENT_RE.sub(
        lambda m: f"{_WORD_NUMBERS_1_TO_10[m.group('num').lower()]}%",
        out,
    )
    out = _FT_WORD_TIME_RE.sub(
        lambda m: f"{_WORD_NUMBERS_1_TO_10[m.group('num').lower()]}{m.group('sep')}{m.group('unit')}",
        out,
    )
    out = _FT_WORD_POINTS_RE.sub(
        lambda m: f"{_WORD_NUMBERS_1_TO_10[m.group('num').lower()]} {m.group('unit')}",
        out,
    )
    return out


def verify_ft_draft(
    draft_body: str,
    newsletter: FTNewsletter,
    ledger_path: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> str:
    """Верифицирует цифры черновика и entity_ledger через verify.verify_drafts."""
    if not draft_body or not draft_body.strip():
        return ""

    normalized_text = normalize_ft_units_for_verify(newsletter.clean_text)
    combined_body = (
        f"{newsletter.clean_text}\n\n{normalized_text}"
        if normalized_text and normalized_text != newsletter.clean_text
        else newsletter.clean_text
    )

    selected = [
        {
            "item": type("FTItemDummy", (), {"url": "https://www.ft.com"})(),
            "body": combined_body,
        }
    ]
    draft_block_format = (
        "SHAPE: single\n"
        f"BODY: {draft_body.strip()}\n"
        "FIGURES: none used\n"
        "SOURCE: Story 1\n"
        f"WHY_THIS_ONE: FT {newsletter.newsletter_name}\n"
        "VERDICT: POST\n"
        f"WHY: FT {newsletter.newsletter_name}\n"
        "CHECK_FIRST: -\n"
    )

    tmp_dir: tempfile.TemporaryDirectory[str] | None = None
    eff_ledger_path = ledger_path
    if dry_run and eff_ledger_path is None and verify.ENTITY_LEDGER_PATH.exists():
        # В dry-run проверяем реальные кросс-дневные конфликты без изменения state/entity_ledger.json
        tmp_dir = tempfile.TemporaryDirectory()
        tmp_ledger = Path(tmp_dir.name) / "entity_ledger.json"
        shutil.copy2(verify.ENTITY_LEDGER_PATH, tmp_ledger)
        eff_ledger_path = tmp_ledger

    try:
        _, _, blocks = verify.verify_drafts(
            draft_block_format,
            selected=selected,
            data_text="",
            ledger_path=eff_ledger_path,
            now=now,
        )
        if not blocks:
            return ""
        b = blocks[0]
        countable = [r for r in b.get("_level_a", []) if r["status"] != "YEAR"]
        denom = len(countable)
        found = sum(1 for r in countable if r["status"] == "FOUND") if denom else 0
        verdict = (b.get("verdict") or "POST").strip().upper()
        if b.get("_downgrade") and verdict == "POST":
            verdict = "MAYBE"

        offending = b.get("_offending") or []
        if not denom:
            footer = f"{verdict} · цифры не заявлены"
        elif found == denom:
            footer = f"{verdict} · цифры {found}/{denom} ✅"
        elif offending:
            shown = offending[:3]
            extra = f" +{len(offending) - 3} more" if len(offending) > 3 else ""
            footer = f"{verdict} · цифры {found}/{denom} — сверь {', '.join(shown)}{extra}"
        else:
            footer = f"{verdict} · цифры {found}/{denom}"

        conflicts = b.get("_cross_day_conflicts") or []
        if conflicts:
            footer += "\n" + "\n".join(conflicts)
        return footer
    except Exception as exc:
        log.warning("Верификация FT черновика упала: %s", exc)
        return "POST — числа не верифицированы"
    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()


def format_ft_telegram_message(
    newsletter: FTNewsletter,
    body: str,
    verify_footer: str = "",
    style_flags: list[str] | None = None,
    editor_failed: bool = False,
) -> str:
    """Формирует сообщение в чистом Telegram HTML."""
    badge = f" {brain.FALLBACK_BADGE}" if editor_failed else ""
    parts = [
        f"<b>FT PREMIUM · {newsletter.newsletter_name}</b>{badge}\n<i>{newsletter.subject}</i>",
        body.strip(),
        "───────────────",
    ]
    footer_lines: list[str] = []
    if verify_footer:
        footer_lines.append(verify_footer.strip())
    if style_flags:
        flags_str = ", ".join(f.replace("[STYLE] ", "") for f in style_flags)
        footer_lines.append(f"⚠️ STYLE FLAGS: {flags_str}")
    if footer_lines:
        parts[-1] = parts[-1] + "\n" + "\n".join(footer_lines)

    return "\n\n".join(parts[:2]) + "\n\n" + parts[2]


def process_newsletter(
    newsletter: FTNewsletter,
    history_path: Path | None = None,
    ledger_path: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Прогоняет 1 письмо FT через Drafter -> Editor -> verify -> linter -> Telegram HTML."""
    prompt = _build_stage1_prompt(newsletter, history_path=history_path, now=now)
    log.info(
        "FT Stage 1 (Drafter) для '%s' (%s, %d симв. текста)",
        newsletter.newsletter_name,
        newsletter.subject[:60],
        len(newsletter.clean_text),
    )
    stage1_raw = brain._call(prompt, as_json=False, temperature=0.3, max_tokens=16384)
    headline, raw_body, _figures_raw = parse_stage1_output(stage1_raw)

    editor_failed = False
    try:
        edited = brain.edit_and_humanize_draft(raw_body, draft_type="single")
        if brain.FALLBACK_BADGE in edited:
            editor_failed = True
            final_body = edited.replace(brain.FALLBACK_BADGE, "").strip() or raw_body
        elif edited and edited.strip():
            final_body = edited.strip()
        else:
            editor_failed = True
            final_body = raw_body
    except Exception as exc:
        log.warning("FT Stage 2 (Editor) упал: %s", exc)
        editor_failed = True
        final_body = raw_body

    verify_footer = verify_ft_draft(
        final_body,
        newsletter=newsletter,
        ledger_path=ledger_path,
        now=now,
        dry_run=dry_run,
    )

    local_results, local_passed = score_draft.run_local_checks(final_body, shape="single")
    if not local_passed:
        failed_checks = [f"{name}: {detail}" for name, ok, detail in local_results if not ok]
        log.warning("FT local checks warnings (%s): %s", newsletter.newsletter_name, "; ".join(failed_checks))

    style_flags = score_draft.lint_style_ticks(final_body)

    if not dry_run:
        brain.inspect_and_collect_flagged_phrases(
            final_body,
            f"FT PREMIUM · {newsletter.newsletter_name}",
        )

    message = format_ft_telegram_message(
        newsletter=newsletter,
        body=final_body,
        verify_footer=verify_footer,
        style_flags=style_flags,
        editor_failed=editor_failed,
    )

    return {
        "newsletter": newsletter,
        "headline": headline,
        "body": final_body,
        "editor_failed": editor_failed,
        "verify_footer": verify_footer,
        "style_flags": style_flags,
        "local_passed": local_passed,
        "message": message,
    }


def run_workflow(
    dry_run: bool = False,
    from_file: str | Path | None = None,
    lookback_hours: int | None = None,
    max_drafts: int | None = None,
    seen_path: Path | None = None,
    history_path: Path | None = None,
    ledger_path: Path | None = None,
    now: datetime | None = None,
    imap_client: Any = None,
) -> list[str]:
    """Запускает полный FT IMAP пайплайн и возвращает список сформированных сообщений."""
    log.info(
        "=== Запуск FT Premium пайплайна (dry_run=%s, from_file=%s) ===",
        dry_run,
        from_file,
    )

    if from_file is not None:
        newsletters = ft_imap.load_newsletter_from_file(from_file)
    else:
        newsletters = ft_imap.fetch_ft_newsletters(
            lookback_hours=lookback_hours,
            seen_path=seen_path,
            now=now,
            imap_client=imap_client,
        )

    if not newsletters:
        log.info("Новых писем FT не найдено — выход.")
        return []

    selected = prioritize_newsletters(newsletters, max_drafts=max_drafts)
    log.info("Отобрано рассылок FT к генерации: %d из %d", len(selected), len(newsletters))

    seen = ft_imap.load_ft_seen(path=seen_path, now=now) if not dry_run else {}
    messages: list[str] = []

    for nl in selected:
        result = process_newsletter(
            nl,
            history_path=history_path,
            ledger_path=ledger_path,
            now=now,
            dry_run=dry_run,
        )
        msg = result["message"]
        messages.append(msg)

        if dry_run:
            print("\n" + "=" * 60 + "\n")
            print(msg)
            print("\n" + "=" * 60 + "\n")
        else:
            try:
                deliver.send(msg)
                log.info("FT черновик '%s' успешно отправлен в Telegram.", nl.newsletter_name)
            except Exception as exc:
                log.error("Ошибка отправки FT черновика в Telegram (%s): %s", nl.newsletter_name, exc)
                raise

            ft_imap.mark_newsletter_seen(nl, seen, now=now)
            ft_imap.save_ft_seen(seen, path=seen_path)
            brain.save_single_topic_history(
                result["body"],
                headline=result["headline"] or nl.subject,
                days=7,
                now=now,
                path=history_path,
                source_pipeline="ft",
            )

    return messages


def _format_inspect_date(date_str: str) -> str:
    if not date_str:
        return "-"
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return date_str[:20]


def format_inbox_inspection_table(items: list[ft_imap.InspectedEmail]) -> str:
    """Форматирует таблицу диагностики входящих писем FT для --inspect-inbox."""
    headers = ("Дата", "Рассылка", "Статус", "Длина", "Тема")
    rows = [
        (
            _format_inspect_date(item.date),
            item.newsletter_name,
            item.status,
            str(item.text_len),
            item.subject,
        )
        for item in items
    ]
    w0 = max([len(headers[0]), *(len(r[0]) for r in rows)])
    w1 = max([len(headers[1]), *(len(r[1]) for r in rows)])
    w2 = max([len(headers[2]), *(len(r[2]) for r in rows)])
    w3 = max([len(headers[3]), *(len(r[3]) for r in rows)])

    def _fmt(row: tuple[str, str, str, str, str]) -> str:
        return f"{row[0]:<{w0}} | {row[1]:<{w1}} | {row[2]:<{w2}} | {row[3]:>{w3}} | {row[4]}"

    lines = [_fmt(headers), f"{'-' * w0}-+-{'-' * w1}-+-{'-' * w2}-+-{'-' * w3}-+-{'-' * 20}"]
    if not rows:
        lines.append("Писем в окне lookback не найдено.")
    else:
        lines.extend(_fmt(r) for r in rows)
    return "\n".join(lines)


def inspect_inbox(
    lookback_hours: int | None = None,
    seen_path: Path | None = None,
    now: datetime | None = None,
    imap_client: Any = None,
) -> list[ft_imap.InspectedEmail]:
    """Диагностика входящих писем IMAP без вызова Gemini, Telegram и записи ft_seen.json."""
    items = ft_imap.inspect_ft_inbox(
        lookback_hours=lookback_hours,
        seen_path=seen_path,
        now=now,
        imap_client=imap_client,
    )
    print(format_inbox_inspection_table(items))
    return items


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="FT Premium IMAP-пайплайн")
    parser.add_argument(
        "--dry-run",
        "--dry",
        dest="dry_run",
        action="store_true",
        help="Выполнить забор и генерацию без отправки в Telegram и без записи ft_seen.json",
    )
    parser.add_argument(
        "--inspect-inbox",
        action="store_true",
        help="Показать таблицу писем в IMAP за окно FT_LOOKBACK_HOURS без вызова Gemini и Telegram",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        default=None,
        help="Путь к локальному .eml, .html или .txt файлу вместо IMAP",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Окно свежести писем в часах (по умолчанию FT_LOOKBACK_HOURS или 48)",
    )
    parser.add_argument(
        "--max-drafts",
        type=int,
        default=None,
        help="Максимум черновиков за запуск (по умолчанию FT_MAX_DRAFTS_PER_RUN или 4)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    if args.inspect_inbox:
        inspect_inbox(lookback_hours=args.hours)
        return 0

    run_workflow(
        dry_run=args.dry_run,
        from_file=args.from_file,
        lookback_hours=args.hours,
        max_drafts=args.max_drafts,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
