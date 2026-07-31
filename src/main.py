"""Точка входа. Запуск: python -m src.main [--dry] [--hours 24]"""
from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib
import re
import sys
from datetime import datetime, timezone, timedelta

import yaml

from . import brain, charts, collect, deliver, enrich, metrics, numbers, verify

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEEN = ROOT / "state" / "seen.json"
METRICS = ROOT / "state" / "metrics.json"
STYLE = ROOT / "style" / "my_posts.md"

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("digest")

# Отбор Gemini теперь один запрос на весь список (см. brain.rank), а дневная
# квота free tier - 20 запросов на модель. Значит вход в rank() надо ограничить
# заранее, дёшево и без LLM. Приоритетные теги - первоисточники.
_PREFILTER_TAG_BONUS = {"poland_official": 1.0, "us_official": 1.0, "ai_primary": 1.0,
                        "eu_official": 0.6}

# Цена пая ETFBW20TR.WA (~80) и уровень индекса WIG20 (~2500+) - разные по
# порядку числа. Печатать голый "value" для этого ключа в ЦИФРЫ - провоцировать
# ложную цитату "уровень WIG20 сейчас X" под настоящим именем (T9 fix 2).
# Проценты изменения корректны в любом случае (это отношение close/close),
# их печатаем как обычно.
_LEVEL_HIDDEN = {"WIG20 TR (ETF)"}


def heuristic_prefilter(items: list, hours: int, cap: int = 100) -> list:
    """Без LLM сужает список до cap самых перспективных по weight/tag/свежести/social."""
    now = datetime.now(timezone.utc)

    def score(it) -> float:
        age_h = (now - datetime.fromisoformat(it.published)).total_seconds() / 3600
        freshness = max(0.0, 1 - age_h / hours)
        social = min(1.0, math.log1p(it.social) / math.log1p(1000))
        return it.weight * 2 + _PREFILTER_TAG_BONUS.get(it.tag, 0.0) + freshness + social

    ranked = sorted(items, key=score, reverse=True)
    kept = ranked[:cap]
    log.info("эвристический предотбор: оставили %d из %d", len(kept), len(items))
    return kept


def filter_by_age(items: list, max_age_days: int) -> list:
    """Догляд поверх --hours: у фида (особенно Google News c q=) запись иногда
    приходит без пригодной даты публикации, и до сих пор такой записи молча
    подставлялось "сейчас" - без проверки возраста она проходила как свежая,
    даже будь ей год. Запись без даты (published_known=False) не отбрасываем
    по требованию - пропускаем как есть, дату в сводке пометим отдельно."""
    now = datetime.now(timezone.utc)
    kept, dropped = [], []
    for it in items:
        if not it.published_known:
            kept.append(it)
            continue
        pub = datetime.fromisoformat(it.published)
        if pub.tzinfo is None:                      # наивную дату считаем UTC
            pub = pub.replace(tzinfo=timezone.utc)
        age_days = (now - pub).total_seconds() / 86400
        if age_days > max_age_days:
            dropped.append((age_days, it))
        else:
            kept.append(it)
    if dropped:
        oldest_age, oldest_it = max(dropped, key=lambda x: x[0])
        log.info("отброшено по возрасту: %d (старейшая: %s, %s)",
                len(dropped), oldest_it.title[:80], oldest_it.published[:10])
    return kept


def load_seen(keep_days: int = 21) -> dict:
    if not SEEN.exists():
        return {}
    try:
        data = json.loads(SEEN.read_text(encoding="utf-8"))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        return {k: v for k, v in data.items() if v > cutoff}
    except Exception:
        return {}


def save_seen(seen: dict) -> None:
    SEEN.parent.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")


def first_draft_figures(drafts_text: str) -> str:
    """Текст FIGURES-поля первого черновика (draft #1 - без сигнала вроде VERDICT
    "лучший" тут просто первый по порядку). Не нашлось - пустая строка."""
    m = re.search(r"FIGURES:\s*(.*?)\n\s*SOURCE:", drafts_text, re.S)
    return m.group(1).strip() if m else ""


def first_draft_covers_one_story(drafts_text: str) -> bool:
    """DRAFT 1 - это digest (2-3 сюжета сразу, см. DRAFT_PROMPT), а не single -
    его FIGURES легко смешивает числа из разных сюжетов. figures_chart не умеет
    видеть, из какого сюжета каждая пара - надёжнее просто не рисовать график,
    когда SOURCE черновика перечисляет больше одной ссылки (T9f)."""
    m = re.search(r"SOURCE:\s*(.*?)\n\s*WHY_THIS_ONE:", drafts_text, re.S)
    if not m:
        return False
    urls = [u.strip() for u in re.split(r"[,\n]", m.group(1)) if u.strip()]
    return len(urls) <= 1


# Реальный прогон: "...energy transitions.- An AI-focused" (буллет приклеен к
# концу предыдущего предложения, без переноса строки) и "forcing more
# selling.This feedback loop" (новое предложение без пробела после точки -
# DRAFT_PROMPT прямо запрещает это ("spikes.Quarterly" - proofing failure), но
# модель иногда всё равно так пишет). Правим на рендере, не в промпте - см.
# CLAUDE.md, промпты в этом ТЗ не трогаем (T9 fix 5).
_GLUED_BULLET = re.compile(r"\.-(?=\s[A-Z])")
_GLUED_SENTENCE = re.compile(r"\.(?=[A-Z][a-z])")


def _fix_glued_punctuation(text: str) -> str:
    """Точка+буллет без переноса строки -> перенос перед буллетом. Точка+новое
    предложение без пробела -> пробел. Оба случая узко специфичны (буллет
    должен начинаться с большой буквы через пробел-тире; предложение - с
    большой буквы сразу после точки), поэтому не трогают десятичные дроби
    ("$3.5 billion") и обычные диапазоны/минусы."""
    text = _GLUED_BULLET.sub(".\n-", text)
    text = _GLUED_SENTENCE.sub(". ", text)
    return text


def _oneline(text: str) -> str:
    """Однострочные поля (заголовок, угол, неочевидно) иногда приходят с сырым
    переводом строки из RSS-описания или из ответа модели - схлопываем в одну
    строку, иначе он ломает межстрочные отступы сводки (T9e)."""
    return " ".join((text or "").split())


def build_message(selected, data, drafts) -> str:
    """Секции собираются списком и склеиваются "\\n\\n" явно - гарантированная
    пустая строка между ними (и между сюжетами) не зависит от того, сколько
    условных строк добавил конкретный сюжет. Раньше отступ держался на побочном
    эффекте f"\\n{i}. ..." внутри "\\n".join(lines) - хрупко (T9e)."""
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    sections = [f"СВОДКА {today}"]

    if data:
        figures_lines = ["ЦИФРЫ"]
        for k, v in data.items():
            bits = [] if k in _LEVEL_HIDDEN else [str(v.get("value"))]
            for f, lbl in (("chg_1d_pct", "д"), ("chg_1m_pct", "мес"),
                           ("chg_30d_pct", "30д"), ("chg_1y_pct", "г")):
                if f in v:
                    bits.append(f"{v[f]:+.2f}% {lbl}")
            figures_lines.append(f"  {k}: {'  '.join(bits)}   [{v.get('as_of','')}]")
        sections.append("\n".join(figures_lines))

    story_blocks = [f"СЮЖЕТЫ ({len(selected)})"]
    for i, s in enumerate(selected, 1):
        it = s["item"]
        date_str = it.published[:10] if it.published_known else "дата неизвестна"
        story_lines = [f"{i}. [{s.get('score')}/10] {_oneline(it.title)}",
                      f"   {it.source}, {date_str} — {it.url}"]
        if s.get("angle"):
            story_lines.append(f"   угол: {_oneline(s['angle'])}")
        if s.get("why_nonobvious"):
            story_lines.append(f"   неочевидно: {_oneline(s['why_nonobvious'])}")
        if "verified" in s and not s["verified"]:
            story_lines.append("   ! текст статьи не догружен — цифры в угле не проверены")
        story_blocks.append("\n".join(story_lines))
    sections.append("\n\n".join(story_blocks))

    sections += ["=" * 30, "ЧЕРНОВИКИ\n\n" + drafts]
    return "\n\n".join(sections)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="не отправлять в Телегу, печатать в консоль")
    ap.add_argument("--hours", type=int, default=26, help="окно свежести новостей")
    ap.add_argument("--drafts", type=int, default=3)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--no-charts", action="store_true", help="не рисовать и не слать графики")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))

    log.info("--- сбор ---")
    items = collect.collect_all(cfg, args.hours)

    seen = load_seen()
    fresh = [i for i in items if i.key not in seen]
    after_dedup = len(fresh)
    log.info("новых (не видели раньше): %d из %d", len(fresh), len(items))
    if not fresh:
        log.info("нечего показывать, выходим")
        return 0

    max_age_days = cfg.get("freshness", {}).get("max_age_days", 7)
    fresh = filter_by_age(fresh, max_age_days)
    if not fresh:
        log.info("после фильтра по возрасту ничего не осталось, выходим")
        return 0

    log.info("--- цифры ---")
    data = numbers.gather(cfg)
    market_source = data.pop("_market_source", {})

    market_chart = None
    if not args.no_charts:
        try:
            market_chart = charts.market_overview(data, market_source, theme="dark")
        except Exception as exc:
            log.warning("market_overview упал: %s", exc)
    data.pop("_series", None)   # сырые ряды дальше по конвейеру не нужны

    log.info("--- отбор ---")
    candidates = heuristic_prefilter(fresh, args.hours)
    selected = brain.rank(candidates, top_n=args.top)
    if not selected:
        log.warning("отбор не дал ничего — сегодня без сводки")
        return 0

    log.info("--- догрузка текста статей ---")
    enrich.enrich(selected, limit=args.drafts + 3)

    log.info("--- черновики ---")
    style_text = STYLE.read_text(encoding="utf-8") if STYLE.exists() else ""
    drafts = _fix_glued_punctuation(brain.draft(selected, data, style_text, n=args.drafts))
    draft_model = brain.last_model_used()

    figures_chart = None
    if not args.no_charts:
        try:
            if first_draft_covers_one_story(drafts):
                pairs = charts.parse_figures(first_draft_figures(drafts))
                figures_chart = charts.figures_chart(pairs, title="Черновик 1", theme="light")
            else:
                log.info("figures_chart: черновик 1 покрывает больше одного "
                        "сюжета - график пропущен")
        except Exception as exc:
            log.warning("figures_chart упал: %s", exc)

    log.info("--- верификация цифр ---")
    draft_stats = verify._empty_stats()
    try:
        drafts, draft_stats = verify.verify_drafts(drafts, selected,
                                                    brain.format_data_block(data))
    except Exception as exc:
        log.warning("verify_drafts упал: %s", exc)

    msg = build_message(selected, data, drafts)
    q = brain.quota_summary()
    # домен -> URL конкретной статьи: модель иногда путает "SOURCE:" с голым
    # доменом вместо ссылки - deliver._to_html подставляет реальный URL, чтобы
    # Telegram не заавтолинковал его сам на корень сайта (T9 fix 4)
    domain_urls = {s["item"].source: s["item"].url for s in selected}

    if args.dry:
        print("\n" + msg)
        if market_chart:
            print(f"\n[график] рынки: {market_chart}")
        if figures_chart:
            print(f"[график] к черновику 1: {figures_chart}")
    else:
        if market_chart:
            deliver.send_photo(market_chart, "Рынки за месяц")
        deliver.send(msg, domain_urls)
        if figures_chart:
            deliver.send_photo(figures_chart,
                               "График к черновику 1 — можно приложить к посту")
        now = datetime.now(timezone.utc).isoformat()
        for s in selected:
            seen[s["item"].key] = now
        save_seen(seen)

        metrics.append(METRICS, metrics.build_record(
            collected=len(items), after_dedup=after_dedup, after_freshness=len(fresh),
            prefiltered=len(candidates), ranked=len(selected),
            drafted=draft_stats["drafted"], verdicts=draft_stats["verdicts"],
            figures=draft_stats["figures"], gemini_successful=q["successful"],
            gemini_quota_refused=q["quota_refused"], draft_model=draft_model,
            market_source=market_source))

    log.info("расход квоты Gemini: %d всего (успешных %d, отказов квоты %d)",
             q["total"], q["successful"], q["quota_refused"])
    log.info("готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())
