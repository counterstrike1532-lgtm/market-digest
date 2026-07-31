"""Отправка в Телегу. Режем на куски по 4000 символов (лимит Telegram).

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
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)
LIMIT = 3900
URL_RE = re.compile(r"https?://\S+")


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


def _chunks(text: str) -> list[str]:
    out, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > LIMIT:
            if cur:
                out.append(cur)
            # блок сам по себе длиннее лимита — режем, не разрывая URL
            while len(block) > LIMIT:
                head, block = _split_avoiding_urls(block, LIMIT)
                out.append(head)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        out.append(cur)
    return out


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except ValueError:
        return url


def _to_html(text: str) -> str:
    """Экранирует под parse_mode=HTML, URL оборачивает в <a href>. Текст ссылки —
    домен, а не весь URL: голый news.google.com/rss/articles/... километровой
    длины делал сводку нечитаемой, даже будучи кликабельным (T9e)."""
    out, last = [], 0
    for m in URL_RE.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        url = m.group()
        label = html.escape(_domain(url))
        out.append(f'<a href="{html.escape(url)}">{label}</a>')
        last = m.end()
    out.append(html.escape(text[last:]))
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


def send(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    for i, part in enumerate(_chunks(text), 1):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": _to_html(part),
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True},
                          timeout=30)
        if not r.ok:
            log.error("Telegram отказал: %s %s", r.status_code, r.text[:300])
        else:
            log.info("отправлен кусок %d (%d симв.)", i, len(part))
        time.sleep(1)
