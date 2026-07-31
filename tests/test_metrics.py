"""metrics.py: накопление state/metrics.json, обрезка до 90 дней (T9d).
Файлы только во временной директории pytest - state/ проекта не трогаем."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src import metrics


def _record(days_ago: int) -> dict:
    date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return metrics.build_record(
        collected=10, after_dedup=8, after_freshness=7, prefiltered=5, ranked=3,
        drafted=3, verdicts={"POST": 1, "MAYBE": 1, "SKIP": 1},
        figures={"found": 2, "not_found": 0, "mismatch": 0, "unparsed": 1, "no_source": 0},
        gemini_successful=2, gemini_quota_refused=0, draft_model="gemini-3.5-flash",
        market_source={"wig20": "stooq"}) | {"date": date}


def test_append_creates_file_and_writes_record(tmp_path):
    path = tmp_path / "metrics.json"
    metrics.append(path, _record(days_ago=0))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["collected"] == 10
    assert data[0]["draft_model"] == "gemini-3.5-flash"


def test_append_accumulates_across_calls(tmp_path):
    path = tmp_path / "metrics.json"
    metrics.append(path, _record(days_ago=2))
    metrics.append(path, _record(days_ago=1))
    metrics.append(path, _record(days_ago=0))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 3


def test_append_trims_records_older_than_90_days(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps([
        _record(days_ago=200),
        _record(days_ago=100),
        _record(days_ago=50),
    ]), encoding="utf-8")

    metrics.append(path, _record(days_ago=0))

    data = json.loads(path.read_text(encoding="utf-8"))
    # 200 и 100 дней - старше 90, вырезаны; 50-дневная и свежая остаются
    assert len(data) == 2


def test_append_is_encoded_utf8_with_cyrillic_safe(tmp_path):
    """Каждое чтение/запись файла - с encoding='utf-8' (ГРАБЛИ п.2)."""
    path = tmp_path / "metrics.json"
    rec = _record(days_ago=0)
    rec["draft_model"] = "gemini-Кириллица-тест"
    metrics.append(path, rec)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0]["draft_model"] == "gemini-Кириллица-тест"


def test_load_missing_file_returns_empty_list(tmp_path):
    assert metrics.load(tmp_path / "does-not-exist.json") == []


def test_load_corrupt_file_recovers_empty(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert metrics.load(path) == []
