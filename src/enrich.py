"""Подгрузка реального текста статей для отобранных сюжетов.

Зачем: модель отбора видит только заголовок и обрывок RSS-описания. Если позволить ей
на этом основании писать посты, она достроит правдоподобные, но выдуманные цифры.
Публиковать такое под своим именем нельзя. Поэтому перед написанием черновиков
догружаем текст статьи, и в промпте разрешаем использовать только те числа,
которые в этом тексте действительно есть.

Без внешних зависимостей: HTML разбирается регулярками. Грубо, но для извлечения
абзацев достаточно, а ставить bs4/trafilatura ради этого не хочется.
"""
from __future__ import annotations

import html
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "pl,en-US;q=0.9,en;q=0.8",
}

_DROP_BLOCKS = re.compile(
    r"<(script|style|nav|header|footer|aside|form|noscript)\b[^>]*>.*?</\1>",
    re.S | re.I)
_PARA = re.compile(r"<(?:p|h[1-3]|li)\b[^>]*>(.*?)</(?:p|h[1-3]|li)>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


def _text_from_html(raw: str, max_chars: int) -> str:
    raw = _DROP_BLOCKS.sub(" ", raw)
    chunks = []
    for m in _PARA.finditer(raw):
        t = html.unescape(_TAGS.sub(" ", m.group(1)))
        t = " ".join(t.split())
        if len(t) > 60:                      # отсекаем меню, подписи, кнопки
            chunks.append(t)
    out = "\n".join(chunks)
    return out[:max_chars]


def fetch_article(url: str, max_chars: int = 4000) -> str:
    """Текст статьи или пустая строка. Пустая строка — не ошибка, а сигнал 'не проверено'."""
    # Google News отдаёт редирект-обёртку, реальный текст за ней не достать без лишних прыжков
    if "news.google.com" in url:
        return ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            log.info("статья %s -> HTTP %s", url[:60], r.status_code)
            return ""
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            return ""
        r.encoding = r.apparent_encoding or r.encoding
        return _text_from_html(r.text, max_chars)
    except Exception as exc:
        log.info("статья %s не догрузилась: %s", url[:60], type(exc).__name__)
        return ""


NUM = re.compile(r"\d[\d\s.,]*")


def numbers_in(text: str) -> set[str]:
    """Числа, встречающиеся в тексте — для проверки, не выдумала ли модель цифру."""
    out = set()
    for m in NUM.finditer(text):
        digits = re.sub(r"[^\d]", "", m.group())
        if len(digits) >= 2:
            out.add(digits)
    return out


def enrich(selected: list[dict], limit: int = 6, pause: float = 1.0) -> int:
    """Догружает текст для первых `limit` сюжетов. Пишет в s['body'] и s['verified'].

    Возвращает количество сюжетов, для которых текст удалось получить.
    """
    ok = 0
    for s in selected[:limit]:
        body = fetch_article(s["item"].url)
        s["body"] = body
        s["verified"] = bool(body)
        if body:
            ok += 1
            log.info("догружено %d символов: %s", len(body), s["item"].title[:55])
        else:
            log.info("текст недоступен, цифры непроверяемы: %s", s["item"].title[:55])
        time.sleep(pause)
    for s in selected[limit:]:
        s.setdefault("body", "")
        s.setdefault("verified", False)
    log.info("текст получен для %d из %d сюжетов", ok, min(limit, len(selected)))
    return ok
