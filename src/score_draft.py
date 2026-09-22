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
    "synergy", "landscape", "paradigm", "unprecedented", "game-changer",
    "delve", "underscore", "pivotal", "robust", "it's not just", "here's the thing",
    # New institutional ban-list
    "plumbing", "double whammy", "lost their shirts", "the house always wins",
    "selling shovels in a gold rush", "tip of the iceberg", "silver bullet",
    "bottleneck", "the reality is different", "the timing is tricky", "here is the catch",
    "the numbers are wild", "this pressure is not a straight line",
    "why? because", "here's why", "why the massive gap", "how did this happen",
    # Institutional throat-clearing ban-list (CRITIQUE_PROMPT)
    "is emerging as", "structural shift", "consequently", "far exceeding",
    "crowding out", "this mechanism shows", "two numbers stand out",
    "the common view is that", "primary bottleneck", "underlying economic reality",
    "as net payer nations demand",
    # Academic & sell-side filler / Childish
    "characterized by a shift toward", "consequently, the persistence of",
    "serves as a testament to", "are emerging as the primary",
    "piggy banks", "paying the bill", "pay the bill", "expensive spot market",
    # Telegraphic fragments
    "the price:", "this pushes borrowing costs higher", "supply chains remain tight",
    "the problem is", "the central bank is stepping in",
    # Conversational sloppiness
    "snapped up the debt",
    # Childish bullet headers
    "expensive imports", "the fuel tax", "the bank squeeze",
    # Empty final dramatic one-liners
    "earnings get crushed", "expect de-rating if growth slows", "corporate margins shrink",
]

PROMPT_LEAK_PHRASES = [
    "ai data centers are running into an insurance wall",
    "persistent energy inflation and heavy debt issuance are breaking the market's rate-cut bets",
    "if you want to see how the state governance discount works in real time, look at orlen",
    "if you want to see how the state governance discount works in real time",
]

QUESTIONING_PHRASES = [
    "what am i missing", "i might be wrong", "i might be reading this wrong",
    "correct me if", "am i off base",
]

ROLE_PHRASES = [
    "as a finance student", "as a student", "for a finance student",
    "as someone learning", "as someone studying",
    "as someone analyzing", "it makes you wonder",
    "i am watching this space", "i am watching this space closely", "time will tell",
]

BANNED_OPENERS = [
    "what caught my eye", "a few developments caught my eye", "a few stories caught my eye",
    "i've been tracking", "these two numbers", "these two trends", "at the same time",
    "many investors assume", "most retail investors think",
    "everyone is watching", "everyone is talking about",
    "in today's volatile market", "in today's fast-paced world",
    "it is no secret that", "the market is shifting",
]

ENGAGEMENT_BAIT = [
    "thoughts?", "what do you think?", "agree?", "let me know what you think",
    "drop a comment", "comment below", "thoughts below",
]

GENERIC_HASHTAGS = {"#finance", "#macroeconomics", "#gpw", "#forex"}


def _find_any(text_low: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if p in text_low]


def get_digest_bullets(text: str) -> list[str]:
    pattern = r"(?:^|\n)\s*(?:•|\-|\*|\d+[.)])\s+([^\n]+)"
    return re.findall(pattern, text)


def detect_shape(text: str, shape: str = "auto") -> str:
    if shape in ("single", "digest"):
        return shape
    bullets = get_digest_bullets(text)
    return "digest" if len(bullets) >= 2 else "single"


def check_length(text: str, low: str, shape: str = "auto"):
    n = len(text.split())
    eff_shape = detect_shape(text, shape)
    if eff_shape == "digest":
        ok = 110 <= n <= 130
        return ok, f"{n} слов ({'ок' if ok else 'FAIL: для Digest нужно строго 110-130 слов'})"
    else:
        ok = 100 <= n <= 120
        return ok, f"{n} слов ({'ок' if ok else 'FAIL: для Single Topic нужно строго 100-120 слов'})"


def check_truncation(text: str, low: str):
    clean = text.strip().rstrip("`\"' \n\t")
    ok = clean.endswith((".", "!"))
    return ok, ("ок" if ok else "текст не завершён закрывающей пунктуацией (. / !), возможен обрыв")


def check_digest_bullets(text: str, low: str, shape: str = "auto"):
    bullets = get_digest_bullets(text)
    eff_shape = detect_shape(text, shape)
    if eff_shape == "digest":
        count = len(bullets)
        ok = 2 <= count <= 3
        return ok, (f"{count} буллетов (ок, 2-3)" if ok else f"{count} буллетов (для Digest нужно строго 2-3 темы)")
    else:
        ok = len(bullets) <= 1
        return ok, ("ок" if ok else f"в Single Topic не должно быть дайджест-буллетов, найдено: {len(bullets)}")


def check_anti_leak(text: str, low: str):
    hits = _find_any(low, PROMPT_LEAK_PHRASES)
    return not hits, ("нет" if not hits else "утечка из промпта (дословный копипаст примера): " + ", ".join(hits))


def check_has_number(text: str, low: str):
    ok = bool(re.search(r"\d", text))
    return ok, ("цифра есть" if ok else "чисел нет")


def check_banned_words(text: str, low: str):
    hits = _find_any(low, BANNED_WORDS)
    return not hits, ("нет" if not hits else "найдено: " + ", ".join(hits))


def check_banned_openers(text: str, low: str):
    first = re.split(r"(?<=[.!?])\s", low.strip(), maxsplit=1)[0].strip()
    hits = [o for o in BANNED_OPENERS if o in first]
    return not hits, ("нет" if not hits else "запрещённый зачин: " + ", ".join(hits))


def check_engagement_bait(text: str, low: str):
    hits = _find_any(low[-120:], ENGAGEMENT_BAIT)
    return not hits, ("нет" if not hits else "в конце: " + ", ".join(hits))


def check_no_closing_question(text: str, low: str):
    clean = text.strip().rstrip("`\"' \n\t")
    ok = not clean.endswith("?")
    return ok, ("ок" if ok else "заканчивается вопросом")


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


WORD_COUNTER_RE = re.compile(r"(?:(?:\b\w+\s*\(\d{1,3}\)|\(\d{1,3}\)\s*\w+)[^\w]*\s*){3,}")


def check_word_counter_leak(text: str, low: str) -> tuple[bool, str]:
    hit = WORD_COUNTER_RE.search(text)
    if hit:
        return False, f"Word Counter Leakage: обнаружена нумерация слов ({hit.group(0).strip()[:60]})"
    return True, "ок"


LOCAL_CHECKS = [
    ("контроль объёма (Word Count Gate)", check_length),
    ("проверка на обрыв (Truncation Gate)", check_truncation),
    ("ограничение тем в Digest (2-3 буллета)", check_digest_bullets),
    ("защита от утечек из промпта (Anti-Leak Gate)", check_anti_leak),
    ("защита от утечки счётчика слов (Word Counter Leakage)", check_word_counter_leak),
    ("есть хотя бы одно число", check_has_number),
    ("нет запрещённых слов", check_banned_words),
    ("нет запрещённых зачинов", check_banned_openers),
    ("нет engagement-bait в конце", check_engagement_bait),
    ("не заканчивается вопросом", check_no_closing_question),
    ("хэштегов 0-1, не generic", check_hashtags),
    ("нет ссылок в теле", check_no_links),
    ("не открывается риторическим вопросом", check_no_rhetorical_open),
    ("числа в английском формате", check_number_format),
    ("не проговаривает роль автора", check_role_phrases),
    ("не в вопросительной позиции", check_questioning_posture),
]


def run_local_checks(text: str, shape: str = "auto") -> tuple[list[tuple[str, bool, str]], bool]:
    import inspect
    low = text.lower()
    results = []
    for name, fn in LOCAL_CHECKS:
        sig = inspect.signature(fn)
        if "shape" in sig.parameters:
            ok, detail = fn(text, low, shape=shape)
        else:
            ok, detail = fn(text, low)
        results.append((name, ok, detail))
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
    ap.add_argument("--shape", choices=["single", "digest", "auto"], default="auto", help="тип черновика")
    ap.add_argument("--deep", action="store_true", help="+ смысловая проверка Gemini")
    args = ap.parse_args()

    text = (pathlib.Path(args.path).read_text(encoding="utf-8") if args.path
            else sys.stdin.read()).strip()
    if not text:
        print("пустой ввод", file=sys.stderr)
        return 1

    results, passed = run_local_checks(text, shape=args.shape)
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
