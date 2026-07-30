"""Верификатор цифр в черновиках (T8d). Это НЕ редактор: не удаляет и не меняет ни
одного символа из того, что написала модель для SHAPE/BODY/FIGURES/SOURCE/
WHY_THIS_ONE/VERDICT/WHY/CHECK_FIRST. Только дописывает отчёт "ЦИФРЫ: ..." сразу
после каждого черновика. Модель, которая ошиблась при извлечении числа, не найдёт
свою же ошибку при повторном чтении - поэтому автокоррекции нет вообще, задача
верификатора только сузить ручную проверку владельца до одной конкретной цифры.

Два уровня:
  a) без LLM, бесплатно, всегда: посимвольная проверка того, что число из FIGURES
     действительно встречается в догруженном тексте нужного сюжета (найденного по
     SOURCE-ссылке черновика) либо в блоке FRESH DATA (эти числа верифицированы
     самим фактом, что пришли из numbers.py, - для них уровень b не нужен).
  b) один запрос Gemini на весь прогон, только для черновиков с их собственным
     VERDICT POST|MAYBE и только по числам со статусом FOUND-в-тексте статьи -
     ловит подмену контекста (не тот год, "на X%" против "до X%", квартал против
     года), которую посимвольная проверка пропускает.
"""
from __future__ import annotations

import logging
import re

from . import brain, charts

log = logging.getLogger(__name__)

_DRAFT_RE = re.compile(
    r"SHAPE:\s*(?P<shape>.*?)\n"
    r"BODY:\s*(?P<body>.*?)\n"
    r"FIGURES:\s*(?P<figures>.*?)\n"
    r"SOURCE:\s*(?P<source>.*?)\n"
    r"WHY_THIS_ONE:\s*(?P<why_this_one>.*?)\n"
    r"VERDICT:\s*(?P<verdict>.*?)\n"
    r"WHY:\s*(?P<why>.*?)\n"
    r"CHECK_FIRST:\s*(?P<check_first>.*?)"
    r"(?=\n\s*SHAPE:|\Z)",
    re.S)


def parse_drafts(raw: str) -> list[dict]:
    """Разбирает вывод brain.draft() на отдельные черновики по полям формата.
    Черновик, не выдержавший формат целиком, просто выпадает - остальные
    разбираются независимо, ничего не подменяем."""
    out = []
    for m in _DRAFT_RE.finditer(raw):
        d = {k: (v or "").strip() for k, v in m.groupdict().items()}
        if d.get("body"):
            out.append(d)
    return out


def _to_search_variants(value: str) -> list[str]:
    """Строковые варианты числа для посимвольного поиска: группа из 3 цифр после
    запятой - разделитель тысяч (английский формат), иначе запятая - десятичная
    (польский формат): "31,0" и "31.0" - одно и то же число.

    Для разделителя тысяч ищем ОБА варианта - "6,872" (как обычно и пишет
    источник) и "6872" (на случай, если источник запятую не ставит). Раньше
    искали только вариант без запятой, из-за чего число, буквально совпадающее
    с текстом источника, помечалось NOT_FOUND."""
    core = re.sub(r"\s+", "", value.replace("$", "").replace("%", ""))
    if re.fullmatch(r"\d{1,3}(,\d{3})+", core):
        return [core, core.replace(",", "")]
    m = re.fullmatch(r"(\d+)[.,](\d+)", core)
    if m:
        whole, frac = m.groups()
        return [f"{whole}.{frac}", f"{whole},{frac}"]
    return [core]


def _to_float(value: str) -> float | None:
    core = re.sub(r"\s+", "", value.replace("$", "").replace("%", ""))
    if re.fullmatch(r"\d{1,3}(,\d{3})+", core):
        core = core.replace(",", "")
    else:
        core = core.replace(",", ".")
    try:
        return float(core)
    except ValueError:
        return None


_TEXT_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _closest_in_text(target: float, text: str) -> str | None:
    """Ближайшее по значению число из текста - для подсказки владельцу при NOT_FOUND."""
    best, best_diff = None, None
    for m in _TEXT_NUM.finditer(text):
        f = _to_float(m.group())
        if f is None:
            continue
        diff = abs(f - target)
        if best_diff is None or diff < best_diff:
            best, best_diff = m.group(), diff
    return best


def bodies_for_source(source_field: str, selected: list[dict]) -> list[str]:
    """Тексты сюжетов, на которые ссылается SOURCE черновика (сопоставление по URL)."""
    urls = [u.strip() for u in re.split(r"[,\n]", source_field) if u.strip()]
    if not urls:
        return []
    out = []
    for s in selected:
        item_url = s["item"].url
        if item_url and any(item_url in u or u in item_url for u in urls):
            out.append(s.get("body") or "")
    return out


def verify_figures_local(pairs: list[tuple[str, str]] | None, bodies: list[str],
                         data_text: str) -> list[dict]:
    """Уровень a. Каждая запись: {value, source, status, matched_in?, closest?}.
    status: FOUND | NOT_FOUND | NO_SOURCE_TEXT."""
    if not pairs:
        return []
    combined_body = "\n".join(b for b in bodies if b)
    out = []
    for value, source in pairs:
        variants = _to_search_variants(value)
        if any(v in data_text for v in variants):
            out.append({"value": value, "source": source, "status": "FOUND",
                       "matched_in": "data"})
            continue
        if not combined_body:
            out.append({"value": value, "source": source, "status": "NO_SOURCE_TEXT"})
            continue
        if any(v in combined_body for v in variants):
            out.append({"value": value, "source": source, "status": "FOUND",
                       "matched_in": "body"})
            continue
        target = _to_float(value)
        closest = _closest_in_text(target, combined_body) if target is not None else None
        out.append({"value": value, "source": source, "status": "NOT_FOUND", "closest": closest})
    return out


def _sentence_containing(text: str, needle_variants: list[str]) -> str:
    for v in needle_variants:
        idx = text.find(v)
        if idx != -1:
            start = text.rfind(".", 0, idx)
            end = text.find(".", idx)
            start = start + 1 if start != -1 else 0
            end = end + 1 if end != -1 else len(text)
            return text[start:end].strip()
    return text[:200].strip()


def _fragment_around(text: str, needle_variants: list[str], window: int = 150) -> str:
    for v in needle_variants:
        idx = text.find(v)
        if idx != -1:
            start = max(0, idx - window)
            end = min(len(text), idx + len(v) + window)
            return text[start:end].strip()
    return ""


CONTEXT_PROMPT = """Check whether numbers used in draft social media posts match the context
of their source, not just the digit string. For each pair below: the first line is the
sentence from the draft that uses the number, the second is the surrounding text from the
original source where that same number appears.

Answer MATCH if the draft's use of the number is consistent with the source context: same
year, same period, same unit, same direction ("up to X" is a different claim from "up by X",
a quarter is not a year, a nominal figure is not a real one).
Answer MISMATCH if the context differs in a way that would mislead a reader. Give one reason
under 15 words either way.

PAIRS:
{pairs}

Return JSON only: an array of objects with keys id, verdict ("MATCH" or "MISMATCH"), why.
"""


def _verify_context_llm(candidates: list[dict]) -> dict[int, dict]:
    """Один вызов на весь прогон. candidates: [{"id", "draft_sentence", "source_fragment"}].
    Возвращает {id: {"verdict": "MATCH"|"MISMATCH", "why": "..."}}."""
    if not candidates:
        return {}
    pairs_txt = "\n\n".join(
        f'id={c["id"]}\ndraft: "{c["draft_sentence"]}"\nsource: "{c["source_fragment"]}"'
        for c in candidates)
    try:
        raw = brain._call(CONTEXT_PROMPT.format(pairs=pairs_txt), as_json=True,
                          temperature=0.1, no_thinking=True)
        res = brain._parse_json(raw)
        return {int(r["id"]): r for r in res if "id" in r}
    except Exception as exc:
        log.warning("контекстная проверка цифр (уровень b) упала: %s", exc)
        return {}


def _render(level_a: list[dict], level_b: dict[str, dict]) -> tuple[str, bool, str | None]:
    """Возвращает (строка отчёта ЦИФРЫ с эмодзи, downgrade_to_maybe, спорное значение)."""
    if not level_a:
        return "✅ verified (no figures used)", False, None

    not_found = [r for r in level_a if r["status"] == "NOT_FOUND"]
    if not_found:
        r = not_found[0]
        closest = f' (closest in text: "{r["closest"]}")' if r.get("closest") else ""
        return f'⚠️ "{r["value"]}" not found in source{closest} - fix by hand', \
            True, r["value"]

    mismatches = []
    for r in level_a:
        if r["status"] != "FOUND":
            continue
        lb = level_b.get(r["value"])
        if lb and lb.get("verdict") == "MISMATCH":
            mismatches.append((r, lb))
    if mismatches:
        r, lb = mismatches[0]
        why = (lb.get("why") or "").strip()
        return f'⚠️ "{r["value"]}" context mismatch - {why}', True, r["value"]

    no_source = [r for r in level_a if r["status"] == "NO_SOURCE_TEXT"]
    found = [r for r in level_a if r["status"] == "FOUND"]
    if no_source and not found:
        return "❌ source text not fetched - figures are on you to verify", False, None
    if no_source:
        return (f"⚠️ {len(found)}/{len(level_a)} figures checked "
                f"({len(no_source)} from an unfetched source) - verify those by hand"), \
            False, None

    ctx_note = ", context ok" if level_b else ""
    return f"✅ verified ({len(found)}/{len(level_a)} found{ctx_note})", False, None


def _report_lines(b: dict) -> list[str]:
    if b.get("_parse_failed"):
        return ["ЦИФРЫ: ⚠️ FIGURES не распарсился - проверь числа руками"]
    report, downgrade, offending = _render(b["_level_a"], b["_level_b"])
    lines = [f"ЦИФРЫ: {report}"]
    if downgrade and b["verdict"].strip().upper() == "POST":
        lines.append(f'  ! верификатор: VERDICT эффективно MAYBE - '
                    f'сверь "{offending}" перед публикацией')
    return lines


def verify_drafts(drafts_text: str, selected: list[dict], data_text: str) -> str:
    """Точка входа. Дописывает под каждым черновиком строку "ЦИФРЫ: ..." и, если
    хоть одно число NOT_FOUND или MISMATCH, явную пометку о принудительном
    понижении VERDICT до MAYBE - саму строку VERDICT черновика не трогает, только
    добавляет новые строки следом. Формат не разобрался - возвращает исходный
    текст как есть, ничего не теряя.

    Один проход regex.finditer по тексту: и данные для проверки, и позиция для
    вставки берутся из ОДНИХ И ТЕХ ЖЕ match-объектов, поэтому аннотация физически
    не может уехать не к своему черновику (раньше data и место вставки собирались
    двумя независимыми проходами, синхронизированными только счётчиком по индексу -
    один непарный матч расходил их на весь оставшийся текст)."""
    blocks = []
    for m in _DRAFT_RE.finditer(drafts_text):
        d = {k: (v or "").strip() for k, v in m.groupdict().items()}
        d["_match"] = m
        blocks.append(d)

    if not blocks:
        log.warning("verify: не удалось разобрать черновики по формату - цифры не проверены")
        return drafts_text

    for b in blocks:
        raw_figures = b["figures"].strip()
        figures = charts.parse_figures(raw_figures)
        no_figures_declared = (not raw_figures) or raw_figures.lower().startswith("none used")
        # текст был, но ни одна пара не распозналась - это НЕ "цифр нет", а сбой
        # парсера. Ложный "все ок" тут хуже, чем явное "проверь руками".
        b["_parse_failed"] = (not no_figures_declared) and not figures
        bodies = bodies_for_source(b["source"], selected)
        b["_bodies"] = bodies
        b["_level_a"] = verify_figures_local(figures, bodies, data_text)

    candidates = []
    for i, b in enumerate(blocks):
        if b["_parse_failed"] or b["verdict"].strip().upper() not in ("POST", "MAYBE"):
            continue
        combined_body = "\n".join(x for x in b["_bodies"] if x)
        for r in b["_level_a"]:
            if r["status"] != "FOUND" or r.get("matched_in") != "body":
                continue          # FRESH DATA уже верифицирован по построению
            variants = _to_search_variants(r["value"])
            fragment = _fragment_around(combined_body, variants)
            if not fragment:
                continue
            candidates.append({
                "id": len(candidates), "draft_idx": i, "value": r["value"],
                "draft_sentence": _sentence_containing(b["body"], variants),
                "source_fragment": fragment,
            })

    level_b_raw = _verify_context_llm(candidates) if candidates else {}
    for i, b in enumerate(blocks):
        b["_level_b"] = {c["value"]: level_b_raw[c["id"]]
                         for c in candidates
                         if c["draft_idx"] == i and c["id"] in level_b_raw}

    out_parts, pos = [], 0
    for b in blocks:
        m = b["_match"]
        out_parts.append(drafts_text[pos:m.end()])
        out_parts.append("\n" + "\n".join(_report_lines(b)))
        pos = m.end()
    out_parts.append(drafts_text[pos:])
    return "".join(out_parts)
