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

from . import brain, charts, collect, deliver, enrich, numbers, verify

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEEN = ROOT / "state" / "seen.json"
STYLE = ROOT / "style" / "my_posts.md"

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("digest")

# Отбор Gemini теперь один запрос на весь список (см. brain.rank), а дневная
# квота free tier - 20 запросов на модель. Значит вход в rank() надо ограничить
# заранее, дёшево и без LLM. Приоритетные теги - первоисточники.
_PREFILTER_TAG_BONUS = {"poland_official": 1.0, "us_official": 1.0, "ai_primary": 1.0,
                        "eu_official": 0.6}


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


def build_message(selected, data, drafts) -> str:
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    lines = [f"СВОДКА {today}", ""]

    if data:
        lines.append("ЦИФРЫ")
        for k, v in data.items():
            bits = [str(v.get("value"))]
            for f, lbl in (("chg_1d_pct", "д"), ("chg_1m_pct", "мес"),
                           ("chg_30d_pct", "30д"), ("chg_1y_pct", "г")):
                if f in v:
                    bits.append(f"{v[f]:+.2f}% {lbl}")
            lines.append(f"  {k}: {'  '.join(bits)}   [{v.get('as_of','')}]")
        lines.append("")

    lines.append(f"СЮЖЕТЫ ({len(selected)})")
    for i, s in enumerate(selected, 1):
        it = s["item"]
        date_str = it.published[:10] if it.published_known else "дата неизвестна"
        lines.append(f"\n{i}. [{s.get('score')}/10] {it.title}")
        lines.append(f"   {it.source}, {date_str} — {it.url}")
        if s.get("angle"):
            lines.append(f"   угол: {s['angle']}")
        if s.get("why_nonobvious"):
            lines.append(f"   неочевидно: {s['why_nonobvious']}")
        if "verified" in s and not s["verified"]:
            lines.append("   ! текст статьи не догружен — цифры в угле не проверены")

    lines += ["", "=" * 30, "", "ЧЕРНОВИКИ", "", drafts]
    return "\n".join(lines)


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

    market_chart = None
    if not args.no_charts:
        try:
            market_chart = charts.market_overview(data, theme="dark")
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
    drafts = brain.draft(selected, data, style_text, n=args.drafts)

    figures_chart = None
    if not args.no_charts:
        try:
            pairs = charts.parse_figures(first_draft_figures(drafts))
            figures_chart = charts.figures_chart(pairs, title="Черновик 1", theme="light")
        except Exception as exc:
            log.warning("figures_chart упал: %s", exc)

    log.info("--- верификация цифр ---")
    try:
        drafts = verify.verify_drafts(drafts, selected, brain.format_data_block(data))
    except Exception as exc:
        log.warning("verify_drafts упал: %s", exc)

    msg = build_message(selected, data, drafts)

    if args.dry:
        print("\n" + msg)
        if market_chart:
            print(f"\n[график] рынки: {market_chart}")
        if figures_chart:
            print(f"[график] к черновику 1: {figures_chart}")
    else:
        if market_chart:
            deliver.send_photo(market_chart, "Рынки за месяц")
        deliver.send(msg)
        if figures_chart:
            deliver.send_photo(figures_chart,
                               "График к черновику 1 — можно приложить к посту")
        now = datetime.now(timezone.utc).isoformat()
        for s in selected:
            seen[s["item"].key] = now
        save_seen(seen)

    q = brain.quota_summary()
    log.info("расход квоты Gemini: %d всего (успешных %d, отказов квоты %d)",
             q["total"], q["successful"], q["quota_refused"])
    log.info("готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())
