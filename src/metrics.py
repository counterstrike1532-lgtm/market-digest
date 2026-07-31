"""Тихое накопление метрик прогона (T9d). Не шлёт ничего в Телеграм, ничего не
визуализирует - просто пишет одну запись в state/metrics.json на прогон, для
будущей динамической приоритизации источников. Каждое чтение/запись с
encoding="utf-8" (см. ГРАБЛИ п.2).
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

KEEP_DAYS = 90


def load(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("metrics.json не читается, начинаю заново: %s", exc)
        return []


def _trim(records: list[dict], keep_days: int = KEEP_DAYS) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    return [r for r in records if r.get("date", "") > cutoff]


def append(path: pathlib.Path, record: dict, keep_days: int = KEEP_DAYS) -> None:
    records = load(path)
    records.append(record)
    records = _trim(records, keep_days)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(records, indent=0, ensure_ascii=False), encoding="utf-8")
    log.info("метрики прогона записаны (%d записей за последние %d дней)",
             len(records), keep_days)


def build_record(*, collected: int, after_dedup: int, after_freshness: int,
                 prefiltered: int, ranked: int, drafted: int, verdicts: dict,
                 figures: dict, gemini_successful: int, gemini_quota_refused: int,
                 draft_model: str | None, market_source: dict) -> dict:
    return {
        "date": datetime.now(timezone.utc).isoformat(),
        "collected": collected,
        "after_dedup": after_dedup,
        "after_freshness": after_freshness,
        "prefiltered": prefiltered,
        "ranked": ranked,
        "drafted": drafted,
        "verdicts": verdicts,
        "figures": figures,
        "gemini_successful": gemini_successful,
        "gemini_quota_refused": gemini_quota_refused,
        "draft_model": draft_model,
        "market_source": market_source,
    }
