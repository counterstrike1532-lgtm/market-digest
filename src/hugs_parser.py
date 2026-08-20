"""Парсинг и фильтрация постов из публичной веб-версии Telegram-канала (t.me/s/HugsFund).
Использует httpx и BeautifulSoup.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import bs4
import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Паттерны для детекции рекламных и промо-ссылок
_PROMO_LINK_PATTERNS = [
    re.compile(r"forms\.gle", re.IGNORECASE),
    re.compile(r"docs\.google\.com/forms", re.IGNORECASE),
    re.compile(r"forms\.office\.com", re.IGNORECASE),
    re.compile(r"lu\.ma", re.IGNORECASE),
    re.compile(r"luma\.com", re.IGNORECASE),
    re.compile(r"zoom\.us/(?:j|webinar|meeting)", re.IGNORECASE),
    re.compile(r"eventbrite\.com", re.IGNORECASE),
    re.compile(r"t\.me/(?:\w+bot\?start=)", re.IGNORECASE),
]

# Паттерны для детекции промо-текстов (вебинары, платные курсы, регистрации)
_PROMO_TEXT_PATTERNS = [
    re.compile(r"(?:зареєструватися|зарегистрироваться|реєстрація|регистрация)(?:\s+на)?\s+(?:вебінар|вебинар|курс|інтенсив|интенсив|ефір|эфир|майстер-клас|мастер-класс|подію|мероприятие|воркшоп|workshop)", re.IGNORECASE),
    re.compile(r"(?:посилання|ссылка)(?:\s+на)?\s+(?:реєстрацію|регистрацию|вебінар|вебинар|курс|майстер-клас|мастер-класс|ефір|эфир|трансляцію|трансляцию)", re.IGNORECASE),
    re.compile(r"(?:купити|купить|придбати|приобрести)\s+(?:курс|підписку|подписку|квиток|билет|доступ)", re.IGNORECASE),
    re.compile(r"(?:промокод|знижка|скидка|спеціальна ціна|специальная цена)", re.IGNORECASE),
    re.compile(r"(?:приєднуйтесь|присоединяйтесь|долучайтесь)\s+до\s+(?:закритого|платного|курсу|курса|клубу|клуба|спільноти|сообщества)", re.IGNORECASE),
    re.compile(r"(?:запис|запись)(?:\s+на)?\s+(?:курс|інтенсив|интенсив|навчання|обучение|вебінар|вебинар)", re.IGNORECASE),
    re.compile(r"(?:відкритий ефір|открытый эфир|вебінар|вебинар|майстер-клас|мастер-класс).*?(?:посилання|ссылка|реєстрац|регистрац|трансляц)", re.IGNORECASE),
]

# Типовой сервисный подвал канала Hugs, который мы очищаем из текста
_CHANNEL_FOOTER_PATTERN = re.compile(
    r"(?:(?:Сайт|Site)\s*\|[^\n]*|"
    r"Сайт\s*\|\s*Канал\s*\|\s*Чат\s*\|\s*Бот[^\n]*|"
    r"https?://hugs\.ua[^\s]*|"
    r"https?://app\.agenthugs\.ai[^\s]*|"
    r"https?://www\.youtube\.com/@HUGSFUND[^\s]*)",
    re.IGNORECASE,
)



def clean_post_text(text: str) -> str:
    """Текст поста без сервисных подвалов со ссылками на сайт/канал."""
    cleaned = _CHANNEL_FOOTER_PATTERN.sub("", text)
    cleaned = re.sub(r"[\s\|\-–—]+$", "", cleaned).strip()
    return cleaned


@dataclass
class HugsPost:
    post_id: str
    url: str
    published_at: datetime  # Timezone-aware UTC
    text: str
    links: list[str] = field(default_factory=list)
    is_promo: bool = False
    raw_html: str = ""

    @property
    def clean_text(self) -> str:
        """Текст поста без сервисных подвалов со ссылками на сайт/канал."""
        return clean_post_text(self.text)



def is_promo_post(text: str, links: list[str]) -> bool:
    """Определяет, является ли пост рекламным/промо (вебинар, курс, форма регистрации)."""
    # 1. Проверка внешних ссылок на формы и вебинары
    for link in links:
        for pattern in _PROMO_LINK_PATTERNS:
            if pattern.search(link):
                return True

    # 2. Проверка ключевых фраз в тексте
    for pattern in _PROMO_TEXT_PATTERNS:
        if pattern.search(text):
            return True

    return False


def parse_post_from_html(wrap: bs4.Tag, base_channel: str = "HugsFund") -> Optional[HugsPost]:
    """Извлекает объект HugsPost из HTML-элемента сообщения (.tgme_widget_message_wrap)."""
    msg = wrap.find("div", class_=re.compile(r"\btgme_widget_message\b"))
    if not msg:
        return None

    # Post ID (data-post="HugsFund/23673")
    data_post = msg.get("data-post", "")
    if not data_post:
        date_a = msg.find("a", class_="tgme_widget_message_date")
        if date_a and date_a.get("href"):
            m = re.search(rf"t\.me/([^/]+)/(\d+)", date_a["href"])
            if m:
                data_post = f"{m.group(1)}/{m.group(2)}"

    if not data_post:
        return None

    post_url = f"https://t.me/{data_post}"

    # Дата публикации из <time class="time" datetime="...">
    time_tag = None
    footer = msg.find("div", class_=re.compile(r"\btgme_widget_message_footer\b"))
    if footer:
        time_tag = footer.find("time", attrs={"datetime": True})

    if not time_tag:
        date_a = msg.find("a", class_="tgme_widget_message_date")
        if date_a:
            time_tag = date_a.find("time", attrs={"datetime": True})

    if not time_tag:
        for t in msg.find_all("time", attrs={"datetime": True}):
            dt_attr = t.get("datetime", "")
            if "T" in dt_attr:
                time_tag = t
                break

    if not time_tag or not time_tag.get("datetime"):
        log.debug("Пост %s: дата не найдена — пропускаем", data_post)
        return None

    try:
        dt_str = time_tag["datetime"]
        published_at = datetime.fromisoformat(dt_str)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        else:
            published_at = published_at.astimezone(timezone.utc)
    except Exception as exc:
        log.warning("Пост %s: ошибка парсинга даты '%s': %s", data_post, time_tag.get("datetime"), exc)
        return None

    # Текст поста
    text_div = msg.find("div", class_=re.compile(r"\btgme_widget_message_text\b"))
    if not text_div:
        return None

    # Извлечение текста с сохранением переводов строк
    for br in text_div.find_all("br"):
        br.replace_with("\n")

    text = text_div.get_text()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

    if not text:
        return None

    # Ссылки из текста и медиа
    links: list[str] = []
    for a in text_div.find_all("a", href=True):
        href = a["href"].strip()
        if href and href.startswith("http"):
            links.append(href)

    promo = is_promo_post(text, links)

    return HugsPost(
        post_id=data_post,
        url=post_url,
        published_at=published_at,
        text=text,
        links=links,
        is_promo=promo,
        raw_html=str(msg),
    )


def parse_page_posts(html_content: str, channel: str = "HugsFund") -> list[HugsPost]:
    """Парсит HTML страницы t.me/s/{channel} и возвращает список постов."""
    soup = bs4.BeautifulSoup(html_content, "html.parser")
    wraps = soup.find_all("div", class_=re.compile(r"\btgme_widget_message_wrap\b"))
    posts: list[HugsPost] = []
    for wrap in wraps:
        post = parse_post_from_html(wrap, base_channel=channel)
        if post:
            posts.append(post)
    return posts


def get_prev_page_before_id(html_content: str) -> Optional[int]:
    """Извлекает ID поста для пагинации назад (?before=12345)."""
    soup = bs4.BeautifulSoup(html_content, "html.parser")
    prev_link = soup.find("link", rel="prev") or soup.find("a", class_=re.compile(r"\btme_messages_more\b"))
    if prev_link and prev_link.get("href"):
        href = prev_link["href"]
        m = re.search(r"[?&]before=(\d+)", href)
        if m:
            return int(m.group(1))
    return None


def fetch_channel_posts(
    channel: str = "HugsFund",
    hours: int = 30,
    max_pages: int = 4,
    client: Optional[httpx.Client] = None,
) -> list[HugsPost]:
    """Загружает посты из публичной веб-версии канала за последние `hours` часов.
    
    Поддерживает пагинацию назад по страницам, если посты на первой странице
    еще не выходят за рамки временного окна `hours`.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    all_posts_by_id: dict[str, HugsPost] = {}
    current_before: Optional[int] = None

    owns_client = False
    if client is None:
        client = httpx.Client(timeout=15.0, headers={"User-Agent": USER_AGENT})
        owns_client = True

    try:
        for page in range(max_pages):
            url = f"https://t.me/s/{channel}"
            params = {}
            if current_before is not None:
                params["before"] = str(current_before)

            log.info("Загрузка страницы %d: %s (params=%s)", page + 1, url, params)
            resp = client.get(url, params=params)
            if resp.status_code != 200:
                log.warning("Ошибка загрузки t.me/s/%s: HTTP %s", channel, resp.status_code)
                break

            html = resp.text
            page_posts = parse_page_posts(html, channel=channel)
            if not page_posts:
                log.info("На странице %d не найдено постов", page + 1)
                break

            for p in page_posts:
                all_posts_by_id[p.post_id] = p

            oldest_on_page = min(p.published_at for p in page_posts)
            log.info(
                "Страница %d: получено %d постов, старейший от %s",
                page + 1,
                len(page_posts),
                oldest_on_page.isoformat(),
            )

            if oldest_on_page < cutoff:
                log.info("Достигли границы окна свежести (%d ч), останавливаем пагинацию", hours)
                break

            before_id = get_prev_page_before_id(html)
            if not before_id:
                num_ids = []
                for p in page_posts:
                    m = re.search(r"/(\d+)$", p.post_id)
                    if m:
                        num_ids.append(int(m.group(1)))
                if num_ids:
                    before_id = min(num_ids)
                else:
                    break

            if before_id == current_before:
                break
            current_before = before_id

    except Exception as exc:
        log.error("Сетевая ошибка при парсинге канала %s: %s", channel, exc)
    finally:
        if owns_client:
            client.close()

    sorted_posts = sorted(all_posts_by_id.values(), key=lambda x: x.published_at)
    log.info("Всего собрано уникальных постов из канала: %d", len(sorted_posts))
    return sorted_posts


def filter_posts(
    posts: list[HugsPost],
    hours: int = 30,
    now: Optional[datetime] = None,
) -> list[HugsPost]:
    """Фильтрует посты:
    - Оставляет только посты за последние `hours` часов.
    - Исключает рекламные и промо-посты (вебинары, курсы, формы регистрации).
    - Исключает посты с пустым текстом.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    kept: list[HugsPost] = []
    dropped_age = 0
    dropped_promo = 0

    for p in posts:
        if p.published_at < cutoff:
            dropped_age += 1
            continue

        if p.is_promo or is_promo_post(p.text, p.links):
            dropped_promo += 1
            log.info("Отфильтрован промо-пост: %s (%s)", p.post_id, p.clean_text[:60].replace("\n", " "))
            continue

        if not p.clean_text:
            continue

        kept.append(p)

    log.info(
        "Фильтрация постов: оставлено %d (отброшено по возрасту: %d, промо: %d)",
        len(kept),
        dropped_age,
        dropped_promo,
    )
    return kept
