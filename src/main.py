"""Точка входа. Запуск: python -m src.main [--dry] [--hours 24]"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone, timedelta

import yaml

from . import brain, collect, deliver, enrich, numbers

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEEN = ROOT / "state" / "seen.json"
STYLE = ROOT / "style" / "my_posts.md"

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("digest")


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
        lines.append(f"\n{i}. [{s.get('score')}/10] {it.title}")
        lines.append(f"   {it.source} — {it.url}")
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

    log.info("--- цифры ---")
    data = numbers.gather(cfg)

    log.info("--- отбор ---")
    selected = brain.rank(fresh, top_n=args.top)
    if not selected:
        log.warning("отбор не дал ничего — сегодня без сводки")
        return 0

    log.info("--- догрузка текста статей ---")
    enrich.enrich(selected, limit=args.drafts + 3)

    log.info("--- черновики ---")
    style_text = STYLE.read_text(encoding="utf-8") if STYLE.exists() else ""
    drafts = brain.draft(selected, data, style_text, n=args.drafts)

    msg = build_message(selected, data, drafts)

    if args.dry:
        print("\n" + msg)
    else:
        deliver.send(msg)
        now = datetime.now(timezone.utc).isoformat()
        for s in selected:
            seen[s["item"].key] = now
        save_seen(seen)

    log.info("готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())
