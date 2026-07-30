"""CLI: проверяет один черновик поста перед публикацией.

Использование:
  python -m src.score_draft draft.txt
  Get-Content draft.txt | python -m src.score_draft
  python -m src.score_draft draft.txt --deep     (+ смысловая проверка Gemini)

Локальные проверки бесплатны и идут всегда первыми, вывод сразу. Gemini имеет
дневной лимит 20 запросов на модель (см. brain.py), поэтому вызывается только
по --deep и только если локальные проверки уже пройдены - незачем тратить
запрос на пост, который провалился по длине или хэштегам.

Пост не переписывается - только PASS/FAIL и конкретные правки.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

from . import brain

BANNED_WORDS = [
    "leverage", "synergy", "landscape", "paradigm", "unprecedented", "game-changer",
    "delve", "underscore", "pivotal", "robust", "it's not just", "here's the thing",
]

QUESTIONING_PHRASES = [
    "what am i missing", "i might be wrong", "i might be reading this wrong",
    "correct me if", "am i off base",
]

ROLE_PHRASES = [
    "as a finance student", "as a student", "for a finance student",
    "as someone learning", "as someone studying",
]

ENGAGEMENT_BAIT = [
    "thoughts?", "what do you think?", "agree?", "let me know what you think",
    "drop a comment", "comment below", "thoughts below",
]

GENERIC_HASHTAGS = {"#finance", "#macroeconomics", "#gpw", "#forex"}


def _find_any(text_low: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if p in text_low]


def check_length(text: str, low: str):
    n = len(text.split())
    return 110 <= n <= 170, f"{n} слов (нужно 110-170)"


def check_has_number(text: str, low: str):
    ok = bool(re.search(r"\d", text))
    return ok, ("цифра есть" if ok else "чисел нет")


def check_banned_words(text: str, low: str):
    hits = _find_any(low, BANNED_WORDS)
    return not hits, ("нет" if not hits else "найдено: " + ", ".join(hits))


def check_engagement_bait(text: str, low: str):
    hits = _find_any(low[-120:], ENGAGEMENT_BAIT)
    return not hits, ("нет" if not hits else "в конце: " + ", ".join(hits))


def check_hashtags(text: str, low: str):
    tags = re.findall(r"#\w+", text)
    generic = [t for t in tags if t.lower() in GENERIC_HASHTAGS]
    ok = len(tags) <= 1 and not generic
    detail = f"{len(tags)} шт {tags}" if tags else "0"
    if generic:
        detail += f", generic: {generic}"
    return ok, detail


def check_no_links(text: str, low: str):
    hit = re.search(r"https?://|www\.", low)
    return not hit, ("нет" if not hit else "есть ссылка в теле")


def check_no_rhetorical_open(text: str, low: str):
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0].strip()
    ok = not first.endswith("?")
    return ok, ("ок" if ok else f'открывается вопросом: "{first}"')


def check_number_format(text: str, low: str):
    # Десятичная запятая (2,6%): цифра-запятая-1-2 цифры, дальше не цифра.
    # Разделитель тысяч (58,600) не задевает - там ровно 3 цифры после запятой.
    hits = re.findall(r"\d,\d{1,2}(?!\d)", text)
    return not hits, ("нет" if not hits else "похоже на decimal-comma: " + ", ".join(hits))


def check_role_phrases(text: str, low: str):
    hits = _find_any(low, ROLE_PHRASES)
    return not hits, ("нет" if not hits else "проговаривает роль: " + ", ".join(hits))


def check_questioning_posture(text: str, low: str):
    hits = _find_any(low, QUESTIONING_PHRASES)
    return not hits, ("нет" if not hits else "вопросительная позиция: " + ", ".join(hits))


LOCAL_CHECKS = [
    ("длина 110-170 слов", check_length),
    ("есть хотя бы одно число", check_has_number),
    ("нет запрещённых слов", check_banned_words),
    ("нет engagement-bait в конце", check_engagement_bait),
    ("хэштегов 0-1, не generic", check_hashtags),
    ("нет ссылок в теле", check_no_links),
    ("не открывается риторическим вопросом", check_no_rhetorical_open),
    ("числа в английском формате", check_number_format),
    ("не проговаривает роль автора", check_role_phrases),
    ("не в вопросительной позиции", check_questioning_posture),
]


def run_local_checks(text: str) -> tuple[list[tuple[str, bool, str]], bool]:
    low = text.lower()
    results = [(name, *fn(text, low)) for name, fn in LOCAL_CHECKS]
    return results, all(ok for _, ok, _ in results)


def print_local_results(results: list[tuple[str, bool, str]]) -> None:
    print("ЛОКАЛЬНЫЕ ПРОВЕРКИ")
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name} - {detail}")


def print_deep_results(result: dict) -> None:
    print("\nСМЫСЛОВАЯ ПРОВЕРКА (Gemini)")
    reasons = result.get("reasons", {})
    labels = (("personal_stake", "личная ставка"), ("posture", "позиция"),
              ("voice", "голос"), ("human", "похоже на человека"))
    for key, label in labels:
        print(f"  {label}: {result.get(key, '?')} - {reasons.get(key, '')}")
    edits = result.get("edits") or []
    if edits:
        print("\nПРАВКИ:")
        for e in edits:
            print(f"  - {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="файл с черновиком; без аргумента - stdin")
    ap.add_argument("--deep", action="store_true", help="+ смысловая проверка Gemini")
    args = ap.parse_args()

    text = (pathlib.Path(args.path).read_text(encoding="utf-8") if args.path
            else sys.stdin.read()).strip()
    if not text:
        print("пустой ввод", file=sys.stderr)
        return 1

    results, passed = run_local_checks(text)
    print_local_results(results)

    if not passed:
        print("\nИТОГ: локальные проверки не пройдены, правь и запускай снова")
        return 1

    print("\nИТОГ: локальные проверки пройдены")

    if args.deep:
        print("\n--- запрос к Gemini ---")
        try:
            print_deep_results(brain.judge_draft(text))
        except Exception as exc:
            print(f"Gemini недоступен: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
