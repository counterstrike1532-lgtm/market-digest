"""Отправка в Телегу. Режем на куски по 4000 символов (лимит Telegram)."""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)
LIMIT = 3900


def _chunks(text: str) -> list[str]:
    out, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > LIMIT:
            if cur:
                out.append(cur)
            # блок сам по себе длиннее лимита — режем по строкам
            while len(block) > LIMIT:
                cut = block.rfind("\n", 0, LIMIT)
                cut = cut if cut > 0 else LIMIT
                out.append(block[:cut])
                block = block[cut:]
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        out.append(cur)
    return out


def send(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    for i, part in enumerate(_chunks(text), 1):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": part,
                                "disable_web_page_preview": True},
                          timeout=30)
        if not r.ok:
            log.error("Telegram отказал: %s %s", r.status_code, r.text[:300])
        else:
            log.info("отправлен кусок %d (%d симв.)", i, len(part))
        time.sleep(1)
