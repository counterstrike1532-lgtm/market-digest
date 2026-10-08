"""Тесты для tools/inspect_ledger.py (CLI Weekly Phrase Ledger)."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys

from tools import inspect_ledger


def test_inspect_ledger_clean_when_file_missing(tmp_path: Path, capsys):
    missing_file = tmp_path / "non_existent.json"
    code = inspect_ledger.main(["--path", str(missing_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert "Weekly Phrase Ledger is clean." in captured.out


def test_inspect_ledger_clean_when_empty_array(tmp_path: Path, capsys):
    empty_file = tmp_path / "empty_ledger.json"
    empty_file.write_text("[]\n", encoding="utf-8")
    code = inspect_ledger.main(["--path", str(empty_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert "Weekly Phrase Ledger is clean." in captured.out


def test_inspect_ledger_displays_records(tmp_path: Path, capsys):
    ledger_file = tmp_path / "ledger.json"
    records = [
        {
            "date": "2026-10-06",
            "draft_title": "DRAFT 1 — DIGEST",
            "flagged_phrases": ["double whammy", "circular financing"],
        },
        {
            "date": "2026-10-07",
            "draft_title": "DRAFT 2 — SINGLE TOPIC",
            "flagged_phrases": ["plumbing"],
        },
    ]
    ledger_file.write_text(json.dumps(records, indent=2), encoding="utf-8")

    code = inspect_ledger.main(["--path", str(ledger_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert "2026-10-06 | DRAFT 1 — DIGEST | double whammy, circular financing" in captured.out
    assert "2026-10-07 | DRAFT 2 — SINGLE TOPIC | plumbing" in captured.out


def test_inspect_ledger_clear(tmp_path: Path, capsys):
    ledger_file = tmp_path / "ledger_to_clear.json"
    records = [
        {
            "date": "2026-10-06",
            "draft_title": "DRAFT 1 — DIGEST",
            "flagged_phrases": ["catch falling knives"],
        }
    ]
    ledger_file.write_text(json.dumps(records, indent=2), encoding="utf-8")

    code = inspect_ledger.main(["--path", str(ledger_file), "--clear"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Weekly Phrase Ledger cleared." in captured.out

    # Проверяем, что файл теперь содержит пустой массив []
    content = ledger_file.read_text(encoding="utf-8").strip()
    assert json.loads(content) == []
