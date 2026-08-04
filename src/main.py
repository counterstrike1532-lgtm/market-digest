"""Точка входа. Запуск: python -m src.main [--dry] [--hours 24]"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pathlib
import re
import sys
from datetime import datetime, timezone, timedelta

import yaml

from . import brain, charts, collect, deliver, enrich, metrics, numbers, verify

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEEN = ROOT / "state" / "seen.json"
METRICS = ROOT / "state" / "metrics.json"
STYLE = ROOT / "style" / "my_posts.md"

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("digest")

# Отбор Gemini теперь один запрос на весь список (см. brain.rank), а дневная
# квота free tier - 20 запросов на модель. Значит вход в rank() надо ограничить
# заранее, дёшево и без LLM. Приоритетные теги - первоисточники.
_PREFILTER_TAG_BONUS = {"poland_official": 1.0, "us_official": 1.0, "ai_primary": 1.0,
                        "eu_official": 0.6}

# Цена пая ETFBW20TR.WA (~80) и уровень индекса WIG20 (~2500+) - разные по
# порядку числа. Печатать голый "value" для этого ключа в ЦИФРЫ - провоцировать
# ложную цитату "уровень WIG20 сейчас X" под настоящим именем (T9 fix 2).
# Проценты изменения корректны в любом случае (это отношение close/close),
# их печатаем как обычно.
_LEVEL_HIDDEN = {"WIG20 TR (ETF)"}


def heuristic_prefilter(items: list, hours: int, cap: int = 100) -> list:
    """Без LLM сужает список до cap самых перспективных по weight/tag/свежести/social."""
    now = datetime.now(timezone.utc)

    def score(it) -> float:
        age_h = (now - datetime.fromisoformat(it.published)).total_seconds() / 3600
        freshness = max(0.0, 1 - age_h / hours)
        social = min(1.0, math.log1p(it.social) / math.log1p(1000))
        return it.weight * 2 + _PREFILTER_TAG_BONUS.get(it.tag, 0.0) + freshness + social

    ranked = sorted(items, key=score, reverse=True)
    kept = ranked[:cap]
    log.info("эвристический предотбор: оставили %d из %d", len(kept), len(items))
    return kept


def filter_by_age(items: list, max_age_days: int) -> list:
    """Догляд поверх --hours: у фида (особенно Google News c q=) запись иногда
    приходит без пригодной даты публикации, и до сих пор такой записи молча
    подставлялось "сейчас" - без проверки возраста она проходила как свежая,
    даже будь ей год. Запись без даты (published_known=False) не отбрасываем
    по требованию - пропускаем как есть, дату в сводке пометим отдельно."""
    now = datetime.now(timezone.utc)
    kept, dropped = [], []
    for it in items:
        if not it.published_known:
            kept.append(it)
            continue
        pub = datetime.fromisoformat(it.published)
        if pub.tzinfo is None:                      # наивную дату считаем UTC
            pub = pub.replace(tzinfo=timezone.utc)
        age_days = (now - pub).total_seconds() / 86400
        if age_days > max_age_days:
            dropped.append((age_days, it))
        else:
            kept.append(it)
    if dropped:
        oldest_age, oldest_it = max(dropped, key=lambda x: x[0])
        log.info("отброшено по возрасту: %d (старейшая: %s, %s)",
                len(dropped), oldest_it.title[:80], oldest_it.published[:10])
    return kept


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


def found_figure_pairs(block: dict) -> list[tuple[str, str]]:
    """Пары (value, source) из FIGURES черновика со статусом FOUND по
    верификатору - единственные, которым разрешено попасть на картинку.
    Картинка расходится по ленте без контекста, планка для неё выше, чем для
    текста - тот же принцип, что в T8a (T10e)."""
    found_values = {r["value"] for r in block.get("_level_a", []) if r["status"] == "FOUND"}
    pairs = charts.parse_figures(block.get("figures", "")) or []
    return [(v, s) for v, s in pairs if v in found_values]


def draft_card(block: dict, num: int, theme: str = "dark"):
    """Картинка к черновику (T10e, откат фолбэка в T11e.2): figures_chart
    утверждает связь между проверенными числами одного сюжета - единственный
    тип картинки в конвейере. Ошибка отрисовки не валит прогон: вызывающий код
    (main()) уже оборачивает это в try/except, здесь - только сам вызов.

    Нет картинки - нет сообщения с картинкой, и это ожидаемый исход (T11 ТЗ,
    п. e.2.3), а не повод откатываться на текстовую карточку-фолбэк (stat_card/
    quote_card, удалены как мёртвый код в T13b). Раньше "у каждого
    черновика ровно одна картинка" было неверным инвариантом: гарантия
    наличия картинки важнее её осмысленности только на бумаге - живой прогон
    02.08 показал три картинки, каждая дословно повторяющая первую фразу
    черновика, который владелец уже прочитал строкой выше."""
    pairs = found_figure_pairs(block)
    return charts.figures_chart(pairs, title=f"Черновик {num}", theme=theme, key=str(num))


# Реальный прогон: "...energy transitions.- An AI-focused" (буллет приклеен к
# концу предыдущего предложения, без переноса строки) и "forcing more
# selling.This feedback loop" (новое предложение без пробела после точки -
# DRAFT_PROMPT прямо запрещает это ("spikes.Quarterly" - proofing failure), но
# модель иногда всё равно так пишет). Правим на рендере, не в промпте - см.
# CLAUDE.md, промпты в этом ТЗ не трогаем (T9 fix 5).
_GLUED_BULLET = re.compile(r"\.-(?=\s[A-Z])")
_GLUED_SENTENCE = re.compile(r"\.(?=[A-Z][a-z])")


def _fix_glued_punctuation(text: str) -> str:
    """Точка+буллет без переноса строки -> перенос перед буллетом. Точка+новое
    предложение без пробела -> пробел. Оба случая узко специфичны (буллет
    должен начинаться с большой буквы через пробел-тире; предложение - с
    большой буквы сразу после точки), поэтому не трогают десятичные дроби
    ("$3.5 billion") и обычные диапазоны/минусы."""
    text = _GLUED_BULLET.sub(".\n-", text)
    text = _GLUED_SENTENCE.sub(". ", text)
    return text


def _oneline(text: str) -> str:
    """Однострочные поля (заголовок, угол, неочевидно) иногда приходят с сырым
    переводом строки из RSS-описания или из ответа модели - схлопываем в одну
    строку, иначе он ломает межстрочные отступы сводки (T9e)."""
    return " ".join((text or "").split())


def build_domain_urls(selected: list[dict]) -> dict[str, str]:
    """домен -> URL конкретной статьи, для deliver._to_html: модель иногда пишет
    в SOURCE: черновика голый домен вместо ссылки (T9 fix 4), и без подсказки
    Telegram сам линкует такой голый текст на корень сайта.

    Строго ОДИН URL на домен. Раньше это был плоский {s["item"].source:
    s["item"].url for s in selected} - при нескольких отобранных сюжетах с
    одного домена (обычное дело для bankier.pl) более поздняя запись в
    словаре молча стирала более раннюю, и deliver._wrap_bare_domains потом
    подставляла этот единственный (чужой для остальных сюжетов) URL везде,
    где в сообщении встречался голый домен - включая метку "www.bankier.pl"
    в самом списке СЮЖЕТЫ, к SOURCE черновиков вообще не относящуюся. Итог
    боевого прогона: у сюжетов 1/2/3 первая ссылка вела на сюжет 5 (T10b).

    Дедуп по заголовку (collect.dedupe_by_title) тут ни при чём - он оставляет
    URL выжившей записи как есть, ничего не подменяет и не мешает. Причина
    была в этом плоском словаре, не в дедупе (проверено по коду, не на веру)."""
    by_domain: dict[str, set[str]] = {}
    for s in selected:
        by_domain.setdefault(s["item"].source, set()).add(s["item"].url)
    return {domain: next(iter(urls)) for domain, urls in by_domain.items() if len(urls) == 1}


_COMPACT_MARKET_NAME = {"WIG20 TR (ETF)": "WIG20 TR", "sp500": "S&P500", "nasdaq": "Nasdaq"}


def _render_cifry_compact(data: dict) -> list[str]:
    """ЦИФРЫ сообщения 2 в максимум три строки (T10d): валюты/рынки/HICP - по
    одной строке на категорию, без построчного перечисления каждого ключа."""
    fx_bits, market_bits, hicp_bits = [], [], []
    for k, v in data.items():
        if k.startswith("PLN/"):
            code = k.split("/", 1)[1]
            chg = v.get("chg_30d_pct")
            chg_txt = f" ({chg:+.1f}%)" if chg is not None else ""
            fx_bits.append(f"{code} {v.get('value')}{chg_txt}")
        elif k.startswith("HICP "):
            hicp_bits.append(f"{k[len('HICP '):]} {v.get('value')}")
        elif "chg_1d_pct" in v or "chg_1m_pct" in v:
            name = _COMPACT_MARKET_NAME.get(k, k)
            bits = []
            if "chg_1d_pct" in v:
                bits.append(f"{v['chg_1d_pct']:+.1f}% д")
            if "chg_1m_pct" in v:
                bits.append(f"{v['chg_1m_pct']:+.1f}% мес")
            market_bits.append(f"{name} {' / '.join(bits)}")

    lines = []
    if fx_bits:
        lines.append(" · ".join(fx_bits) + "  30д")
    if market_bits:
        lines.append(" · ".join(market_bits))
    if hicp_bits and len(lines) < 3:
        lines.append(" / ".join(hicp_bits) + " (г/г)")
    return lines


# T11d: владелец - "текста как-то дофига". Пояснение печатаем только у топ-N
# сюжетов (отбор Gemini уже отсортирован по final score, см. brain.rank) -
# сюжеты за пределами топа остаются заголовком и ссылкой.
_EXPLAINED_TOP_N = 5

# Первое предложение целиком, если оно короче лимита; если и оно длиннее -
# не трогать вовсе (обрезка посреди мысли хуже длины, T11d).
_SENTENCE_END_RE = re.compile(r"^(.*?[.!?])(\s|$)", re.S)


def _truncate_at_sentence(text: str, limit: int = 160) -> str:
    if len(text) <= limit:
        return text
    m = _SENTENCE_END_RE.match(text)
    if m and len(m.group(1)) <= limit:
        return m.group(1)
    return text


def render_summary(selected: list[dict], data: dict) -> str:
    """Сообщение 2 (T10d, сжато в T11d): дата, компактные ЦИФРЫ, список сюжетов
    без служебных полей - без "неочевидно", без второй ссылки. URL сюжета - как
    обычный текст: deliver._to_html сам покажет его доменом-ссылкой (URL_RE),
    без риска T10b - ссылка ровно одна и принадлежит именно этому сюжету, тут
    вообще не нужен domain_urls/build_domain_urls.

    Пояснение - только у топ-_EXPLAINED_TOP_N. Предупреждения о недогруженном
    тексте не разбивают список построчно - схлопнуты в одну строку в конце."""
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    sections = [f"СВОДКА {today}"]

    cifry_lines = _render_cifry_compact(data) if data else []
    if cifry_lines:
        sections.append("\n".join(cifry_lines))

    story_blocks = [f"СЮЖЕТЫ ({len(selected)})"]
    unverified_nums = []
    for i, s in enumerate(selected, 1):
        it = s["item"]
        # эвристический top-N (rank деградировал, T13a) не даёт score модели -
        # "[None]" в сводке выглядело бы как баг, а не как явный статус
        score_txt = s["score"] if "score" in s else "heuristic"
        lines = [f"{i}. [{score_txt}] {_oneline(it.title)}"]
        if i <= _EXPLAINED_TOP_N and s.get("angle"):
            lines.append(f"   {_truncate_at_sentence(_oneline(s['angle']))}")
        lines.append(f"   {it.url}")
        story_blocks.append("\n".join(lines))
        if "verified" in s and not s["verified"]:
            unverified_nums.append(str(i))
    if unverified_nums:
        story_blocks.append(
            f"! текст не догружен: {', '.join(unverified_nums)} — цифры оттуда не проверены")
    sections.append("\n\n".join(story_blocks))

    return "\n\n".join(sections)


def _word_count(text: str) -> int:
    return len((text or "").split())


def _offending_source_quotes(values: list[str], figures_raw: str) -> dict[str, str | None]:
    """value -> цитата из FIGURES (verify.quoted_source_form) - показать проблемное
    число вместе с его формой в источнике, как в живом отчёте (T10d)."""
    pairs = charts.parse_figures(figures_raw) or []
    return {v: verify.quoted_source_form(src) for v, src in pairs if v in values}


def resolve_draft_source_numbers(block: dict, selected: list[dict] | None) -> list[int]:
    """T14a (откат T11c): футер печатает номера сюжетов, не URL. Один голый URL
    под многосюжетным черновиком выдавал себя за источник всего текста (боевой
    прогон 03.08: money.pl под черновиком из money.pl+bankier.pl+bankier.pl) -
    а сами ссылки на эти сюжеты уже есть кликабельными в блоке СЮЖЕТЫ выше,
    дублировать их текстом в футере незачем и небезопасно (голые URL в
    Телеграме - T9e). Номер сюжета несёт то, чего URL не несёт: соответствие
    "буллет -> какая по счёту история в СЮЖЕТЫ", по нему и проверяют цифру.

    Номера сюжетов извлекает verify._referenced_story_numbers, отфильтрованы
    диапазоном selected, без дублей (сам _referenced_story_numbers их уже не
    даёт), по возрастанию - не в порядке появления в FIGURES."""
    if not selected:
        return []
    numbers = {n for n in verify._referenced_story_numbers(block.get("figures", ""))
              if 1 <= n <= len(selected)}
    return sorted(numbers)


def render_draft_message(block: dict, num: int, selected: list[dict] | None = None) -> str:
    """Сообщение 3+ (T10d): один черновик на сообщение. SHAPE/FIGURES/
    WHY_THIS_ONE в Telegram не идут никогда - полный отчёт верификатора уже
    ушёл в digest.log через build_message() (см. main()). Заголовок черновика
    ставит рендерер - строку модели "DRAFT n (...)" сюда не пускаем вовсе,
    её и не было в распарсенных полях block."""
    body = block.get("body", "").strip()
    header = f"ЧЕРНОВИК {num} — {block.get('shape') or '?'}, {_word_count(body)} слов"
    lines = [header, "", body]

    if block.get("_parse_failed"):
        lines += ["", "⚠️ FIGURES не распарсился - проверь числа руками"]
    elif block.get("_verify_unavailable"):
        # T13a: verify.verify_drafts() упал целиком (не путать с падением только
        # уровня b - оно уже гасится внутри verify.py и сюда не долетает).
        # Одна строка на сообщение, не пометка на каждое число (alarm fatigue).
        verdict = (block.get("verdict") or "").strip().upper()
        lines += ["", (f"{verdict} — числа не верифицированы" if verdict
                       else "числа не верифицированы")]
    else:
        verdict = (block.get("verdict") or "").strip().upper()
        if block.get("_downgrade") and verdict == "POST":
            verdict = "MAYBE"          # эффективный вердикт, не сырой (T9 fix 6)

        offending = block.get("_offending") or []
        if offending:
            quotes = _offending_source_quotes(offending, block.get("figures", ""))
            shown = offending[:3]
            parts = [f'{v} ("{quotes[v]}")' if quotes.get(v) else v for v in shown]
            extra = f" +{len(offending) - 3} more" if len(offending) > 3 else ""
            lines += ["", f"{verdict} — сверь {', '.join(parts)}{extra}"]
        else:
            # T14b: пустой offending НЕ значит "всё найдено" - NO_SOURCE_TEXT в
            # него не входит по замыслу verify.py (это не вина модели, а
            # недогруженный источник), поэтому раньше ✅ печаталась при found=0
            # (все пары NO_SOURCE_TEXT) и даже при 0 < found < denom. Второе
            # достижимо: FRESH DATA даёт FOUND через data_text независимо от
            # bodies (verify._render/verify_figures_local, data_text общий на
            # все черновики), а числа конкретного сюжета в этом же черновике
            # уходят в NO_SOURCE_TEXT, если тело сюжета не догрузилось -
            # combined_body общий на весь черновик, а не per-pair. Условие
            # галочки поэтому не "offending пуст", а "found == denom > 0".
            countable = [r for r in block.get("_level_a", []) if r["status"] != "YEAR"]
            denom = len(countable)
            if not denom:
                lines += ["", verdict]
            else:
                found = sum(1 for r in countable if r["status"] == "FOUND")
                if found == denom:
                    lines += ["", f"{verdict} · цифры {found}/{denom} ✅"]
                elif found == 0:
                    lines += ["", f"{verdict} · цифры {found}/{denom} — "
                                  "источник не догружен, сверь вручную"]
                else:
                    lines += ["", f"{verdict} · цифры {found}/{denom} — "
                                  "часть чисел не сверена, источник не догружен"]

    check_first = (block.get("check_first") or "").strip()
    if check_first and check_first != "-":
        lines.append(f"CHECK_FIRST: {check_first}")

    # T12a (и T14a поверх него): номер сюжета не разрешился - строку источника
    # не печатаем вовсе, ни как URL, ни как домен-фолбэк. Настоящие ссылки на
    # все сюжеты уже есть кликабельными в сводке (сообщение 2).
    numbers = resolve_draft_source_numbers(block, selected)
    if numbers:
        if len(numbers) == 1:
            lines.append(f"источник: сюжет {numbers[0]}")
        else:
            lines.append(f"источники: сюжеты {', '.join(str(n) for n in numbers)}")

    return "\n".join(lines)


def build_message(selected, data, drafts) -> str:
    """Секции собираются списком и склеиваются "\\n\\n" явно - гарантированная
    пустая строка между ними (и между сюжетами) не зависит от того, сколько
    условных строк добавил конкретный сюжет. Раньше отступ держался на побочном
    эффекте f"\\n{i}. ..." внутри "\\n".join(lines) - хрупко (T9e)."""
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    sections = [f"СВОДКА {today}"]

    if data:
        figures_lines = ["ЦИФРЫ"]
        for k, v in data.items():
            bits = [] if k in _LEVEL_HIDDEN else [str(v.get("value"))]
            for f, lbl in (("chg_1d_pct", "д"), ("chg_1m_pct", "мес"),
                           ("chg_30d_pct", "30д"), ("chg_1y_pct", "г")):
                if f in v:
                    bits.append(f"{v[f]:+.2f}% {lbl}")
            figures_lines.append(f"  {k}: {'  '.join(bits)}   [{v.get('as_of','')}]")
        sections.append("\n".join(figures_lines))

    story_blocks = [f"СЮЖЕТЫ ({len(selected)})"]
    for i, s in enumerate(selected, 1):
        it = s["item"]
        date_str = it.published[:10] if it.published_known else "дата неизвестна"
        score_txt = f"{s['score']}/10" if "score" in s else "heuristic"
        story_lines = [f"{i}. [{score_txt}] {_oneline(it.title)}",
                      f"   {it.source}, {date_str} — {it.url}"]
        if s.get("angle"):
            story_lines.append(f"   угол: {_oneline(s['angle'])}")
        if s.get("why_nonobvious"):
            story_lines.append(f"   неочевидно: {_oneline(s['why_nonobvious'])}")
        if "verified" in s and not s["verified"]:
            story_lines.append("   ! текст статьи не догружен — цифры в угле не проверены")
        story_blocks.append("\n".join(story_lines))
    sections.append("\n\n".join(story_blocks))

    sections += ["=" * 30, "ЧЕРНОВИКИ\n\n" + drafts]
    return "\n\n".join(sections)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="не отправлять в Телегу, печатать в консоль")
    ap.add_argument("--hours", type=int, default=26, help="окно свежести новостей")
    ap.add_argument("--drafts", type=int, default=2)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--no-charts", action="store_true", help="не рисовать и не слать графики")
    ap.add_argument("--markets", action="store_true",
                    help="рисовать и слать график рынков (T11e: выключен по умолчанию, "
                         "цифры остаются в текстовом блоке ЦИФРЫ)")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))

    log.info("--- сбор ---")
    items = collect.collect_all(cfg, args.hours)

    seen = load_seen()
    fresh = [i for i in items if i.key not in seen]
    after_dedup = len(fresh)
    log.info("новых (не видели раньше): %d из %d", len(fresh), len(items))
    if not fresh:
        log.info("нечего показывать, выходим")
        return 0

    max_age_days = cfg.get("freshness", {}).get("max_age_days", 7)
    fresh = filter_by_age(fresh, max_age_days)
    if not fresh:
        log.info("после фильтра по возрасту ничего не осталось, выходим")
        return 0

    log.info("--- цифры ---")
    data = numbers.gather(cfg)
    market_source = data.pop("_market_source", {})

    # T11e: график рынков убран из ежедневной отправки - владелец им не пользовался,
    # а WIG20 TR (с дивидендами) против ценового S&P 500 систематически рисует
    # Польшу лучше конкурента (асимметрия баз, не сигнал). Цифры остаются в
    # текстовом блоке ЦИФРЫ - это и есть ежедневная ценность. Код и тесты не
    # удалены, доступен по требованию через `.\run.ps1 dry -Markets` / `-Markets`.
    market_chart = None
    if args.markets and not args.no_charts:
        try:
            market_chart = charts.market_overview(data, market_source, theme="dark")
        except Exception as exc:
            log.warning("market_overview упал: %s", exc)
    data.pop("_series", None)   # сырые ряды дальше по конвейеру не нужны

    log.info("--- отбор ---")
    candidates = heuristic_prefilter(fresh, args.hours)
    selected = brain.rank(candidates, top_n=args.top)
    rank_degraded = brain.rank_degraded()

    # T13a: ни один отказ Gemini не должен приводить к пустому сообщению - цифры
    # (NBP/рынки/HICP) и эвристический предотбор их не требуют и уходят всегда.
    # `notices` - строки-пометки простым текстом, без новых эмодзи-маркеров,
    # дописываются к сводке ниже.
    notices: list[str] = []
    drafts = ""
    draft_model = None
    draft_stats = verify._empty_stats()
    draft_blocks: list[dict] = []

    if rank_degraded:
        # Отбор шёл без модели - на эвристическом top-N нет score/angle, черновик
        # по ним не построить осмысленно, и раз rank уже не достучался до Gemini,
        # черновики тоже не пробуем (тот же день, та же исчерпанная квота).
        log.warning("rank деградировал: эвристический top-N (%d сюжетов), "
                    "черновики пропущены", len(selected))
        notices.append("! отбор сюжетов сегодня без модели — эвристический список, "
                       "черновиков нет")
    elif not selected:
        # Кандидатов реально не осталось (либо модель честно всех отбраковала) -
        # валидный пустой день, не отказ: без строки-пометки, без черновиков.
        log.info("отбор не дал сюжетов — сводка без черновиков")
    else:
        log.info("--- догрузка текста статей ---")
        enrich.enrich(selected, limit=args.drafts + 3)

        log.info("--- черновики ---")
        style_text = STYLE.read_text(encoding="utf-8") if STYLE.exists() else ""
        try:
            drafts = _fix_glued_punctuation(
                brain.draft(selected, data, style_text, n=args.drafts))
            draft_model = brain.last_model_used()
        except Exception as exc:
            log.warning("draft упал: %s", exc)
            notices.append(f"! черновиков сегодня нет — Gemini недоступен: {str(exc)[:200]}")

        if drafts:
            log.info("--- верификация цифр ---")
            try:
                drafts, draft_stats, draft_blocks = verify.verify_drafts(
                    drafts, selected, brain.format_data_block(data))
            except Exception as exc:
                # verify_drafts упал целиком (не путать с уровнем b - тот уже
                # гасится внутри verify.py). Черновики всё равно должны уйти:
                # парсим форматные поля тем же парсером, что и verify_drafts
                # внутри себя (verify.parse_drafts), без верификации чисел -
                # render_draft_message покажет одну строку "не верифицированы"
                # вместо разбора по каждому числу (T13a).
                log.warning("verify_drafts упал: %s", exc)
                draft_blocks = verify.parse_drafts(drafts)
                for b in draft_blocks:
                    b["_verify_unavailable"] = True
                draft_stats = verify._empty_stats()
                draft_stats["drafted"] = len(draft_blocks)
                for b in draft_blocks:
                    v = (b.get("verdict") or "").strip().upper()
                    if v in draft_stats["verdicts"]:
                        draft_stats["verdicts"][v] += 1

    # Полный отчёт (FIGURES, оба URL, "неочевидно", сырые заголовки модели,
    # полный текст ЦИФРЫ) в Telegram не идёт (T10d) - только в лог. Лог уже
    # уходит артефактом в Actions, владелец откроет его, когда что-то заподозрит.
    log.info("полный отчёт прогона (в Telegram не отправляется):\n%s",
             build_message(selected, data, drafts))

    summary_msg = render_summary(selected, data)
    if notices:
        summary_msg += "\n\n" + "\n".join(notices)
    draft_msgs = [render_draft_message(b, i, selected) for i, b in enumerate(draft_blocks, 1)]

    # Картинка к черновику (T10e, откат фолбэка в T11e.2) - только figures_chart,
    # только числа со статусом FOUND от верификатора, минимум два значения.
    # Ноль картинок за прогон - валидный исход, не повод рисовать что-то ещё.
    draft_images = [None] * len(draft_blocks)
    if not args.no_charts:
        for i, b in enumerate(draft_blocks):
            try:
                draft_images[i] = draft_card(b, i + 1, theme="dark")
            except Exception as exc:
                log.warning("картинка к черновику %d упала: %s", i + 1, exc)

    q = brain.quota_summary()
    domain_urls = build_domain_urls(selected)

    if args.dry:
        print("\n" + summary_msg)
        for i, dm in enumerate(draft_msgs):
            print("\n" + "-" * 30)
            print(dm)
            if draft_images[i]:
                print(f"[картинка к черновику {i + 1}] {draft_images[i]}")
        if market_chart:
            print(f"\n[график] рынки: {market_chart}")
    else:
        if market_chart:
            deliver.send_photo(market_chart, "Рынки за месяц")
        deliver.send(summary_msg, domain_urls)
        for i, dm in enumerate(draft_msgs):
            deliver.send(dm, domain_urls)
            if draft_images[i]:
                try:
                    deliver.send_photo(draft_images[i], f"К черновику {i + 1}")
                except Exception as exc:
                    log.warning("отправка картинки к черновику %d упала: %s", i + 1, exc)
        # T13c (грабля 6): состояние публикует только Actions. Локальный send без
        # явного -PersistState не трогает state/ вообще - иначе вечерний ручной
        # прогон помечает сюжеты виденными, и утренний прогон Actions их теряет,
        # это уже один раз ломалось и откатывалось руками. Гейт по переменной
        # окружения, по аналогии с NEWSBOT_ALLOW_LIVE (T11a) - ставит её только
        # соответствующий шаг digest.yml или run.ps1 send -PersistState.
        if os.environ.get("NEWSBOT_PERSIST_STATE") == "1":
            now = datetime.now(timezone.utc).isoformat()
            for s in selected:
                seen[s["item"].key] = now
            save_seen(seen)

            metrics.append(METRICS, metrics.build_record(
                collected=len(items), after_dedup=after_dedup, after_freshness=len(fresh),
                prefiltered=len(candidates), ranked=len(selected),
                drafted=draft_stats["drafted"], verdicts=draft_stats["verdicts"],
                figures=draft_stats["figures"], gemini_successful=q["successful"],
                gemini_quota_refused=q["quota_refused"], draft_model=draft_model,
                market_source=market_source))
        else:
            log.info("NEWSBOT_PERSIST_STATE не выставлена - state/ не тронут (T13c)")

    log.info("расход квоты Gemini: %d всего (успешных %d, отказов квоты %d)",
             q["total"], q["successful"], q["quota_refused"])
    log.info("готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())
