"""Weekly Phrase Ledger CLI inspector.

Reads flagged phrases from state/flagged_phrases.json, displays them
in a readable grouped format (Дата | Черновик | Найденные фразы),
and optionally clears the ledger with --clear.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Windows console encoding fix
for stream in (sys.stdout, sys.stderr, sys.stdin):
    if stream and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "flagged_phrases.json"


def load_ledger(path: Path = LEDGER_PATH) -> list[dict]:
    """Загружает список записей из flagged_phrases.json.
    Возвращает пустой список, если файл отсутствует, пуст или содержит некорректный JSON.
    """
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return []
        data = json.loads(content)
        if isinstance(data, list):
            return data
        return []
    except Exception as exc:
        sys.stderr.write(f"Warning: Failed to parse ledger {path}: {exc}\n")
        return []


def clear_ledger(path: Path = LEDGER_PATH) -> None:
    """Очищает массив в flagged_phrases.json, записывая пустой список []."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]\n", encoding="utf-8")


def format_records(records: list[dict]) -> str:
    """Форматирует записи в виде списка строк: Дата | Черновик | Найденные фразы."""
    if not records:
        return "Weekly Phrase Ledger is clean."

    lines: list[str] = []
    for r in records:
        date = r.get("date", "Unknown date")
        draft = r.get("draft_title", "Unknown draft")
        phrases = r.get("flagged_phrases", [])
        if isinstance(phrases, list):
            phrases_str = ", ".join(f'"{p}"' if "," in str(p) else str(p) for p in phrases)
        else:
            phrases_str = str(phrases)
        lines.append(f"{date} | {draft} | {phrases_str}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or clear Weekly Phrase Ledger.")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Очистить массив в state/flagged_phrases.json после еженедельного просмотра.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=LEDGER_PATH,
        help="Путь к файлу flagged_phrases.json (по умолчанию: state/flagged_phrases.json).",
    )
    args = parser.parse_args(argv)

    if args.clear:
        clear_ledger(args.path)
        sys.stdout.write("Weekly Phrase Ledger cleared.\n")
        return 0

    records = load_ledger(args.path)
    if not records:
        sys.stdout.write("Weekly Phrase Ledger is clean.\n")
        return 0

    sys.stdout.write(format_records(records) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
