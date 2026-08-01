"""Отправка в Телегу. Режем на куски по ~3500 символов (запас до лимита Telegram
4096) - только по границам блок/абзац/предложение, никогда по счётчику символов
посреди текста (T10a).

parse_mode=HTML и явный <a href> вокруг ссылок - иначе Telegram сам решает, что
считать URL при автолинковании plain-текста, и иногда склеивает ссылку со
следующей строкой в кашу.
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
from urllib.parse import urlparse, urlunparse

import requests

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4096
LIMIT = 3500          # целевой размер куска - запас на служебные строки до TELEGRAM_LIMIT
URL_RE = re.compile(r"https?://\S+")

# Границы дробления по убыванию приоритета - блок (пустая строка), абзац
# (одиночный перевод строки), предложение. Все три - lookbehind/lookahead
# нулевой ширины: re.split ничего не поглощает, поэтому "".join(parts) == text
# всегда, при любом количестве вложенных уровней дробления (T10a). Резать по
# счётчику символов посреди текста запрещено требованиями T10a - счётчик
# решает только, где ОСТАНОВИТЬ накопление уже готовых кусков.
_BLOCK_SPLIT = re.compile(r"(?<=\n\n)")
_LINE_SPLIT = re.compile(r"(?<=\n)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])(?=\s+[A-ZА-ЯЁ0-9])")


def _split_avoiding_urls(block: str, limit: int) -> tuple[str, str]:
    """Режет block на (head, rest) на границе limit, но не посреди URL.

    news.google.com/rss/articles/... легко тянет на 200-400 символов без единого
    пробела - наивный разрез "по последнему переводу строки перед limit" рано или
    поздно попадает внутрь такой ссылки (переноса рядом просто нет). Тогда
    "голая" половина URL без "http" в начале уезжает бы в следующий кусок как
    обычный текст, а Telegram — по T9e — красит её как попало. Если предложенная
    граница попадает внутрь совпадения URL_RE, сдвигаем её на начало этого URL —
    вся ссылка целиком уходит в следующий кусок, а не рвётся."""
    cut = block.rfind("\n", 0, limit)
    cut = cut if cut > 0 else limit
    for m in URL_RE.finditer(block):
        if m.start() < cut < m.end():
            cut = m.start()
            break
    if cut <= 0:
        cut = limit          # сама ссылка длиннее limit - резать больше негде
    return block[:cut], block[cut:]


def _fits(text: str, domain_urls: dict[str, str] | None) -> bool:
    """Кусок годится в одно сообщение Telegram, только если проходит ОБЕ
    проверки: сырая длина <= LIMIT (читаемый целевой размер) и длина ПОСЛЕ
    _to_html <= TELEGRAM_LIMIT (настоящий предел API). <a href> вокруг
    каждого URL и html.escape() кавычек в FIGURES добавляют символы, которых
    в сыром тексте не было - кусок 3329 сырых символов после рендера дорастал
    до 3973 из 4096, запас в 123 символа, а не гарантия. Раньше проверялась
    только сырая длина (T10a review)."""
    return len(text) <= LIMIT and len(_to_html(text, domain_urls)) <= TELEGRAM_LIMIT


def _pieces_within_limit(text: str, domain_urls: dict[str, str] | None = None) -> list[str]:
    """text -> куски, каждый из которых уже проходит _fits (см. выше) - неделимая
    единица дальше. Порядок попыток: блок -> абзац -> предложение -> (если и
    предложение не помещается - как правило, из-за одного длинного URL)
    жёсткий разрез, не разрывающий сам URL. На каждом уровне используется
    только та граница, которая реально нашлась внутри text (parts > 1) -
    иначе спускаемся на уровень ниже. Инвариант: "".join(_pieces_within_limit(t))
    == t всегда, потому что _BLOCK_SPLIT/_LINE_SPLIT/_SENTENCE_SPLIT нулевой
    ширины (ничего не поглощают), а _split_avoiding_urls доказанно сохраняет
    все символы (head + rest == text). Проверка через _fits, а не через голую
    длину - иначе кусок, который умещается в LIMIT сырых символов, но набит
    ссылками (и после _to_html вырастает за TELEGRAM_LIMIT), вообще не делится:
    ранняя проверка "длина <= limit" срабатывала до того, как код успевал
    заглянуть внутрь блока (T10a review)."""
    if _fits(text, domain_urls):
        return [text]
    for split_re in (_BLOCK_SPLIT, _LINE_SPLIT, _SENTENCE_SPLIT):
        parts = [p for p in split_re.split(text) if p]
        if len(parts) > 1:
            out = []
            for p in parts:
                out.extend(_pieces_within_limit(p, domain_urls))
            return out
    # ни блока, ни абзаца, ни предложения внутри нет - жёсткий разрез, не
    # разрывающий URL. Он метит по сырой длине (limit), а не по _fits, поэтому
    # уменьшаем limit и повторяем, пока полученный head сам не пройдёт _fits -
    # частокол коротких ссылок вплотную друг к другу раздувается на _to_html
    # быстрее, чем растёт сырая длина, и один проход на полный LIMIT мог отдать
    # head, который сам за TELEGRAM_LIMIT (T10a review).
    limit = LIMIT
    while True:
        head, rest = _split_avoiding_urls(text, limit)
        if _fits(head, domain_urls) or limit <= 200:
            break
        limit -= 200
    if not rest:
        return [head]
    return [head] + _pieces_within_limit(rest, domain_urls)


def _chunks(text: str, domain_urls: dict[str, str] | None = None) -> list[str]:
    """Группирует неделимые куски (см. _pieces_within_limit) в сообщения
    Telegram жадно, не превышая _fits. Куски внутри группы просто
    конкатенируются - разделители (пустые строки, переносы) уже являются
    частью самих кусков, отдельно склеивать их не нужно и нельзя (иначе
    "".join(_chunks(text)) != text)."""
    if not text:
        return []
    out, cur = [], ""
    for piece in _pieces_within_limit(text, domain_urls):
        candidate = cur + piece
        if cur and not _fits(candidate, domain_urls):
            out.append(cur)
            cur = piece
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except ValueError:
        return url


_UTM_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}


def _strip_utm(url: str) -> str:
    """utm-хвосты (?utm_source=RSS&utm_medium=RSS&utm_campaign=Wiadomosci) дают
    треть длины типичной RSS-ссылки и ничего не значат для читателя - режем
    при рендере (T10b req 4). Сам Item.url (state/seen.json, дедуп по ссылке)
    не трогаем: key() и так игнорирует query string целиком.

    Режем целыми "key=value"-сегментами по литеральным "&"/"=", не проходя
    через parse_qsl/urlencode - тот раунд-трип перекодирует ВСЕ параметры
    заодно (например ":" в "ceid=US:en" превращался в "%3A"), меняя URL,
    даже когда ни один utm-параметр не резался вовсе."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    segments = parts.query.split("&")
    kept = [seg for seg in segments if seg.split("=", 1)[0] not in _UTM_PARAMS]
    if len(kept) == len(segments):
        return url
    return urlunparse(parts._replace(query="&".join(kept)))


def _wrap_bare_domains(segment: str, domain_re: re.Pattern | None,
                       domain_urls: dict[str, str]) -> str:
    """Домены без "https://" (модель иногда путает "SOURCE: www.cnbc.com" с
    URL - см. T9 fix 4) сами по себе не попадают под URL_RE, и Telegram решает
    судьбу такого голого текста сам - обычно ссылкой на корень сайта, а не на
    статью. Известные домены (из selected - те же, что уже видны в СЮЖЕТЫ)
    оборачиваем в <a href> на конкретную статью явно, чтобы Telegram вообще
    не трогал этот текст своим автолинкованием."""
    if not domain_re:
        return html.escape(segment)
    out, last = [], 0
    for m in domain_re.finditer(segment):
        out.append(html.escape(segment[last:m.start()]))
        domain = m.group()
        url = _strip_utm(domain_urls[domain])
        out.append(f'<a href="{html.escape(url)}">{html.escape(domain)}</a>')
        last = m.end()
    out.append(html.escape(segment[last:]))
    return "".join(out)


def _domain_pattern(domain_urls: dict[str, str] | None) -> re.Pattern | None:
    if not domain_urls:
        return None
    alts = "|".join(re.escape(d) for d in sorted(domain_urls, key=len, reverse=True))
    return re.compile(rf"\b(?:{alts})\b")


def _to_html(text: str, domain_urls: dict[str, str] | None = None) -> str:
    """Экранирует под parse_mode=HTML, URL оборачивает в <a href>. Текст ссылки —
    домен, а не весь URL: голый news.google.com/rss/articles/... километровой
    длины делал сводку нечитаемой, даже будучи кликабельным (T9e).

    domain_urls - {домен: URL конкретной статьи} из selected (main.py), для
    случаев, когда модель в поле SOURCE черновика написала голый домен вместо
    ссылки (T9 fix 4). Без этого параметра ведёт себя как раньше."""
    domain_re = _domain_pattern(domain_urls)
    out, last = [], 0
    for m in URL_RE.finditer(text):
        out.append(_wrap_bare_domains(text[last:m.start()], domain_re, domain_urls or {}))
        url = _strip_utm(m.group())
        label = html.escape(_domain(url))
        out.append(f'<a href="{html.escape(url)}">{label}</a>')
        last = m.end()
    out.append(_wrap_bare_domains(text[last:], domain_re, domain_urls or {}))
    return "".join(out)


def send_photo(path, caption: str = "") -> None:
    """sendPhoto, multipart/form-data. Caption режем до лимита Telegram (1024)."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    cap = _to_html(caption)[:1024]
    with open(path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                          data={"chat_id": chat, "caption": cap, "parse_mode": "HTML"},
                          files={"photo": f}, timeout=60)
    if not r.ok:
        log.error("Telegram sendPhoto отказал: %s %s", r.status_code, r.text[:300])
    else:
        log.info("фото отправлено: %s", path)


def send(text: str, domain_urls: dict[str, str] | None = None) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    for i, part in enumerate(_chunks(text, domain_urls), 1):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": _to_html(part, domain_urls),
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True},
                          timeout=30)
        if not r.ok:
            log.error("Telegram отказал: %s %s", r.status_code, r.text[:300])
        else:
            log.info("отправлен кусок %d (%d симв.)", i, len(part))
        time.sleep(1)
