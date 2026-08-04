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


_THOUSAND_SEPS = (" ", " ", " ")          # пробел, НБСП, узкий НБСП
_MAGNITUDE_WORDS = (("tys.", 1_000), ("mln", 1_000_000), ("mld", 1_000_000_000))


def _group_thousands(whole: str, sep: str) -> str:
    """"406000" -> "406 000" (или другим разделителем) - разряды по 3 цифры
    справа налево, последняя (левая) группа может быть короче."""
    parts = []
    s = whole
    while len(s) > 3:
        parts.insert(0, s[-3:])
        s = s[:-3]
    parts.insert(0, s)
    return sep.join(parts)


def _polish_thousand_forms(no_sep: str) -> list[str]:
    """no_sep - число без разделителя тысяч, "." - десятичная точка (английская
    форма), если есть. Возвращает польские формы того же числа: разряды
    пробелом / неразрывным пробелом (U+00A0) / узким неразрывным (U+202F) с
    десятичной запятой ("406 000", "15 019,5"), и для круглых целых - словесный
    порядок величины (tys./mln/mld): "406000" -> "406 tys." (T10c req 2)."""
    if "." in no_sep:
        whole, frac = no_sep.split(".", 1)
        dec_part = f",{frac}"
    else:
        whole, frac = no_sep, None
        dec_part = ""

    out = []
    if len(whole) > 3:
        out.extend(_group_thousands(whole, sep) + dec_part for sep in _THOUSAND_SEPS)

    if frac is None and whole.isdigit():
        n = int(whole)
        for word, mag in _MAGNITUDE_WORDS:
            if n and n % mag == 0:
                out.append(f"{n // mag} {word}")
    return out


def _core_number_variants(value: str) -> list[str]:
    """Числовые варианты БЕЗ хвоста-единицы: группа из 3 цифр после запятой -
    разделитель тысяч (английский формат), иначе запятая - десятичная (польский
    формат): "31,0" и "31.0" - одно и то же число.

    Для разделителя тысяч ищем ОБА варианта - "6,872" (как обычно и пишет
    источник) и "6872" (на случай, если источник запятую не ставит). Раньше
    искали только вариант без запятой, из-за чего число, буквально совпадающее
    с текстом источника, помечалось NOT_FOUND.

    Дополнительно (T10c req 2): польские формы разрядов (пробел/НБСП/узкий
    НБСП) и словесный порядок величины (tys./mln/mld) для круглых чисел -
    "406,000" по-польски источник пишет "406 tys.", не "406,000" ни в каком
    варианте разделителя. Это второй эшелон, после прямой цитаты из FIGURES
    (см. quoted_source_form) - но нужен и сам по себе, когда цитаты нет.

    Выделено из _to_search_variants отдельной функцией (T11b): единица
    измерения теперь проверяется отдельно, в окне предложения
    (_unit_matches_in_sentence), а не приклеена к самому искомому числу -
    "4.1 million tons" никогда не встретится в польском тексте дословно.

    T14b: core == "" (value не начинается с цифры/"$" - квалификатор словом
    "over 67 days" или числительное словом "four days longer", см. charts.py
    T14b шаг 1) обязан вернуть [], а не [core] ([""]). Пустая строка
    "находится" в НАЧАЛЕ любого текста через str.find(""), и вызывающий код
    (_find_first в verify_figures_local) принимал бы это за настоящее
    совпадение числового ядра в позиции 0 - реальный, а не гипотетический
    путь к ложному FOUND, см. test_empty_core_never_falsely_found_via_incidental_unit_word."""
    num_part, _suffix = _split_value(value)
    core = re.sub(r"\s+", "", num_part.replace("$", "").replace("%", ""))
    if not core:
        return []
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
        num_variants += _polish_thousand_forms(no_sep)
    else:
        m = re.fullmatch(r"(\d+)[.,](\d+)", core)
        if m:
            whole, frac = m.groups()
            num_variants = [f"{whole}.{frac}", f"{whole},{frac}"]
        else:
            num_variants = [core]
            num_variants += _polish_thousand_forms(core)
    return num_variants


def _to_search_variants(value: str) -> list[str]:
    """Строковые варианты числа И его хвоста для посимвольного поиска - первый,
    самый строгий эшелон проверки (число и единица склеены в одну строку, как
    их пишет DRAFT_PROMPT). Не находит польскую запись с другим словом-единицей
    ("8.5 million tons" вместо "8,5 mln ton") - для этого второй эшелон,
    _unit_matches_in_sentence (T11b).

    Хвост (единица/порядок слова, "billion", "PLN", "percentage points")
    приклеивается назад с одним пробелом к каждому варианту числа - именно так
    источник и пишет, "45 billion", а не слитно "45billion" (T9 fix 3). "%"
    приклеен без пробела к числу в исходном значении и в _NUM_AND_SUFFIX
    попадает в числовое ядро, а не в хвост - явно выделяем его и добавляем
    польскую форму "23 proc." рядом с "23%" (T10c req 2)."""
    num_part, suffix = _split_value(value)
    is_percent = num_part.endswith("%")
    num_variants = _core_number_variants(value)

    tails = []
    if is_percent:
        tails += ["%", " proc."]
    if suffix:
        tails.append(f" {suffix}")
    if not tails:
        return num_variants
    return [v + t for v in num_variants for t in tails]


# T11b: 0/8 FOUND у польских сюжетов живого прогона 02.08 - _to_search_variants
# переводит ФОРМАТ числа (разряды, запятая, tys./mln/mld), но не слово-единицу
# рядом с ним, поэтому "4.1 million tons" никогда не совпадёт с "4,1 mln ton"
# дословно. Не полный переводчик единиц - таблица по живым случаям, расширять
# по факту следующих прогонов.
_UNIT_SYNONYMS: dict[str, list[str]] = {
    "ton": ["ton", "tony", "tona", "tys. ton", "mln ton"],
    "tons": ["ton", "tony", "tona", "tys. ton", "mln ton"],
    "tonne": ["ton", "tony", "tona", "tys. ton", "mln ton"],
    "tonnes": ["ton", "tony", "tona", "tys. ton", "mln ton"],
    "hectare": ["ha", "hektar", "hektara", "hektarów", "hektarow"],
    "hectares": ["ha", "hektar", "hektara", "hektarów", "hektarow"],
    # Оба варианта диакритики - фиды отдают польский текст без "ł"/"ó" не
    # реже, чем с ними (см. существующие фикстуры этого файла - "obnizyl",
    # не "obniżył").
    "pln": ["zł", "zl", "złotych", "zlotych", "złoty", "zloty", "PLN"],
    "zl": ["zł", "zl", "złotych", "zlotych", "złoty", "zloty", "PLN"],
    "eur": ["euro", "EUR"],
    "euro": ["euro", "EUR"],
    "euros": ["euro", "EUR"],
    "million": ["mln"],
    "billion": ["mld"],
    "thousand": ["tys."],
    "percent": ["proc.", "%"],
}


def _unit_groups(value: str) -> list[list[str]]:
    """Синонимы единиц из хвоста value, по одной группе на распознанное слово
    (в т.ч. "%" из числового ядра). Слово не нашлось в таблице - молча
    пропускается, не глушит остальные группы."""
    num_part, suffix = _split_value(value)
    groups: list[list[str]] = []
    seen: set[str] = set()
    if num_part.rstrip().endswith("%"):
        seen.add("percent")
        groups.append(_UNIT_SYNONYMS["percent"])
    for word in suffix.split():
        key = word.strip(".,").lower()
        if key in _UNIT_SYNONYMS and key not in seen:
            seen.add(key)
            groups.append(_UNIT_SYNONYMS[key])
    return groups


def _unit_matches_in_sentence(value: str, sentence: str) -> bool:
    """T11b req 3: FOUND только когда совпало И числовое ядро (проверяется
    отдельно, см. verify_figures_local), И единица - в этом же предложении, не
    обязательно вплотную к числу. Хвост value не распознался в таблице синонимов
    (пустой groups) - единица не подтверждена, матч не засчитывается: молчаливое
    "любая единица подходит" наштамповало бы ложных FOUND, а планку ослаблять
    нельзя (T11 ТЗ)."""
    groups = _unit_groups(value)
    if not groups:
        return False
    low = sentence.lower()
    return all(any(syn.lower() in low for syn in group) for group in groups)


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
    """Ближайшее по значению число из текста - для подсказки владельцу при
    NOT_FOUND, но только того же порядка величины (в пределах x10 в любую
    сторону). "closest: 2026" для цели 406000 - шум, не подсказка: год и
    число тысяч не могут быть опечаткой друг друга. Не нашлось ничего
    сопоставимого по порядку - None, а не первое попавшееся число (T10c req 5)."""
    if not target:
        return None
    best, best_diff = None, None
    for m in _TEXT_NUM.finditer(text):
        f = _to_float(m.group())
        if not f:
            continue
        ratio = f / target
        if not (0.1 <= ratio <= 10):
            continue
        diff = abs(f - target)
        if best_diff is None or diff < best_diff:
            best, best_diff = m.group(), diff
    return best


# T11 ТЗ сам пишет и "Story [N]", и "Source [N]" как равноценные примеры, а
# живой прогон 02.08 после T11c показал третью форму - "Story 3 source text"
# вовсе без скобок. DRAFT_PROMPT не требует буквального формата для этой
# колонки FIGURES ("where it came from"), модель вольна в фразировке - якорное
# слово ловим в любом варианте, скобки опциональны. Перенесено из main.py в
# T11f (verify.py его тоже использует теперь, main → verify - существующее
# направление импорта, обратное дало бы цикл).
_STORY_NUM_RE = re.compile(r"(?:Story|Source)\s*\[?(\d+)\]?", re.IGNORECASE)


def _referenced_story_numbers(figures_raw: str) -> list[int]:
    """Номера сюжетов ("Story [2] source text" / "Story 3 source text"), на
    которые в FIGURES реально ссылается черновик - по одному разу, в порядке
    появления. FIGURES видит ту же нумерацию [i], что brain.draft() подставила
    в блоки историй. Общий якорь для номеров источника под черновиком
    (main.resolve_draft_source_numbers, T11c/T14a) и для выбора тела статьи на
    верификацию (bodies_for_source ниже, T11f)."""
    pairs = charts.parse_figures(figures_raw) or []
    numbers = []
    for _, source in pairs:
        for m in _STORY_NUM_RE.finditer(source):
            n = int(m.group(1))
            if n not in numbers:
                numbers.append(n)
    return numbers


def bodies_for_source(source_field: str, selected: list[dict],
                      figures_raw: str = "") -> list[str]:
    """Тексты сюжетов для верификации (T11f). Сначала - по номеру сюжета из
    FIGURES, тем же якорем, что уже держит ссылку под черновиком (T11c).
    Строковое сопоставление SOURCE-поля с URL - только фолбэк, когда номер не
    разрешился: SOURCE пишет модель САМА, отдельно от FIGURES, и опечатка в
    одну букву URL (боевой прогон 02.08: "na-ryku" вместо "na-rynku") даёт
    False в обе стороны у подстрочного сравнения - тело статьи не находится,
    хотя оно было загружено, и черновик получает "source text not fetched"
    незаслуженно, при этом верификатор существует именно для того, чтобы
    ловить ошибки модели, а не доверять ей выбор текста для проверки.

    Номера НЕ объединяются со строковым совпадением: если хотя бы один номер
    разрешился, тела берутся строго по ним. Иначе в проверку заедет текст
    соседнего сюжета, и число из статьи A "найдётся" в статье B - тот же
    класс ложного FOUND, которого избегали в T11b."""
    if selected:
        numbers = [n for n in _referenced_story_numbers(figures_raw) if 1 <= n <= len(selected)]
        if numbers:
            return [selected[n - 1].get("body") or "" for n in numbers]

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
_QUOTED = re.compile(r'"([^"]+)"')


def quoted_source_form(source: str) -> str | None:
    """Фрагмент в кавычках из поля FIGURES ("406 tys.") - модель уже указала,
    в какой форме число стоит в источнике. Посимвольный поиск этой цитаты -
    первый и самый дешёвый и надёжный эшелон проверки, до любой реконструкции
    форм (T10c req 1)."""
    m = _QUOTED.search(source)
    return m.group(1) if m else None


def _find_first(text: str, variants: list[str]) -> tuple[int, str] | None:
    """Первое совпадение любого из variants в text - (позиция, сама строка) или
    None. Позиция нужна отдельно от факта совпадения (T11b: чтобы найти
    предложение вокруг числового ядра для проверки единицы), а не только
    да/нет, как раньше давал плоский `any(v in text for v in variants)`."""
    for v in variants:
        idx = text.find(v)
        if idx != -1:
            return idx, v
    return None


def verify_figures_local(pairs: list[tuple[str, str]] | None, bodies: list[str],
                         data_text: str) -> list[dict]:
    """Уровень a. Каждая запись: {value, source, status, matched_in?, closest?}.
    status: FOUND | NOT_FOUND | NO_SOURCE_TEXT | UNPARSED | YEAR.

    Порядок поиска (T10c req 1-2): сначала цитата из FIGURES, если она есть
    ("406 tys.") - её пишет сама модель, форма источника дана прямо в тексте.
    Не нашлось цитаты или она не совпала - реконструированные формы значения
    (_to_search_variants: польские разряды, tys./mln/mld, "23 proc." для
    "23%"). Цитата проверяется ДО проверки "приводится ли value к float" -
    диапазон ("80,000 to 120,000") или дата ("August 1, 2026") не приводятся
    к одному числу, но с цитатой всё равно могут быть найдены посимвольно.

    T11b, третий эшелон (после цитаты и склеенного числа+хвоста): числовое
    ядро БЕЗ хвоста (_core_number_variants) ищется отдельно, и если нашлось -
    единица проверяется в окне того же предложения (_unit_matches_in_sentence),
    не как часть искомой строки. Живой прогон 02.08 дал 0/8 FOUND именно на
    польских сюжетах - "4.1 million tons" не совпадает с "4,1 mln ton"
    посимвольно ни в каком виде, а раздельная проверка числа и единицы это
    ловит. Планка не ослаблена: без совпадения единицы в предложении статус
    остаётся NOT_FOUND, как и раньше.

    UNPARSED - значение структурно распозналось как пара (value, source), но не
    приводится к числу и цитата не нашлась. Раньше такая пара (а также пары
    вроде диапазонов/дат, которые charts.parse_figures вообще не распознавал
    структурно) не долетала до этой функции - знаменатель в отчёте занижался
    без следа (T9b, T10c req 3). Голый год ("2012") - YEAR, не считается
    проверяемым числом вовсе и не входит в знаменатель, но это видно в отчёте
    отдельной пометкой."""
    if not pairs:
        return []
    combined_body = "\n".join(b for b in bodies if b)
    out = []
    for value, source in pairs:
        if _BARE_YEAR.fullmatch(value.strip()):
            out.append({"value": value, "source": source, "status": "YEAR"})
            continue

        quote = quoted_source_form(source)
        variants = ([quote] if quote else []) + _to_search_variants(value)

        if any(v in data_text for v in variants):
            out.append({"value": value, "source": source, "status": "FOUND",
                       "matched_in": "data"})
            continue
        if combined_body and any(v in combined_body for v in variants):
            out.append({"value": value, "source": source, "status": "FOUND",
                       "matched_in": "body"})
            continue

        if combined_body:
            core_hit = _find_first(combined_body, _core_number_variants(value))
            if core_hit is not None:
                idx, matched = core_hit
                start = _sentence_start(combined_body, idx)
                end = _sentence_end(combined_body, idx + len(matched))
                if _unit_matches_in_sentence(value, combined_body[start:end]):
                    out.append({"value": value, "source": source, "status": "FOUND",
                               "matched_in": "body"})
                    continue

        if _to_float(value) is None:
            out.append({"value": value, "source": source, "status": "UNPARSED"})
            continue
        if not combined_body:
            out.append({"value": value, "source": source, "status": "NO_SOURCE_TEXT"})
            continue
        target = _to_float(value)
        closest = _closest_in_text(target, combined_body)
        # T11b (ГИПОТЕЗА): окно вокруг ближайшего кандидата в digest.log -
        # следующий живой прогон покажет причину NOT_FOUND, а не догадку.
        # Часть значений (пересчёт модели, а не цитата) честно не обязана
        # встречаться в источнике дословно - это не всегда баг верификатора.
        window = _fragment_around(combined_body, [closest], window=80) if closest else None
        out.append({"value": value, "source": source, "status": "NOT_FOUND",
                   "closest": closest, "search_window": window})
    return out


def _is_decimal_dot(text: str, pos: int) -> bool:
    """"." на позиции pos - десятичная точка числа (окружена цифрами с обеих
    сторон), не конец предложения."""
    before = text[pos - 1] if pos > 0 else ""
    after = text[pos + 1] if pos + 1 < len(text) else ""
    return before.isdigit() and after.isdigit()


def _sentence_start(text: str, idx: int) -> int:
    """Начало предложения, содержащего позицию idx - ищем "." назад от idx,
    пропуская десятичные точки ПРЕДЫДУЩИХ чисел. T9 починил границу конца
    (см. _sentence_end), начало оставалось кривым: "GDP grew 3.5 percent and
    inflation hit 4.60%." при поиске "4.60%" - text.rfind(".", 0, idx) цеплялся
    за точку в "3.5" (она же последняя "." перед idx), обрывая фрагмент
    серединой предыдущего предложения - "5 percent and inflation hit 4.60%."
    вместо "GDP grew 3.5 percent and inflation hit 4.60%." (T10c req 4)."""
    pos = idx
    while True:
        dot = text.rfind(".", 0, pos)
        if dot == -1:
            return 0
        if _is_decimal_dot(text, dot):
            pos = dot
            continue
        return dot + 1


def _sentence_end(text: str, idx: int) -> int:
    """Симметрично _sentence_start: ищем "." вперёд от idx, пропуская
    десятичные точки СЛЕДУЮЩИХ чисел в том же предложении."""
    pos = idx
    while True:
        dot = text.find(".", pos)
        if dot == -1:
            return len(text)
        if _is_decimal_dot(text, dot):
            pos = dot + 1
            continue
        return dot + 1


def _sentence_containing(text: str, needle_variants: list[str]) -> str:
    for v in needle_variants:
        idx = text.find(v)
        if idx != -1:
            start = _sentence_start(text, idx)
            # Конец ищем НАЧИНАЯ ПОСЛЕ самого needle, не с его начала - у
            # десятичного числа ("4.60") первая же "." это его собственная
            # точка, а не конец предложения (T9).
            end = _sentence_end(text, idx + len(v))
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
        # T11b: окно вокруг ближайшего кандидата - в digest.log (это уходит
        # только в build_message()/полный отчёт, не в Telegram), не догадка,
        # а то, что реально стояло в тексте источника рядом с искомым числом.
        window = f' [window: "{r["search_window"]}"]' if r.get("search_window") else ""
        extra = f" +{len(not_found) - 1} more" if len(not_found) > 1 else ""
        problems.append(f'"{r["value"]}" not found{closest}{window}{extra}')
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


def verify_drafts(drafts_text: str, selected: list[dict],
                  data_text: str) -> tuple[str, dict, list[dict]]:
    """Точка входа. Дописывает под каждым черновиком строку "ЦИФРЫ: ..." и, если
    хоть одно число NOT_FOUND или MISMATCH, явную пометку о принудительном
    понижении VERDICT до MAYBE - саму строку VERDICT черновика не трогает, только
    добавляет новые строки следом. Формат не разобрался - возвращает исходный
    текст как есть, ничего не теряя.

    Один проход regex.finditer по тексту: и данные для проверки, и позиция для
    вставки берутся из ОДНИХ И ТЕХ ЖЕ match-объектов, поэтому аннотация физически
    не может уехать не к своему черновику (раньше data и место вставки собирались
    двумя независимыми проходами, синхронизированными только счётчиком по индексу -
    один непарный матч расходил их на весь оставшийся текст).

    Третье значение - blocks: те же структуры, что использовались для сборки
    текста (shape/body/figures/source/verdict/why/check_first/_level_a/_report/
    _downgrade/_offending), без повторного захода в Gemini. Нужны T10d-рендереру
    Telegram-сообщений - разбирать заново уже готовый плоский текст обратно в
    поля было бы хрупко и дублировало бы эту же работу."""
    blocks = []
    for m in _DRAFT_RE.finditer(drafts_text):
        d = {k: (v or "").strip() for k, v in m.groupdict().items()}
        d["_match"] = m
        blocks.append(d)

    if not blocks:
        log.warning("verify: не удалось разобрать черновики по формату - цифры не проверены")
        return drafts_text, _empty_stats(), []

    for b in blocks:
        raw_figures = b["figures"].strip()
        figures = charts.parse_figures(raw_figures)
        no_figures_declared = (not raw_figures) or raw_figures.lower().startswith("none used")
        # текст был, но ни одна пара не распозналась - это НЕ "цифр нет", а сбой
        # парсера. Ложный "все ок" тут хуже, чем явное "проверь руками".
        b["_parse_failed"] = (not no_figures_declared) and not figures
        bodies = bodies_for_source(b["source"], selected, raw_figures)
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
            # фрагмент источника ищем теми же вариантами, что нашли значение в
            # verify_figures_local (с цитатой из FIGURES, если она есть) -
            # иначе матч, найденный только по цитате, здесь не находился бы
            # вовсе, и candidate тихо терялся (T10c).
            quote = quoted_source_form(r["source"])
            source_variants = ([quote] if quote else []) + _to_search_variants(r["value"])
            fragment = _fragment_around(combined_body, source_variants)
            if not fragment:
                continue
            # предложение черновика ищем английскими вариантами value - BODY
            # черновика пишет модель по-английски (DRAFT_PROMPT), польская
            # цитата там искать нечего. Если значения в BODY дословно нет,
            # _sentence_containing тихо откатится на первые 200 символов текста -
            # случайное предложение, не имеющее отношения к этой цифре, уйдёт
            # в LLM как "то самое" и получит MISMATCH не по делу (боевой прогон:
            # претензия про год приклеилась к "0.50%" - разъезжающаяся пара).
            # Не находится дословно - не строим кандидата вовсе (T10c req 6).
            draft_variants = _to_search_variants(r["value"])
            if not any(v in b["body"] for v in draft_variants):
                continue
            candidates.append({
                "id": len(candidates), "draft_idx": i, "value": r["value"],
                "draft_sentence": _sentence_containing(b["body"], draft_variants),
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
    return "".join(out_parts), stats, blocks
