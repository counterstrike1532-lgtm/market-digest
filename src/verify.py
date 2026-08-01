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
    # CHECK_FIRST - последнее поле блока, по формату одна строка. Раньше конец
    # определялся lookahead'ом "до следующего SHAPE:", а \s* в нём проглатывал
    # любой разделитель между черновиками (пустые строки, "---" от модели) ВНУТРЬ
    # CHECK_FIRST - аннотация вставлялась после разделителя, визуально прилипая
    # к следующему черновику. Явная граница по одной строке ("[^\n]*", перевод
    # строки не входит независимо от re.S) убирает саму возможность такого сдвига.
    r"CHECK_FIRST:[ \t]*(?P<check_first>[^\n]*)",
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


# Порядковые слова-множители (англ. и польск.) - "$45 billion", "8.5 mln PLN".
# Разворачивать до "45,000,000,000" не нужно (T9 fix 3): достаточно, что
# число само по себе распознаётся как число, а искать в тексте источника
# будем словесной формой ("45 billion"), как источник и пишет.
_MAGNITUDE = {
    "thousand": 1e3, "tys": 1e3,
    "million": 1e6, "mln": 1e6,
    "billion": 1e9, "mld": 1e9,
    "trillion": 1e12,
}

_NUM_AND_SUFFIX = re.compile(r"^(\$?\d[\d,.%$]*)\s*(.*)$")


def _split_value(value: str) -> tuple[str, str]:
    """(числовое ядро, словесный хвост): "1.3 percentage points" -> ("1.3",
    "percentage points"), "$45 billion" -> ("$45", "billion"). Не начинается
    с цифры/$ - ядра нет, всё уходит в хвост (не распарсится дальше, честно)."""
    m = _NUM_AND_SUFFIX.match(value.strip())
    if not m:
        return "", value.strip()
    return m.group(1), m.group(2).strip()


def _to_search_variants(value: str) -> list[str]:
    """Строковые варианты числа для посимвольного поиска: группа из 3 цифр после
    запятой - разделитель тысяч (английский формат), иначе запятая - десятичная
    (польский формат): "31,0" и "31.0" - одно и то же число.

    Для разделителя тысяч ищем ОБА варианта - "6,872" (как обычно и пишет
    источник) и "6872" (на случай, если источник запятую не ставит). Раньше
    искали только вариант без запятой, из-за чего число, буквально совпадающее
    с текстом источника, помечалось NOT_FOUND.

    Хвост (единица/порядок слова, "billion", "PLN", "percentage points")
    приклеивается назад с одним пробелом к каждому варианту числа - именно так
    источник и пишет, "45 billion", а не слитно "45billion" (T9 fix 3)."""
    num_part, suffix = _split_value(value)
    core = re.sub(r"\s+", "", num_part.replace("$", "").replace("%", ""))
    if re.fullmatch(r"\d+(,\d{3})+(\.\d+)?", core):
        no_sep = core.replace(",", "")
        num_variants = [core, no_sep]
        if "." in no_sep:
            # Тысячи без разделителя И десятичная точка одновременно
            # ("15,019.5" -> "15019.5") - это ещё не польская запись того же
            # числа. Источник в польском тексте пишет разряды слитно и
            # десятичную часть через запятую: "15019,5". Раньше этот вариант
            # не генерировался вовсе, и число, буквально присутствующее в
            # тексте, помечалось NOT_FOUND (боевой прогон 01.08.2026).
            num_variants.append(no_sep.replace(".", ","))
    else:
        m = re.fullmatch(r"(\d+)[.,](\d+)", core)
        if m:
            whole, frac = m.groups()
            num_variants = [f"{whole}.{frac}", f"{whole},{frac}"]
        else:
            num_variants = [core]
    if not suffix:
        return num_variants
    return [f"{v} {suffix}" for v in num_variants]


def _to_float(value: str) -> float | None:
    """Числа с тысячным разделителем И десятичной точкой одновременно
    ("15,019.5") раньше ломали конвертацию: код проверял "либо тысячи, либо
    десятичное" по отдельности, а сочетание обеих форм не матчило ни один из
    паттернов - "15,019.5" превращалось в "15.019.5" (две точки, float() падал)
    и число уходило в UNPARSED незаслуженно (найдено живым прогоном после
    T9 fix 3, тот же класс проблемы - см. _MAGNITUDE выше)."""
    num_part, suffix = _split_value(value)
    core = re.sub(r"\s+", "", num_part.replace("$", "").replace("%", ""))
    if re.fullmatch(r"\d+(,\d{3})*(\.\d+)?", core):
        core = core.replace(",", "")
    elif re.fullmatch(r"\d+,\d+", core):
        core = core.replace(",", ".")
    try:
        base = float(core)
    except ValueError:
        return None
    first_word = suffix.split()[0].lower().rstrip(".") if suffix else ""
    return base * _MAGNITUDE.get(first_word, 1.0)


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


_BARE_YEAR = re.compile(r"^(19|20)\d{2}$")


def verify_figures_local(pairs: list[tuple[str, str]] | None, bodies: list[str],
                         data_text: str) -> list[dict]:
    """Уровень a. Каждая запись: {value, source, status, matched_in?, closest?}.
    status: FOUND | NOT_FOUND | NO_SOURCE_TEXT | UNPARSED | YEAR.

    UNPARSED - значение структурно распозналось как пара (value, source), но не
    приводится к числу (единица измерения в тексте: "$200 billion", "100,000 PLN").
    Раньше такая пара просто не долетала до этой функции - parse_figures молча
    ронял её на этапе регулярки, и знаменатель в отчёте занижался без следа (T9b).
    Голый год ("2012") - YEAR, не считается проверяемым числом вовсе и не входит
    в знаменатель, но это видно в отчёте отдельной пометкой."""
    if not pairs:
        return []
    combined_body = "\n".join(b for b in bodies if b)
    out = []
    for value, source in pairs:
        if _BARE_YEAR.fullmatch(value.strip()):
            out.append({"value": value, "source": source, "status": "YEAR"})
            continue
        if _to_float(value) is None:
            out.append({"value": value, "source": source, "status": "UNPARSED"})
            continue
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
            # Искать конец предложения НАЧИНАЯ ПОСЛЕ самого needle, не с его
            # начала - у десятичного числа ("4.60") первая же "." это его
            # собственная точка, а не конец предложения. Раньше поиск начинался
            # с idx и обрывал "сюжет" сразу после "4.", отправляя в LLM обрубок
            # "...4." вместо "...4.60%..." - модель честно сверяла "4" с
            # исходником и репортила MISMATCH на пустом месте.
            end = text.find(".", idx + len(v))
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


def _render(level_a: list[dict], level_b: dict[str, dict]) -> tuple[str, bool, list[str]]:
    """Возвращает (строка отчёта ЦИФРЫ с эмодзи, downgrade_to_maybe, спорные значения).

    Знаменатель - число ПАР в FIGURES (countable), а не число распознанных как
    число значений. YEAR исключается из знаменателя явно (голый год - легитимно
    не проверяемое число), UNPARSED - нет: это реальная пара, просто без единицы
    измерения, которую мы умеем сверять, и её обязаны показать честно (T9b).

    downgrade=True при ЛЮБОМ NOT_FOUND, MISMATCH или UNPARSED (T9 fix 6):
    непроверенное число - это не проверенное число, POST с непроверенной цифрой
    не может остаться чистым POST. offending перечисляет ВСЕ такие значения
    сразу, не только первое найденное - иначе владелец узнаёт про остальные
    только после ручной проверки первого."""
    if not level_a:
        return "✅ verified (no figures used)", False, []

    countable = [r for r in level_a if r["status"] != "YEAR"]
    year_n = len(level_a) - len(countable)
    year_note = f" (+{year_n} year{'s' if year_n != 1 else ''} not counted)" if year_n else ""

    if not countable:
        return f"✅ verified (no checkable figures{year_note})", False, []

    not_found = [r for r in countable if r["status"] == "NOT_FOUND"]
    no_source = [r for r in countable if r["status"] == "NO_SOURCE_TEXT"]
    unparsed = [r for r in countable if r["status"] == "UNPARSED"]

    mismatches = []
    mismatched_values = set()
    for r in countable:
        if r["status"] != "FOUND":
            continue
        lb = level_b.get(r["value"])
        if lb and lb.get("verdict") == "MISMATCH":
            mismatches.append((r, lb))
            mismatched_values.add(r["value"])

    found = [r for r in countable
             if r["status"] == "FOUND" and r["value"] not in mismatched_values]
    denom = len(countable)

    if no_source and not (found or not_found or mismatches or unparsed):
        return f"❌ source text not fetched - figures are on you to verify{year_note}", \
            False, []

    offending = ([r["value"] for r in not_found] + [v for v in mismatched_values]
                + [r["value"] for r in unparsed])
    downgrade = bool(offending)

    problems = []
    if not_found:
        r = not_found[0]
        closest = f' (closest: "{r["closest"]}")' if r.get("closest") else ""
        extra = f" +{len(not_found) - 1} more" if len(not_found) > 1 else ""
        problems.append(f'"{r["value"]}" not found{closest}{extra}')
    if mismatches:
        r, lb = mismatches[0]
        why = (lb.get("why") or "").strip()
        extra = f" +{len(mismatches) - 1} more" if len(mismatches) > 1 else ""
        problems.append(f'"{r["value"]}" context mismatch - {why}{extra}')
    if unparsed:
        vals = ", ".join(f'"{r["value"]}"' for r in unparsed)
        problems.append(f"{len(unparsed)} unparsed ({vals})")
    if no_source:
        problems.append(f"{len(no_source)} from an unfetched source")

    if problems:
        return (f"⚠️ {len(found)}/{denom} found{year_note} - " + "; ".join(problems)
                + " - fix/verify by hand"), downgrade, offending

    ctx_note = ", context ok" if level_b else ""
    return f"✅ verified ({len(found)}/{denom} found{ctx_note}){year_note}", False, []


_MODEL_DRAFT_HEADER = re.compile(r"^[ \t]*\**\s*DRAFT\s*\d*\s*\**[ \t]*:?[ \t]*$",
                                 re.IGNORECASE)


def _strip_model_header(segment: str) -> str:
    """Модель иногда сама печатает "DRAFT n" прямо перед SHAPE: - рендерер всегда
    подписывает заголовок сам, в каноническом формате "DRAFT n (shape)", поэтому
    чужой заголовок вырезается, а не дублируется. Раньше он просто дописывался
    следом без разделителя - "DRAFT 2DRAFT 2 (A)" (T9e)."""
    lines = segment.rstrip().splitlines()
    if lines and _MODEL_DRAFT_HEADER.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


def _report_lines(b: dict) -> list[str]:
    if b.get("_parse_failed"):
        return ["ЦИФРЫ: ⚠️ FIGURES не распарсился - проверь числа руками"]
    lines = [f"ЦИФРЫ: {b['_report']}"]
    if b["_downgrade"] and b["verdict"].strip().upper() == "POST":
        values = ", ".join(f'"{v}"' for v in b["_offending"])
        lines.append(f'  ! верификатор: VERDICT эффективно MAYBE - '
                    f'сверь {values} перед публикацией')
    return lines


def _empty_stats() -> dict:
    return {"drafted": 0, "verdicts": {"POST": 0, "MAYBE": 0, "SKIP": 0},
           "figures": {"found": 0, "not_found": 0, "mismatch": 0,
                       "unparsed": 0, "no_source": 0}}


def _stats_from_blocks(blocks: list[dict]) -> dict:
    """Метрики прогона (T9d): вердикты С УЧЁТОМ понижения верификатором (не
    сырой VERDICT черновика) и статусы цифр по всем блокам сразу. Годы (status
    YEAR) не считаются - это не проверяемое число, см. verify_figures_local."""
    stats = _empty_stats()
    stats["drafted"] = len(blocks)
    for b in blocks:
        v = b["verdict"].strip().upper()
        if v in stats["verdicts"]:
            if b.get("_downgrade") and v == "POST":
                v = "MAYBE"
            stats["verdicts"][v] += 1

        if b.get("_parse_failed"):
            continue
        for r in b["_level_a"]:
            status = r["status"]
            if status == "YEAR":
                continue
            if status == "FOUND":
                lb = b["_level_b"].get(r["value"])
                key = "mismatch" if lb and lb.get("verdict") == "MISMATCH" else "found"
            elif status == "NOT_FOUND":
                key = "not_found"
            elif status == "UNPARSED":
                key = "unparsed"
            elif status == "NO_SOURCE_TEXT":
                key = "no_source"
            else:
                continue
            stats["figures"][key] += 1
    return stats


def verify_drafts(drafts_text: str, selected: list[dict], data_text: str) -> tuple[str, dict]:
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
        return drafts_text, _empty_stats()

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
        if not b["_parse_failed"]:
            b["_report"], b["_downgrade"], b["_offending"] = \
                _render(b["_level_a"], b["_level_b"])

    stats = _stats_from_blocks(blocks)

    out_parts, pos = [], 0
    for i, b in enumerate(blocks, 1):
        m = b["_match"]
        # заголовок "DRAFT n" печатает рендерер, не модель: в одном прогоне модель
        # сама подписала черновики "DRAFT 1/2/3", в другом - нет, промпт этого не
        # требует буквально. Нумерация не должна зависеть от того, вставит ли
        # модель такую строку в конкретном ответе - свою, если есть, вырезаем.
        segment = _strip_model_header(drafts_text[pos:m.start()])
        if segment:
            out_parts.append(segment + "\n\n")
        elif i > 1:
            # гарантируем пустую строку между черновиками, даже если в сырых
            # данных модели её не было - иначе соседние черновики слипаются (T9e)
            out_parts.append("\n\n")
        out_parts.append(f"DRAFT {i} ({b['shape'] or '?'})\n")
        out_parts.append(drafts_text[m.start():m.end()])
        out_parts.append("\n" + "\n".join(_report_lines(b)))
        pos = m.end()
    out_parts.append(drafts_text[pos:])
    return "".join(out_parts), stats
